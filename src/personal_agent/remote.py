"""Phone access through Tailscale Serve: allowed host names plus paired devices.

Two layers (plan 2026-11-13, user confirmed Tailscale + pairing):
1. Network: the service still listens on 127.0.0.1 only; `tailscale serve`
   forwards https://<machine>.<tailnet>.ts.net to it, so only devices logged
   into the user's tailnet can reach it at all.
2. Application: a request counts as remote when its Host is not loopback or
   it carries any proxy header (Tailscale's docs do not say whether Serve keeps
   the original Host, so either signal is enough). A remote request needs a
   configured host name, a same-origin https Origin, and a device cookie
   issued by pairing. Pairing codes are created only on this machine.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from aiohttp import web

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "Tailscale-User-Login", "Tailscale-User-Name")
HOST = re.compile(r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
COOKIE = "noyi_device"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
CODE_LENGTH = 8
CODE_TTL = 300
FAILURE_WINDOW, FAILURE_LIMIT = 300, 5
COOKIE_MAX_AGE = 400 * 86400                         # browsers cap cookie lifetime at 400 days
# Reachable by an unpaired remote device: just enough to pair and install.
PUBLIC = {"/", "/manifest.webmanifest", "/assets/pair.js", "/assets/pair.css", "/assets/icon-192.png", "/assets/icon-512.png", "/assets/apple-touch-icon.png"}
# Never from a phone: secrets, process control, pairing itself, device admin.
LOCAL_ONLY = ("/api/pairing", "/api/devices", "/api/settings", "/api/shutdown", "/api/backup")

REMOTE = web.RequestKey("remote", bool)
DEVICE = web.RequestKey("device", object)

SQL = [
    """CREATE TABLE IF NOT EXISTS paired_devices(id TEXT PRIMARY KEY, name TEXT NOT NULL, secret_sha256 TEXT NOT NULL UNIQUE,
        created REAL NOT NULL, last_seen REAL, revoked REAL)""",
    """CREATE TABLE IF NOT EXISTS pairing_codes(code_sha256 TEXT PRIMARY KEY, name TEXT NOT NULL, expires REAL NOT NULL,
        used REAL, device_id TEXT, created REAL NOT NULL)""",
]


def hash_secret(value):
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_code(value):
    return re.sub(r"[\s-]", "", str(value or "")).upper()


@dataclass
class RemoteConfig:
    hosts: set = field(default_factory=set)
    # Optional second check: Tailscale Serve sets this header (and strips a
    # client-supplied one). Only enforced when configured.
    login: str | None = None

    @classmethod
    def load(cls):
        hosts = {h.strip().lower() for h in os.environ.get("NOYI_REMOTE_HOSTS", "").split(",") if h.strip()}
        bad = [h for h in hosts if not HOST.fullmatch(h)]
        if bad:
            raise ValueError("NOYI_REMOTE_HOSTS 只能是逗号分隔的域名，例如 noyi-home.tailxxxx.ts.net")
        login = os.environ.get("NOYI_REMOTE_LOGIN", "").strip() or None
        return cls(hosts, login)

    @property
    def origins(self):
        return {"https://" + h for h in self.hosts}


def is_remote(request):
    return request.url.host not in LOOPBACK or any(h in request.headers for h in PROXY_HEADERS)


class RemoteGate:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.failures = deque()
        with store.transaction():
            for statement in SQL:
                store.db.execute(statement)

    # ---- pairing (code created locally, redeemed by the phone) ---------------
    def new_code(self, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
            raise ValueError("给这台设备起个名字（40 字以内）")
        if not self.config.hosts:
            raise ValueError("还没有配置外网地址：先在家里电脑上运行 tailscale serve，并设置 NOYI_REMOTE_HOSTS")
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        now = time.time()
        with self.store.transaction():
            # One live code at a time: a new one voids any earlier unused code.
            self.store.db.execute("UPDATE pairing_codes SET expires=? WHERE used IS NULL AND expires>?", (now, now))
            self.store.db.execute("INSERT INTO pairing_codes(code_sha256,name,expires,created) VALUES(?,?,?,?)", (hash_secret(code), name.strip(), now + CODE_TTL, now))
        host = sorted(self.config.hosts)[0]
        shown = code[:4] + "-" + code[4:]
        return {"code": shown, "expires": now + CODE_TTL, "url": f"https://{host}/?pair={code}"}

    def locked(self, now=None):
        now = time.time() if now is None else now
        while self.failures and self.failures[0] < now - FAILURE_WINDOW:
            self.failures.popleft()
        return len(self.failures) >= FAILURE_LIMIT

    def redeem(self, code):
        now = time.time()
        if self.locked(now):
            return None, "locked"
        code = normalize_code(code)
        with self.store.transaction():
            row = self.store.one("SELECT * FROM pairing_codes WHERE code_sha256=?", (hash_secret(code),)) if len(code) == CODE_LENGTH else None
            if not row or row["used"] or row["expires"] < now:
                self.failures.append(now)
                if self.locked(now):
                    # Too many wrong tries: void the live code too.
                    self.store.db.execute("UPDATE pairing_codes SET expires=? WHERE used IS NULL AND expires>?", (now, now))
                return None, "locked" if self.locked(now) else "invalid"
            secret = secrets.token_urlsafe(32)
            device = uuid.uuid4().hex
            self.store.db.execute("INSERT INTO paired_devices(id,name,secret_sha256,created,last_seen) VALUES(?,?,?,?,?)", (device, row["name"], hash_secret(secret), now, now))
            self.store.db.execute("UPDATE pairing_codes SET used=?,device_id=? WHERE code_sha256=?", (now, device, row["code_sha256"]))
        return secret, device

    # ---- devices -------------------------------------------------------------
    def device(self, request):
        secret = request.cookies.get(COOKIE)
        if not secret or len(secret) > 200:
            return None
        row = self.store.one("SELECT * FROM paired_devices WHERE secret_sha256=? AND revoked IS NULL", (hash_secret(secret),))
        if row and (row["last_seen"] or 0) < time.time() - 600:
            self.store.db.execute("UPDATE paired_devices SET last_seen=? WHERE id=?", (time.time(), row["id"]))
        return row

    def devices(self):
        return self.store.all("SELECT id,name,created,last_seen,revoked FROM paired_devices ORDER BY created DESC")

    def revoke(self, device_id):
        with self.store.transaction():
            if not self.store.one("SELECT id FROM paired_devices WHERE id=? AND revoked IS NULL", (device_id,)):
                raise ValueError("没有这台设备，或已经移除")
            self.store.db.execute("UPDATE paired_devices SET revoked=? WHERE id=?", (time.time(), device_id))
            self.store.db.execute("DELETE FROM push_subscriptions WHERE device_id=?", (device_id,)) if self.store.one(
                "SELECT name FROM sqlite_master WHERE name='push_subscriptions'") else None
        return {"revoked": device_id}

    # ---- request admission -----------------------------------------------------
    def admit(self, request):
        """None to continue normally; otherwise (status, text) to refuse. Sets
        request['remote'] / request['device'] for handlers."""
        remote = is_remote(request)
        request[REMOTE] = remote
        if not remote:
            return None
        host = request.url.host
        if not self.config.hosts or (host not in self.config.hosts and host not in LOOPBACK):
            return 403, "这个地址没有开放"
        if self.config.login and request.headers.get("Tailscale-User-Login") != self.config.login:
            return 403, "不是这台电脑主人的 Tailscale 账号"
        origin = request.headers.get("Origin")
        if origin and origin not in self.config.origins:
            return 403, "Cross-origin access denied"
        path = request.path
        if path.startswith(LOCAL_ONLY):
            return 403, "这项操作只能在家里电脑上做"
        device = self.device(request)
        request[DEVICE] = device
        if device:
            return None
        if path in PUBLIC and request.method in {"GET", "HEAD"}:
            return None
        if path == "/api/pair" and request.method == "POST":
            if request.headers.get("Sec-Fetch-Site") == "cross-site" or not origin:
                return 403, "Cross-origin access denied"
            return None
        return 401, "这台设备还没有配对"
