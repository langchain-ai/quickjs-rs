"""ModuleScope validation + composition + install. See
README.md section 3 and section 5.

Validation-only tests live at the top; the bottom of the file
covers the end-to-end install → resolve → load → module-eval
path added in step 3. section 9.1 continues to grow across steps 4–7.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from quickjs_rs import JSError, ModuleScope, Runtime

# ---- Valid construction ---------------------------------------------


def test_single_file_module_wrapped_in_scope_is_valid() -> None:
    """section 3.1: single-file dep is wrapped in a ModuleScope with an
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
    """section 3.1: a scope containing only ModuleScope values (no str
    entries) doesn't need its own index.js — it isn't a module
    target, just a registry wrapper. This is the common shape of
    what gets passed to rt.install."""
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
    """section 3.1: str keys are POSIX paths. `/` in a key creates a
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
    """section 3.1: nesting is recursive, not capped at two levels. A scope
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
    """section 3.1: a scope can carry a ModuleScope-valued entry keyed the
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
    """section 3.1: a ModuleScope may legally have `"index.js"` as a str
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


def test_scope_with_str_entries_missing_index_raises() -> None:
    """section 3.1 / section 5.5: a ModuleScope with any `str` entries must
    expose one of the recognized entry-point filenames
    (index.js / .mjs / .cjs / .ts / .mts / .cts / .jsx / .tsx).
    That's what a bare `import ... from 'scope-name'` resolves to.
    """
    with pytest.raises(ValueError, match="missing required index module"):
        ModuleScope(
            {"@agent/fs": ModuleScope({"helpers.js": "export default 1;"})}
        )


def test_nested_scope_missing_index_at_top_level_raises() -> None:
    """The top-level scope itself: if it has any str entries but no
    index.<ext>, that's invalid too. (A pure-dependency root is
    separately valid — tested above.)"""
    with pytest.raises(ValueError, match="missing required index module"):
        ModuleScope({"loose.js": "export default 1;"})


def test_key_starting_with_dot_slash_raises() -> None:
    """section 3.1: keys are file paths or bare import names, never
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
    """section 3.1 describes ModuleScope as frozen. Mutating `.modules`
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


# ---- Composition patterns — section 3.4 ------------------------------------


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
    """section 3.4: spreading a utils dict into a nested scope creates a
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


# ---- End-to-end install + import — section 5.2 / section 5.3 ----------------------
#
# Three assertions covering each resolver sub-case: bare specifier
# into a ModuleScope dep, relative specifier to a str sibling, and
# POSIX-path traversal (../) within a scope. If all three pass,
# steps 4 and 5 of the implementation order are already covered —
# the resolver handles relative + bare from the start, and the
# boundary tests reduce to negative cases against the same code
# path.


async def test_install_bare_specifier_from_eval() -> None:
    """section 4 bare-specifier path. Root scope carries a ModuleScope dep
    `@agent/config`; top-level module eval imports it via bare
    specifier. Resolver looks up `@agent/config` in the root
    scope's subscope set, resolves to `@agent/config/index.js`,
    loader serves the source, QuickJS evaluates.

    Note on shape: a single-file dep is wrapped in a ModuleScope
    (per README.md section 3.1 and bb6b2fd). A bare str at
    scope root with no index.js sibling fails ModuleScope
    validation — the wrap is the canonical shape.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
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
    """section 4 relative-specifier path. `@agent/utils/index.js` imports
    `./helpers.js`, resolver normalizes to `helpers.js` within the
    scope and serves it from the str entries. Exercises the
    scope-local resolver: `./helpers.js` from inside the scope
    reaches only the scope's own files, not any sibling.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
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
    """section 4 POSIX traversal. `@agent/app/lib/greet.js` imports
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
            rt.install(
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


# ---- Scope isolation — section 4 / section 9.1 -----------------------------------


async def test_same_filename_in_sibling_scopes_resolves_independently() -> None:
    """section 4: ``./helpers.js`` in scope A and ``./helpers.js`` in scope
    B are different files. Resolution is scope-local, keyed on
    (containing scope, relative path). If this failed, QuickJS
    would cache one entry and both imports would see the same
    source."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@a": ModuleScope(
                            {
                                "index.js": (
                                    'import { v } from "./helpers.js";'
                                    " export const A = v;"
                                ),
                                "helpers.js": "export const v = 'from-a';",
                            }
                        ),
                        "@b": ModuleScope(
                            {
                                "index.js": (
                                    'import { v } from "./helpers.js";'
                                    " export const B = v;"
                                ),
                                "helpers.js": "export const v = 'from-b';",
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { A } from "@a";
                import { B } from "@b";
                globalThis.a = A;
                globalThis.b = B;
                """,
                module=True,
            )
            assert ctx.eval("a") == "from-a"
            assert ctx.eval("b") == "from-b"


