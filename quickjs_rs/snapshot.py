"""Snapshot value object for serialized context state.

Snapshots captures the restorable portion of a context's script-mode
top-level state. The payload is opaque at the Python layer: structural
encoding, compatibility metadata, active-value serialization, and
tombstone records are all owned by the Rust engine.

Use :meth:`quickjs_rs.Context.create_snapshot` or
:meth:`quickjs_rs.Context.create_snapshot_async` to create snapshots,
then :meth:`quickjs_rs.Runtime.restore_snapshot` to rehydrate them into
another context.
"""

from __future__ import annotations


class Snapshot:
    """Opaque wrapper around a serialized Snapshot V1 payload.

    A :class:`Snapshot` is transportable and can be converted to and
    from raw bytes, but the payload is only validated structurally when
    restored into a context by the runtime.
    """

    __slots__ = ("_data",)

    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)

    def to_bytes(self) -> bytes:
        """Return the serialized snapshot payload as immutable bytes."""
        return self._data

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview) -> Snapshot:
        """Decode a snapshot from bytes.

        Args:
            data: Previously serialized snapshot bytes.

        Returns:
            A :class:`Snapshot` wrapper around the raw payload.

        Structural, version, and runtime-compatibility validation is
        performed when the snapshot is restored into a context.
        """
        return cls(bytes(data))

    def __repr__(self) -> str:
        return f"Snapshot(size={len(self._data)})"
