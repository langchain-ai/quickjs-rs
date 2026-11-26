# Host functions

A host function is a Python function that scripts call like any other JavaScript function. Scripts use host
functions to read data, call APIs, and do anything else you write for them.

## The basics

`@ctx.function` installs a function as a JavaScript global. The global's name is the function's name.

```python
from quickjs_rs import Runtime

with Runtime() as rt, rt.new_context() as ctx:

    @ctx.function
    def get_temperature(city: str) -> float:
        return {"London": 21.5, "Oslo": 4.0}[city]

    print(ctx.eval("`${get_temperature('London')} degrees`"))
    #> 21.5 degrees
```

To pick a different JavaScript name, pass `name=`. To register a function you did not define with a decorator, use
`ctx.register`:

```python
from quickjs_rs import Runtime

with Runtime() as rt, rt.new_context() as ctx:

    @ctx.function(name="getUser")
    def get_user(user_id: int) -> dict:
        return {"id": user_id, "name": "Ada"}

    ctx.register("upper", str.upper)

    print(ctx.eval("upper(getUser(7).name)"))
    #> ADA
```

Host functions belong to one context. A new context, or a context restored from a snapshot, starts with none.
Registering a name again replaces the old function.

## Arguments and return values

Arguments arrive as Python values, converted by the rules in [values](../concepts/values.md). Return values convert back
the same way.

```python
from quickjs_rs import Runtime

with Runtime() as rt, rt.new_context() as ctx:

    @ctx.function
    def describe(*args: object) -> str:
        return repr(args)

    print(ctx.eval("describe(1, 'two', [3], { four: 4 }, null)"))
    #> (1, 'two', [3], {'four': 4}, None)
```

JavaScript passes every argument the caller wrote, so match your signature to how scripts call it.
`items.map(fn)` passes three arguments (the value, the index, and the array). Write `items.map((x) => fn(x))`, or give
the function `*args`.

Arguments that cannot be converted, such as a function or a promise, make the call fail before your code runs.
JavaScript's `this` is not passed.

## Errors

When a host function raises, the script receives an `Error` named `HostError` with the message
`Host function failed`. The message is the same for every failure. It never includes the Python exception, its type,
or the function's name, so nothing from your process leaks into the script.

```python
from quickjs_rs import Runtime

with Runtime() as rt, rt.new_context() as ctx:

    @ctx.function
    def charge(amount: int) -> str:
        if amount > 100:
            raise ValueError(f"amount {amount} exceeds the card limit")
        return "ok"

    print(ctx.eval("try { charge(500) } catch (e) { e.message }"))
    #> Host function failed
```

If the script does not catch the error, the eval raises your original exception:

```python
from quickjs_rs import Runtime

with Runtime() as rt, rt.new_context() as ctx:

    @ctx.function
    def charge(amount: int) -> str:
        raise ValueError(f"amount {amount} exceeds the card limit")

    try:
        ctx.eval("charge(500)")
    except ValueError as e:
        print(e)
        #> amount 500 exceeds the card limit
```

To tell the script what went wrong, raise `JSError(name, message)`. The script receives an `Error` with exactly that
name and message, and an uncaught one raises `JSError` from the eval like any script error. This works the same way for
`async def` functions.

```python
from quickjs_rs import JSError, Runtime

with Runtime() as rt, rt.new_context() as ctx:

    @ctx.function
    def charge(amount: int) -> str:
        raise JSError("RangeError", "amount too large")

    print(ctx.eval("try { charge(500) } catch (e) { `${e.name}: ${e.message}` }"))
    #> RangeError: amount too large
```

## Async host functions

An `async def` host function returns a promise to the script. Run scripts that call one with
[`eval_async`](../concepts/async.md), which runs the event loop the function needs.

```python
import asyncio

from quickjs_rs import Runtime


async def main() -> None:
    with Runtime() as rt, rt.new_context() as ctx:

        @ctx.function
        async def fetch_user(user_id: int) -> dict:
            await asyncio.sleep(0.1)
            return {"id": user_id, "name": "Ada"}

        print(await ctx.eval_async("(await fetch_user(7)).name"))
        #> Ada


asyncio.run(main())
```

A sync `eval` has no event loop, so calling an async host function from one raises `ConcurrentEvalError`.

If the function raises, the promise rejects with the same `HostError` as a sync failure.

## Concurrency

