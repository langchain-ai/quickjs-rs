from __future__ import annotations

import queue
import threading

from quickjs_rs import Runtime


def _call_in_thread(fn: object) -> tuple[str, str]:
    """Run fn() in another thread and return ("ok", value_repr) or
    ("err", message). Catch BaseException because PyO3 unsendable
    violations surface as PanicException (BaseException subclass)."""
    out: queue.Queue[tuple[str, str]] = queue.Queue()

    def worker() -> None:
        try:
            result = fn()  # type: ignore[misc,operator]
            out.put(("ok", repr(result)))
        except BaseException as exc:
            out.put(("err", f"{type(exc).__name__}: {exc}"))

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "worker thread hung"
    return out.get_nowait()


def test_context_cannot_be_used_from_another_thread() -> None:
    """Threat: cross-thread access to interpreter state.

    Current behavior: blocked by PyO3 unsendable checks before eval.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            status, payload = _call_in_thread(lambda: ctx.eval("1 + 1"))
            assert status == "err"
            assert "unsendable" in payload
            assert "sent to another thread" in payload


def test_handle_cannot_be_used_from_another_thread() -> None:
    """Threat: cross-thread use of opaque value handles.

    Current behavior: blocked by PyO3 unsendable checks.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle("({x: 1})") as h:
                status, payload = _call_in_thread(lambda: h.get("x").to_python())
                assert status == "err"
                assert "unsendable" in payload
                assert "sent to another thread" in payload
