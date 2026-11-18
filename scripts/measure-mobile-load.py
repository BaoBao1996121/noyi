"""Measure how long the page takes to show the conversation on a slow, far network.

    python scripts/measure-mobile-load.py --data <copy of a data dir> [--rtt 300] [--kbps 2000] [--runs 3]

Starts this checkout's WebUI (background service off: nothing runs, nothing is
sent) on a spare port, puts a throttling proxy in front of it and opens the page
in Chromium through the proxy. The proxy adds half the round trip before the
request reaches the server and half before the first response byte, and paces
every byte through one shared link of the given bandwidth. Throttling lives in
the proxy, not in the browser, so requests a service worker makes are slowed too.

Reports for a cold open (empty browser cache) and a warm one (same browser,
page opened again): time until the first message is on screen, requests and
bytes over the link, and the request timeline (to spot serial chains).

Needs Playwright (pip install playwright; playwright install chromium). It is a
measuring tool, not a project dependency. Use a copy of the data directory:
the WebUI takes the directory's service lock.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

ROOT = Path(__file__).resolve().parents[1]
HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-connection", "te", "trailer"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Link:
    """One shared pipe: bytes leave at most `rate` bytes per second."""
    def __init__(self, kbps):
        self.rate, self.free_at, self.lock = kbps * 1000 / 8, 0.0, asyncio.Lock()

    async def send(self, n):
        async with self.lock:
            start = max(time.monotonic(), self.free_at)
            self.free_at = start + n / self.rate
            wait = self.free_at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)


def proxy_app(upstream, rtt, link, log):
    session = None

    async def handle(request):
        nonlocal session
        if session is None:
            session = ClientSession(connector=TCPConnector(limit=0), auto_decompress=False, timeout=ClientTimeout(total=60))
        started = time.monotonic()
        body = await request.read()
        await link.send(len(body) + 400)
        await asyncio.sleep(rtt / 2)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        async with session.request(request.method, upstream + request.path_qs, headers=headers, data=body or None, allow_redirects=False) as up:
            raw = await up.read()
            out = web.StreamResponse(status=up.status, headers={k: v for k, v in up.headers.items() if k.lower() not in HOP | {"content-length"}})
        await asyncio.sleep(rtt / 2)
        out.content_length = len(raw)
        try:
            await out.prepare(request)
        except ConnectionError:
            return out  # the browser gave up on this request (page closed)
        size = len(raw) + 300  # rough header bytes
        for i in range(0, max(len(raw), 1), 16384):
            chunk = raw[i:i + 16384]
            await link.send(len(chunk) + (300 if i == 0 else 0))
            await out.write(chunk)
        await out.write_eof()
        log.append({"path": request.path_qs, "status": up.status, "bytes": size, "start": started, "end": time.monotonic(),
                    "encoding": up.headers.get("Content-Encoding", ""), "cache": up.headers.get("Cache-Control", "")})
        return out

    async def close(_):
        if session is not None:
            await session.close()

    app = web.Application(client_max_size=10_000_000)
    app.router.add_route("*", "/{tail:.*}", handle)
    app.on_cleanup.append(close)
    return app


SERVER = """
import sys
from pathlib import Path
from aiohttp import web
from personal_agent.store import Store
from personal_agent.webui import create_app
class Model:
    ready = True
    async def complete(self, *a, **k): raise RuntimeError('measuring only')
    async def close(self): pass
data = Path(sys.argv[1]).resolve()
web.run_app(create_app(Store(data / 'state.sqlite'), data, Model(), run_service=False), host='127.0.0.1', port=int(sys.argv[2]), print=None)
"""


async def wait_up(url):
    async with ClientSession() as s:
        for _ in range(200):
            try:
                async with s.get(url + "/api/bootstrap") as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            await asyncio.sleep(0.1)
    raise SystemExit("server did not start")


async def open_once(context, url, log, timeout=120):
    log.clear()
    page = await context.new_page()
    t0 = time.monotonic()
    await page.add_init_script("""new MutationObserver((_, o) => { if (document.querySelector('#messages .message')) { window.__shown = performance.now(); o.disconnect(); } }).observe(document, {subtree: true, childList: true});""")
    await page.goto(url, wait_until="commit")
    await page.wait_for_selector("#messages .message", timeout=timeout * 1000)
    shown = time.monotonic() - t0
    marks = await page.evaluate("""() => ({shown: window.__shown, res: performance.getEntriesByType('resource').filter(r => r.name.includes('/api/')).map(r => [new URL(r.name).pathname, Math.round(r.startTime), Math.round(r.responseEnd)])})""")
    await asyncio.sleep(1.5)  # let late startup requests show up in the log
    reqs = [r for r in log if r["start"] - t0 < shown + 1.5]
    await page.close()
    return {"shown_s": round(shown, 2), "page_shown_ms": round(marks["shown"] or 0), "page_api": marks["res"], "requests": len(reqs), "bytes": sum(r["bytes"] for r in reqs),
            "before_shown": sum(1 for r in reqs if r["end"] - t0 <= shown),
            "timeline": [(round(r["start"] - t0, 2), round(r["end"] - t0, 2), r["path"], r["status"], r["bytes"], r["encoding"]) for r in reqs]}


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True, type=Path)
    p.add_argument("--rtt", type=float, default=300)
    p.add_argument("--kbps", type=float, default=2000)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--json", type=Path)
    p.add_argument("--server-python", default=sys.executable, help="interpreter with the project installed (its aiohttp)")
    a = p.parse_args()
    from playwright.async_api import async_playwright
    port, proxy_port = free_port(), free_port()
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1"}
    server = subprocess.Popen([a.server_python, "-c", SERVER, str(a.data), str(port)], env=env, cwd=ROOT)
    log = []
    runner = None
    try:
        await wait_up(f"http://127.0.0.1:{port}")
        runner = web.AppRunner(proxy_app(f"http://127.0.0.1:{port}", a.rtt / 1000, Link(a.kbps), log), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", proxy_port).start()
        url = f"http://127.0.0.1:{proxy_port}/"
        results = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            for _ in range(a.runs):
                context = await browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                cold = await open_once(context, url, log)
                await asyncio.sleep(2)  # service worker install
                warm = await open_once(context, url, log)
                await context.close()
                results.append({"cold": cold, "warm": warm})
                print(f"page shown cold {cold['page_shown_ms']}ms api {cold['page_api']} | warm {warm['page_shown_ms']}ms api {warm['page_api']}"); print(f"cold {cold['shown_s']}s {cold['requests']} req {cold['bytes']} B | warm {warm['shown_s']}s {warm['requests']} req {warm['bytes']} B", flush=True)
            await browser.close()
        for kind in ("cold", "warm"):
            shown = sorted(r[kind]["shown_s"] for r in results)
            print(f"{kind}: median {shown[len(shown)//2]}s, requests {results[-1][kind]['requests']}, bytes {results[-1][kind]['bytes']}")
            for row in results[-1][kind]["timeline"]:
                print("   ", row)
        if a.json:
            a.json.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        if runner:
            await runner.cleanup()
        server.terminate()
        server.wait(10)


if __name__ == "__main__":
    asyncio.run(main())
