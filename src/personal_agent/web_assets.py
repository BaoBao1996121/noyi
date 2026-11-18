"""Static files for the WebUI: one build version, long-lived caching, gzip.

Every asset URL the server writes (in pages, in module imports, in the manifest
and in the service worker's list) carries ?v=<build>, a hash of all static
files together. A request for the current build may be cached for a year; any
other request, and every page, is revalidated by ETag (a 304 costs one round
trip and no body). A release changes the build, so pages ask for new URLs and
no phone keeps running old scripts.

Pages also get a modulepreload link for every module their scripts import,
directly or not, so the browser fetches the whole import tree at once instead
of one level per round trip. Modules loaded with import() are left out: they
are not needed for the first screen.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import re
import time

from aiohttp import web

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".webmanifest": "application/manifest+json", ".png": "image/png"}
TEXT = {".html", ".js", ".css", ".webmanifest"}
PAGES = {"index.html", "pair.html"}
IMMUTABLE = "private, max-age=31536000, immutable"
STATIC_IMPORT = re.compile(r"""\bfrom\s*(['"])\./([\w.-]+\.js)\1""")
ANY_IMPORT = re.compile(r"""(\bfrom\s*|\bimport\s*\(\s*)(['"])\./([\w.-]+\.js)\2""")
ASSET_URL = re.compile(r"""(["'(])/assets/([\w.-]+)(?=["')])""")
ENTRY = re.compile(r"""<script type="module" src="/assets/([\w.-]+\.js)"></script>""")


class Asset:
    __slots__ = ("body", "gz", "etag", "type")

    def __init__(self, body, kind, text):
        self.body, self.type = body, kind
        self.etag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        self.gz = gzip.compress(body, 9, mtime=0) if text and len(body) > 512 else None


class Assets:
    def __init__(self, root, names):
        self.root, self.names = Path(root), sorted(names)
        self.version, self.files, self._stamp, self._checked = "", {}, None, 0.0

    def stamp(self):
        return tuple((n, s.st_mtime_ns, s.st_size) for n in self.names for s in [(self.root / n).stat()])

    def current(self):
        # Files changed on disk (development) are picked up within a second.
        now = time.monotonic()
        if not self.files or now - self._checked > 1:
            self._checked = now
            stamp = self.stamp()
            if stamp != self._stamp:
                self.build()
                self._stamp = stamp
        return self.files

    def url(self, name):
        return f"/assets/{name}?v={self.version}"

    def build(self):
        raw = {n: (self.root / n).read_bytes() for n in self.names}
        h = hashlib.sha256()
        for n in self.names:
            h.update(n.encode() + b"\0" + hashlib.sha256(raw[n]).digest())
        self.version = h.hexdigest()[:12]
        known = set(self.names)

        def versioned_imports(text):
            return ANY_IMPORT.sub(lambda m: f"{m[1]}{m[2]}./{m[3]}?v={self.version}{m[2]}" if m[3] in known else m[0], text)

        def asset_urls(text):
            return ASSET_URL.sub(lambda m: m[1] + self.url(m[2]) if m[2] in known else m[0], text)

        def imports_of(name, seen):
            for m in STATIC_IMPORT.finditer(raw[name].decode("utf-8")):
                if m[2] in known and m[2] not in seen:
                    seen.add(m[2])
                    imports_of(m[2], seen)
            return seen

        files = {}
        for name in self.names:
            suffix = Path(name).suffix
            body = raw[name]
            if suffix == ".js" and name != "sw.js":
                body = versioned_imports(body.decode("utf-8")).encode()
            elif suffix in {".html", ".webmanifest"}:
                text = body.decode("utf-8")
                if suffix == ".html":
                    entries = ENTRY.findall(text)
                    preload = sorted(set().union(*(imports_of(e, set()) for e in entries)) - set(entries)) if entries else []
                    links = "".join(f'  <link rel="modulepreload" href="/assets/{n}">\n' for n in preload)
                    text = text.replace("</head>", links + "</head>", 1)
                body = asset_urls(text).encode()
            files[name] = Asset(body, TYPES[suffix], suffix in TEXT)
        if "sw.js" in raw:
            shell = [self.url(n) for n in self.names if Path(n).suffix in {".js", ".css"} and n not in {"sw.js"} and not n.startswith("pair.")]
            shell.append(self.url("icon-192.png"))
            text = raw["sw.js"].decode("utf-8").replace("/*BUILD*/'dev'", json.dumps(self.version)).replace("/*SHELL*/[]", json.dumps(shell))
            files["sw.js"] = Asset(text.encode(), TYPES[".js"], True)
        self.files = files

    def respond(self, request, name, *, headers=None):
        asset = self.current()[name]
        cache = IMMUTABLE if name not in PAGES | {"sw.js", "manifest.webmanifest"} and request.query.get("v") == self.version else "no-cache"
        out = {"Cache-Control": cache, "ETag": asset.etag, "Vary": "Accept-Encoding", **(headers or {})}
        sent = {t.strip().removeprefix("W/") for t in request.headers.get("If-None-Match", "").split(",")}
        if asset.etag in sent:
            return web.Response(status=304, headers=out)
        body = asset.body
        if asset.gz is not None and "gzip" in request.headers.get("Accept-Encoding", ""):
            body, out["Content-Encoding"] = asset.gz, "gzip"
        out["Content-Type"] = asset.type
        return web.Response(body=body, headers=out)
