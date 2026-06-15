//! Host imports the guest depends on. Phase 1: `host_interrupt`, the graceful
//! timeout channel (spec → CPU And Timeouts).
//!
//! The host writes a deadline/cancel flag into host-side state (a Python
//! atomic, or a SharedArrayBuffer in worker-hosted JS — see the spikes); the
//! guest's QuickJS interrupt handler polls it via this import on the
//! interpreter's cadence. When it returns nonzero the running eval unwinds and
//! control returns to the guest, which maps it to a `Timeout` status. The
//! instance survives and accepts further evals (graceful at the instance
//! level).
//!
//! Note: QuickJS's interrupt raises an *uncatchable* interruption, so the
//! current eval cannot `try/catch` it mid-flight — JS-level cancellation
//! absorption (finally blocks observing the timeout) is a later-phase concern
//! tied to the async/cancel machinery. Phase 1 guarantees only that the
//! instance is not trapped/discarded.

extern "C" {
    /// Returns nonzero when the host wants the running eval interrupted.
    /// Provided by the host adapter under the `quickjs_host` import module.
    #[link_name = "host_interrupt"]
    fn host_interrupt() -> u32;
}

use std::cell::Cell;

thread_local! {
    /// Set when the interrupt handler last returned `true`, so the eval path
    /// can distinguish "our timeout fired" from an ordinary JS exception
    /// (rquickjs surfaces both as `Error::Exception`). Cleared before each eval.
    static INTERRUPTED: Cell<bool> = const { Cell::new(false) };
}

/// Poll the host's interrupt flag (called by the QuickJS interrupt handler on
/// the interpreter's cadence). Records the fire so eval can classify it.
pub(crate) fn poll_interrupt() -> bool {
    // SAFETY: FFI import with no arguments and a trivial return; the host
    // contract is that it only reads its own flag.
    let fired = unsafe { host_interrupt() != 0 };
    if fired {
        INTERRUPTED.with(|c| c.set(true));
    }
    fired
}

/// Clear the interrupt flag before an eval.
pub(crate) fn clear_interrupted() {
    INTERRUPTED.with(|c| c.set(false));
}

/// Whether the interrupt handler fired during the last eval.
pub(crate) fn was_interrupted() -> bool {
    INTERRUPTED.with(|c| c.get())
}