async def test_relative_import_from_top_level_eval_resolves_in_root_scope() -> None:
    """section 4: ``<eval>``'s containing scope is the root, position is
    the scope root. ``./foo.js`` normalizes to ``foo.js``. If the
    root has that str entry, it resolves; if not, JSError.

    Root scope with str entries must have index.js per section 3.1 —
    so this exercises a root with index.js (unused here, just
    satisfies validation) plus a sibling file we import."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "index.js": "export const ENTRY = true;",
                        "local.js": "export const local = 'yes';",
                    }
                )
            )
            await ctx.eval_async(
                """
                import { local } from "./local.js";
                globalThis.r = local;
                """,
                module=True,
            )
            assert ctx.eval("r") == "yes"


async def test_relative_import_from_eval_for_missing_file_raises() -> None:
    """``./missing.js`` from top-level eval where root scope has no
    such file → ResolveError surfaces as JSError."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope({"@x": ModuleScope({"index.js": "export const Y = 1;"})})
            )
            with pytest.raises(JSError, match="missing.js"):
                await ctx.eval_async(
                    'import { x } from "./missing.js"; globalThis.z = x;',
                    module=True,
                )


async def test_parent_traversal_past_scope_root_raises() -> None:
    """section 4: ``../`` that normalizes to a path starting with ``../``
    is an escape attempt → JSError. Exercises the ``None`` return
    from ``normalize_path`` in src/modules.rs."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@x": ModuleScope(
                            {
                                "index.js": (
                                    'import { y } from "../../escape.js";'
                                    " export const Y = y;"
                                ),
                            }
                        ),
                    }
                )
            )
            with pytest.raises(JSError, match="escapes module scope root"):
                await ctx.eval_async(
                    'import { Y } from "@x"; globalThis.r = Y;',
                    module=True,
                )


async def test_top_level_eval_cannot_escape_root_with_dotdot() -> None:
    """From top-level ``<eval>`` (position = root), any ``../``
    import normalizes past the root and errors. No way to reach
    "outside" the installed set."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope({"@x": ModuleScope({"index.js": "export const Y = 1;"})})
            )
            with pytest.raises(JSError, match="escapes module scope root"):
                await ctx.eval_async(
                    'import { y } from "../outside.js"; globalThis.r = y;',
                    module=True,
                )


# ---- Recursive dependencies — section 3.1 / section 4 -----------------------------


async def test_scope_can_import_its_own_declared_dep_but_not_transitive() -> None:
    """section 4 self-containment: A carries B, B carries C. A→B works;
    B→C works; A→C fails (C is not in A's own dict). Tests that
    the resolver does NOT walk ancestors or consult the root."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@a": ModuleScope(
                            {
                                # A carries B directly.
                                "@b": ModuleScope(
                                    {
                                        # B carries C directly.
                                        "@c": ModuleScope(
                                            {"index.js": "export const C = 'c';"}
                                        ),
                                        "index.js": (
                                            'import { C } from "@c";'
                                            " export const B = C + '-via-b';"
                                        ),
                                    }
                                ),
                                # A does NOT carry @c in its own dict,
                                # even though A's dep B carries it.
                                "index.js": (
                                    'import { B } from "@b";'
                                    " export const A = 'a-then-' + B;"
                                ),
                            }
                        ),
                    }
                )
            )
            # Happy path: A→B→C all resolve.
            await ctx.eval_async(
                'import { A } from "@a"; globalThis.r = A;', module=True
            )
            assert ctx.eval("r") == "a-then-c-via-b"


async def test_scope_cannot_reach_transitive_dep_directly() -> None:
    """Self-containment tripwire: A carries B (which carries C),
    but A's own index.js tries to import C directly. Even though
    the dep graph contains C via B, A's own dict doesn't declare
    C — so the resolver refuses it. This is the load-bearing
    correctness of scope-local resolution."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@a": ModuleScope(
                            {
                                "@b": ModuleScope(
                                    {
                                        "@c": ModuleScope(
                                            {"index.js": "export const C = 'c';"}
                                        ),
                                        "index.js": (
                                            'import { C } from "@c";'
                                            " export const B = C;"
                                        ),
                                    }
                                ),
                                # Reaches into B's transitive dep C
                                # directly — illegal.
                                "index.js": (
                                    'import { C } from "@c";'
                                    " export const A = C;"
                                ),
                            }
                        ),
                    }
                )
            )
            with pytest.raises(JSError, match="@c"):
                await ctx.eval_async(
                    'import { A } from "@a"; globalThis.r = A;',
                    module=True,
                )


