from __future__ import annotations

import pytest

from quickjs_rs import ConcurrentEvalError, Handle, Runtime, Snapshot


def test_driver_captures_deferred_host_request_and_resolves() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def task(value: str) -> str:
                raise AssertionError("manual driver should not execute host call automatically")

            ctx.register("task", task)

            with ctx.driver.start_eval("await task('hello')") as session:
                requests = session.take_host_requests()
                assert len(requests) == 1
                request = requests[0]
                assert request.args == ("hello",)
                assert session.promise_state() == "pending"

                session.resolve(request.deferred_id, "world")
                session.run_pending_jobs()

                assert session.promise_state() == "fulfilled"
                with session.promise_result() as envelope:
                    with envelope.get("value") as value:
                        assert value.to_python() == "world"


def test_driver_snapshot_preserves_parked_deferred_and_can_resume_after_restore() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def task(value: str) -> str:
                raise AssertionError("manual driver should not execute host call automatically")

            ctx.register("task", task)

            with ctx.driver.start_eval(
                "globalThis.__root = (async () => await task('hello'))(); await globalThis.__root"
            ) as session:
                request = session.take_host_requests()[0]
                snapshot = session.create_snapshot().to_bytes()
                deferred_id = request.deferred_id

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:

            async def task(value: str) -> str:
                raise AssertionError("restored parked deferred should be resolved directly")

            ctx2.register("task", task)
            rt2.restore_snapshot(Snapshot.from_bytes(snapshot), ctx2)

            with ctx2.eval_handle("globalThis.__root") as root:
                assert root._require_live().is_promise()
                assert ctx2._engine_ctx.promise_state(root._require_live()) == 0

                ctx2._engine_ctx.resolve_pending(deferred_id, "restored")
                ctx2._engine_ctx.run_pending_jobs()

                assert ctx2._engine_ctx.promise_state(root._require_live()) == 1
                with Handle(ctx2, ctx2._engine_ctx.promise_result(root._require_live())) as result:
                    assert result.to_python() == "restored"


async def test_driver_take_host_requests_clears_queue() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def task(value: str) -> str:
                raise AssertionError("manual driver should not execute host call automatically")

            ctx.register("task", task)

            with ctx.driver.start_eval("await task('hello')") as session:
                assert len(session.take_host_requests()) == 1
                assert session.take_host_requests() == ()


async def test_driver_captures_multiple_host_requests_in_order() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def task(value: str) -> str:
                raise AssertionError("manual driver should not execute host call automatically")

            ctx.register("task", task)

            with ctx.driver.start_eval(
                "await Promise.all([task('a'), task('b'), task('c')])"
            ) as session:
                session.run_pending_jobs()
                requests = session.take_host_requests()
                assert [request.args for request in requests] == [("a",), ("b",), ("c",)]
                assert len({request.deferred_id for request in requests}) == 3


async def test_driver_resolving_request_can_emit_next_host_request() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def task(value: str) -> str:
                raise AssertionError("manual driver should not execute host call automatically")

            ctx.register("task", task)

            with ctx.driver.start_eval(
                "const first = await task('first'); await task(first)"
            ) as session:
                first_request = session.take_host_requests()[0]
                assert first_request.args == ("first",)
                assert session.take_host_requests() == ()

                session.resolve(first_request.deferred_id, "second")
                session.run_pending_jobs()

                second_request = session.take_host_requests()[0]
                assert second_request.args == ("second",)
                assert session.promise_state() == "pending"

                session.resolve(second_request.deferred_id, "done")
                session.run_pending_jobs()
                assert session.promise_state() == "fulfilled"
                with session.promise_result() as envelope:
                    with envelope.get("value") as value:
                        assert value.to_python() == "done"


async def test_driver_rejects_deferred_host_request() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def task(value: str) -> str:
                raise AssertionError("manual driver should not execute host call automatically")

            ctx.register("task", task)

            with ctx.driver.start_eval("await task('boom')") as session:
                request = session.take_host_requests()[0]
                session.reject(request.deferred_id, "CustomError", "boom")
                session.run_pending_jobs()

                assert session.promise_state() == "rejected"
                with session.promise_result() as envelope:
                    with envelope.get("name") as name:
                        assert name.to_python() == "CustomError"
                    with envelope.get("message") as message:
                        assert message.to_python() == "boom"


async def test_driver_active_session_blocks_other_evals_until_closed() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:

            async def task(value: str) -> str:
                raise AssertionError("manual driver should not execute host call automatically")

            ctx.register("task", task)

            session = ctx.driver.start_eval("await task('hello')")
            try:
                with pytest.raises(ConcurrentEvalError):
                    ctx.driver.start_eval("1 + 1")
                with pytest.raises(ConcurrentEvalError):
                    await ctx.eval_async("1 + 1")
            finally:
                session.close()

            assert await ctx.eval_async("1 + 1") == 2
