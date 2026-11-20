"""Browser capability package: confirmation, secrets, live view gating, export.

These drive a real headless Chromium against a local test site (127.0.0.1).
They need the Playwright headless shell: python -m playwright install chromium --only-shell
"""
import asyncio
import json
import zipfile

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import ScriptedModel, tool_reply
from personal_agent.contracts import TaskSpec
from personal_agent.service import Service

pytestmark = pytest.mark.asyncio(loop_scope="module")

CANARY = "CANARY-pw-7731"
OTP = "990417"

PAGES = {
    "/form": """<title>订单</title><h1>订单</h1><form method=post action=/post><label>名字<input name=n></label>
        <button>提交订单</button></form>
        <form method=get action=/search><input name=q aria-label=搜索><button>搜</button></form>""",
    "/spa": """<title>文章</title><h1>文章</h1><label>评论<input id=c></label><button id=like>点赞</button>
        <script>
        fetch('/beacon', {method: 'POST', body: 'load'});
        document.getElementById('c').addEventListener('input', () => fetch('/beacon', {method: 'POST', body: 'input'}));
        document.getElementById('like').onclick = () => fetch('/api/like', {method: 'POST', body: '1'});
        </script>""",
    "/gaps": """<title>缺口</title><h1>缺口</h1><a href=/delete?id=1>删除这条</a><button id=send>发送消息</button>
        <script>const ws = new WebSocket('ws://' + location.host + '/wsock');
        document.getElementById('send').onclick = () => ws.send('hello');</script>""",
    "/login": """<title>登录</title><h1>登录</h1><form method=post action=/do-login><label>账号<input name=u></label>
        <label>密码<input type=password name=p></label><label>验证码<input autocomplete=one-time-code name=otp></label>
        <button>登录</button></form>""",
}
PAGES["/links"] = """<title>结果</title><h1>结果</h1><a href=/spa target=_blank>文章（新窗口打开）</a>"""
PAGES["/filled"] = PAGES["/login"].replace("name=p>", f"name=p value={CANARY}>").replace("name=otp>", f"name=otp value={OTP}>")


