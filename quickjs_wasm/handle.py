"""Handle. See spec/implementation.md §7.2."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Literal

ValueKind = Literal[
    "null",
    "undefined",
    "boolean",
    "number",
    "bigint",
    "string",
    "symbol",
    "object",
    "function",
    "array",
]


class Handle:
    def __enter__(self) -> Handle:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError

    def __del__(self) -> None:
        pass

    def dispose(self) -> None:
        raise NotImplementedError

    @property
    def disposed(self) -> bool:
        raise NotImplementedError

    @property
    def type_of(self) -> ValueKind:
        raise NotImplementedError

    @property
    def is_promise(self) -> bool:
        raise NotImplementedError

    def get(self, key: str | int) -> Handle:
        raise NotImplementedError

    def set(self, key: str, value: Handle | Any) -> None:
        raise NotImplementedError

    def call(self, *args: Handle | Any, this: Handle | None = None) -> Handle:
        raise NotImplementedError

    def call_method(self, name: str, *args: Handle | Any) -> Handle:
        raise NotImplementedError

    def new(self, *args: Handle | Any) -> Handle:
        raise NotImplementedError

    def to_python(self, *, allow_opaque: bool = False) -> Any:
        raise NotImplementedError

    def await_promise(self, *, deadline: float | None = None) -> Handle:
        raise NotImplementedError("await_promise lands in v0.3; see spec §7.2.")
