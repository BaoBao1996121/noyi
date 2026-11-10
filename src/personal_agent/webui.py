"""Loopback WebUI using the same durable ledger and scheduler as the CLI."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
import re
from pathlib import Path
import secrets
import time
import uuid

from aiohttp import web

from .contracts import TaskSpec, digest, encode
from .model import ModelConfig, QwenModel
from .service import Service, ServiceLock
from .store import Store
from .tools import Toolset, MAX_SOURCE_BYTES, MAX_IMAGE_BYTES, atomic_write, image_type, safe_child
from .goal_api import goal_snapshot, goal_action
from .memory_curation import MemoryCurator
from .memory_store import KINDS


WEB = Path(__file__).with_name("web")
CONSOLE = web.AppKey("console", object)


class LocalModel:
    """Lazy credentials keep the setup screen usable before a key is configured."""
    def __init__(self, data, config):
        self.data, self.config, self.client = data, config, None
        saved = data / "model.json"
        if saved.is_file():
            self.config = ModelConfig(**json.loads(saved.read_text(encoding="utf-8")))
        key_file = data / "credentials.key"
        if not os.environ.get(self.config.api_key_env) and key_file.is_file():
            os.environ[self.config.api_key_env] = key_file.read_text(encoding="utf-8").strip()

    @property
    def ready(self):
        return bool(os.environ.get(self.config.api_key_env))

    @property
    def metadata(self):
        return {"provider": self.config.provider, "requested_model": self.config.name,
                "base_url": self.config.base_url, "effort": self.config.effort, "chat_effort": self.config.chat_effort}

    async def configure(self, *, name, base_url, key=None):
        keep = {k: getattr(self.config, k) for k in ("api_key_env", "effort", "chat_effort", "timeout_seconds", "max_output_tokens")}
        config = ModelConfig(name=name, base_url=base_url, **keep)
        if key is not None and (not isinstance(key, str) or not key.strip() or any(c.isspace() for c in key.strip())):
            raise ValueError("请输入有效的 API Key")
        await self.close()
        if key:
            atomic_write(self.data / "credentials.key", key.strip().encode())
            os.environ[config.api_key_env] = key.strip()
        atomic_write(self.data / "model.json", encode(asdict(config)).encode())
        self.config = config
        return {"ready": self.ready, "name": config.name}

    async def complete(self, messages, tools):
        if self.client is None:
            self.client = QwenModel(self.config)
        return await self.client.complete(messages, tools)

    async def close(self):
        if self.client:
            await self.client.close()
            self.client = None


class Console:
    def __init__(self, store, data, model, *, run_service=True, shutdown_event=None, plugins=None):
        self.store, self.data, self.model = store, Path(data).resolve(), model
        self.store.db.execute("CREATE TABLE IF NOT EXISTS local_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.store.db.execute("INSERT OR IGNORE INTO local_metadata VALUES('workspace_id',?)", (uuid.uuid4().hex,))
        self.workspace_id = self.store.one("SELECT value FROM local_metadata WHERE key='workspace_id'")["value"]
        self.token = secrets.token_urlsafe(32)
        self.instance_id = uuid.uuid4().hex
        self.shutdown_event = shutdown_event
        self.service = Service(store, self.data, model, plugins=plugins)
        self.curator = MemoryCurator(self.service.runtime)
        self.run_service = run_service
        self.service_error = None
        self.store.db.execute("CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,room_id TEXT NOT NULL,account TEXT NOT NULL,name TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,created REAL NOT NULL)")

    def room(self, room_id="local"):
        self.store.owned_room(room_id)
        return room_id

    def task(self, task_id):
        return self.store._owned(task_id, "local-user")

    def snapshot(self, room="local"):
        self.room(room)
        tasks = [self.store.task(r["id"]) for r in self.store.all("SELECT id FROM tasks WHERE room_id=? ORDER BY created DESC LIMIT 200", (room,))]
        # Details are fetched separately; physical paths and credentials are not UI state.
        for t in tasks:
            t["spec"]["sources"] = [{k: v for k, v in s.items() if k != "location"} for s in t["spec"]["sources"]]
            review = self.store.one("SELECT id FROM goal_reviews WHERE task_id=? ORDER BY created DESC LIMIT 1", (t["id"],))
            t["review_report_id"] = review["id"] if review else None
        messages = self.store.all("SELECT m.id,m.source,m.actor_id,m.task_id,m.kind,m.content,m.status,COALESCE(p.created,f.created,MIN(e.created),0) AS created FROM messages m LEFT JOIN events e ON e.message_id=m.id LEFT JOIN followups f ON f.message_id=m.id LEFT JOIN pending_routes p ON p.message_id=m.id WHERE m.room_id=? AND m.source NOT IN ('runtime','route') GROUP BY m.id ORDER BY m.rowid DESC LIMIT 300", (room,))
        for message in messages:
            if message["source"] == "goal_review":
                goal = self.store.one("SELECT g.title FROM goals g JOIN goal_review_requests r ON r.goal_id=g.id WHERE r.task_id=?", (message["task_id"],))
                if goal:
                    message["content"] = "请复盘持续目标「" + goal["title"] + "」的已有证据，说明未知、建议和下一次检验条件。"
        deliveries = self.store.all("SELECT d.id,d.task_id,r.actor_id,d.content,d.artifact,d.state,d.kind,d.attention_id,COALESCE(NULLIF(d.created,0),r.ended,a.created,0) AS created FROM deliveries d LEFT JOIN runs r ON r.id=d.run_id LEFT JOIN attentions a ON a.id=d.attention_id WHERE d.room_id=? ORDER BY d.rowid DESC LIMIT 300", (room,))
        for d in deliveries:
            d["artifact"] = json.loads(d["artifact"]) if d["artifact"] else None
            if d['actor_id'] and d['actor_id'].startswith('conversation:'):
                speaker = self.store.one("SELECT c.actor_id FROM conversation_inputs c JOIN events e ON e.message_id=c.message_id JOIN deliveries d ON d.run_id=e.claimed_by_run WHERE d.id=? AND e.kind='conversation'", (d['id'],))
                if speaker:
                    d['actor_id'] = speaker['actor_id']
        questions = self.store.all("SELECT a.id,a.task_id,a.question,a.options,a.state,a.answer FROM attentions a JOIN tasks t ON t.id=a.task_id WHERE t.room_id=? AND a.account='local-user' ORDER BY a.created", (room,))
        for q in questions:
            q["options"] = json.loads(q["options"])
        pending = self.store.all("SELECT f.id,f.state,m.content FROM followups f JOIN messages m ON m.id=f.message_id WHERE m.room_id=? AND f.owner_task IS NULL AND f.state='unassigned'", (room,))
        config = getattr(self.model, "config", ModelConfig())
        return {"workspace_id": self.workspace_id, "tasks": tasks, "messages": list(reversed(messages)), "deliveries": list(reversed(deliveries)), "questions": questions,
                "goals": goal_snapshot(self.store, room),
                "unassigned": pending, "server_time": time.time(), "ready": getattr(self.model, "ready", True),
                "model": {"name": config.name, "base_url": config.base_url, "provider": config.provider}, "service_error": self.service_error or self.service.triggers.error,
                "controls": self.store.all("SELECT c.* FROM controls c JOIN tasks t ON t.id=c.task_id WHERE t.room_id=? ORDER BY c.created DESC LIMIT 50", (room,)),
                "rooms": [{**r, "members": self.store.members(r["id"])} for r in self.store.all("SELECT * FROM rooms WHERE owner='local-user' ORDER BY rowid")],
                "bots": self.store.all("SELECT * FROM bots WHERE owner='local-user' ORDER BY created,id"),
                "team_templates": [{**t, "configuration": json.loads(t["configuration"])} for t in self.store.all("SELECT * FROM team_templates WHERE owner='local-user' ORDER BY created")],
                "pending_routes": self.store.all("SELECT p.id,p.mode,p.actor_id,p.message_id,m.content FROM pending_routes p JOIN messages m ON m.id=p.message_id WHERE p.room_id=? AND p.state='unassigned' ORDER BY p.created", (room,)),
                "commitments": self.store.commitment_items(room),
                "memories": self.store.memory_items(room),
                "memory_scope": "personal" if self.store.memory_collection(room).startswith("personal:") else "group",
                "memory_kinds": KINDS,
                "memory_proposals": self.store.memory_proposals(room),
                "memory_curation": self.store.one("SELECT state,trigger,proposals,error,created,ended FROM memory_curations WHERE collection_id=? ORDER BY created DESC LIMIT 1", (self.store.memory_collection(room),))}

    def detail(self, task_id):
        task = self.task(task_id)
        task["spec"]["sources"] = [{k: v for k, v in s.items() if k != "location"} for s in task["spec"]["sources"]]
        return {"task": task, "runs": self.store.all("SELECT id,trigger,epoch,revision,phase,end_reason,started,ended,stop_reason FROM runs WHERE task_id=? ORDER BY started", (task_id,)),
                "operations": [{"id": o["id"], "tool": o["tool"], "status": o["status"], "created": o["created"]} for o in self.store.operations(task_id)],
                "followups": self.store.all("SELECT id,state,generation,reason,evidence_ref FROM followups WHERE owner_task=?", (task_id,)),
                "usage": [json.loads(s["data"]) for s in self.store.all("SELECT data FROM steps WHERE task_id=? AND kind='model'", (task_id,))]}

    def submit(self, body):
        room = self.room(body.get("room", "local"))
        mode, nonce = body.get("mode", "auto"), body["nonce"]
        text = body["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise ValueError("请输入 1–20000 字的内容")
        if not isinstance(nonce, str) or not 1 <= len(nonce) <= 150:
            raise ValueError("无效的提交标识")
        if mode == "followup":
            tid = body.get("task_id")
            return self.store.route_input(None, text=text, nonce=nonce, mode=mode, room=room, actor_id=body.get("actor_id"), task_id=tid, reply_to=body.get("reply_to"))
        if mode == "chat":
            return self.store.route_input(None, text=text, nonce=nonce, mode=mode, room=room, actor_id=body.get("actor_id"), reply_to=body.get("reply_to"))
        if mode not in {"task", "auto"}:
            raise ValueError("未知消息模式")
        ids = body.get("uploads", [])
        if not isinstance(ids, list) or len(ids) > 12 or len(ids) != len(set(ids)):
            raise ValueError("最多选择 12 份不同资料")
        sources = []
        for upload_id in ids:
            upload = self.store.one("SELECT * FROM uploads WHERE id=? AND room_id=? AND account='local-user'", (upload_id, room))
            if not upload:
                raise ValueError("资料不属于当前会话")
            path = self.data / upload["path"]
            if path.is_symlink() or not path.resolve().is_relative_to(self.data) or digest(path.read_bytes()) != upload["sha256"]:
                raise ValueError("资料已更改，请重新上传")
            sources.append({"id": "s_" + upload_id[:12], "kind": "file", "location": str(path.resolve()), "required": True, "description": upload["name"]})
        urls = body.get("urls", [])
        if not isinstance(urls, list) or len(urls) > 6 or any(not isinstance(u, str) for u in urls):
            raise ValueError("最多提供 6 个明确的资料网址")
        sources.extend({"id": f"web{i+1}", "kind": "http", "location": url, "required": True} for i, url in enumerate(urls))
        if mode == "auto":
            # Only literal locations in this user's message add a grant. Model
            # output, source contents and old conversation text never do.
            inline_urls = [u.rstrip('，。；！）)]') for u in re.findall(r'https?://[^\s<>"`]+', text)]
            inline_files = re.findall(r'[A-Za-z]:[\\/][^\r\n"<>|`]*?\.(?:txt|md|markdown|json|csv|log|yaml|yml)(?=$|[\s`"，。；）)])', text, re.IGNORECASE)
            seen = {s['location'] for s in sources}
            for kind, values in [('http', inline_urls), ('file', inline_files)]:
                for location in values:
                    if location not in seen:
                        sources.append({'id': 'user_source' + str(len(sources)+1), 'kind': kind, 'location': location, 'required': True})
                        seen.add(location)
            if len(sources) > 18:
                raise ValueError('一次最多处理 18 份明确资料，请分批发送')
        spec = TaskSpec(text, sources, max_calls=40, max_tools=100, max_seconds=900, enable_nudge=True, review_workers=True)
        if mode == "auto":
            return self.store.accept_conversation(spec, text=text, nonce=nonce, room=room, actor_id=body.get('actor_id'), task_id=body.get('task_id'), reply_to=body.get('reply_to'))
        return self.store.route_input(spec, text=text, nonce=nonce, mode=mode, room=room, actor_id=body.get("actor_id"), reply_to=body.get("reply_to"))

    def artifact(self, delivery_id):
        d = self.store.one("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        if not d or not d["artifact"]:
            raise ValueError("产物不存在")
        task = self.task(d["task_id"])
        artifact = json.loads(d["artifact"])
        path = Toolset(self.data, task).artifact_path(artifact["ref"])
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("产物超过限制")
        raw = path.read_bytes()
        if digest(raw) != artifact["sha256"]:
            raise ValueError("产物发生变化，无法作为已验收版本提供")
        return raw, path.name


@web.middleware
async def security(request, handler):
    console = request.app[CONSOLE]
    try:
        if request.url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise web.HTTPForbidden(text="Only loopback access is supported")
        origin = request.headers.get("Origin")
        if origin and origin != str(request.url.origin()):
            raise web.HTTPForbidden(text="Cross-origin access denied")
        if request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("X-PA-Token", ""), console.token):
                raise web.HTTPForbidden(text="Refresh this page before making changes")
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                raise web.HTTPForbidden()
        response = await handler(request)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        # No tracebacks, filesystem locations, SDK bodies or credentials in responses.
        message = str(exc) if isinstance(exc, ValueError) else "请求无效或文件暂不可用"
        response = web.json_response({"error": message[:240]}, status=400)
    response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                             "Cross-Origin-Resource-Policy": "same-origin", "X-Frame-Options": "DENY",
                             "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    if response.headers.pop("X-Page-CSP", None):
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    return response


async def json_body(request):
    if request.content_type != "application/json":
        raise ValueError("需要 JSON 请求")
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("请求需要一个对象")
    return body


async def boot(request):
    c = request.app[CONSOLE]
    return web.json_response({"token": c.token, "instance_id": c.instance_id, "workspace_id": c.workspace_id, "version": "0.6.0", "title": "Personal Agent"})


async def snapshot(request):
    return web.json_response(request.app[CONSOLE].snapshot(request.query.get("room", "local")))


async def detail(request):
    return web.json_response(request.app[CONSOLE].detail(request.match_info["task_id"]))


async def submit(request):
    return web.json_response(request.app[CONSOLE].submit(await json_body(request)))


async def action(request):
    c, body, name = request.app[CONSOLE], await json_body(request), request.match_info["name"]
    if name in {"stop_reply", "stop_task", "cancel"}:
        result = c.store.request_control(body["task_id"], name, run_id=body.get("run_id"))
    elif name == "resume":
        c.task(body["task_id"])
        result = c.store.resume(body["task_id"], extra_calls=body.get("extra_calls", 0), extra_tools=body.get("extra_tools", 0), extra_seconds=body.get("extra_seconds", 0))
    elif name == "answer":
        result = c.store.answer_attention(body["question_id"], body["text"], nonce=body["nonce"])
    elif name == "assign":
        result = c.store.assign_followup(body["followup_id"], body["task_id"])
    elif name == "assign_route":
        result = c.store.assign_route(body["route_id"], actor_id=body.get("actor_id"), task_id=body.get("task_id"))
    elif name == "reassign":
        result = c.store.reassign_task(body["task_id"], body["actor_id"])
    else:
        raise ValueError("未知操作")
    return web.json_response(result)


async def manage(request):
    c, b, name = request.app[CONSOLE], await json_body(request), request.match_info["name"]
    s = c.store
    if name == "create_bot":
        result = s.create_bot(b["name"], b["role"])
    elif name == "update_bot":
        result = s.update_bot(b["id"], name=b["name"], role=b["role"], expected_revision=b["revision"])
    elif name == "copy_bot":
        result = s.copy_bot(b["id"], name=b.get("name"))
    elif name == "archive_bot":
        result = s.archive_bot(b["id"], archived=b["archived"])
    elif name == "create_group":
        result = s.create_group(b["name"], b["members"])
    elif name == "update_group":
        result = s.update_group(b["id"], name=b["name"], member_ids=b["members"], expected_revision=b["revision"])
    elif name == "save_team":
        result = s.save_team_template(b["room"], b["name"])
    elif name == "instantiate_team":
        result = s.instantiate_team(b["id"], name=b["name"])
    elif name == "save_memory":
        result = s.save_memory(b["room"], key=b["key"], value=b["value"], purpose=b["purpose"], expected_version=b.get("version", 0), source_ref=b.get("source_ref"), restore=b.get("restore", False),
                               kind=b.get("kind"), expires_at=b.get("expires") or None, keywords=b.get("keywords"), subject=b.get("subject") or None)
    elif name == "renew_memory":
        result = s.renew_memory(b["room"], b["id"], expected_version=b["version"])
    elif name == "delete_memory":
        result = s.delete_memory(b["room"], b["id"], expected_version=b["version"])
    elif name == "decide_memory":
        result = s.decide_memory(b["id"], accept=b["accept"], choice=b.get("choice"))
    elif name == "run_memory_curation":
        if not getattr(c.model, "ready", True):
            raise ValueError("模型还没配置好，暂时不能整理")
        result = await c.curator.run(s.memory_collection(b["room"]), trigger="manual")
    else:
        raise ValueError("未知管理操作")
    return web.json_response(result)


async def goal_manage(request):
    return web.json_response(goal_action(request.app[CONSOLE].store, request.match_info["name"], await json_body(request)))


async def policy_detail(request):
    return web.json_response(request.app[CONSOLE].store.policy(request.match_info["policy_id"], "local-user"))


async def review_report(request):
    report = request.app[CONSOLE].store.review_report(request.match_info["review_id"])
    if request.query.get("download") == "1":
        return web.Response(body=report["text"].encode(), content_type="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="goal-review.md"'})
    return web.json_response(report)


async def upload(request):
    c = request.app[CONSOLE]
    room = c.room(request.query.get("room", "local"))
    reader = await request.multipart()
    part = await reader.next()
    if not part or part.name != "file" or not part.filename:
        raise ValueError("请选择一份 UTF-8 文本、Markdown、JSON 资料或一张图片")
    raw = bytearray()
    while chunk := await part.read_chunk():
        raw.extend(chunk)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("图片最多 4 MB，文本资料最多 128 KB")
    # The kind is decided by the file's own bytes, not its name or the browser.
    if image_type(bytes(raw)):
        suffix = ".image"
    else:
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("每份文本资料最多 128 KB")
        try:
            raw.decode("utf-8-sig")
        except UnicodeError:
            raise ValueError("目前支持 UTF-8 文本资料和 PNG、JPEG、GIF、WebP 图片") from None
        if b"\x00" in raw:
            raise ValueError("不能读取二进制资料")
        suffix = ".txt"
    upload_id = uuid.uuid4().hex
    path = safe_child(c.data / "uploads", upload_id + suffix)
    if not path.resolve().is_relative_to(c.data):
        raise ValueError("资料保存路径无效")
    atomic_write(path, bytes(raw))
    name = Path(part.filename).name[:200]
    c.store.db.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?)", (upload_id, room, "local-user", name, str(path.relative_to(c.data)), digest(raw), len(raw), time.time()))
    return web.json_response({"id": upload_id, "name": name, "bytes": len(raw)})


async def artifact(request):
    raw, name = request.app[CONSOLE].artifact(request.match_info["delivery_id"])
    if request.query.get("download") == "1":
        return web.Response(body=raw, content_type="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="result.md"'})
    return web.json_response({"name": name, "text": raw.decode("utf-8")})


async def result_page(request):
    from .result_page import render
    c = request.app[CONSOLE]
    html = render(c.store, c.data, request.match_info["task_id"])
    if html is None:
        raise web.HTTPNotFound(text="这份成果还没有通过验收")
    response = web.Response(text=html, content_type="text/html")
    # Inline style only; still no scripts, frames or external loads.
    response.headers["X-Page-CSP"] = "page"
    return response


async def settings(request):
    c, body = request.app[CONSOLE], await json_body(request)
    if c.store.one("SELECT id FROM runs WHERE phase='running'") or c.service._active:
        raise ValueError("请等当前执行结束后再切换模型配置")
    if not isinstance(c.model, LocalModel):
        raise ValueError("此测试服务使用固定模型")
    return web.json_response(await c.model.configure(name=body["name"], base_url=body["base_url"], key=body.get("key")))


async def backup(request):
    from .backup import create_backup
    c = request.app[CONSOLE]
    # Synchronous snapshot runs between host tool commits, so the database and
    # its managed immutable files are captured without a concurrent writer.
    path = c.data.parent / "backups" / ("personal-agent-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".zip")
    create_backup(c.store, c.data, path)
    return web.FileResponse(path, headers={"Content-Disposition": 'attachment; filename="' + path.name + '"'})


async def shutdown(request):
    c = request.app[CONSOLE]
    if c.shutdown_event is None:
        raise ValueError("当前服务未启用桌面停止入口")
    asyncio.get_running_loop().call_later(0.1, c.shutdown_event.set)
    return web.json_response({"accepted": True, "instance_id": c.instance_id})


async def static(request):
    name = request.match_info.get("name", "index.html")
    if name not in {"index.html", "app.js", "style.css", "modules.css", "goals.js", "goals.css", "reviews.js"}:
        raise web.HTTPNotFound()
    return web.FileResponse(WEB / name)


async def lifecycle(app):
    c = app[CONSOLE]
    job = trigger_job = None
    with ServiceLock(c.data):
        c.store.recover()
        async def run():
            while True:
                if getattr(c.model, "ready", True):
                    try:
                        await c.service.run_until_quiescent()
                        c.service_error = None
                    except Exception as exc:
                        c.service_error = type(exc).__name__
                        # Stop on a scheduler fault; do not repeatedly run an inconsistent ledger.
                        return
                await asyncio.sleep(0.3)
        async def curate():
            # Daily tidy; it only proposes, so a failure leaves nothing half-done.
            while True:
                await asyncio.sleep(300)
                if getattr(c.model, "ready", True):
                    try:
                        await c.curator.tick()
                    except Exception as exc:
                        c.service_error = c.service_error or "memory_curation:" + type(exc).__name__
        if c.run_service:
            job = asyncio.create_task(run())
            trigger_job = asyncio.create_task(c.service.triggers.run())
            curate_job = asyncio.create_task(curate())
        try:
            yield
        finally:
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            if trigger_job:
                trigger_job.cancel()
                curate_job.cancel()
                await asyncio.gather(trigger_job, curate_job, return_exceptions=True)


def create_app(store, data, model, *, run_service=True, shutdown_event=None, plugins=None):
    app = web.Application(middlewares=[security], client_max_size=MAX_IMAGE_BYTES + 20000)
    app[CONSOLE] = Console(store, data, model, run_service=run_service, shutdown_event=shutdown_event, plugins=plugins)
    app.add_routes([web.get("/", static), web.get("/assets/{name}", static), web.get("/api/bootstrap", boot),
                    web.get("/api/state", snapshot), web.get("/api/tasks/{task_id}", detail), web.post("/api/messages", submit),
                    web.post("/api/actions/{name}", action), web.post("/api/uploads", upload),
                    web.get("/api/artifacts/{delivery_id}", artifact), web.post("/api/settings", settings),
                    web.post("/api/backup", backup),
                    web.post("/api/shutdown", shutdown),
                    web.post("/api/manage/{name}", manage), web.post("/api/goals/{name}", goal_manage),
                    web.get("/api/policies/{policy_id}", policy_detail)])
    app.router.add_get("/api/reviews/{review_id}", review_report)
    app.router.add_get("/r/{task_id}", result_page)
    app.cleanup_ctx.append(lifecycle)
    return app


def serve_web(data, config=None, port=8765, plugins=None):
    data = Path(data).resolve()
    store = Store(data / "state.sqlite")
    model = LocalModel(data, config or ModelConfig.load())
    async def run():
        stopping = asyncio.Event()
        app = create_app(store, data, model, shutdown_event=stopping, plugins=plugins)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=10)
        try:
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
            print(f"Personal Agent: http://127.0.0.1:{port}  （关闭浏览器后仍会运行；使用停止入口结束服务）", flush=True)
            await stopping.wait()
        finally:
            await runner.cleanup()
            await model.close()
    try:
        asyncio.run(run())
    finally:
        store.close()