class Site:
    """A local site that records every request it receives."""

    def __init__(self):
        self.hits = []
        self.ws_messages = []

    async def page(self, request):
        return web.Response(text="<!doctype html><meta charset=utf-8>" + PAGES[request.path], content_type="text/html")

    async def record(self, request):
        self.hits.append((request.method, request.path_qs, await request.text()))
        return web.Response(text="<!doctype html><meta charset=utf-8><title>ok</title><p>收到了</p>", content_type="text/html")

    async def wsock(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            self.ws_messages.append(message.data)
        return ws

    async def hop(self, request):
        # Replaces itself right after loading, like a search page that redirects once.
        return web.Response(text="<!doctype html><title>跳转</title><p>旧页面</p><script>setTimeout(() => location.replace('/slow'), 30)</script>", content_type="text/html")

    async def slow(self, request):
        # The new document arrives in two parts: no body for a while.
        response = web.StreamResponse(headers={"Content-Type": "text/html; charset=utf-8"})
        await response.prepare(request)
        await response.write("<!doctype html><html><head><title>结果</title>".encode() + b" " * 4096)
        await asyncio.sleep(1.2)
        await response.write("</head><body><p>新页面的结果</p></body></html>".encode())
        await response.write_eof()
        return response

    def app(self):
        app = web.Application()
        app.add_routes([web.get(path, self.page) for path in PAGES] + [web.get("/wsock", self.wsock), web.get("/hop", self.hop), web.get("/slow", self.slow)]
                       + [web.route("*", path, self.record) for path in ("/post", "/search", "/beacon", "/api/like", "/delete", "/do-login")])
        return app

    def writes(self, path):
        return [h for h in self.hits if h[1].split("?")[0] == path and h[0] != "GET"]


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def site():
    s = Site()
    server = TestServer(s.app(), host="127.0.0.1")
    await server.start_server()
    s.base = str(server.make_url("")).rstrip("/")
    yield s
    await server.close()


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def chromium(tmp_path_factory, site):
    # Depends on site so the browser closes first: open WebSockets would hold the server's shutdown.
    from personal_agent.browser import Browser, BrowserConfig
    b = Browser(BrowserConfig(profile=tmp_path_factory.mktemp("profile"), public_only=False))
    try:
        await b.context()
    except Exception as exc:   # Chromium not installed on this machine
        pytest.skip(f"Chromium unavailable: {type(exc).__name__}")
    yield b
    await b.close()


@pytest.fixture
def tools(chromium, tmp_path):
    from personal_agent.browser import BrowserTools
    return BrowserTools(chromium, tmp_path / "data")


def result(messages, tool_name):
    """The last tool result the model saw for this tool."""
    names = {}
    for m in messages:
        for c in m.get("tool_calls") or []:
            names[c["id"]] = c["function"]["name"]
    return next(json.loads(m["content"]) for m in reversed(messages) if m["role"] == "tool" and names.get(m["tool_call_id"]) == tool_name)


def ref(snapshot, label):
    import re
    return re.search(r'"' + label + r'"[^\n]*?\[ref=(\w+)\]', snapshot).group(1)


def stop(messages, tools):
    return tool_reply("report_unable", {"explanation": "测试到此为止。"}, "end")


async def run(store, data, tools_obj, steps, objective="用浏览器办事"):
    tid = store.submit(TaskSpec(objective, max_calls=20, max_tools=40), nonce=objective + str(len(store.all("SELECT id FROM tasks"))))["task_id"]
    model = ScriptedModel(steps)
    await Service(store, data, model, plugins=[tools_obj.plugin()]).run_until_quiescent()
    return tid, model


def open_page(url):
    return [tool_reply("load_capability", {"name": "browser"}, "load"), tool_reply("browser_open", {"url": url}, "open")]


# ---- capability package --------------------------------------------------------------

async def test_browser_tools_appear_only_after_the_package_is_loaded(store, tmp_path, tools, site):
    seen = []

    def check(messages, tools_now):
        seen.append({t["function"]["name"] for t in tools_now})
        return stop(messages, tools_now)
    _, model = await run(store, tmp_path / "data", tools, [lambda m, t: (seen.append({x["function"]["name"] for x in t}), tool_reply("load_capability", {"name": "browser"}, "load"))[1], check])
    assert "browser_open" not in seen[0] and "load_capability" in seen[0]
    assert {"browser_open", "browser_act", "browser_confirmed", "browser_login"} <= seen[1]
    assert "Browser tools operate" in result(model.requests[1]["messages"], "load_capability")["guide"]


async def test_a_page_that_replaces_itself_while_loading_still_gives_a_snapshot(store, tmp_path, tools, site):
    _, model = await run(store, tmp_path / "data", tools, open_page(site.base + "/hop") + [stop])
    opened = result(model.requests[2]["messages"], "browser_open")
    assert "error" not in opened, opened
    assert "新页面的结果" in opened["snapshot"]


async def test_a_link_that_opens_a_new_window_opens_in_the_task_tab(store, tmp_path, tools, site, chromium):
    def click(messages, tools_now):
        return tool_reply("browser_act", {"actions": [{"do": "click", "ref": ref(result(messages, "browser_open")["snapshot"], "文章（新窗口打开）")}]}, "c")
    _, model = await run(store, tmp_path / "data", tools, open_page(site.base + "/links") + [click, stop])
    clicked = result(model.requests[3]["messages"], "browser_act")
    assert clicked["url"].endswith("/spa") and "点赞" in clicked["snapshot"]
    assert len((await chromium.context()).pages) == len(chromium.tabs)     # no stray window left behind


# ---- confirmation ----------------------------------------------------------------------

async def test_post_form_submit_is_held_until_the_user_confirms(store, tmp_path, tools, site):
    data = tmp_path / "data"

    def act(messages, _):
        snap = result(messages, "browser_open")["snapshot"]
        return tool_reply("browser_act", {"actions": [{"do": "fill", "ref": ref(snap, "名字"), "text": "小明"},
                                                      {"do": "click", "ref": ref(snap, "提交订单")}]}, "act")
    tid, model = await run(store, data, tools, open_page(site.base + "/form") + [act])
    assert site.writes("/post") == []                       # nothing reached the server
    task = store.task(tid)
    assert task["status"] == "waiting_user"
    question = store.one("SELECT * FROM attentions WHERE task_id=? AND state='pending'", (tid,))
    assert question["kind"] == "authorization" and json.loads(question["options"]) == ["执行", "不执行"]
    assert "名字" in question["question"] and "小明" not in question["question"]

    store.answer_attention(question["id"], "执行", nonce="yes")
    model.steps.extend([lambda m, t: tool_reply("browser_confirmed", {"question_id": question["id"]}, "ok"), stop])
    await Service(store, data, model, plugins=[tools.plugin()]).run_until_quiescent()
    assert [h[2] for h in site.writes("/post")] == ["n=%E5%B0%8F%E6%98%8E"]
    assert result(model.requests[-1]["messages"], "browser_confirmed")["confirmation"] == "confirmed"


async def test_a_confirmation_is_void_when_the_form_changed_and_is_asked_again(store, tmp_path, tools, site):
    data = tmp_path / "data"
    site.hits.clear()

    def act(messages, _):
        snap = result(messages, "browser_open")["snapshot"]
        return tool_reply("browser_act", {"actions": [{"do": "fill", "ref": ref(snap, "名字"), "text": "甲"}, {"do": "click", "ref": ref(snap, "提交订单")}]}, "act")
    tid, model = await run(store, data, tools, open_page(site.base + "/form") + [act])
    first = store.one("SELECT id FROM attentions WHERE task_id=? AND state='pending'", (tid,))["id"]
    store.answer_attention(first, "执行", nonce="first")
    # Before the confirmed action runs again, the form content changes.
    tab = tools.browser.tabs[tid]
    await tab.page.fill("input[name=n]", "乙")
    model.steps.extend([lambda m, t: tool_reply("browser_confirmed", {"question_id": first}, "ok")])
    await Service(store, data, model, plugins=[tools.plugin()]).run_until_quiescent()
    assert site.writes("/post") == []
    again = store.one("SELECT * FROM attentions WHERE task_id=? AND state='pending'", (tid,))
    assert again and again["id"] != first and store.task(tid)["status"] == "waiting_user"
    # The first confirmation cannot be used a second time either.
    store.answer_attention(again["id"], "执行", nonce="second")
    model.steps.extend([lambda m, t: tool_reply("browser_confirmed", {"question_id": first}, "reuse"), stop])
    await Service(store, data, model, plugins=[tools.plugin()]).run_until_quiescent()
    assert site.writes("/post") == []
    assert "error" in result(model.requests[-1]["messages"], "browser_confirmed")


async def test_a_declined_action_is_not_run(store, tmp_path, tools, site):
    data = tmp_path / "data"
    site.hits.clear()

    def act(messages, _):
        snap = result(messages, "browser_open")["snapshot"]
        return tool_reply("browser_act", {"actions": [{"do": "click", "ref": ref(snap, "提交订单")}]}, "act")
    tid, model = await run(store, data, tools, open_page(site.base + "/form") + [act])
    qid = store.one("SELECT id FROM attentions WHERE task_id=? AND state='pending'", (tid,))["id"]
    store.answer_attention(qid, "不执行", nonce="no")
    model.steps.extend([lambda m, t: tool_reply("browser_confirmed", {"question_id": qid}, "no"), stop])
    await Service(store, data, model, plugins=[tools.plugin()]).run_until_quiescent()
    assert site.writes("/post") == []
    assert result(model.requests[-1]["messages"], "browser_confirmed")["confirmation"] == "declined"


async def test_spa_write_request_is_blocked_but_ambient_telemetry_and_get_forms_are_not(store, tmp_path, tools, site):
    site.hits.clear()

    def act(messages, _):
        snap = result(messages, "browser_open")["snapshot"]
        return tool_reply("browser_act", {"actions": [{"do": "fill", "ref": ref(snap, "评论"), "text": "好文"},
                                                      {"do": "click", "ref": ref(snap, "点赞")}]}, "act")
    tid, _ = await run(store, tmp_path / "data", tools, open_page(site.base + "/spa") + [act])
    assert site.writes("/api/like") == []
    # The page posts /beacon by itself on load; the same endpoint after an action is not attributed to it.
    assert len(site.writes("/beacon")) >= 2
    assert store.task(tid)["status"] == "waiting_user"

    def search(messages, _):
        snap = result(messages, "browser_open")["snapshot"]
        return tool_reply("browser_act", {"actions": [{"do": "fill", "ref": ref(snap, "搜索"), "text": "鞋"}, {"do": "press", "ref": ref(snap, "搜索"), "text": "Enter"}]}, "act")
    tid2, model = await run(store, tmp_path / "data", tools, open_page(site.base + "/form") + [search, stop], objective="搜一下")
    assert any(h[1].startswith("/search?q=") for h in site.hits)
    assert result(model.requests[-1]["messages"], "browser_act")["confirmation"] == "not_needed"


@pytest.mark.parametrize("label,kind", [("删除这条", "get"), ("发送消息", "websocket")])
async def test_known_gaps_are_never_reported_as_confirmed(store, tmp_path, tools, site, label, kind):
    """GET writes and WebSocket messages cannot be intercepted (design §6)."""
    site.hits.clear()

    def act(messages, _):
        return tool_reply("browser_act", {"actions": [{"do": "click", "ref": ref(result(messages, "browser_open")["snapshot"], label)}]}, "act")
    tid, model = await run(store, tmp_path / "data", tools, open_page(site.base + "/gaps") + [act, stop], objective="点 " + label)
    out = result(model.requests[-1]["messages"], "browser_act")
    assert out["confirmation"] != "confirmed"
    assert not store.one("SELECT id FROM attentions WHERE task_id=?", (tid,))
    if kind == "get":
        assert any(h[1] == "/delete?id=1" for h in site.hits)    # it did happen: the gap is real
    else:
        assert out["unguarded"] == ["websocket"]


# ---- secrets ---------------------------------------------------------------------------

async def test_browser_tools_never_see_or_type_passwords(store, tmp_path, tools, site):
    """The password and code are already in the page (as the user, or autofill, left them)."""
    data = tmp_path / "data"

    def try_fill(messages, _):
        snap = result(messages, "browser_snapshot")["snapshot"]
        return tool_reply("browser_act", {"actions": [{"do": "fill", "ref": ref(snap, "密码"), "text": "guess"}]}, "fill")
    tid, model = await run(store, data, tools, open_page(site.base + "/filled") + [
        tool_reply("browser_snapshot", {}, "s"), tool_reply("browser_snapshot", {"find": "密码"}, "f"),
        tool_reply("browser_read", {}, "r"), try_fill, stop])
    snap = result(model.requests[3]["messages"], "browser_snapshot")
    assert snap["login_page"] is True and "[secret]" in snap["snapshot"]
    refused = result(model.requests[-1]["messages"], "browser_act")
    assert "error" in refused and await tools.browser.tabs[tid].page.input_value("input[name=p]") == CANARY
    ledger = json.dumps([dict(r) for r in store.all("SELECT * FROM operations")] + [dict(r) for r in store.all("SELECT * FROM steps")], ensure_ascii=False)
    files = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in (data / "tasks").rglob("*") if p.is_file())
    for secret in (CANARY, OTP):
        assert secret not in json.dumps(model.requests, ensure_ascii=False)
        assert secret not in ledger and secret not in files


