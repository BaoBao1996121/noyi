from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlparse


def encode(value: object) -> str:
    # Strict JSON: every consumer (ledger, engine bridge, browser) parses
    # standard JSON, so a non-finite number must fail where it is produced.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ModelReply:
    message: dict
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "unknown"
    request_id: str | None = None
    finish_reason: str | None = None


class Model(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply: ...


@dataclass
class TaskSpec:
    objective: str
    sources: list[dict] = field(default_factory=list)
    required_text: list[str] = field(default_factory=list)
    max_calls: int = 12
    max_tools: int = 40
    max_seconds: float = 600
    max_workers: int = 4
    max_continuations: int = 12
    enable_nudge: bool = False
    max_nudges: int = 2
    max_stagnant_tools: int = 20
    review_workers: bool = False
    max_handoffs: int = 2
    result_kind: str = 'artifact'
    # The room dialogue that led to this request, oldest first. It explains
    # what a short request refers to; it is context, never new authority.
    conversation: list[dict] = field(default_factory=list)

    def validate(self) -> None:
        if self.result_kind not in {'artifact', 'action_receipt'}:
            raise ValueError('invalid result contract')
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("objective must not be empty")
        if (type(self.max_calls) is not int or type(self.max_tools) is not int
                or not 1 <= self.max_calls <= 100 or not 1 <= self.max_tools <= 200):
            raise ValueError("invalid call/tool budget")
        if (type(self.max_seconds) not in (int, float) or not 1 <= self.max_seconds <= 3600
                or not math.isfinite(self.max_seconds)):
            raise ValueError("invalid time budget")
        if (type(self.max_workers) is not int or not 0 <= self.max_workers <= 8
                or type(self.max_continuations) is not int or not 0 <= self.max_continuations <= 100):
            raise ValueError("invalid worker/continuation budget")
        if (type(self.enable_nudge) is not bool or type(self.review_workers) is not bool
                or type(self.max_nudges) is not int or not 0 <= self.max_nudges <= 5
                or type(self.max_stagnant_tools) is not int or not 1 <= self.max_stagnant_tools <= 40
                or type(self.max_handoffs) is not int or not 0 <= self.max_handoffs <= 8):
            raise ValueError("invalid progress/review policy")
        if not isinstance(self.sources, list) or any(not isinstance(s, dict) for s in self.sources):
            raise ValueError("sources must be a list of objects")
        ids = [s.get("id") for s in self.sources]
        if (any(not isinstance(i, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", i) for i in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("source IDs must be unique simple ASCII identifiers")
        for s in self.sources:
            if (not isinstance(s.get("kind"), str) or s["kind"] not in {"file", "http"} or not isinstance(s.get("location"), str)
                    or not s["location"].strip() or "\x00" in s["location"]):
                raise ValueError("sources need kind=file|http and location")
            if not isinstance(s.get("required", True), bool):
                raise ValueError("source required must be boolean")
            if s["kind"] == "file":
                path = Path(s["location"])
                if not path.is_absolute() or s["location"].startswith(("\\\\.\\", "\\\\?\\")):
                    raise ValueError("file sources require an absolute filesystem path")
                if path.exists() and not path.is_file():
                    raise ValueError("file source must be a regular file")
            else:
                parsed = urlparse(s["location"])
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username or parsed.password or parsed.fragment):
                    raise ValueError("HTTP source requires a credential-free absolute URL without fragment")
                try:
                    parsed.port
                except ValueError:
                    raise ValueError("invalid source port") from None
        if (not isinstance(self.required_text, list)
                or any(not isinstance(t, str) or not t.strip() for t in self.required_text)):
            raise ValueError("required_text entries must be nonempty strings")
        if (not isinstance(self.conversation, list) or len(self.conversation) > 16
                or any(not isinstance(t, dict) or t.get("speaker") not in {"user", "assistant"}
                       or not isinstance(t.get("content"), str) or len(t["content"]) > 2200
                       or set(t) - {"speaker", "content"} for t in self.conversation)):
            raise ValueError("conversation must be a short list of user/assistant turns")
