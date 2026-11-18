"""Opening the page from a phone far from home: few round trips, small transfers,
nothing re-downloaded on the next open, never an old script after a release."""
import json
import re
import time
import uuid

from aiohttp.test_utils import TestClient, TestServer

from conftest import ScriptedModel
from personal_agent.contracts import TaskSpec
from personal_agent.webui import ASSETS, STATIC_FILES, WEB, create_app

GZ = {"Accept-Encoding": "gzip, deflate, br"}


def seed(store, scale):
    """About `scale` times the user's real ledger (8 tasks, 21 messages, 29 replies, 34 memories)."""
    for i in range(8 * scale):
        store.route_input(TaskSpec(f"整理第 {i} 份资料", [], max_calls=40, max_tools=100, max_seconds=900), text=f"整理第 {i} 份资料", nonce=f"t{i}", mode="task")
    for i in range(21 * scale):
        store.route_input(None, text=f"第 {i} 条消息 <script>alert(1)</script>", nonce=f"m{i}", mode="chat")
    for i in range(29 * scale):
        store.db.execute("INSERT INTO deliveries(id,run_id,room_id,content,created) VALUES(?,?,?,?,?)", (uuid.uuid4().hex, uuid.uuid4().hex, "local", f"回复 {i} " + "内容" * 200, time.time()))
    for i in range(34 * scale):
        store.save_memory("local", key=f"记忆{i}", value=f"内容 {i}", purpose="测试")


def page_assets(html):
    return sorted(set(re.findall(r'(?:href|src)="(/assets/[^"]+)"', html)))


def early_of(html):
    block = re.search(r'<script type="application/json" id="noyi-early">(.*?)</script>', html, re.S)
    return json.loads(block[1])


async def open_page(client):
    """What a browser fetches before the conversation can show: the page and every
    script and style it names (modulepreload included). Returns the page, the
    asset responses and how many requests that was."""
    page = await client.get("/", headers=GZ)
    html = await page.text()
    assets = {url: await client.get(url, headers=GZ) for url in page_assets(html)}
    return page, html, assets, 1 + len(assets)


async def test_first_screen_needs_the_same_requests_whatever_the_ledger_size(store, tmp_path):
    app = create_app(store, tmp_path / "data", ScriptedModel([]), run_service=False)
    async with TestClient(TestServer(app)) as client:
        _, html_small, _, small = await open_page(client)
        seed(store, 3)
        started = time.monotonic()
        page, html, assets, large = await open_page(client)
        elapsed = time.monotonic() - started
        # The page carries the first state: no /api round trip before the conversation shows.
        early = early_of(html)
        assert len(early["state"]["messages"]) >= 63 and len(early["state"]["deliveries"]) >= 87 and early["boot"]["token"]
        assert "<script>alert" not in html  # message text cannot close the data block
        assert large == small and page_assets(html) == page_assets(html_small)
        assert page.headers["Content-Encoding"] == "gzip" and page.headers["Cache-Control"] == "no-store"
        # Every module the page imports, at any depth, is preloaded (one round trip, not one per level).
        names = {re.sub(r"\?.*", "", u).removeprefix("/assets/") for u in assets}
        for name in [n for n in names if n.endswith(".js")]:
            for imported in re.findall(r"from\s*'\./([\w.-]+\.js)", (WEB / name).read_text(encoding="utf-8")):
                assert imported in names, f"{name} imports {imported}, which the page does not preload"
        # Not needed for the first screen: loaded when their tab opens.
        assert not names & {"replay.js", "replay-pixel.js", "galaxy.js"}
        assert all(r.status == 200 for r in assets.values())
        assert elapsed < 10  # loose bound only; ~0.3 s here


async def test_second_open_downloads_no_static_file_and_a_release_changes_every_url(store, tmp_path):
    app = create_app(store, tmp_path / "data", ScriptedModel([]), run_service=False)
    async with TestClient(TestServer(app)) as client:
        _, html, assets, _ = await open_page(client)
        version = app[ASSETS].version
        for url, response in assets.items():
            assert f"?v={version}" in url
            assert response.headers["Cache-Control"] == "private, max-age=31536000, immutable"
            if url.split("?")[0].endswith((".js", ".css")) and len(await response.read()) > 600:
                assert response.headers["Content-Encoding"] == "gzip"
            again = await client.get(url, headers={"If-None-Match": response.headers["ETag"], **GZ})
            assert again.status == 304 and not await again.read()
        # Anything not naming the current build is only revalidated, never kept for long.
        stale = await client.get("/assets/app.js?v=old")
        assert stale.headers["Cache-Control"] == "no-cache"
        # The ledger itself is never stored by the browser, and is compressed.
        state = await client.get("/api/state?room=local", headers=GZ)
        assert state.headers["Cache-Control"] == "no-store" and state.headers["Content-Encoding"] == "gzip"
        # The service worker caches exactly this build's shell (all scripts and styles), no API.
        sw = await (await client.get("/sw.js")).text()
        shell = json.loads(re.search(r"const SHELL = (\[.*?\]);", sw)[1])
        expected = {n for n in STATIC_FILES if n.endswith((".js", ".css")) and n != "sw.js" and not n.startswith("pair.")}
        assert {u.split("?")[0].removeprefix("/assets/") for u in shell} >= expected
        assert all(u.endswith(f"?v={version}") for u in shell) and not any("/api/" in u for u in shell)
        assert json.dumps(version) in sw

        # A release: one file changes, so the build, every URL and the worker change.
        assets_obj = app[ASSETS]
        real = assets_obj.root
        copy = tmp_path / "web"
        copy.mkdir()
        for name in assets_obj.names:
            (copy / name).write_bytes((real / name).read_bytes())
        (copy / "progress.js").write_text((real / "progress.js").read_text(encoding="utf-8") + "\n// new release\n", encoding="utf-8")
        assets_obj.root, assets_obj._checked = copy, 0
        _, new_html, new_assets, _ = await open_page(client)
        assert assets_obj.version != version
        assert not set(new_assets) & set(assets)
        imported = await (await client.get(f"/assets/app.js?v={assets_obj.version}")).text()
        assert f"./progress.js?v={assets_obj.version}" in imported and f"./progress.js?v={version}" not in imported
        assert json.dumps(assets_obj.version) in await (await client.get("/sw.js")).text()


async def test_page_opens_on_the_room_it_last_showed_and_ignores_rooms_it_does_not_own(store, tmp_path):
    app = create_app(store, tmp_path / "data", ScriptedModel([]), run_service=False)
    async with TestClient(TestServer(app)) as client:
        assert early_of(await (await client.get("/", headers={"Cookie": "noyi_room=nope"})).text())["room"] == "local"
        bot = store.create_bot("助手", "帮忙")["id"]
        group = store.create_group("小组", [bot])["id"]
        assert early_of(await (await client.get("/", headers={"Cookie": f"noyi_room={group}"})).text())["room"] == group