# ---- login handoff and live view -------------------------------------------------------

HOST = "noyi-home.tail1234.ts.net"


def remote(cookie=None, extra=None):
    headers = {"Host": HOST, "X-Forwarded-For": "100.101.102.103", "Origin": "https://" + HOST, **(extra or {})}
    if cookie:
        headers["Cookie"] = "noyi_device=" + cookie
    return headers


async def handoff(store, data, tools, site):
    tid, model = await run(store, data, tools, open_page(site.base + "/login") + [lambda m, t: tool_reply("browser_login", {"reason": "要登录"}, "login")])
    qid = store.one("SELECT id FROM attentions WHERE task_id=? AND state='pending'", (tid,))["id"]
    return tid, qid, model


async def test_live_view_needs_pairing_a_fresh_ticket_and_a_pending_handoff(store, tmp_path, tools, site, monkeypatch):
    from personal_agent.webui import create_app
    data = tmp_path / "data"
    monkeypatch.setenv("NOYI_REMOTE_HOSTS", HOST)
    tid, qid, _ = await handoff(store, data, tools, site)
    assert store.task(tid)["status"] == "waiting_user"
    app = create_app(store, data, ScriptedModel([]), run_service=False, plugins=[tools.plugin()])
    async with TestClient(TestServer(app)) as client:
        token = (await (await client.get("/api/bootstrap")).json())["token"]
        state = await (await client.get("/api/state")).json()
        assert next(q for q in state["questions"] if q["id"] == qid)["live"] is True
        # Unpaired phone: refused before anything else.
        assert (await client.post("/api/browser/live-ticket", json={"question_id": qid}, headers=remote(extra={"X-PA-Token": token}))).status == 401
        assert (await client.get("/api/browser/live?ticket=x", headers=remote())).status == 401
        # Pair a phone.
        code = await (await client.post("/api/pairing", json={"name": "手机"}, headers={"X-PA-Token": token})).json()
        cookie = (await client.post("/api/pair", json={"code": code["code"]}, headers=remote())).cookies["noyi_device"].value
        boot = await (await client.get("/api/bootstrap", headers=remote(cookie))).json()
        # A ticket only for a question that is a pending login handoff.
        other = await client.post("/api/browser/live-ticket", json={"question_id": "nope"}, headers=remote(cookie, {"X-PA-Token": boot["token"]}))
        assert other.status == 400
        ticket = (await (await client.post("/api/browser/live-ticket", json={"question_id": qid}, headers=remote(cookie, {"X-PA-Token": boot["token"]}))).json())["ticket"]
        assert (await client.get("/api/browser/live?ticket=wrong", headers=remote(cookie))).status == 403
        ws = await client.ws_connect("/api/browser/live?ticket=" + ticket, headers=remote(cookie))
        frame = await ws.receive(timeout=10)
        while frame.type != web.WSMsgType.BINARY:
            frame = await ws.receive(timeout=10)
        assert frame.data[:3] == b"\xff\xd8\xff"           # a JPEG frame of the page
        await ws.close()
        # Tickets are single use.
        assert (await client.get("/api/browser/live?ticket=" + ticket, headers=remote(cookie))).status == 403
        # Once the user answered, the live view is closed for good.
        ticket2 = (await (await client.post("/api/browser/live-ticket", json={"question_id": qid}, headers=remote(cookie, {"X-PA-Token": boot["token"]}))).json())["ticket"]
        store.answer_attention(qid, "我登好了", nonce="done")
        assert (await client.get("/api/browser/live?ticket=" + ticket2, headers=remote(cookie))).status == 403
        # A revoked phone cannot ask for tickets.
        device = store.one("SELECT id FROM paired_devices")["id"]
        await client.post("/api/devices/revoke", json={"id": device}, headers={"X-PA-Token": token})
        assert (await client.post("/api/browser/live-ticket", json={"question_id": qid}, headers=remote(cookie, {"X-PA-Token": boot["token"]}))).status == 401


