"""Module loading. See spec/module-loading.md §3.1.

ModuleScope is a frozen dataclass — a composable registry of JS
module sources. Top-level maps import specifiers to either a source
string (single-file module) or a nested ModuleScope (multi-file
scope). Validation happens at construction: see __post_init__ and
CLAUDE.md's §4 non-negotiables.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ModuleScope:
    """A composable module registry.

    Top-level: maps import specifiers to source strings or nested
    scopes. Nested: maps filenames to source strings (flat, no
    subdirectories; no further nesting).

    Validation is done at construction. Syntax errors in source
    strings are NOT caught here — they surface at eval time per
    spec/module-loading.md §11 (pre-parse on install was considered
    and deferred).

    Examples:
        # Single-file module
        ModuleScope({"my-lib": "export const x = 1;"})

        # Multi-file scope
        ModuleScope({"my-lib": ModuleScope({
            "index.js": "export { foo } from './util.js';",
            "util.js": "export function foo() { return 1; }",
        })})

        # Composition via dict operations — no special methods.
        extended = ModuleScope({**base.modules, "@new/lib": "..."})
        overridden = ModuleScope({**base.modules, "@old/lib": "..."})
        filtered = ModuleScope(
            {k: v for k, v in extended.modules.items() if k != "@drop"}
        )
    """

    # Typed as dict for the dataclass-generated __init__ (users pass
    # a regular dict), but after __post_init__ runs the attribute is
    # swapped to a MappingProxyType — so runtime `.modules` is
    # read-only. Python's type system doesn't have a "read-only dict"
    # that spread-composes cleanly, so the declared type stays dict
    # for ergonomic construction, and `Mapping` is what you get at
    # read time. `modules.items()`, dict-spread `{**x.modules, ...}`,
    # and `x.modules[k]` all work — `x.modules[k] = ...` raises.
    modules: dict[str, str | ModuleScope]

    def __post_init__(self) -> None:
        for specifier, value in self.modules.items():
            # Top-level keys are import specifiers, never relative
            # paths. Relative specifiers belong inside nested scopes
            # and resolve via the scope's file map.
            if specifier.startswith("./") or specifier.startswith("../"):
                raise ValueError(
                    f"top-level key {specifier!r} looks like a relative "
                    "specifier; top-level keys are bare module names "
                    "(e.g. 'my-lib', '@agent/fs'), not ./ or ../ paths"
                )

            if isinstance(value, ModuleScope):
                _validate_nested_scope(specifier, value)
            elif isinstance(value, str):
                # Single-file module: source is the value. No further
                # validation — syntax errors are caught at eval time.
                pass
            else:
                raise TypeError(
                    f"module {specifier!r}: value must be str or "
                    f"ModuleScope, got {type(value).__name__}"
                )

        # Freeze. object.__setattr__ bypasses the frozen dataclass
        # __setattr__ — required in __post_init__. Wrap in a fresh
        # dict so a caller who passed in a dict and kept a reference
        # can't mutate it after the fact to smuggle in invalid
        # entries. MappingProxyType is stdlib; zero deps.
        object.__setattr__(
            self, "modules", MappingProxyType(dict(self.modules))
        )


def _validate_nested_scope(specifier: str, scope: ModuleScope) -> None:
    """Validate a second-level ModuleScope. §3.1 rules:

    1. Must contain an "index.js" entry point.
    2. All values must be str — no third level of nesting.
    3. Filenames have no `/` (flat namespace, §4 non-negotiable).
    """
    if "index.js" not in scope.modules:
        raise ValueError(
            f"nested scope {specifier!r}: missing required 'index.js' "
            "entry point. A multi-file ModuleScope must expose its "
            "public surface through index.js (that's what bare "
            f"`import ... from {specifier!r}` resolves to)"
        )

    for filename, source in scope.modules.items():
        if "/" in filename:
            raise ValueError(
                f"nested scope {specifier!r}: filename {filename!r} "
                "contains '/'. Nested scopes are flat — no "
                "subdirectories. If you need hierarchy, build it with "
                "separate top-level ModuleScope entries."
            )
        if isinstance(source, ModuleScope):
            raise ValueError(
                f"nested scope {specifier!r}: file {filename!r} is "
                "itself a ModuleScope. Only two levels of nesting are "
                "allowed — nested scopes contain source strings, not "
                "further scopes."
            )
        if not isinstance(source, str):
            raise TypeError(
                f"nested scope {specifier!r}: file {filename!r} must "
                f"be str, got {type(source).__name__}"
            )


__all__ = ["ModuleScope"]
