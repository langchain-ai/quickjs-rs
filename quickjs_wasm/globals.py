"""Globals proxy. See spec/implementation.md §7.2."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from quickjs_wasm.handle import Handle


class Globals:
    """Dict-like proxy for the JS global object."""

    def __getitem__(self, key: str) -> Any:
        raise NotImplementedError

    def __setitem__(self, key: str, value: Handle | Any) -> None:
        raise NotImplementedError

    def __contains__(self, key: str) -> bool:
        raise NotImplementedError

    def get_handle(self, key: str) -> Handle:
        raise NotImplementedError