async def test_after_login_the_task_continues_with_the_saved_session(store, tmp_path, tools, site):
    data = tmp_path / "data"
    tid, qid, model = await handoff(store, data, tools, site)
    store.answer_attention(qid, "我登好了", nonce="done")
    model.steps.extend([lambda m, t: tool_reply("browser_snapshot", {}, "after"), stop])
    await Service(store, data, model, plugins=[tools.plugin()]).run_until_quiescent()
    assert result(model.requests[-1]["messages"], "browser_snapshot")["title"] == "登录"


async def test_a_page_read_in_the_browser_can_be_cited_like_a_fetched_page(store, tmp_path, tools, site):
    data = tmp_path / "data"

    def write(messages, _):
        page = result(messages, "browser_read")
        return tool_reply("write_artifact", {"name": "r.md", "content": f"页面写着收到了。[{page['page_id']}]"}, "w")

    def submit(messages, _):
        page = result(messages, "browser_read")
        artifact = result(messages, "write_artifact")
        return tool_reply("submit_result", {"artifact_ref": artifact["artifact_ref"], "summary": "好了", "limitations": [],
                                            "quotes": [{"source_id": page["page_id"], "quote": page["text"][:8]}]}, "s")
    tid, model = await run(store, data, tools, open_page(site.base + "/form") + [tool_reply("browser_read", {}, "r"), write, submit])
    assert store.task(tid)["status"] == "succeeded", result(model.requests[-1]["messages"], "submit_result")


