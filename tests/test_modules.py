"""ModuleScope validation + composition + install. See
spec/module-loading.md §3 and §5.

Validation-only tests live at the top; the bottom of the file
covers the end-to-end install → resolve → load → module-eval
path added in step 3. §9.1 continues to grow across steps 4–7.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from quickjs_rs import ModuleScope, Runtime

# ---- Valid construction ---------------------------------------------


def test_single_file_module_wrapped_in_scope_is_valid() -> None:
    """§3.1: single-file dep is wrapped in a ModuleScope with an
    index.js. A bare str at scope root has no index.js sibling and
    no longer parses — the wrap is the canonical shape."""
    scope = ModuleScope(
        {
            "lodash": ModuleScope(
                {"index.js": "export function get(o, k) { return o[k]; }"}
            )
        }
    )
    assert isinstance(scope.modules["lodash"], ModuleScope)
    assert "index.js" in scope.modules["lodash"].modules


def test_multi_file_scope_is_valid() -> None:
    """Nested ModuleScope with index.js + helper file."""
    scope = ModuleScope(
        {
            "@agent/utils": ModuleScope(
                {
                    "index.js": "export { slugify } from './strings.js';",
                    "strings.js": (
                        "export function slugify(s) {"
                        " return s.toLowerCase().replace(/ /g, '-'); }"
                    ),
                }
            )
        }
    )
    inner = scope.modules["@agent/utils"]
    assert isinstance(inner, ModuleScope)
    assert set(inner.modules.keys()) == {"index.js", "strings.js"}


def test_pure_dependency_root_is_valid_without_index_js() -> None:
    """§3.1: a scope containing only ModuleScope values (no str
    entries) doesn't need its own index.js — it isn't a module
    target, just a registry wrapper. This is the common shape of
    what gets passed to ctx.install."""
    scope = ModuleScope(
        {
            "@agent/config": ModuleScope(
                {"index.js": "export const ENV = 'prod';"}
            ),
            "@agent/fs": ModuleScope({"index.js": "export default 1;"}),
        }
    )
    assert isinstance(scope.modules["@agent/config"], ModuleScope)
    assert isinstance(scope.modules["@agent/fs"], ModuleScope)


def test_empty_scope_is_valid() -> None:
    """An empty registry is a degenerate but valid shape — useful as
    a base for composition. Pure-dependency containers (no str
    entries) don't need an index.js, and an empty scope satisfies
    that trivially."""
    scope = ModuleScope({})
    assert len(scope.modules) == 0


def test_posix_subdirectory_paths_in_str_keys_are_valid() -> None:
    """§3.1: str keys are POSIX paths. `/` in a key creates a
    subdirectory structure within the scope — `./lib/util.js`
    normalizes against this set at resolve time."""
    scope = ModuleScope(
        {
            "@agent/fs": ModuleScope(
                {
                    "index.js": "export { foo } from './lib/util.js';",
                    "lib/util.js": "export function foo() { return 1; }",
                    "lib/helpers/str.js": (
                        "export function lower(s) { return s.toLowerCase(); }"
                    ),
                }
            )
        }
    )
    inner = scope.modules["@agent/fs"]
    assert isinstance(inner, ModuleScope)
    assert set(inner.modules.keys()) == {
        "index.js",
        "lib/util.js",
        "lib/helpers/str.js",
    }


def test_recursive_nesting_is_unbounded() -> None:
    """§3.1: nesting is recursive, not capped at two levels. A scope
    can carry a dep that itself carries a dep, as many levels deep
    as the dependency graph needs. Five-level chain here as a
    sanity check."""
    five_deep = ModuleScope(
        {
            "A": ModuleScope(
                {
                    "B": ModuleScope(
                        {
                            "C": ModuleScope(
                                {
                                    "D": ModuleScope(
                                        {
                                            "E": ModuleScope(
                                                {"index.js": "export const v = 5;"}
                                            ),
                                            "index.js": (
                                                'import { v } from "E";'
                                                " export const d = v + 1;"
                                            ),
                                        }
                                    ),
                                    "index.js": (
                                        'import { d } from "D";'
                                        " export const c = d + 1;"
                                    ),
                                }
                            ),
                            "index.js": (
                                'import { c } from "C";'
                                " export const b = c + 1;"
                            ),
                        }
                    ),
                    "index.js": (
                        'import { b } from "B";'
                        " export const a = b + 1;"
                    ),
                }
            )
        }
    )
    assert isinstance(five_deep.modules["A"], ModuleScope)


def test_scope_with_self_peer_dependency_is_valid() -> None:
    """§3.1: a scope can carry a ModuleScope-valued entry keyed the
    same way at multiple depths (self-containment via spreading).
    This is how shared deps travel into each scope that needs them."""
    utils = {
        "@agent/utils": ModuleScope(
            {"index.js": "export function id(x) { return x; }"}
        )
    }
    scope = ModuleScope(
        {
            **utils,
            "@agent/fs": ModuleScope(
                {
                    **utils,
                    "index.js": (
                        'import { id } from "@agent/utils";'
                        " export const p = id;"
                    ),
                }
            ),
        }
    )
    outer = scope.modules["@agent/utils"]
    fs = scope.modules["@agent/fs"]
    assert isinstance(outer, ModuleScope)
    assert isinstance(fs, ModuleScope)
    assert isinstance(fs.modules["@agent/utils"], ModuleScope)


def test_two_namespaces_coexist_same_key() -> None:
    """§3.1: a ModuleScope may legally have `"index.js"` as a str
    AND as a ModuleScope value side-by-side. The two are different
    namespaces — `./index.js` finds the str, bare `index.js` finds
    the ModuleScope. Unusual but legal and non-ambiguous."""
    scope = ModuleScope(
        {
            "@agent/fs": ModuleScope(
                {
                    "index.js": "export const main = true;",
                    # Side-by-side: different namespace. (Yes, the
                    # bare specifier `"index.js"` is an odd dep name,
                    # but it's legal and mirrors the strict two-namespace
                    # rule.)
                    "helpers": ModuleScope(
                        {"index.js": "export const H = 1;"}
                    ),
                }
            )
        }
    )
    fs = scope.modules["@agent/fs"]
    assert isinstance(fs, ModuleScope)
    assert isinstance(fs.modules["index.js"], str)
    assert isinstance(fs.modules["helpers"], ModuleScope)


# ---- Invalid construction -------------------------------------------


def test_scope_with_str_entries_missing_index_js_raises() -> None:
    """§3.1: a ModuleScope with any `str` entries must have
    `'index.js'` — that's what a bare `import ... from 'scope-name'`
    resolves to."""
    with pytest.raises(ValueError, match="missing required 'index.js'"):
        ModuleScope(
            {"@agent/fs": ModuleScope({"helpers.js": "export default 1;"})}
        )


def test_nested_scope_missing_index_js_at_top_level_raises() -> None:
    """The top-level scope itself: if it has any str entries but no
    index.js, that's invalid too. (A pure-dependency root is
    separately valid — tested above.)"""
    with pytest.raises(ValueError, match="missing required 'index.js'"):
        ModuleScope({"loose.js": "export default 1;"})


def test_key_starting_with_dot_slash_raises() -> None:
    """§3.1: keys are file paths or bare import names, never
    relative specifiers. `./local` is an import specifier valid
    inside JS source, not as a dict key."""
    with pytest.raises(ValueError, match="relative specifier"):
        ModuleScope({"./local": "export default 1;"})


def test_key_starting_with_dot_dot_slash_raises() -> None:
    """Same rule as `./` — `../` is never a valid key anywhere."""
    with pytest.raises(ValueError, match="relative specifier"):
        ModuleScope({"../parent": "export default 1;"})


def test_nested_key_starting_with_dot_slash_raises() -> None:
    """The ./ / ../ key rule applies at every nesting level, not
    just the top. A nested scope that holds a key starting with
    ./ is the same error shape as at root."""
    with pytest.raises(ValueError, match="relative specifier"):
        ModuleScope(
            {
                "@agent/fs": ModuleScope(
                    {
                        "index.js": "export default 1;",
                        "./sneaky": "nope",
                    }
                )
            }
        )


def test_non_string_non_scope_value_raises() -> None:
    """Values must be str | ModuleScope. Anything else is a Python
    programming error — not 'wrong JS,' wrong type."""
    with pytest.raises(TypeError, match="must be str or ModuleScope"):
        ModuleScope({"@agent/fs": 42})  # type: ignore[dict-item]


def test_nested_non_string_non_scope_value_raises() -> None:
    """Same rule nested: the two-type restriction holds at every
    level, applied by each ModuleScope's __post_init__."""
    with pytest.raises(TypeError, match="must be str or ModuleScope"):
        ModuleScope(
            {
                "@agent/fs": ModuleScope(
                    {
                        "index.js": "export default 1;",
                        "bad.js": 123,  # type: ignore[dict-item]
                    }
                )
            }
        )


