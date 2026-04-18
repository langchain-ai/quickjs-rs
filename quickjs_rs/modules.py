"""Module loading. See spec/module-loading.md §3.1.

ModuleScope is a frozen dataclass — a recursive, self-contained
registry of JS module sources and named dependencies. Each scope
has two sub-namespaces distinguished by value type: `str` values
are files (addressed by relative specifiers with POSIX-path keys
that may contain `/`), and `ModuleScope` values are named
dependencies (addressed by bare import specifiers).

Validation happens at construction — see __post_init__ and
CLAUDE.md's non-negotiables block. Nesting is recursive and
unbounded; the dependency graph shape is whatever the user hands
us.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

# §5.5: recognized extensions for a scope's entry-point module.
# Order is the resolver's preference order when a bare specifier
# lands on a scope: whichever index.<ext> exists first in this
# list wins. JS variants first (faster — no strip), then TS
# variants (always stripped via oxidase), then JSX/TSX.
_INDEX_EXTENSIONS: tuple[str, ...] = (
    "js",
    "mjs",
    "cjs",
    "ts",
    "mts",
    "cts",
    "jsx",
    "tsx",
)


@dataclass(frozen=True)
class ModuleScope:
    """A recursive, self-contained module registry.

    Two kinds of entries, keyed by value type:

      * `str` values — the scope's own files. Keys are POSIX-style
        paths (``"index.js"``, ``"lib/util.js"``, ``"tests/deep/x.js"``).
        Addressable only by relative import specifiers (``./X``, ``../X``);
        resolution uses ``posixpath.normpath`` against the scope's file
        set, with the scope root as ceiling.
      * `ModuleScope` values — the scope's named dependencies. Keys
        are bare import specifiers (``"@agent/fs"``, ``"lodash"``).
        Addressable only by bare import specifiers.

    The two namespaces don't cross. A bare specifier never matches a
    str key; a relative specifier never matches a ModuleScope key —
    even if the names happen to be identical.

    Validation is done at construction. Syntax errors in source
    strings are NOT caught here — they surface at eval time per
    spec/module-loading.md §11 (pre-parse on install was considered
    and deferred).

    Examples:
        # Multi-file scope with a subdirectory path
        ModuleScope({"my-lib": ModuleScope({
            "index.js": "export { foo } from './lib/util.js';",
            "lib/util.js": "export function foo() { return 1; }",
        })})

        # Scope with a named dependency (bare import) it carries itself
        ModuleScope({"my-lib": ModuleScope({
            "@peer": ModuleScope({"index.js": "export const P = 1;"}),
            "index.js": 'import { P } from "@peer"; export const x = P;',
        })})

        # Pure-dependency container (only ModuleScope values, no str
        # entries, no index.js required). Common shape for the root
        # scope passed to ctx.install.
        ModuleScope({
            "@agent/utils": ModuleScope({"index.js": "..."}),
            "@agent/fs": ModuleScope({"index.js": "..."}),
        })

        # Composition via dict operations — no special methods.
        extended = ModuleScope({**base.modules, "@new/lib":
            ModuleScope({"index.js": "..."})})
        overridden = ModuleScope({**base.modules, "@old/lib":
            ModuleScope({"index.js": "..."})})
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
        has_str_entry = False

        for key, value in self.modules.items():
            # Keys are never relative specifiers. Those belong in JS
            # source as import statements — not as dict keys that
            # name a file or a dep.
            if key.startswith("./") or key.startswith("../"):
                raise ValueError(
                    f"key {key!r} looks like a relative specifier; "
                    "keys are file paths (str-valued) or bare import "
                    "names (ModuleScope-valued), never ./ or ../ paths"
                )

            if isinstance(value, str):
                has_str_entry = True
                # No shape validation on str keys — they're POSIX
                # paths, and normalization is a resolver concern.
                # Anything goes here as long as the top-level ./ and
                # ../ checks above pass.
            elif isinstance(value, ModuleScope):
                # Nested ModuleScope is itself a scope — its own
                # __post_init__ already validated its contents. No
                # additional rule: recursive nesting is unbounded.
                pass
            else:
                raise TypeError(
                    f"key {key!r}: value must be str or ModuleScope, "
                    f"got {type(value).__name__}"
                )

        # A scope that owns any files must expose an entry point.
        # Bare `import "scope-name"` from the parent resolves to
        # "scope-name/index.<ext>" where <ext> is one of the
        # recognized module extensions (§5.5). Accept .js, .mjs,
        # .cjs for plain JS and .ts, .mts, .cts, .jsx, .tsx for
        # files that get processed at install time. A pure-
        # dependency container (only ModuleScope entries, no str
        # entries) doesn't need one.
        if has_str_entry and not any(
            f"index.{ext}" in self.modules for ext in _INDEX_EXTENSIONS
        ):
            raise ValueError(
                "ModuleScope with str entries is missing required "
                "index module. A scope that owns files must declare "
                "one of: "
                + ", ".join(f"index.{e}" for e in _INDEX_EXTENSIONS)
                + ". That's what a bare `import ... from "
                "'scope-name'` resolves to. If this is a pure-"
                "dependency container, remove the str entries or "
                "wrap them in a nested ModuleScope."
            )

        # Freeze. object.__setattr__ bypasses the frozen dataclass
        # __setattr__ — required in __post_init__. Wrap in a fresh
        # dict so a caller who passed in a dict and kept a reference
        # can't mutate it after the fact to smuggle in invalid
        # entries. MappingProxyType is stdlib; zero deps.
        object.__setattr__(
            self, "modules", MappingProxyType(dict(self.modules))
        )


__all__ = ["ModuleScope"]
