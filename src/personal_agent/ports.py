"""Replaceable runtime implementations. Task authority remains in Store.

Adapters are trusted installed application code, not untrusted model output.
An engine receives only messages/schemas and a scoped callback. It cannot finish
tasks, expand source grants, or receive model credentials through this interface.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Protocol


Callback = Callable[[str, dict], Awaitable[dict]]


class AgentEngine(Protocol):
    identity: str

    async def run(self, messages: list[dict], schemas: list[dict], model_name: str, callback: Callback) -> None: ...


class PiEngine:
    identity = "pi-agent-core@1.5.0"

    async def run(self, messages, schemas, model_name, callback):
        from .pi_client import run_pi
        await run_pi(messages, schemas, model_name, callback)


class SourceReader(Protocol):
    async def read(self, source: dict) -> bytes: ...


class DeliveryChannel(Protocol):
    async def send(self, delivery: dict) -> dict: ...


class MemoryProvider(Protocol):
    """Replaceable value loader; Store controls membership, IDs and versions."""
    def load(self, collection_id: str, records: list[dict]) -> list[dict]: ...


class ObservationSource(Protocol):
    """Only observes a registered descriptor; cannot register policies or work."""
    identity: str
    async def observe(self, source: dict) -> bytes: ...