# ---- Immutability ---------------------------------------------------


def test_modules_dict_is_read_only() -> None:
    """§3.1 describes ModuleScope as frozen. Mutating `.modules`
    after construction would let a caller smuggle in invalid
    entries, bypassing the validation above — block it."""
    scope = ModuleScope(
        {"@a": ModuleScope({"index.js": "export const X = 1;"})}
    )
    with pytest.raises(TypeError):
        scope.modules["@b"] = "export const Y = 2;"  # type: ignore[index]


def test_scope_instance_is_frozen_at_dataclass_level() -> None:
    """Dataclass(frozen=True) — reassigning the modules attribute
    itself also fails, not just mutating the dict contents."""
    scope = ModuleScope(
        {"@a": ModuleScope({"index.js": "export const X = 1;"})}
    )
    with pytest.raises(FrozenInstanceError):
        scope.modules = {}  # type: ignore[misc]


def test_constructor_takes_defensive_copy() -> None:
    """Passing a dict in and mutating it afterwards must NOT affect
    the scope — validation happens once, at construction time, and
    then the scope snapshot is immutable."""
    raw: dict[str, object] = {
        "@a": ModuleScope({"index.js": "export const X = 1;"})
    }
    scope = ModuleScope(raw)  # type: ignore[arg-type]
    raw["@sneaky"] = "this should not appear"
    assert "@sneaky" not in scope.modules


