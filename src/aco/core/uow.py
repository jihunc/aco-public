"""Single transaction boundary; concrete tenant-bound repositories arrive with T03."""

from types import TracebackType
from typing import Protocol, Self

from aco.core.context import TenantContext


class UnitOfWork(Protocol):
    context: TenantContext

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
