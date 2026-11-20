# Snapshots

A snapshot saves everything a context holds: every object, closure, and pending promise. Restore it into another
context, in this process or another one, and the script carries on where it was. Use it to prepare an environment once
and start every run from it, or to pause a long-running agent and resume it elsewhere.

## The basics

```python
from quickjs_rs import Runtime, Snapshot

with Runtime() as rt:
    with rt.new_context() as ctx:
        ctx.eval("const counter = (() => { let n = 0; return () => ++n; })(); counter()")
        payload = ctx.create_snapshot().to_bytes()

    with rt.new_context() as restored:
        rt.restore_snapshot(Snapshot.from_bytes(payload), restored)
        print(restored.eval("counter()"))
        #> 2
```

The closure's private `n` came back with it. So do shared references, prototype changes, the queue of pending
promise callbacks, and the module cache.

A snapshot is a copy of the context's WebAssembly memory. Because everything a context holds lives in that memory,
copying it back is enough to bring the whole state back exactly.

## Register host functions again

The JavaScript side of a host function is in the snapshot, but the Python function is not. Register the same names on
the restored context before calling them:

```python
from quickjs_rs import HostError, Runtime

with Runtime() as rt:
    with rt.new_context() as ctx:
        ctx.register("double", lambda x: x * 2)
        ctx.eval("var answer = () => double(21)")
        snapshot = ctx.create_snapshot()

    with rt.new_context() as restored:
        rt.restore_snapshot(snapshot, restored)
        try:
            restored.eval("answer()")
        except HostError:
            print("double is not registered yet")
            #> double is not registered yet
        restored.register("double", lambda x: x * 2)
        print(restored.eval("answer()"))
        #> 42
```

| Restored from the snapshot | Not in the snapshot |
| --- | --- |
| All JavaScript objects, closures, and globals | The Python host functions |
| Pending promises and queued callbacks | `Handle` objects from the original context |
| The module cache | The runtime's clock and module loader |
| The memory limit of the original runtime | |
| The state of `Math.random()` | |

The restored context reads the clock of the runtime it now belongs to.

## Limitations

- **The context must be idle.** No `eval_async` can be running and no async host call can be waiting, or
  `create_snapshot()` raises. Async code can use `await ctx.create_snapshot_async()`.
- **No module-mode contexts.** A context that ran code with `module=True` raises `NotImplementedError`. Dynamic
  `import()` is fine.
- **Python state does not travel.** Host functions and `Handle` objects stay behind. Register host functions again
  after restoring.
- **Some settings travel and some do not.** The memory limit comes from the runtime that took the snapshot. The clock
  and module loader come from the runtime you restore into.
- **All or nothing.** A restore replaces everything in the destination context. There is no way to restore part of a
  snapshot or merge two of them.
- **Same build only.** A snapshot restores only into the exact engine build that made it. See
  [tied to one build](#snapshots-are-tied-to-one-build).
- **Size is peak memory.** A snapshot is never smaller than the most memory the context ever used. See [size](#size).
- **Not signed.** Restoring a snapshot trusts its bytes completely. See [treat snapshots like code](#treat-snapshots-like-code).

## Size

A snapshot is as large as the context's memory. Memory never shrinks, so a context
that once used 60 MB snapshots at 60 MB or more, even after its garbage is collected. Take snapshots before heavy work
when size matters.

## Snapshots are tied to one build

Every snapshot records the SHA-256 of the exact engine build that made it. Restoring into a different build raises
`ValueError`, so upgrading quickjs-rs invalidates saved snapshots. Use snapshots to move a running state between
processes on the same version, not as long-term storage.

`restore_snapshot(snapshot, ctx, inject_globals=False)` checks the header and changes nothing. Use it to test whether
a snapshot fits this build.

## Treat snapshots like code

The header check catches accidents, not attacks. Snapshots are not signed. Whoever can change the bytes controls
everything the restored context does and returns, and the header decides how much memory a restore allocates. The
sandbox still contains the result, the same way it contains any script.

A snapshot also holds everything the context ever had in memory, including host function results and secrets.

- Only restore snapshots you created, from storage only you can write.
- Sign or MAC snapshots that are stored or sent anywhere less trusted, and verify them before restoring.
- Check the size against your own limit before restoring.
- Encrypt snapshots at rest if the context handled sensitive data.
- Do not share one snapshot between users unless you took it before any user data went in.

### Format

```text
offset  size  field
0       4     magic            b"QFGS"
4       4     format_version   u32 little-endian, currently 1
8       32    build_id         SHA-256 of the engine .wasm
40      8     memory_size      u64 little-endian, bytes of memory that follow
48      4     stack_pointer    u32 little-endian
52      ...   image            memory_size bytes
```

Restore checks the length, magic, format version, build ID, image size, and stack pointer, in that order, and writes
nothing until all pass.

## See also

- [Threat model](../reference/threat-model.md#snapshots): snapshot tampering and disclosure.
- [Host functions](../concepts/host-functions.md): how calls find their Python function by name.