# ---- Composition patterns — §3.4 ------------------------------------


def test_dict_spread_extend() -> None:
    """Spread an existing scope's modules into a new dict to add
    entries. The new ModuleScope re-validates on construction."""
    base = ModuleScope(
        {"@a": ModuleScope({"index.js": "export const X = 1;"})}
    )
    extended = ModuleScope(
        {
            **base.modules,
            "@b": ModuleScope({"index.js": "export const Y = 2;"}),
        }
    )
    assert set(extended.modules.keys()) == {"@a", "@b"}
    # Original untouched.
    assert set(base.modules.keys()) == {"@a"}


def test_dict_spread_override() -> None:
    """Later keys win in a dict spread — the canonical way to
    override one module in a composed scope."""
    base = ModuleScope(
        {"@a": ModuleScope({"index.js": "export const X = 1;"})}
    )
    overridden = ModuleScope(
        {
            **base.modules,
            "@a": ModuleScope({"index.js": "export const X = 99;"}),
        }
    )
    inner = overridden.modules["@a"]
    assert isinstance(inner, ModuleScope)
    assert inner.modules["index.js"].endswith("99;")


def test_dict_comprehension_filter() -> None:
    """Removal via comprehension — no dedicated remove method."""
    full = ModuleScope(
        {
            "@a": ModuleScope({"index.js": "export const X = 1;"}),
            "@b": ModuleScope({"index.js": "export const Y = 2;"}),
            "@c": ModuleScope({"index.js": "export const Z = 3;"}),
        }
    )
    filtered = ModuleScope(
        {k: v for k, v in full.modules.items() if k != "@b"}
    )
    assert set(filtered.modules.keys()) == {"@a", "@c"}


def test_merge_independent_scopes() -> None:
    """Two scopes authored independently can be combined by
    spreading both into a fresh ModuleScope."""
    team_a = ModuleScope(
        {"@a/lib": ModuleScope({"index.js": "export const A = 1;"})}
    )
    team_b = ModuleScope(
        {"@b/lib": ModuleScope({"index.js": "export const B = 2;"})}
    )
    combined = ModuleScope({**team_a.modules, **team_b.modules})
    assert set(combined.modules.keys()) == {"@a/lib", "@b/lib"}


def test_composition_preserves_nested_scopes() -> None:
    """Spread on a scope with nested ModuleScope values must keep
    those values intact — validation on the new scope sees the same
    nested structure and accepts it."""
    base = ModuleScope(
        {
            "@agent/utils": ModuleScope(
                {
                    "index.js": "export const X = 1;",
                    "helpers.js": "export const Y = 2;",
                }
            )
        }
    )
    extended = ModuleScope(
        {
            **base.modules,
            "@new": ModuleScope({"index.js": "export const Z = 3;"}),
        }
    )
    inner = extended.modules["@agent/utils"]
    assert isinstance(inner, ModuleScope)
    assert "helpers.js" in inner.modules


