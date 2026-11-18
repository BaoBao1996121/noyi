"""Measure how long a sent message takes to show on a slow, far network.

    python scripts/measure-send.py [--rtt 300] [--kbps 2000] [--runs 3] [--scale 3]

Builds a throwaway data directory with made-up content (`--scale` times the size
of a typical personal ledger: 8 tasks, 21 messages, 29 replies), starts this
checkout's WebUI on it with the background service off, and puts the throttling
proxy from measure-mobile-load.py in front. Chromium (phone viewport) opens the
page, waits until it is idle, types a message and clicks send.

Reports, from the click: when the message shows in the conversation, when the
input box is empty again, and the requests made until then. Also times
POST /api/messages against the server directly (no proxy), so server work and
network are told apart.

Needs Playwright (pip install playwright; playwright install chromium).
"""
import argparse
import asyncio
import importlib.util
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

from aiohttp import ClientSession, web

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("measure_mobile_load", ROOT / "scripts" / "measure-mobile-load.py")
load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load)


SEED = """
import sys, time, uuid
from pathlib import Path
from personal_agent.contracts import TaskSpec
from personal_agent.store import Store
data, scale = Path(sys.argv[1]), int(sys.argv[2])
store = Store(data / "state.sqlite")
for i in range(8 * scale):
    store.route_input(TaskSpec(f"整理第 {i} 份资料", [], max_calls=40, max_tools=100, max_seconds=900), text=f"整理第 {i} 份资料", nonce=f"t{i}", mode="task")
for i in range(21 * scale):
    store.route_input(None, text=f"第 {i} 条消息", nonce=f"m{i}", mode="chat")
for i in range(29 * scale):
    store.db.execute("INSERT INTO deliveries(id,run_id,room_id,content,created) VALUES(?,?,?,?,?)", (uuid.uuid4().hex, uuid.uuid4().hex, "local", f"回复 {i} " + "内容" * 200, time.time()))
store.close()
"""


async def server_side(url, n=10):
    """POST /api/messages straight to the server: its own processing time."""
    async with ClientSession() as s:
        boot = await (await s.get(url + "/api/bootstrap")).json()
        times = []
        for i in range(n):
            t = time.perf_counter()
            r = await s.post(url + "/api/messages", json={"text": f"直连测速 {i}", "mode": "auto", "room": "local", "nonce": uuid.uuid4().hex, "uploads": [], "urls": []}, headers={"X-PA-Token": boot["token"]})
            await r.read()
            times.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            await (await s.get(url + "/api/state?room=local")).read()
            times[-1] = (times[-1], (time.perf_counter() - t) * 1000)
    return times


async def send_once(page, log, text):
    await page.wait_for_selector("#messages .message")
    await asyncio.sleep(1)
    await page.fill("#message-input", text)
    log.clear()
    started = time.monotonic()
    # Watch from inside the page so the moments are not delayed by the driver's own round trips.
    await page.evaluate("""text => { const mark = window.__sent = {}; const t0 = performance.now();
      const check = () => { const shown = [...document.querySelectorAll('#messages .message.user .bubble')].some(b => b.textContent === text);
        if (shown && !mark.shown) mark.shown = performance.now() - t0;
        if (!document.querySelector('#message-input').value && !mark.cleared) mark.cleared = performance.now() - t0;
        if (mark.shown && mark.cleared) { watch.disconnect(); clearInterval(mark.timer); } };
      const watch = new MutationObserver(check); watch.observe(document, {subtree: true, childList: true, characterData: true, attributes: true});
      mark.timer = setInterval(check, 5);
      document.querySelector('#send').click(); }""", text)
    for _ in range(6000):  # wait_for_function needs eval, which the page's CSP forbids
        if await page.evaluate("() => Boolean(window.__sent.shown && window.__sent.cleared)"):
            break
        await asyncio.sleep(0.01)
    else:
        raise SystemExit("message never showed")
    marks = await page.evaluate("() => window.__sent")
    await asyncio.sleep(3)  # let in-flight requests finish so they show in the timeline
    shown_at = started + marks["shown"] / 1000
    before = [r for r in log if r["start"] <= shown_at and not r["path"].startswith("/api/state?room=local&since")]
    return {"shown_ms": round(marks["shown"]), "cleared_ms": round(marks["cleared"]), "requests_before_shown": len(before),
            "timeline": [(round((r["start"] - started) * 1000), round((r["end"] - started) * 1000), r["path"][:60], r["status"]) for r in log]}


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rtt", type=float, default=300)
    p.add_argument("--kbps", type=float, default=2000)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--scale", type=int, default=3)
    p.add_argument("--server-python", default=sys.executable)
    a = p.parse_args()
    from playwright.async_api import async_playwright
    data = Path(tempfile.mkdtemp(prefix="noyi-send-"))
    port, proxy_port = load.free_port(), load.free_port()
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1"}
    subprocess.run([a.server_python, "-c", SEED, str(data), str(a.scale)], env=env, cwd=ROOT, check=True)
    server = subprocess.Popen([a.server_python, "-c", load.SERVER, str(data), str(port)], env=env, cwd=ROOT)
    log, runner = [], None
    try:
        direct = f"http://127.0.0.1:{port}"
        await load.wait_up(direct)
        runner = web.AppRunner(load.proxy_app(direct, a.rtt / 1000, load.Link(a.kbps), log), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", proxy_port).start()
        results = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            context = await browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{proxy_port}/")
            for i in range(a.runs):
                r = await send_once(page, log, f"测速消息 {i} {uuid.uuid4().hex[:6]}")
                results.append(r)
                print(f"run {i}: shown {r['shown_ms']} ms, input cleared {r['cleared_ms']} ms, requests before shown {r['requests_before_shown']}", flush=True)
                for row in r["timeline"]:
                    print("   ", row)
            await browser.close()
        print(f"median: shown {statistics.median(r['shown_ms'] for r in results)} ms, cleared {statistics.median(r['cleared_ms'] for r in results)} ms")
        direct_times = await server_side(direct)
        post, state = [t[0] for t in direct_times], [t[1] for t in direct_times]
        print(f"server direct (no proxy): POST /api/messages median {statistics.median(post):.1f} ms max {max(post):.1f} ms; GET /api/state median {statistics.median(state):.1f} ms")
    finally:
        if runner:
            await runner.cleanup()
        server.terminate()
        server.wait(10)


if __name__ == "__main__":
    asyncio.run(main())
