from pathlib import Path
from urllib.parse import urlparse

import httpx


class ExactSourceReader:
    """Only read the exact descriptor granted by the task; never follow redirects."""
    async def read(self, source):
        from .tools import MAX_SOURCE_BYTES, SourceUnavailable
        if source["kind"] == "file":
            path = Path(source["location"])
            if path.is_symlink() or str(path.resolve()) != source["location"]:
                raise SourceUnavailable("source path changed")
            with path.open("rb") as handle:
                return handle.read(MAX_SOURCE_BYTES + 1)
        url = source["location"]
        parsed = urlparse(url)
        if source["kind"] != "http" or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("invalid authorized HTTP source")
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                raw = b""
                async for chunk in response.aiter_bytes():
                    raw += chunk
                    if len(raw) > MAX_SOURCE_BYTES:
                        break
                return raw