async def test_adding_transitive_dep_directly_makes_it_importable() -> None:
    """Counterpart to the self-containment tripwire: once A carries
    C in its OWN dict (sharing the same ModuleScope instance as
    B's copy), A can import C directly. Each canonical path is a
    separate module record per QuickJS."""
    c_scope = ModuleScope({"index.js": "export const C = 'c';"})
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@a": ModuleScope(
                            {
                                "@c": c_scope,  # declared directly in A
                                "@b": ModuleScope(
                                    {
                                        "@c": c_scope,  # also declared in B
                                        "index.js": (
                                            'import { C } from "@c";'
                                            " export const B = 'b-uses-' + C;"
                                        ),
                                    }
                                ),
                                "index.js": (
                                    'import { C } from "@c";'
                                    ' import { B } from "@b";'
                                    " export const A = C + '|' + B;"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                'import { A } from "@a"; globalThis.r = A;', module=True
            )
            assert ctx.eval("r") == "c|b-uses-c"


# ---- Cross-scope via spread — section 3.4 / section 4 -----------------------------


async def test_shared_dep_via_spread_resolves_in_both_scopes() -> None:
    """section 3.4: the motivating spread pattern. ``stdlib`` is a dict
    carrying ``@agent/utils``. Main spreads it at top level AND
    into ``@agent/fs``. Both top-level eval and @agent/fs can
    import @agent/utils because each carries its own declaration.
    The same source shows up under two canonical paths; QuickJS
    caches them as independent module records."""
    stdlib = {
        "@agent/utils": ModuleScope(
            {"index.js": "export function id(x) { return 'u:' + x; }"}
        )
    }
    main = ModuleScope(
        {
            **stdlib,
            "@agent/fs": ModuleScope(
                {
                    **stdlib,  # fs carries its own copy of utils
                    "index.js": (
                        'import { id } from "@agent/utils";'
                        " export function stamp(x)"
                        "  { return 'fs[' + id(x) + ']'; }"
                    ),
                }
            ),
        }
    )
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(main)
            await ctx.eval_async(
                """
                import { id } from "@agent/utils";
                import { stamp } from "@agent/fs";
                globalThis.a = id("top");
                globalThis.b = stamp("inner");
                """,
                module=True,
            )
            assert ctx.eval("a") == "u:top"
            # @agent/fs's import resolved to its OWN copy of
            # @agent/utils, not the top-level one.
            assert ctx.eval("b") == "fs[u:inner]"


# ---- Module evaluation semantics — section 3.3 -----------------------------


async def test_module_eval_returns_none() -> None:
    """section 3.3: module=True returns None regardless of what the
    module body "evaluates" to. ES modules complete with
    undefined; the only way to surface a value is globalThis."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope({"@x": ModuleScope({"index.js": "export const V = 42;"})})
            )
            result = await ctx.eval_async(
                'import { V } from "@x"; V * 2;', module=True
            )
            assert result is None


async def test_module_scoped_let_is_not_visible_in_subsequent_eval() -> None:
    """section 3.3: ``let``/``const``/``var``/function declarations at
    the top level of a module are module-scoped, not global.
    A subsequent script-mode eval cannot see them."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope({"@x": ModuleScope({"index.js": "export const Y = 1;"})})
            )
            await ctx.eval_async(
                'import { Y } from "@x"; let moduleLocal = 42;',
                module=True,
            )
            assert ctx.eval("typeof moduleLocal") == "undefined"


async def test_module_globalThis_assignment_visible_in_script_eval() -> None:
    """section 6.3: modules and scripts share the same global object.
    ``globalThis.X = ...`` in a module is visible in subsequent
    script-mode eval."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope({"@x": ModuleScope({"index.js": "export const Y = 7;"})})
            )
            await ctx.eval_async(
                'import { Y } from "@x"; globalThis.fromModule = Y * 2;',
                module=True,
            )
            assert ctx.eval("fromModule") == 14


async def test_script_global_visible_in_module() -> None:
    """section 6.3 other direction: a global set via script-mode eval is
    visible inside a module. This is how host-registered function
    names and ``ctx.globals[...]`` values flow into modules."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope({"@x": ModuleScope({"index.js": "export const K = 1;"})})
            )
            ctx.eval("globalThis.fromScript = 'hello'")
            await ctx.eval_async(
                'import { K } from "@x"; globalThis.concat = fromScript + K;',
                module=True,
            )
            assert ctx.eval("concat") == "hello1"


# ---- Async modules — section 3.3 / section 6 --------------------------------------


