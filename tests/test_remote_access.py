"""Phone access over Tailscale: only allowed host names, only paired devices."""
import pytest
from aiohttp.test_utils import TestClient, TestServer

from conftest import ScriptedModel
from personal_agent.remote import RemoteConfig, hash_secret
from personal_agent.webui import create_app

HOST = "noyi-home.tail1234.ts.net"


@pytest.fixture
async def client(store, tmp_path, monkeypatch):
    monkeypatch.setenv("NOYI_REMOTE_HOSTS", HOST)
    app = create_app(store, tmp_path / "data", ScriptedModel([]), run_service=False)
    async with TestClient(TestServer(app)) as c:
        yield c


def remote(extra=None, cookie=None):
    headers = {"Host": HOST, "X-Forwarded-For": "100.101.102.103", **(extra or {})}
    if cookie:
        headers["Cookie"] = "noyi_device=" + cookie
    return headers


async def pair(client):
    local = await client.get("/api/bootstrap")
    token = (await local.json())["token"]
    code = await (await client.post("/api/pairing", json={"name": "我的 iPhone"}, headers={"X-PA-Token": token})).json()
    response = await client.post("/api/pair", json={"code": code["code"]}, headers=remote({"Origin": "https://" + HOST}))
    assert response.status == 200, await response.text()
    cookie = response.cookies["noyi_device"]
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Strict"
    return cookie.value, code


def test_remote_hosts_come_only_from_configuration(monkeypatch):
    monkeypatch.setenv("NOYI_REMOTE_HOSTS", "a.ts.net, b.ts.net ,,")
    assert RemoteConfig.load().hosts == {"a.ts.net", "b.ts.net"}
    monkeypatch.setenv("NOYI_REMOTE_HOSTS", "evil.example.com;rm -rf")
    with pytest.raises(ValueError):
        RemoteConfig.load()


async def test_unknown_host_is_refused_even_with_a_device(client):
    response = await client.get("/api/state", headers={"Host": "attacker.example.com"})
    assert response.status == 403


async def test_unpaired_remote_gets_only_the_pairing_page(client):
    for path in ["/api/bootstrap", "/api/state", "/api/tasks/x", "/assets/app.js"]:
        response = await client.get(path, headers=remote())
        assert response.status in {401, 403}, path
    page = await client.get("/", headers=remote())
    assert page.status == 200 and "配对" in await page.text()
    for path in ["/manifest.webmanifest", "/assets/pair.js", "/assets/icon-192.png"]:
        assert (await client.get(path, headers=remote())).status == 200, path


async def test_pairing_code_is_created_only_on_this_machine_and_used_once(client, store):
    cookie, code = await pair(client)
    # A code cannot be created remotely, even by a paired device.
    boot = await (await client.get("/api/bootstrap", headers=remote(cookie=cookie))).json()
    denied = await client.post("/api/pairing", json={"name": "x"}, headers=remote({"X-PA-Token": boot["token"], "Origin": "https://" + HOST}, cookie))
    assert denied.status == 403
    again = await client.post("/api/pair", json={"code": code["code"]}, headers=remote({"Origin": "https://" + HOST}))
    assert again.status == 400
    row = store.one("SELECT * FROM paired_devices")
    assert row["secret_sha256"] == hash_secret(cookie) and cookie not in str(row)


async def test_paired_device_can_use_the_app_until_revoked(client, store):
    cookie, _ = await pair(client)
    assert (await client.get("/api/state", headers=remote(cookie=cookie))).status == 200
    assert (await client.get("/assets/app.js", headers=remote(cookie=cookie))).status == 200
    token = (await (await client.get("/api/bootstrap")).json())["token"]
    device = store.one("SELECT id FROM paired_devices")["id"]
    revoked = await client.post("/api/devices/revoke", json={"id": device}, headers={"X-PA-Token": token})
    assert revoked.status == 200
    assert (await client.get("/api/state", headers=remote(cookie=cookie))).status == 401


async def test_expired_or_wrong_codes_fail_and_too_many_tries_lock_pairing(client, store):
    token = (await (await client.get("/api/bootstrap")).json())["token"]
    await client.post("/api/pairing", json={"name": "p"}, headers={"X-PA-Token": token})
    store.db.execute("UPDATE pairing_codes SET expires=0")
    code = store.one("SELECT * FROM pairing_codes")
    for _ in range(6):
        response = await client.post("/api/pair", json={"code": "WRONGCODE1"}, headers=remote({"Origin": "https://" + HOST}))
        assert response.status in {400, 429}
    assert response.status == 429


async def test_remote_requests_still_need_the_write_token_and_same_origin(client):
    cookie, _ = await pair(client)
    no_token = await client.post("/api/messages", json={"text": "hi", "nonce": "n"}, headers=remote({"Origin": "https://" + HOST}, cookie))
    assert no_token.status == 403
    boot = await (await client.get("/api/bootstrap", headers=remote(cookie=cookie))).json()
    cross = await client.post("/api/messages", json={"text": "hi", "nonce": "n"}, headers=remote({"X-PA-Token": boot["token"], "Origin": "https://evil.example.com"}, cookie))
    assert cross.status == 403


async def test_local_access_is_unchanged(client):
    assert (await client.get("/api/state")).status == 200
    assert (await client.get("/assets/app.js")).status == 200
