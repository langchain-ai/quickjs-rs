# Security model

quickjs-rs is built to run code that a language model wrote and nobody reviewed.

## Sandboxing

Scripts run inside WebAssembly. The JavaScript engine is compiled to a wasm module, and each `Context` is its own
instance of that module with its own memory. The sandbox is not something quickjs-rs adds on top: it is the isolation
WebAssembly itself guarantees, enforced by wasmtime.

- **Memory.** A wasm instance's loads and stores address its own linear memory, a single byte array, and every access
  is bounds-checked. There is no way to form a pointer into the host process.
- **Control flow.** Code can branch only within its own structured blocks and call only its own functions or its
  imports, with types checked on indirect calls. The call stack and return addresses live outside linear memory, so
  corrupting memory cannot redirect execution into host code.
- **Capabilities.** An instance can affect the outside world only by calling the functions it imports, and the host
  decides what those are.

So a script has no way to reach your Python objects, your files, or another context. See the
[WebAssembly security model](https://webassembly.org/docs/security/) and
[wasmtime's security documentation](https://docs.wasmtime.dev/security.html) for the details.

What a script gets:

- **No ambient access.** No files, environment variables, or network. No `console`, `fetch`, timers, `require`, or
  `process`.
- **A short list of ways out.** The engine can ask the host to run a host function, to check its deadline, and to
  resolve and load a module. Each of these does only what you configured.
- **Containment of engine bugs.** A memory-safety bug in QuickJS gives an attacker that context's memory, which holds
  only what you put there.

```python
from quickjs_rs import Runtime

ambient = ["console", "fetch", "setTimeout", "require", "process", "WebAssembly"]

with Runtime() as rt, rt.new_context() as ctx:
    found = ctx.eval(f"{ambient}.filter((name) => name in globalThis)")
    print(found)
    #> []
```

The remaining escape path is wasmtime itself. wasmtime compiles and runs the guest inside your process, so a bug in
wasmtime or its Cranelift compiler could cross the boundary. Keep `wasmtime` up to date. If you must assume an
attacker who can exploit wasmtime, also run quickjs-rs in a container or a separate process.

## Host functions are the attack surface

The sandbox guarantees that a script reaches nothing you did not hand it. It cannot make what you handed it safe, and
a badly written host function is the easiest way out of a sound sandbox.

A host function runs in your Python process with all of its permissions. Any code in the context can call it, with
any arguments, as many times as it likes. If it reads a file by path, the script can read any file the path reaches.
If it runs a query, the script can run any query the function lets through.

### Validate every argument

Treat arguments like an untrusted HTTP request body. This host function looks harmless and is a file-system
escape:

```python
import tempfile
from pathlib import Path

from quickjs_rs import Runtime

with tempfile.TemporaryDirectory() as tmp:
    notes = Path(tmp).resolve() / "notes"
    notes.mkdir()
    (notes / "todo.txt").write_text("buy milk")
    (notes.parent / "secret.txt").write_text("hunter2")

    def read_note_unsafe(name: str) -> str:
        return (notes / name).read_text()

    def read_note(name: str) -> str:
        path = (notes / name).resolve()
        if not path.is_relative_to(notes):
            raise PermissionError(name)
        return path.read_text()

    with Runtime() as rt, rt.new_context() as ctx:
        ctx.register("read_note", read_note_unsafe)
        print(ctx.eval("read_note('../secret.txt')"))
        #> hunter2

        ctx.register("read_note", read_note)
        print(ctx.eval("read_note('todo.txt')"))
        #> buy milk
        print(ctx.eval("try { read_note('../secret.txt') } catch (e) { e.message }"))
        #> Host function failed
```

- Check types and ranges. Resolve paths and URLs, then compare them against an allowlist.
- Apply the permissions of the end user the script runs for, not the permissions of your service.
- Assume the script's author sees everything you return, and leave out what they should not see.
- Never register a general-purpose function: a Python `eval`, a shell, raw file or HTTP access, or a function that
  calls "any function by name".

### Do not leak through errors

When a host function raises, the script sees a fixed error with no detail. Exception messages often contain paths,
queries, or secrets, and anything a script can read can end up in model output. The original exception stays in
Python:

```python
from quickjs_rs import Runtime

with Runtime() as rt, rt.new_context() as ctx:

    @ctx.function
    def read_config() -> str:
        raise RuntimeError("cannot connect: password=hunter2")

    print(ctx.eval("try { read_config() } catch (e) { `${e.name}: ${e.message}` }"))
    #> HostError: Host function failed

    try:
        ctx.eval("read_config()")
    except RuntimeError as e:
        print("Python sees:", e)
        #> Python sees: cannot connect: password=hunter2
```

Return values get no such protection. If a host function returns a secret, the script has it.

### Limit what host functions can be made to do

quickjs-rs does not limit how many host calls a script makes or how many run at once. One `Promise.all` can start
100,000 async calls, and a loop can make millions of sync calls within one timeout. Every call is real work in your
process, and often a request to someone else's API.

Put the limits inside your host functions: a total call budget per run, a semaphore for concurrency, and the same
rate limits you would put on a public endpoint. See
[budget your host functions](../concepts/host-functions.md#budget-your-host-functions) for an example.

## Contexts do not share memory

Two contexts never share JavaScript objects, even when one `Runtime` made both.

```python
from quickjs_rs import Runtime

with Runtime() as rt, rt.new_context() as a, rt.new_context() as b:
    a.eval("globalThis.secret = 42")
    print(b.eval("typeof secret"))
    #> undefined
```

A context does keep its own state from one eval to the next. Globals, modified prototypes, and cached modules are all
still there for the next script. Do not reuse a context across users.

A runtime's contexts do share some things: the module loader, the clock, and the timeout slot. Use one `Runtime` per
tenant, and one per thread.

## Limits have edges

Memory and time limits stop most runaway scripts. Each has gaps you should know about before relying on it:

| Limit | Default | What it does not cover |
| --- | --- | --- |
| `memory_limit` | 64 MiB per context | Memory outside the QuickJS heap. Before 0.3.0 it also undercounted badly. |
| `timeout` | 5 seconds per eval | Time spent awaiting async host functions, and time inside a blocking sync host function. |
| Stack | Fixed by the wasm build | Deep recursion raises `wasmtime.Trap`, which is not a `QuickJSError`. |
| Async host calls | No limit | One `Promise.all` can start thousands of calls. |

[Resource limits](../concepts/resource-limits.md) covers each one.

## Snapshots are trusted input

A [snapshot](../concepts/snapshots.md) is a raw copy of a context's memory. Restoring one checks that it came from the same
build of the engine, but snapshots are not signed. Anyone who can change the bytes controls everything the restored
context computes and returns. A snapshot also holds every secret the context ever saw.

Only restore snapshots you created, from storage only you can write.

## Error classes are not proof

quickjs-rs picks the exception class from the error's `name` and `message`. A script can throw an object that looks
like an out-of-memory error:

```python
from quickjs_rs import MemoryLimitError, Runtime

with Runtime() as rt, rt.new_context() as ctx:
    try:
        ctx.eval("throw { name: 'InternalError', message: 'out of memory' }")
    except MemoryLimitError:
        print("raised as MemoryLimitError")
        #> raised as MemoryLimitError
```

Use exception classes to decide how to report a failure, not to decide what the script was allowed to do.

## Checklist for untrusted code

1. **Register the fewest host functions you can.** Register only what the task needs. Never register a general-purpose
   function: a Python `eval`, a shell, raw file or HTTP access, or a function that calls "any function by name".
2. **Validate inside every host function.** Check types and ranges, resolve paths and URLs against an allowlist, and
   enforce the end user's permissions. Assume the script's author sees everything you return.
3. **Budget the run.** Keep `memory_limit` set and pick a `timeout` that fits the task. Wrap `eval_async` in
   `asyncio.timeout()` to bound total time, because awaited host calls do not count against `timeout`. Cap how often,
   and how many at once, each host function can run.
4. **Start from clean state.** Run each untrusted program in a new context, or in one restored from a snapshot you made.
5. **Separate tenants.** Use one `Runtime` per tenant. For hostile multi-tenant workloads, add process or container
   isolation and recycle workers after a timeout, an out-of-memory error, or a trap.
6. **Catch every failure.** Catch `QuickJSError` and `wasmtime.Trap`. Close the context after a trap.

## Report a vulnerability

quickjs-rs has not had an independent security audit. If you find a way out of the sandbox, report it privately
through the repository's [security page](https://github.com/langchain-ai/quickjs-rs/security), not in a public issue.

## See also

- [Threat model](../reference/threat-model.md): assets, attackers, trust boundaries, and each threat with its status.
- [Architecture](../reference/architecture.md): how the pieces fit together.