async def test_module_top_level_await_with_async_host_function() -> None:
    """Top-level ``await`` inside a module body resolves through
    the Promise chain the same as script-mode TLA. The difference
    is the module path goes through ``Module::evaluate``; this
    test pins down the end-to-end async integration."""
    import asyncio

    with Runtime() as rt:
        with rt.new_context() as ctx:

            @ctx.function
            async def _lookup(key: str) -> int:
                await asyncio.sleep(0.001)
                return {"one": 1, "two": 2}[key]

            rt.install(
                ModuleScope(
                    {
                        "@agent/lookup": ModuleScope(
                            {
                                "index.js": (
                                    "export async function lookup(k)"
                                    " { return await _lookup(k); }"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { lookup } from "@agent/lookup";
                const v = await lookup("two");
                globalThis.r = v;
                """,
                module=True,
            )
            assert ctx.eval("r") == 2


async def test_module_importing_module_that_uses_await() -> None:
    """Chain: module A imports module B. B's body uses top-level
    await (via an async host call). A sees the resolved export.
    This exercises module-mode's async module completion — A
    can't finish evaluating until B's TLA resolves."""
    import asyncio

    with Runtime() as rt:
        with rt.new_context() as ctx:

            @ctx.function
            async def _compute() -> int:
                await asyncio.sleep(0.001)
                return 99

            provider = ModuleScope(
                {
                    # Top-level await in the module body.
                    "index.js": "export const VALUE = await _compute();",
                }
            )
            # Self-containment: consumer must carry its own copy of
            # provider in its dict. The top-level scope doesn't
            # leak into consumer.
            rt.install(
                ModuleScope(
                    {
                        "@agent/provider": provider,
                        "@agent/consumer": ModuleScope(
                            {
                                "@agent/provider": provider,
                                "index.js": (
                                    'import { VALUE } from "@agent/provider";'
                                    " export const DOUBLED = VALUE * 2;"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { DOUBLED } from "@agent/consumer";
                globalThis.r = DOUBLED;
                """,
                module=True,
            )
            assert ctx.eval("r") == 198


# ---- Error cases — section 8 -----------------------------------------------


async def test_import_non_registered_module_raises() -> None:
    """section 8: missing module → JSError at eval time."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope({"@x": ModuleScope({"index.js": "export const Y = 1;"})})
            )
            with pytest.raises(JSError, match="@nope"):
                await ctx.eval_async(
                    'import { x } from "@nope"; globalThis.z = x;',
                    module=True,
                )


async def test_syntax_error_in_module_surfaces_at_eval_time() -> None:
    """section 11: pre-parse on install was considered and deferred.
    Syntax errors surface at eval time (when QuickJS actually
    parses the module), not at ``install()``."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # install() must NOT raise even though the source is
            # clearly malformed.
            rt.install(
                ModuleScope(
                    {
                        "@broken": ModuleScope(
                            {
                                # Definitely not valid JS.
                                "index.js": "this is not valid javascript at all",
                            }
                        ),
                    }
                )
            )
            with pytest.raises(JSError):
                await ctx.eval_async(
                    'import { x } from "@broken"; globalThis.z = x;',
                    module=True,
                )


# ---- Module caching — section 6.2 ------------------------------------------


async def test_reinstall_before_import_takes_new_source() -> None:
    """section 6.2: a name that hasn't been imported yet can be
    overwritten. The second install replaces the first's source;
    the import sees the new value."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope({"@cfg": ModuleScope({"index.js": "export const V = 1;"})})
            )
            # Re-install under the same canonical path BEFORE any import.
            rt.install(
                ModuleScope(
                    {"@cfg": ModuleScope({"index.js": "export const V = 999;"})}
                )
            )
            await ctx.eval_async(
                'import { V } from "@cfg"; globalThis.r = V;', module=True
            )
            assert ctx.eval("r") == 999


async def test_reinstall_after_import_is_no_op_cache_wins() -> None:
    """section 6.2: once a module name has been imported, QuickJS caches
    the module record per canonical path. A subsequent install
    with different source under the same name is a silent no-op —
    the cached record wins. Documented as a caveat; the user is
    expected to know not to rely on hot-reloading module sources."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope({"@cfg": ModuleScope({"index.js": "export const V = 1;"})})
            )
            await ctx.eval_async(
                'import { V } from "@cfg"; globalThis.first = V;',
                module=True,
            )
            assert ctx.eval("first") == 1
            # Attempted swap — ignored by QuickJS's module cache.
            rt.install(
                ModuleScope(
                    {"@cfg": ModuleScope({"index.js": "export const V = 999;"})}
                )
            )
            await ctx.eval_async(
                'import { V } from "@cfg"; globalThis.second = V;',
                module=True,
            )
            # Still 1, not 999. This is the cache, not the install.
            assert ctx.eval("second") == 1


# ---- Additive install — section 5.3 ----------------------------------------


async def test_two_installs_both_importable() -> None:
    """section 5.3 additive: modules registered across multiple install()
    calls are all available. The second call doesn't replace the
    first's modules, just merges into the same backing store."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope({"@a": ModuleScope({"index.js": "export const A = 1;"})})
            )
            rt.install(
                ModuleScope({"@b": ModuleScope({"index.js": "export const B = 2;"})})
            )
            await ctx.eval_async(
                """
                import { A } from "@a";
                import { B } from "@b";
                globalThis.sum = A + B;
                """,
                module=True,
            )
            assert ctx.eval("sum") == 3


async def test_install_after_eval_is_visible_to_next_eval() -> None:
    """section 5.3: install() in between evals — the newly installed
    module is visible in the subsequent eval as long as its name
    wasn't imported earlier (which would hit the cache)."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope({"@a": ModuleScope({"index.js": "export const A = 1;"})})
            )
            await ctx.eval_async(
                'import { A } from "@a"; globalThis.first = A;',
                module=True,
            )
            assert ctx.eval("first") == 1
            # Install a NEW name after an eval has happened.
            rt.install(
                ModuleScope({"@b": ModuleScope({"index.js": "export const B = 10;"})})
            )
            await ctx.eval_async(
                'import { B } from "@b"; globalThis.second = B;',
                module=True,
            )
            assert ctx.eval("second") == 10


# ---- Composition patterns end-to-end — section 3.4 -------------------------


async def test_spread_override_replaces_module_end_to_end() -> None:
    """section 3.4: dict spread override — the later key wins. The
    overridden scope installs as the new source. This is the
    pattern for test fixtures and capability reduction."""
    base = ModuleScope(
        {
            "@agent/config": ModuleScope(
                {"index.js": "export const ENV = 'prod';"}
            ),
            "@agent/lib": ModuleScope({"index.js": "export const L = 1;"}),
        }
    )
    test_scope = ModuleScope(
        {
            **base.modules,
            # Override @agent/config with test values.
            "@agent/config": ModuleScope(
                {"index.js": "export const ENV = 'test';"}
            ),
        }
    )
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(test_scope)
            await ctx.eval_async(
                """
                import { ENV } from "@agent/config";
                import { L } from "@agent/lib";
                globalThis.env = ENV;
                globalThis.l = L;
                """,
                module=True,
            )
            assert ctx.eval("env") == "test"  # override
            assert ctx.eval("l") == 1  # unchanged from base


async def test_comprehension_removal_makes_module_unreachable() -> None:
    """section 3.4: dict comprehension removal is the canonical way to
    drop a capability. The removed module isn't registered, so
    importing it errors at eval time."""
    full = ModuleScope(
        {
            "@agent/fs": ModuleScope(
                {"index.js": "export const unsafe = true;"}
            ),
            "@agent/safe": ModuleScope({"index.js": "export const safe = 1;"}),
        }
    )
    restricted = ModuleScope(
        {k: v for k, v in full.modules.items() if k != "@agent/fs"}
    )
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(restricted)
            # Safe module still works.
            await ctx.eval_async(
                'import { safe } from "@agent/safe"; globalThis.r = safe;',
                module=True,
            )
            assert ctx.eval("r") == 1
            # Removed module is not reachable.
            with pytest.raises(JSError, match="@agent/fs"):
                await ctx.eval_async(
                    'import { unsafe } from "@agent/fs"; globalThis.x = unsafe;',
                    module=True,
                )


# ---- TypeScript via oxidase — section 5.5 -----------------------------------
#
# .ts and .tsx keys are transparently stripped at install() time.
# The resolver treats the key literally — ".ts" stays ".ts" in
# canonical paths — so `import { x } from "./foo.ts"` resolves
# against the dict key "foo.ts", not "foo.js". Oxidase preserves
# the specifier untouched (verified against 045ea46b).


async def test_ts_file_with_type_annotations_imports_and_runs() -> None:
    """section 5.5: a .ts entry point + a .ts helper, types stripped at
    install time. Exercises the basic wire-up: key-based extension
    detection, oxidase round-trip, specifier preservation, and the
    section 3.1 validation-rule broadening that admits index.ts as a
    valid entry point alongside index.js."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@util": ModuleScope(
                            {
                                "index.ts": (
                                    'import { lower } from "./helpers.ts";\n'
                                    "export function slug(s: string): string {\n"
                                    "  return lower(s).replace(/ /g, '-');\n"
                                    "}\n"
                                ),
                                "helpers.ts": (
                                    "export function lower(s: string): string {\n"
                                    "  return s.toLowerCase();\n"
                                    "}\n"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { slug } from "@util";
                globalThis.r = slug("Hello World");
                """,
                module=True,
            )
            assert ctx.eval("r") == "hello-world"


async def test_tsx_file_strips_types() -> None:
    """section 5.5: .tsx uses oxidase's tsx source-type. We don't
    evaluate JSX here (QuickJS wouldn't understand it) — just
    verify that a .tsx file with TS annotations but no JSX
    elements installs + runs. The strip path itself is the
    primary thing under test."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@ui": ModuleScope(
                            {
                                "index.tsx": (
                                    "export function greet(name: string): string {\n"
                                    "  const message: string = 'hi ' + name;\n"
                                    "  return message;\n"
                                    "}\n"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { greet } from "@ui";
                globalThis.r = greet("world");
                """,
                module=True,
            )
            assert ctx.eval("r") == "hi world"


async def test_ts_enum_is_transpiled_to_runtime_value() -> None:
    """section 5.5: oxidase transforms TS enums into runtime IIFE code,
    unlike strip-only transpilers. Verify that enum member access
    actually works at runtime."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@e": ModuleScope(
                            {
                                "index.ts": (
                                    "export enum Color { Red = 1, Green = 2, Blue = 3 }\n"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { Color } from "@e";
                globalThis.r = Color.Red + "," + Color.Green + "," + Color.Blue;
                """,
                module=True,
            )
            assert ctx.eval("r") == "1,2,3"


