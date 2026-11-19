"""Per-wire intent and redacted completion observations; values grant no authority."""

from typing import Protocol
from uuid import UUID

from aco.integrations.ports.voice import CallContext, VoiceSetupContext


class VoiceRequestObserver(Protocol):
    async def started(self, ctx: CallContext | VoiceSetupContext, operation: str) -> UUID: ...

    async def finished(
        self, key: UUID, operation: str, *, native: str | None, failure: str | None
    ) -> None: ...
