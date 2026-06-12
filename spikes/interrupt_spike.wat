;; Spike B guest module: models the two timeout cases for JS hosts.
;;
;; spin_cooperative models QuickJS with an interrupt handler installed: the
;; hot loop periodically calls the imported host.interrupt and exits when it
;; returns nonzero. The import is a JS closure in the worker that reads a
;; SharedArrayBuffer flag written by the main thread — no shared wasm linear
;; memory or threads feature required.
;;
;; spin_hostile models a loop that never reaches an interrupt check; only
;; worker termination can stop it (the documented backstop).
(module
  (import "host" "interrupt" (func $interrupt (result i32)))
  (func (export "spin_cooperative") (result i32)
    (local $n i32)
    (block $done
      (loop $l
        (local.set $n (i32.add (local.get $n) (i32.const 1)))
        (br_if $done (call $interrupt))
        (br $l)))
    (local.get $n))
  (func (export "spin_hostile")
    (loop $l (br $l))))
