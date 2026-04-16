"""Context. See spec/implementation.md §7.2."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Any, Callable, overload

if TYPE_CHECKING:
    from quickjs_wasm.globals import Globals
    from quickjs_wasm.handle import Handle
    from quickjs_wasm.runtime import Runtime


class Context:
    def __init__(self, runtime: Runtime, *, timeout: float = 5.0) -> None:
        raise NotImplementedError("Context is not yet implemented; see spec §7.2.")

    def __enter__(self) -> Context:
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

    def eval(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
    ) -> Any:
        raise NotImplementedError

    def eval_handle(
        self,
        code: str,
        *,
        module: bool = False,
        strict: bool = False,
        filename: str = "<eval>",
    ) -> Handle:
        raise NotImplementedError

    @property
    def globals(self) -> Globals:
        raise NotImplementedError

    @overload
    def function(self, fn: Callable[..., Any]) -> Callable[..., Any]: ...
    @overload
    def function(
        self, *, name: str
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...
    def function(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
    ) -> Any:
        raise NotImplementedError

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        raise NotImplementedError

    @property
    def timeout(self) -> float:
        raise NotImplementedError

    @timeout.setter
    def timeout(self, value: float) -> None:
        raise NotImplementedError