# ---- privacy ---------------------------------------------------------------------------

async def test_the_service_browser_reaches_only_public_addresses(site, tmp_path):
    """The same SSRF rule as fetch_page: nothing on this machine or the home network."""
    from personal_agent.browser import Browser, BrowserConfig
    b = Browser(BrowserConfig(profile=tmp_path / "p"))
    try:
        tab = await b.tab("t")
        with pytest.raises(Exception):
            await tab.page.goto(site.base + "/form", timeout=10000)
        assert not site.hits or all(h[1] != "/form" for h in site.hits)
    finally:
        await b.close()


async def test_browser_results_never_reach_the_ops_bundle(store, tmp_path, tools, site):
    from personal_agent.health import parse_local
    from personal_agent.ops_export import ENUMS, build_bundle, validate_bundle
    assert {"browser_open", "browser_act", "browser_read", "load_capability"} <= ENUMS["operations.tool"]
    store.db.execute("INSERT INTO operations(id,task_id,run_id,tool,arguments,status,result,created,ended) VALUES('b','t','r','browser_read',?,'succeeded',?,?,?)",
                     (json.dumps({"url": "https://x/" + CANARY}), json.dumps({"text": CANARY, "snapshot": CANARY, "url": CANARY}), parse_local("2026-11-20 10:00"), parse_local("2026-11-20 10:01")))
    bundle = build_bundle(store, since=0, until=parse_local("2026-11-21 00:00"), salt="s", service={}, version={})
    text = json.dumps(bundle, ensure_ascii=False)
    assert CANARY not in text and '"browser_read"' in text
    validate_bundle(bundle)


async def test_browser_profile_is_not_in_backups(store, tmp_path):
    from personal_agent.backup import create_backup
    data = tmp_path / "data"
    (data / "browser" / "profile").mkdir(parents=True)
    (data / "browser" / "profile" / "Cookies").write_text(CANARY, encoding="utf-8")
    archive = tmp_path / "out" / "b.zip"
    create_backup(store, data, archive)
    with zipfile.ZipFile(archive) as z:
        assert not any(n.startswith("browser/") for n in z.namelist())
        assert all(CANARY.encode() not in z.read(n) for n in z.namelist())
