"""Web Push to the paired iPhone (user decision 2026-11-13: system notifications
from the phone web app, not Feishu).

Delivered means seen (P-1, tightened for push): a push only says "sent". iOS
may delay or drop pushes for a Home Screen app that has not been opened in a
while, so a delivery becomes `accepted` (and the promise waiting on it is
fulfilled) only when the open page shows it and reports it seen. Anything
missed is listed when the page next opens; nothing is lost, nothing is
counted twice.

Privacy: the push carries a fixed line per kind and a delivery id, never the
message text, so no meal, weight or reply passes through Apple's push service.

Encryption: RFC 8291 (aes128gcm, RFC 8188) with VAPID (RFC 8292), checked
against RFC 8291 Appendix A in the tests.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Push services we will post to. A subscription naming anything else is refused,
# so a page bug or a hostile script cannot turn us into a request relay.
PUSH_HOSTS = (".push.apple.com", "fcm.googleapis.com", ".notify.windows.com", "updates.push.services.mozilla.com")
RECORD_SIZE = 4096
TTL = 24 * 3600
# While the page reported in this recently, the user is looking at it: no push.
FOREGROUND = 45
LINES = {"health": "今晚的饮食汇总好了，点开查看", "health_alert": "有一条饮食提醒，点开查看", "commitment": "有一件答应你的事需要你看一下",
         "question": "有个问题需要你回答", "result": "有一条新回复，点开查看"}
PUSH_SQL = """CREATE TABLE IF NOT EXISTS push_subscriptions(device_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL,
    p256dh TEXT NOT NULL, auth TEXT NOT NULL, created REAL NOT NULL, last_ok REAL, failures INTEGER NOT NULL DEFAULT 0)"""
PUSHED_SQL = """CREATE TABLE IF NOT EXISTS push_sent(delivery_id TEXT NOT NULL, device_id TEXT NOT NULL, status INTEGER,
    created REAL NOT NULL, PRIMARY KEY(delivery_id, device_id))"""
SEEN_SQL = "CREATE TABLE IF NOT EXISTS app_presence(id INTEGER PRIMARY KEY CHECK(id=1), device_id TEXT, at REAL NOT NULL)"


class PushGone(Exception):
    """The push service says the subscription no longer exists (404/410)."""


def b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf(salt, ikm, info, length):
    return HKDF(hashes.SHA256(), length, salt, info).derive(ikm)


def encrypt(plaintext, *, ua_public, auth_secret, as_private=None, salt=None):
    """RFC 8291 message encryption; as_private/salt are only fixed in tests."""
    if as_private is None:
        key = ec.generate_private_key(ec.SECP256R1())
    else:
        key = ec.derive_private_key(int.from_bytes(as_private, "big"), ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(plaintext) + 17 > RECORD_SIZE:
        raise ValueError("push payload too large")
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + ciphertext


@dataclass
class Vapid:
    key: ec.EllipticCurvePrivateKey
    public: str


def load_vapid(data):
    """This install's VAPID key, created once under the data directory."""
    path = Path(data) / "push-vapid.pem"
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), None)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return Vapid(key, b64e(public))


