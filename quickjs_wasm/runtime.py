"""Runtime. See spec/implementation.md §7.2."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quickjs_wasm.context import Context


class Runtime:
    def __init__(
        self,
        *,
        memory_limit: int | None = 64 * 1024 * 1024,
        stack_limit: int | None = 1 * 1024 * 1024,
    ) -> None:
        raise NotImplementedError("Runtime is not yet implemented; see spec §7.2.")

    def __enter__(self) -> Runtime:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def new_context(self, *, timeout: float = 5.0) -> Context:
        raise NotImplementedError

    def run_pending_jobs(self) -> int:
        raise NotImplementedError

    @property
    def has_pending_jobs(self) -> bool:
        raise NotImplementedError