Calling an async host function starts it right away, as an asyncio task, and hands the script a promise. The script
decides when to wait for it. That makes the order of `await`s decide what runs at the same time:

- `await a(); await b()` runs `a`, waits for it, then runs `b`.
- `await Promise.all([a(), b()])` starts both, then waits for both.
- A call the script never awaits still runs. `eval_async` does not return until every call the script started has
  finished.

```python
import asyncio
import time

from quickjs_rs import Runtime


async def main() -> None:
    with Runtime() as rt, rt.new_context() as ctx:

        @ctx.function
        async def fetch_user(user_id: int) -> dict:
            await asyncio.sleep(0.2)
            return {"id": user_id}

        start = time.perf_counter()
        await ctx.eval_async("await fetch_user(1); await fetch_user(2)")
        print("one after another:", time.perf_counter() - start > 0.35)
        #> one after another: True

        start = time.perf_counter()
        await ctx.eval_async("await Promise.all([fetch_user(1), fetch_user(2)])")
        print("together:", time.perf_counter() - start < 0.35)
        #> together: True


asyncio.run(main())
```

Sync host functions never overlap. Each one runs to completion on the evaluating thread while the script waits.

## Sync host functions and the timeout

The [timeout](../concepts/resource-limits.md#time) keeps running while a sync host function runs, but it cannot stop the
function. The engine only checks the deadline while JavaScript runs, so a sync function that takes 10 seconds holds
the context for 10 seconds. As soon as JavaScript runs again with the deadline already passed, the eval raises
`TimeoutError`:

```python
import time

from quickjs_rs import Runtime, TimeoutError

with Runtime() as rt, rt.new_context(timeout=0.2) as ctx:

    @ctx.function
    def slow_lookup() -> int:
        time.sleep(0.5)
        return 1

    try:
        ctx.eval("const n = slow_lookup(); let total = 0; for (let i = 0; i < 1e6; i++) total += n; total")
    except TimeoutError:
        print("timed out after the host function returned")
        #> timed out after the host function returned
```

Async host functions work the other way: time spent awaiting them does not count against the timeout at all. See
[async evaluation](../concepts/async.md#only-javascript-time-counts).

Keep sync host functions fast, and make anything that does I/O `async`. A sync host function must not call `eval` on
its own context.

## Cancellation

If the task running `eval_async` is cancelled, every host call still in flight is rejected with an error named
`HostCancellationError`. The script's `catch` and `finally` blocks then get one chance to run.

```python
import asyncio

from quickjs_rs import Runtime


async def main() -> None:
    with Runtime() as rt, rt.new_context() as ctx:

        @ctx.function
        async def slow_tool() -> str:
            await asyncio.sleep(10)
            return "done"

        task = asyncio.create_task(ctx.eval_async("""
            try { await slow_tool() }
            catch (e) { `cleaned up after ${e.name}` }
        """))
        await asyncio.sleep(0.05)
        task.cancel()
        print(await task)
        #> cleaned up after HostCancellationError


asyncio.run(main())
```

If the script handles the cancellation and returns a value, `eval_async` returns it. Otherwise `eval_async` raises
`asyncio.CancelledError`.

## Budget your host functions

quickjs-rs does not limit how many host calls a script makes, or how many run at once. A script can start 100,000 async
calls with one `Promise.all`. Put the limit inside the host function, and see the
[security model](../concepts/security-model.md#limit-what-host-functions-can-be-made-to-do) for why it matters:

```python
import asyncio

from quickjs_rs import Runtime


async def main() -> None:
    gate = asyncio.Semaphore(4)
    calls = 0

    with Runtime() as rt, rt.new_context() as ctx:

        @ctx.function
        async def search(query: str) -> str:
            nonlocal calls
            calls += 1
            if calls > 10:
                raise RuntimeError("search budget exhausted")
            async with gate:
                await asyncio.sleep(0.01)
                return query.upper()

        print(await ctx.eval_async("""
            const results = await Promise.allSettled(
                Array.from({ length: 12 }, (_, i) => search(`q${i}`))
            );
            results.filter((r) => r.status === "fulfilled").length
        """))
        #> 10


asyncio.run(main())
```

## See also

- [Async evaluation](../concepts/async.md): how `eval_async` drives promises and counts time.
- [Security model](../concepts/security-model.md): writing host functions that are safe to expose.
