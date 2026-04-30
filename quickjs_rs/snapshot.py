"""Snapshot value object for serialized context state."""

from __future__ import annotations


class Snapshot:
    """Opaque V1 snapshot payload wrapper."""

    __slots__ = ("_data",)

    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)

    def to_bytes(self) -> bytes:
        """Encode this snapshot into transport bytes."""
        return self._data

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview) -> Snapshot:
        """Decode a snapshot from bytes.

        Structural/version validation is performed when the snapshot is
        restored into a context.
        """
        return cls(bytes(data))

    def __repr__(self) -> str:
        return f"Snapshot(size={len(self._data)})"
