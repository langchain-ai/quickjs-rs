"""Type stubs for the PyO3-compiled ``_engine`` extension.

Kept in sync by hand with ``src/lib.rs``. Each phase 1 step that adds
Rust-exported methods updates this file in the same commit.
"""

from collections.abc import Callable
from typing import Any

class QuickJSError(Exception): ...

class QjsRuntime:
    def __new__(
        cls,
        *,
        memory_limit: int | None = ...,
        stack_limit: int | None = ...,
    ) -> QjsRuntime: ...
    def set_interrupt_handler(self, handler: Callable[[], Any]) -> None: ...
    def clear_interrupt_handler(self) -> None: ...
    def close(self) -> None: ...
    def is_closed(self) -> bool: ...