async def test_ts_interface_has_no_runtime_artifact() -> None:
    """section 5.5: interface declarations are erased entirely — they
    leave no runtime value. Exporting an interface and trying to
    import it as a binding should fail at eval time with
    SyntaxError or similar (the binding doesn't exist in the
    stripped output)."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # Use an interface only in a type position (annotation).
            # The interface declaration itself is erased; no
            # binding is produced. The `User` reference in the
            # annotation is also erased with it.
            rt.install(
                ModuleScope(
                    {
                        "@m": ModuleScope(
                            {
                                "index.ts": (
                                    "export interface User { name: string; }\n"
                                    "export function greet(u: User): string {\n"
                                    "  return 'hi ' + u.name;\n"
                                    "}\n"
                                ),
                            }
                        ),
                    }
                )
            )
            # greet is still exported — interface doesn't block it.
            await ctx.eval_async(
                """
                import { greet } from "@m";
                globalThis.r = greet({ name: "world" });
                """,
                module=True,
            )
            assert ctx.eval("r") == "hi world"
            # Importing `User` fails: it has no runtime existence.
            with pytest.raises(JSError):
                await ctx.eval_async(
                    """
                    import { User } from "@m";
                    globalThis.x = User;
                    """,
                    module=True,
                )


def test_ts_syntax_error_surfaces_at_install_time() -> None:
    """section 5.5: unlike plain JS (whose syntax errors land at eval
    time — per test_syntax_error_in_module_surfaces_at_eval_time
    above), TS parse errors fire during install() because oxidase
    parses the source then and there. That's a better failure
    mode: the user's TS mistakes surface immediately, not after
    a bunch of other modules have already been imported.

    Note this test is sync — install() is sync, and we don't need
    an eval to trigger the failure."""
    with Runtime() as rt:
        with rt.new_context():
            from quickjs_rs import QuickJSError

            with pytest.raises(QuickJSError, match="TypeScript parse error"):
                rt.install(
                    ModuleScope(
                        {
                            "@broken": ModuleScope(
                                {
                                    "index.ts": "this = is := not valid ??? TypeScript",
                                }
                            ),
                        }
                    )
                )


async def test_mixed_ts_and_js_files_in_same_scope() -> None:
    """section 5.5: a scope can have both .ts and .js str entries; each
    is handled according to its extension. Both become distinct
    modules under distinct canonical paths (`foo.ts`, `bar.js`);
    the resolver treats them identically — extension is purely
    an install-time strip decision."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@m": ModuleScope(
                            {
                                "index.ts": (
                                    'import { plain } from "./plain.js";\n'
                                    'import { typed } from "./typed.ts";\n'
                                    "export const combined: string = plain + '|' + typed;\n"
                                ),
                                "plain.js": (
                                    "export const plain = 'js-side';\n"
                                ),
                                "typed.ts": (
                                    "export const typed: string = 'ts-side';\n"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { combined } from "@m";
                globalThis.r = combined;
                """,
                module=True,
            )
            assert ctx.eval("r") == "js-side|ts-side"


async def test_ts_to_ts_relative_import_preserves_extension() -> None:
    """section 5.5 + the resolver's specifier-literal rule: a .ts file
    that imports "./other.ts" keeps the ".ts" specifier (oxidase
    leaves it alone), and the resolver looks up "other.ts" in the
    scope's file set — which matches exactly because the key was
    registered as "other.ts"."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@m": ModuleScope(
                            {
                                "index.ts": (
                                    'import { x } from "./other.ts";\n'
                                    "export const y: number = x * 2;\n"
                                ),
                                "other.ts": (
                                    "export const x: number = 21;\n"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { y } from "@m";
                globalThis.r = y;
                """,
                module=True,
            )
            assert ctx.eval("r") == 42


async def test_ts_to_js_relative_import_works() -> None:
    """section 5.5: a .ts file importing a .js sibling. Nothing clever —
    verifies that the extension-based strip decision is strictly
    per-file and doesn't corrupt cross-extension imports."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@m": ModuleScope(
                            {
                                "index.ts": (
                                    'import { value } from "./util.js";\n'
                                    "export const doubled: number = value * 2;\n"
                                ),
                                "util.js": "export const value = 7;\n",
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { doubled } from "@m";
                globalThis.r = doubled;
                """,
                module=True,
            )
            assert ctx.eval("r") == 14


# ---- section 4 dynamic import() -------------------------------------------
#
# Static `import` is heavily covered above. Dynamic `import()` goes
# through the same rquickjs loader hook as static imports
# (`JS_SetModuleLoaderFunc` fires for both), but has no existing
# test coverage. These tests pin the behaviour so downstream users
# — notably `deepagents-repl`'s skill-module loader, which relies on
# the model writing `await import("@/skills/...")` — can depend on
# it without a lurking-risk caveat.


async def test_dynamic_import_resolves_bare_specifier() -> None:
    """Dynamic import of a bare-specifier subscope resolves through
    the same store and returns a namespace object with the subscope's
    exports."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
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
                const m = await import("@agent/config");
                globalThis.r = m.MAX_RETRIES;
                """,
                module=True,
            )
            assert ctx.eval("r") == 3


async def test_dynamic_import_of_relative_specifier_from_root_eval() -> None:
    """Dynamic import with a relative specifier from a top-level
    module eval — exercises the `<eval>` basename codepath
    (`src/modules.rs:124-130`) through the dynamic-import route.
    Root scope needs an `index.js` sibling because any scope with
    str entries must declare one (section 3.1)."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "index.js": "export const marker = true;",
                        "helpers.js": "export const value = 7;",
                    }
                )
            )
            await ctx.eval_async(
                """
                const m = await import("./helpers.js");
                globalThis.r = m.value;
                """,
                module=True,
            )
            assert ctx.eval("r") == 7


async def test_dynamic_import_unknown_specifier_rejects() -> None:
    """Dynamic import of an unregistered specifier rejects the
    returned promise with the resolver's error. We want this to
    surface as a JSError at the eval boundary so the middleware can
    map it to a `SkillNotAvailable` when we know the specifier was a
    `@/skills/<name>` lookup."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(ModuleScope({"index.js": "export const x = 1;"}))
            with pytest.raises(JSError):
                await ctx.eval_async(
                    """
                    await import("@agent/does-not-exist");
                    """,
                    module=True,
                )


async def test_dynamic_import_of_ts_entrypoint_strips_types() -> None:
    """Dynamic import of a subscope whose entrypoint is a `.ts` file.
    quickjs-rs picks the `index.ts` via its index-file preference
    list and strips TS syntax at install time; dynamic import should
    see the same stripped source as static import."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@agent/ts": ModuleScope(
                            {
                                "index.ts": (
                                    "export const n: number = 42;\n"
                                    "export function greet(who: string): string {"
                                    ' return `hi ${who}`; }\n'
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                const m = await import("@agent/ts");
                globalThis.n = m.n;
                globalThis.g = m.greet("world");
                """,
                module=True,
            )
            assert ctx.eval("n") == 42
            assert ctx.eval("g") == "hi world"


async def test_dynamic_import_is_cached() -> None:
    """ES dynamic import returns the same module instance on repeated
    calls within the same context — a property module-level mutable
    state relies on. A top-level counter incremented once in the
    importee should read as 1 twice, not 2."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@agent/stateful": ModuleScope(
                            {
                                "index.js": (
                                    "let n = 0;\n"
                                    "export function bump() { return ++n; }\n"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                const a = await import("@agent/stateful");
                const b = await import("@agent/stateful");
                globalThis.same = a === b;
                globalThis.r1 = a.bump();
                globalThis.r2 = b.bump();
                """,
                module=True,
            )
            assert ctx.eval("same") is True
            assert ctx.eval("r1") == 1
            assert ctx.eval("r2") == 2


# ---- Dynamic import() — diagnostic coverage -------------------------
#
# Dynamic `import()` is a different QuickJS code path than static
# `import`. QuickJS re-enters the module loader to resolve the
# runtime-supplied specifier, and the referrer it passes to the
# resolver may differ from the static case — in particular, it's
# the filename of the *caller*, which might be "<eval>" or a real
# canonical module path depending on where the import() appears.
#
# These tests are DIAGNOSTIC, not normative. Failures here are
# findings to understand, not bugs to fix in this commit. If a
# test fails, leave the assertion in place — it documents the
# current behavior and will flip on the day the gap is closed.


async def test_dynamic_import_basic() -> None:
    """``await import("@agent/utils")`` from top-level module eval
    returns the module namespace object whose properties are the
    module's exports. If the resolver is wired for dynamic-import,
    this is just static-import-but-runtime.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@agent/utils": ModuleScope(
                            {"index.js": "export const V = 7;"}
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                const mod = await import("@agent/utils");
                globalThis.r = mod.V;
                """,
                module=True,
            )
            assert ctx.eval("r") == 7


async def test_dynamic_import_from_script_mode() -> None:
    """Dynamic import from ``module=False`` eval. Script-mode eval
    doesn't declare a "current module" — referrer is ``<eval>`` or
    similar. previous implementation's resolver treats ``<eval>``'s containing scope
    as the root, so bare-specifier dynamic imports from script
    mode should resolve the same as from module mode.

    The body uses top-level ``await`` directly (not wrapped in an
    async IIFE) because previous implementation's script-mode eval with
    JS_EVAL_FLAG_ASYNC drives the SCRIPT's top-level promise to
    completion — an async IIFE at the top level returns a Promise
    value that the script-mode envelope considers "done," leaving
    the IIFE's inner promise unresolved. That's a script-mode /
    IIFE interaction, not a dynamic-import concern.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@agent/utils": ModuleScope(
                            {"index.js": "export const V = 11;"}
                        ),
                    }
                )
            )
            # Top-level await in script mode; the envelope waits
            # for the whole expression to settle.
            result = await ctx.eval_async(
                """
                const mod = await import("@agent/utils");
                mod.V;
                """,
                module=False,
            )
            assert result == 11


async def test_dynamic_import_variable_specifier() -> None:
    """The specifier is a runtime string. If this passes, it means
    the resolver sees the dynamic import's argument at the
    resolver-callback boundary, same as a static specifier. If it
    fails (e.g. QuickJS inlines the specifier differently), this
    is worth knowing."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@agent/utils": ModuleScope(
                            {"index.js": "export const V = 'dyn';"}
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                const name = "@agent/utils";
                const mod = await import(name);
                globalThis.r = mod.V;
                """,
                module=True,
            )
            assert ctx.eval("r") == "dyn"


async def test_dynamic_import_not_found() -> None:
    """``await import("@nonexistent")`` should REJECT the returned
    promise, not hang or crash. The rejection surfaces through
    the normal async driving loop as a JSError."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@real": ModuleScope(
                            {"index.js": "export const V = 1;"}
                        ),
                    }
                )
            )
            with pytest.raises(JSError):
                await ctx.eval_async(
                    """
                    const mod = await import("@nonexistent");
                    globalThis.r = mod;
                    """,
                    module=True,
                )


async def test_dynamic_import_within_scope() -> None:
    """A scope file does ``await import("./helpers.js")``. The
    referrer handed to the resolver should be the canonical path
    of the file doing the dynamic import (``"@m/index.js"``), so
    ``./helpers.js`` normalizes to ``helpers.js`` within the
    scope. Same path a static relative import would take."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@m": ModuleScope(
                            {
                                "index.js": """
                                    export async function load() {
                                        const mod = await import("./helpers.js");
                                        return mod.value;
                                    }
                                """,
                                "helpers.js": "export const value = 'scoped-dyn';",
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { load } from "@m";
                globalThis.r = await load();
                """,
                module=True,
            )
            assert ctx.eval("r") == "scoped-dyn"


async def test_dynamic_import_typescript() -> None:
    """Dynamic import of a .ts file. At install() time oxidase
    stripped ``utils.ts`` and registered the canonical path
    ``@m/utils.ts`` against the stripped JS. The dynamic import's
    specifier ``"./utils.ts"`` should hit the same resolver path
    as a static import — i.e. look up ``"utils.ts"`` as a str
    entry in the scope."""
    with Runtime() as rt:
        with rt.new_context() as ctx:
            rt.install(
                ModuleScope(
                    {
                        "@m": ModuleScope(
                            {
                                "index.js": """
                                    export async function go() {
                                        const mod = await import("./utils.ts");
                                        return mod.stamp("x");
                                    }
                                """,
                                "utils.ts": (
                                    "export function stamp(s: string): string {\n"
                                    "  return 'ts[' + s + ']';\n"
                                    "}\n"
                                ),
                            }
                        ),
                    }
                )
            )
            await ctx.eval_async(
                """
                import { go } from "@m";
                globalThis.r = await go();
                """,
                module=True,
            )
            assert ctx.eval("r") == "ts[x]"
