"""Opt-in paid live check of memory phase 1 against a real model, in its own data dir.

Starts the WebUI on a separate port with a fresh data directory (model settings
are copied file-for-file from --model-from, never printed), then checks:
1 the reply layer uses memory; 2 two private chats share it and a group does
not; 3 a manual tidy produces proposals; 4 confirming a memory does not stop a
running task; 5 imported seeds show as pending. Prints observations only.
"""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        self.token = None

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET",
                                         headers={"Content-Type": "application/json", "Origin": self.base, **({"X-PA-Token": self.token} if self.token else {})})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{path} -> {exc.code} {exc.read().decode(errors='replace')[:300]}") from None

    def state(self, room):
        return self.call("/api/state?room=" + room)

    def say(self, room, text):
        return self.call("/api/messages", {"text": text, "nonce": uuid.uuid4().hex, "room": room, "mode": "auto", "uploads": [], "urls": []})

    def reply_after(self, room, since_count, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            replies = [d for d in self.state(room)["deliveries"] if d["kind"] != "system"]
            if len(replies) > since_count:
                return replies[since_count]["content"]
            time.sleep(1)
        return None

    def replies(self, room):
        return len([d for d in self.state(room)["deliveries"] if d["kind"] != "system"])


def ask(client, room, text):
    before = client.replies(room)
    client.say(room, text)
    return client.reply_after(room, before)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-from", type=Path, default=REPO / ".pa" / "personal")
    parser.add_argument("--output", type=Path, default=REPO / ".pa" / ("memory-check-" + str(int(time.time()))))
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--seeds", type=Path)
    args = parser.parse_args()
    data = args.output.resolve()
    data.mkdir(parents=True, exist_ok=False)
    for name in ("model.json", "credentials.key", "search.key"):
        if (args.model_from / name).is_file():
            shutil.copyfile(args.model_from / name, data / name)
    log = open(data.parent / (data.name + ".log"), "w", encoding="utf-8")
    server = subprocess.Popen([sys.executable, "-m", "personal_agent", "--data", str(data), "web", "--port", str(args.port)], cwd=REPO, stdout=log, stderr=log)
    results = {}
    try:
        client = Client(args.port)
        for _ in range(60):
            try:
                client.token = client.call("/api/bootstrap")["token"]
                break
            except Exception:
                time.sleep(0.5)
        db = sqlite3.connect(data / "state.sqlite", timeout=10)

        # 1 reply layer reads memory
        client.call("/api/manage/save_memory", {"room": "local", "key": "称呼", "value": "叫我阿岚", "purpose": "对话时称呼我", "kind": "about_me"})
        client.call("/api/manage/save_memory", {"room": "local", "key": "回答格式", "value": "回答时第一句先给结论", "purpose": "回复", "kind": "rule"})
        answer = ask(client, "local", "你记得我希望你怎么称呼我吗？")
        results["1_reply_layer_uses_memory"] = {"reply": answer, "mentions_name": bool(answer and "阿岚" in answer)}

        # 2 another private chat shares it; a group does not
        bot = client.call("/api/manage/create_bot", {"name": "资料助手", "role": "帮我查资料、整理要点"})
        answer = ask(client, bot["private_room"], "我希望别人怎么称呼我？")
        group = client.call("/api/manage/create_group", {"name": "测试群", "members": [bot["id"], "default"]})
        group_answer = ask(client, group["id"], "你们知道我希望怎么被称呼吗？不知道就直说。")
        group_state = client.state(group["id"])
        results["2_shared_private_not_group"] = {"other_private_reply": answer, "other_private_mentions": bool(answer and "阿岚" in answer),
                                                  "group_reply": group_answer, "group_mentions": bool(group_answer and "阿岚" in group_answer),
                                                  "group_memory_scope": group_state["memory_scope"], "group_memories": len(group_state["memories"])}

        # 3 manual tidy: the same thing said in two conversations hours apart
        client.say("local", "我最近在学做菜，想每周学一道家常菜")
        time.sleep(1)
        db.execute("UPDATE events SET created=created-10800 WHERE message_id IN (SELECT id FROM messages WHERE content LIKE '我最近在学做菜%')")
        db.execute("UPDATE conversation_inputs SET created=created-10800 WHERE message_id IN (SELECT id FROM messages WHERE content LIKE '我最近在学做菜%')")
        db.commit()
        ask(client, "local", "这周学做菜学了红烧肉，下周想学鱼香肉丝")
        tidy = client.call("/api/manage/run_memory_curation", {"room": "local"})
        proposals = [p for p in client.state("local")["memory_proposals"] if p["column"] == "tidy"]
        results["3_manual_tidy"] = {"result": tidy, "proposals": [{"op": p["op"], "kind": p["kind"], "key": p["key"], "value": p["value"], "reason": p["reason"]} for p in proposals]}

        # 4 confirming a memory while a task runs does not stop it
        pending = client.call("/api/manage/save_memory", {"room": "local", "key": "临时", "value": "仅测试", "purpose": "测试", "kind": "preference"})
        client.say("local", "帮我写一份 300 字左右的一周家常菜学习计划，作为成果交付给我")
        task_run = None
        for _ in range(120):
            task_run = db.execute("SELECT r.id,r.task_id FROM runs r WHERE r.phase='running' AND r.task_id IS NOT NULL").fetchone()
            if task_run:
                break
            time.sleep(0.5)
        confirmed = None
        if task_run:
            imported = client.call("/api/manage/save_memory", {"room": "local", "key": "做菜口味", "value": "喜欢少油少盐", "purpose": "做饭建议", "kind": "preference"})
            confirmed = {"saved_new_memory_while_running": imported["version"] == 1}
            props = [p for p in client.state("local")["memory_proposals"]]
            if props:
                confirmed["decided"] = client.call("/api/manage/decide_memory", {"id": props[0]["id"], "accept": True, "choice": "new" if props[0]["op"] == "conflict" and len(props[0]["targets"]) == 1 else "both" if props[0]["op"] == "conflict" else None})["state"]
        outcome = None
        for _ in range(300):
            row = db.execute("SELECT status FROM tasks WHERE id=?", (task_run[1],)).fetchone() if task_run else None
            if row and row[0] in {"succeeded", "failed", "review", "paused", "stopped", "cancelled"}:
                outcome = row[0]
                break
            time.sleep(1)
        stops = db.execute("SELECT COUNT(*) FROM runs WHERE task_id=? AND end_reason='scope_changed'", (task_run[1],)).fetchone()[0] if task_run else None
        results["4_confirm_does_not_interrupt"] = {"task_seen_running": bool(task_run), "confirm": confirmed, "task_outcome": outcome, "scope_changed_stops": stops, "pending_test_memory": pending["version"]}

        # 5 seeds wait as pending
        if args.seeds:
            items = json.loads(args.seeds.read_text(encoding="utf-8"))
            out = subprocess.run([sys.executable, "-m", "personal_agent", "--data", str(data), "memory-import", str(args.seeds), "--origin", "从 Codex / Claude Code 导入"], cwd=REPO, capture_output=True, text=True, encoding="utf-8")
            imported = [p for p in client.state("local")["memory_proposals"] if p["origin"] == "从 Codex / Claude Code 导入"]
            results["5_seeds_pending"] = {"file_items": len(items), "pending": len(imported), "column": sorted({p["column"] for p in imported}),
                                          "active_from_import": len([m for m in client.state("local")["memories"] if m.get("origin") == "从 Codex / Claude Code 导入"]), "cli_exit": out.returncode}
        results["model_requests"] = db.execute("SELECT COUNT(*) FROM model_requests").fetchone()[0]
        db.close()
    finally:
        server.terminate()
        try:
            server.wait(20)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
    print(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
