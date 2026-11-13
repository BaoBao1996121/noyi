"""Web Push: RFC 8291 encryption, VAPID, and 'delivered' meaning the user actually saw it."""
import base64
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from conftest import ScriptedModel
from personal_agent.contracts import TaskSpec
from personal_agent.push import (PushChannel, PushGone, b64d, b64e, encrypt, load_vapid, push_payload, vapid_headers)
from personal_agent.service import FileChannel
from personal_agent.webui import create_app

HOST = "noyi-home.tail1234.ts.net"


def test_encryption_matches_rfc8291_appendix_a():
    body = encrypt(b64d("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
                   ua_public=b64d("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
                   auth_secret=b64d("BTBZMqHH6r4Tts7J_aSIgg"),
                   as_private=b64d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"),
                   salt=b64d("DGv6ra1nlYgDCS1FRnbzlw"))
    assert b64e(body) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
                          "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
                          "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_vapid_header_is_a_valid_es256_jwt_for_the_push_origin(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography.hazmat.primitives import hashes
    key = load_vapid(tmp_path)
    assert load_vapid(tmp_path).public == key.public            # created once, then reused
    headers = vapid_headers(key, "https://web.push.apple.com/abc/def", subject="mailto:noyi@localhost", now=1_000_000)
    scheme, rest = headers["Authorization"].split(" ", 1)
    token, k = rest.split(", k=")
    token = token.removeprefix("t=")
    assert scheme == "vapid" and k == key.public
    head, claims, sig = token.split(".")
    assert json.loads(b64d(head)) == {"typ": "JWT", "alg": "ES256"}
    body = json.loads(b64d(claims))
    assert body["aud"] == "https://web.push.apple.com" and body["exp"] == 1_000_000 + 12 * 3600
    raw = b64d(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64d(key.public))
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")), f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_push_payload_carries_no_content():
    long = "今天摄入 1,480–1,690 千卡：读表 620（1 餐），估算 860–1,070（2 餐）。体重 73.4 公斤。"
    payload = json.loads(push_payload({"id": "d1", "kind": "health", "content": long}))
    assert payload == {"title": "Noyi", "body": "今晚的饮食汇总好了，点开查看", "tag": "health", "delivery": "d1"}
    assert "73.4" not in json.dumps(payload, ensure_ascii=False)
    other = json.loads(push_payload({"id": "d2", "kind": "result", "content": "任意内容"}))
    assert other["body"] == "有一条新回复，点开查看"


class FakePush:
    def __init__(self, status=201):
        self.status, self.sent = status, []

    async def __call__(self, endpoint, headers, body):
        self.sent.append((endpoint, headers, body))
        return self.status


def subscription(store, device="dev1", endpoint="https://web.push.apple.com/abc"):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    store.db.execute("INSERT INTO paired_devices(id,name,secret_sha256,created) VALUES(?,?,?,?)", (device, "iPhone", device + "h", time.time()))
    store.save_push_subscription(device, {"endpoint": endpoint, "keys": {"p256dh": b64e(pub), "auth": b64e(b"0123456789abcdef")}})


async def test_delivery_is_accepted_only_when_the_user_sees_it(store, tmp_path):
    from personal_agent.remote import RemoteGate, RemoteConfig
    RemoteGate(store, RemoteConfig({HOST}))
    subscription(store)
    sender = FakePush()
    channel = PushChannel(store, tmp_path / "data", FileChannel(tmp_path / "data"), send=sender)
    tid = store.submit(TaskSpec("x"), nonce="p")["task_id"]
    did = store._notice(tid, "local", "今晚汇总：摄入 1,480 千卡", kind="health")
    receipt = await channel.send(store.one("SELECT * FROM deliveries WHERE id=?", (did,)))
    # The push left; that alone is not the user receiving it.
    assert receipt == {"id": did, "accepted": False, "awaiting_seen": True, "channel": "web_push", "pushed": 1}
    assert len(sender.sent) == 1 and "1,480" not in sender.sent[0][2].decode("latin-1")
    assert store.one("SELECT state FROM deliveries WHERE id=?", (did,))["state"] != "accepted"
    seen = store.mark_seen([did], device_id="dev1")
    assert seen == [did]
    assert store.one("SELECT state FROM deliveries WHERE id=?", (did,))["state"] == "accepted"


async def test_without_any_phone_the_local_inbox_still_delivers(store, tmp_path):
    channel = PushChannel(store, tmp_path / "data", FileChannel(tmp_path / "data"), send=FakePush())
    did = store._notice(None, "local", "hello", kind="result")
    receipt = await channel.send(store.one("SELECT * FROM deliveries WHERE id=?", (did,)))
    assert receipt["accepted"] is True and receipt["channel"] == "local_file"


async def test_expired_subscription_is_removed_and_reported(store, tmp_path):
    from personal_agent.remote import RemoteGate, RemoteConfig
    RemoteGate(store, RemoteConfig({HOST}))
    subscription(store)
    channel = PushChannel(store, tmp_path / "data", FileChannel(tmp_path / "data"), send=FakePush(status=410))
    did = store._notice(None, "local", "x", kind="result")
    with pytest.raises(PushGone):
        await channel.send(store.one("SELECT * FROM deliveries WHERE id=?", (did,)))
    assert not store.all("SELECT * FROM push_subscriptions")


async def test_each_delivery_is_pushed_at_most_once(store, tmp_path):
    from personal_agent.remote import RemoteGate, RemoteConfig
    RemoteGate(store, RemoteConfig({HOST}))
    subscription(store)
    sender = FakePush()
    channel = PushChannel(store, tmp_path / "data", FileChannel(tmp_path / "data"), send=sender)
    did = store._notice(None, "local", "x", kind="result")
    row = store.one("SELECT * FROM deliveries WHERE id=?", (did,))
    await channel.send(row)
    await channel.send(row)
    assert len(sender.sent) == 1


@pytest.fixture
async def phone(store, tmp_path, monkeypatch):
    monkeypatch.setenv("NOYI_REMOTE_HOSTS", HOST)
    app = create_app(store, tmp_path / "data", ScriptedModel([]), run_service=False)
    async with TestClient(TestServer(app)) as client:
        token = (await (await client.get("/api/bootstrap")).json())["token"]
        code = (await (await client.post("/api/pairing", json={"name": "iPhone"}, headers={"X-PA-Token": token})).json())["code"]
        headers = {"Host": HOST, "X-Forwarded-For": "100.64.0.2", "Origin": "https://" + HOST}
        cookie = (await client.post("/api/pair", json={"code": code}, headers=headers)).cookies["noyi_device"].value
        headers["Cookie"] = "noyi_device=" + cookie
        boot = await (await client.get("/api/bootstrap", headers=headers)).json()
        headers["X-PA-Token"] = boot["token"]
        yield client, headers, boot


async def test_phone_subscribes_and_the_page_marks_what_was_seen(phone, store):
    client, headers, boot = phone
    assert boot["push_public_key"]
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    sub = {"endpoint": "https://web.push.apple.com/xyz", "keys": {"p256dh": b64e(point), "auth": b64e(b"b" * 16)}}
    fake = {**sub, "keys": {"p256dh": b64e(b"" + b"a" * 64), "auth": b64e(b"b" * 16)}}
    assert (await client.post("/api/push/subscribe", json=fake, headers=headers)).status == 400
    assert (await client.post("/api/push/subscribe", json=sub, headers=headers)).status == 200
    assert store.one("SELECT endpoint FROM push_subscriptions")["endpoint"] == sub["endpoint"]
    bad = {**sub, "endpoint": "https://evil.example.com/collect"}
    assert (await client.post("/api/push/subscribe", json=bad, headers=headers)).status == 400
    did = store._notice(None, "local", "补出来的提醒", kind="health")
    seen = await (await client.post("/api/seen", json={"ids": [did]}, headers=headers)).json()
    assert seen["accepted"] == [did]
    assert store.one("SELECT state FROM deliveries WHERE id=?", (did,))["state"] == "accepted"


async def test_service_keeps_a_pushed_delivery_open_until_seen(store, tmp_path):
    from personal_agent.remote import RemoteGate, RemoteConfig
    from personal_agent.service import Service
    RemoteGate(store, RemoteConfig({HOST}))
    subscription(store)
    sender = FakePush()
    service = Service(store, tmp_path / "data", ScriptedModel([]), channel=PushChannel(store, tmp_path / "data", FileChannel(tmp_path / "data"), send=sender))
    did = store._notice(None, "local", "x", kind="health")
    await service.deliver()
    await service.deliver()
    assert store.one("SELECT state FROM deliveries WHERE id=?", (did,))["state"] == "pushed" and len(sender.sent) == 1
    store.mark_seen([did])
    assert store.one("SELECT state FROM deliveries WHERE id=?", (did,))["state"] == "accepted"


async def test_no_push_while_the_user_is_looking_at_the_app(store, tmp_path):
    from personal_agent.remote import RemoteGate, RemoteConfig
    RemoteGate(store, RemoteConfig({HOST}))
    subscription(store)
    sender = FakePush()
    channel = PushChannel(store, tmp_path / "data", FileChannel(tmp_path / "data"), send=sender)
    store.mark_seen([])                       # the open page reports in
    did = store._notice(None, "local", "x", kind="result")
    receipt = await channel.send(store.one("SELECT * FROM deliveries WHERE id=?", (did,)))
    assert receipt["deferred"] is True and not sender.sent