def vapid_headers(vapid, endpoint, *, subject, now=None):
    parts = urlsplit(endpoint)
    head = b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64e(json.dumps({"aud": f"{parts.scheme}://{parts.netloc}", "exp": int((now or time.time()) + 12 * 3600), "sub": subject},
                             separators=(",", ":")).encode())
    r, s = decode_dss_signature(vapid.key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    return {"Authorization": f"vapid t={head}.{claims}.{b64e(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}, k={vapid.public}"}


def push_payload(delivery):
    kind = delivery.get("kind") or "result"
    return json.dumps({"title": "Noyi", "body": LINES.get(kind, LINES["result"]), "tag": kind if kind in LINES else "result",
                       "delivery": delivery["id"]}, ensure_ascii=False)


def valid_endpoint(endpoint):
    parts = urlsplit(endpoint or "")
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and not parts.username and (parts.port in (None, 443)) and any(
        host == h.lstrip(".") or host.endswith(h) if h.startswith(".") else host == h for h in PUSH_HOSTS)


class PushStore:
    """Mixed into Store: subscriptions, what was pushed, and what the user saw."""

    def __init__(self, path):
        super().__init__(path)
        from .remote import SQL as PAIRING_SQL
        with self.transaction():
            for statement in PAIRING_SQL:
                self.db.execute(statement)
            self.db.execute(PUSH_SQL)
            self.db.execute(PUSHED_SQL)
            self.db.execute(SEEN_SQL)

    def save_push_subscription(self, device_id, subscription):
        if not isinstance(subscription, dict) or not valid_endpoint(subscription.get("endpoint")):
            raise ValueError("推送地址不是认可的推送服务")
        keys = subscription.get("keys") or {}
        try:
            p256dh, auth = b64d(keys["p256dh"]), b64d(keys["auth"])
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
        except (KeyError, TypeError, ValueError):
            raise ValueError("推送订阅的密钥格式不对") from None
        if len(auth) != 16:
            raise ValueError("推送订阅的密钥格式不对")
        self.db.execute("INSERT INTO push_subscriptions(device_id,endpoint,p256dh,auth,created) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(device_id) DO UPDATE SET endpoint=excluded.endpoint,p256dh=excluded.p256dh,auth=excluded.auth,failures=0",
                        (device_id, subscription["endpoint"], keys["p256dh"], keys["auth"], time.time()))
        return {"subscribed": True}

    def push_targets(self):
        return self.all("SELECT s.* FROM push_subscriptions s JOIN paired_devices d ON d.id=s.device_id WHERE d.revoked IS NULL")

    def app_in_foreground(self, now=None):
        row = self.one("SELECT at FROM app_presence WHERE id=1")
        return bool(row and (now or time.time()) - row["at"] < FOREGROUND)

    def mark_seen(self, delivery_ids, *, device_id=None, account="local-user"):
        """The page showed these to the user. Settles them (and their promises)."""
        if not isinstance(delivery_ids, list) or len(delivery_ids) > 300 or any(not isinstance(i, str) for i in delivery_ids):
            raise ValueError("无效的已读列表")
        accepted = []
        with self.transaction():
            self.db.execute("INSERT INTO app_presence(id,device_id,at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET device_id=excluded.device_id,at=excluded.at",
                            (device_id, time.time()))
            for did in dict.fromkeys(delivery_ids):
                row = self.one("SELECT d.id,d.state FROM deliveries d JOIN rooms r ON r.id=d.room_id WHERE d.id=? AND r.owner=?", (did, account))
                if row and row["state"] in {"pending", "failed", "pushed"}:
                    self.accept_delivery(did, {"channel": "web_seen", "id": did, "accepted": True, "human_read": "shown", "device": device_id})
                    accepted.append(did)
        return accepted


class PushChannel:
    """DeliveryChannel: local inbox when no phone is paired; otherwise push, then wait to be seen."""

    def __init__(self, store, data, fallback, *, send=None, subject="mailto:noyi@localhost"):
        self.store, self.data, self.fallback = store, Path(data), fallback
        self.subject = subject
        self._send = send or self._post
        self._vapid = None

    @property
    def vapid(self):
        if self._vapid is None:
            self._vapid = load_vapid(self.data)
        return self._vapid

    async def _post(self, endpoint, headers, body):
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.post(endpoint, data=body, headers=headers) as response:
                return response.status

    async def send(self, delivery):
        targets = self.store.push_targets()
        if not targets:
            return await self.fallback.send(delivery)
        if self.store.app_in_foreground():
            # The open page will show it and report it seen within seconds.
            return {"id": delivery["id"], "accepted": False, "awaiting_seen": True, "deferred": True, "channel": "web_push", "pushed": 0}
        payload = push_payload(delivery).encode()
        pushed, gone = 0, 0
        for target in targets:
            if self.store.one("SELECT 1 FROM push_sent WHERE delivery_id=? AND device_id=?", (delivery["id"], target["device_id"])):
                pushed += 1
                continue
            body = encrypt(payload, ua_public=b64d(target["p256dh"]), auth_secret=b64d(target["auth"]))
            headers = {**vapid_headers(self.vapid, target["endpoint"], subject=self.subject), "Content-Encoding": "aes128gcm",
                       "Content-Type": "application/octet-stream", "TTL": str(TTL), "Urgency": "normal"}
            status = await self._send(target["endpoint"], headers, body)
            if status in (404, 410):
                self.store.db.execute("DELETE FROM push_subscriptions WHERE device_id=?", (target["device_id"],))
                gone += 1
                continue
            if 200 <= status < 300:
                self.store.db.execute("INSERT OR IGNORE INTO push_sent VALUES(?,?,?,?)", (delivery["id"], target["device_id"], status, time.time()))
                self.store.db.execute("UPDATE push_subscriptions SET last_ok=?,failures=0 WHERE device_id=?", (time.time(), target["device_id"]))
                pushed += 1
            else:
                self.store.db.execute("UPDATE push_subscriptions SET failures=failures+1 WHERE device_id=?", (target["device_id"],))
        if gone and not pushed:
            raise PushGone()
        if not pushed:
            raise ConnectionError("push service did not accept the notification")
        return {"id": delivery["id"], "accepted": False, "awaiting_seen": True, "channel": "web_push", "pushed": pushed}
