"""Cancellation checkpoints for synchronous work on a ThreadWorker."""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar


class CancellationRequest:
    """Deliver one cancellation at a checkpoint, like Task.cancel().

    The caller can signal the worker without scheduling code on its blocked
    event loop. Delivery is one-shot so native cancellation cleanup can run;
    the worker's asyncio Task also receives the normal cancellation request.
    """

    def __init__(self) -> None:
        self.requested = threading.Event()
        self.delivered = False

    def checkpoint(self) -> None:
        if self.requested.is_set() and not self.delivered:
            self.delivered = True
            raise asyncio.CancelledError


current_request: ContextVar[CancellationRequest | None] = ContextVar(
    "quickjs_cancellation_request", default=None
)


def cancellation_checkpoint() -> None:
    request = current_request.get()
    if request is not None:
        request.checkpoint()