def test_recursive_spread_creates_self_contained_subscope() -> None:
    """§3.4: spreading a utils dict into a nested scope creates a
    self-contained subscope that carries the shared dep. Each
    spread is an independent canonical path at install time."""
    utils = {
        "@agent/utils": ModuleScope(
            {"index.js": "export const U = 1;"}
        )
    }
    main = ModuleScope(
        {
            **utils,
            "@agent/fs": ModuleScope(
                {
                    **utils,
                    "index.js": (
                        'import { U } from "@agent/utils";'
                        " export const F = U;"
                    ),
                }
            ),
            "@agent/http": ModuleScope(
                {
                    **utils,
                    "index.js": (
                        'import { U } from "@agent/utils";'
                        " export const H = U;"
                    ),
                }
            ),
        }
    )
    # Each scope that uses @agent/utils carries its own copy.
    fs = main.modules["@agent/fs"]
    http = main.modules["@agent/http"]
    assert isinstance(fs, ModuleScope) and "@agent/utils" in fs.modules
    assert isinstance(http, ModuleScope) and "@agent/utils" in http.modules


# ---- End-to-end install + import — §5.2 / §5.3 ----------------------
#
# Three assertions covering each resolver sub-case: bare specifier
# into a ModuleScope dep, relative specifier to a str sibling, and
# POSIX-path traversal (../) within a scope. If all three pass,
# steps 4 and 5 of the implementation order are already covered —
# the resolver handles relative + bare from the start, and the
# boundary tests reduce to negative cases against the same code
# path.


async def test_install_bare_specifier_from_eval() -> None:
    """§4 bare-specifier path. Root scope carries a ModuleScope dep
    `@agent/config`; top-level module eval imports it via bare
    specifier. Resolver looks up `@agent/config` in the root
    scope's subscope set, resolves to `@agent/config/index.js`,
    loader serves the source, QuickJS evaluates.

    Note on shape: a single-file dep is wrapped in a ModuleScope
    (per spec/module-loading.md §3.1 and bb6b2fd). A bare str at
    scope root with no index.js sibling fails ModuleScope
    validation — the wrap is the canonical shape.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.install(
                ModuleScope(
                    {
                        "@agent/config": ModuleScope(
                            {"index.js": "export const MAX_RETRIES = 3;"}
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { MAX_RETRIES } from "@agent/config";
                globalThis.r1 = MAX_RETRIES;
                """,
                module=True,
            )
            assert ctx.eval("r1") == 3


async def test_install_relative_specifier_within_scope() -> None:
    """§4 relative-specifier path. `@agent/utils/index.js` imports
    `./helpers.js`, resolver normalizes to `helpers.js` within the
    scope and serves it from the str entries. Exercises the
    scope-local resolver: `./helpers.js` from inside the scope
    reaches only the scope's own files, not any sibling.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.install(
                ModuleScope(
                    {
                        "@agent/utils": ModuleScope(
                            {
                                "index.js": (
                                    'import { lower } from "./helpers.js";'
                                    " export { lower };"
                                ),
                                "helpers.js": (
                                    "export function lower(s)"
                                    " { return s.toLowerCase(); }"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { lower } from "@agent/utils";
                globalThis.r2 = lower("HELLO");
                """,
                module=True,
            )
            assert ctx.eval("r2") == "hello"


async def test_install_posix_subdirectory_and_parent_traversal() -> None:
    """§4 POSIX traversal. `@agent/app/lib/greet.js` imports
    `../index.js` — normalizer resolves `lib/../index.js` to
    `index.js` within the scope, which is a valid str entry. The
    scope root is the ceiling (a further `../` would escape); this
    case stays within bounds.

    Exercises the subdirectory-path str key (`lib/greet.js`) and
    the POSIX normalization pipeline in one shot.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # `@agent/app`'s index.js is what a bare
            # `import ... from "@agent/app"` resolves to. It
            # re-exports `greet` from `./lib/greet.js`, which in
            # turn reaches up to `../index.js` (= `index.js` at
            # scope root) to import `ROOT`. Exercises both
            # directions of POSIX traversal within a scope.
            ctx.install(
                ModuleScope(
                    {
                        "@agent/app": ModuleScope(
                            {
                                "index.js": (
                                    "export const ROOT = 'root';"
                                    ' export { greet } from "./lib/greet.js";'
                                ),
                                "lib/greet.js": (
                                    'import { ROOT } from "../index.js";'
                                    " export function greet()"
                                    " { return ROOT + '!'; }"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { greet } from "@agent/app";
                globalThis.r3 = greet();
                """,
                module=True,
            )
            # Expected: "root!" — lib/greet.js walked up to
            # ../index.js successfully.
            assert ctx.eval("r3") == "root!"
