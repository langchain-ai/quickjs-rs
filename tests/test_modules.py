"""ModuleScope validation + composition. See spec/module-loading.md §3.

Step-2 coverage is construction-only: no Runtime, no Context, no
end-to-end eval. Step 3 adds the install + single-file-import
tests; this file grows to match §9.1 over steps 3–7.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from quickjs_rs import ModuleScope

# ---- Valid construction ---------------------------------------------


def test_single_file_module_is_valid() -> None:
    """Top-level string value — the simplest shape."""
    scope = ModuleScope({"lodash": "export function get(o, k) { return o[k]; }"})
    assert "lodash" in scope.modules
    assert scope.modules["lodash"].startswith("export function get")


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


def test_mixed_dict_is_valid() -> None:
    """Top-level can freely mix string and ModuleScope values."""
    scope = ModuleScope(
        {
            "@agent/config": "export const ENV = 'prod';",
            "@agent/fs": ModuleScope({"index.js": "export default 1;"}),
            "plain-lib": "export const Q = 42;",
        }
    )
    assert isinstance(scope.modules["@agent/config"], str)
    assert isinstance(scope.modules["@agent/fs"], ModuleScope)
    assert isinstance(scope.modules["plain-lib"], str)


def test_empty_scope_is_valid() -> None:
    """An empty registry is a degenerate but valid shape — useful as
    a base for composition. Validation only trips on shape, not size."""
    scope = ModuleScope({})
    assert len(scope.modules) == 0


# ---- Invalid construction -------------------------------------------


def test_nested_scope_without_index_js_raises() -> None:
    """§3.1: nested ModuleScope must expose its entry point via
    'index.js' — that's what a bare `import ... from 'name'`
    resolves to (§4 rule 2)."""
    with pytest.raises(ValueError, match="missing required 'index.js'"):
        ModuleScope({"@agent/fs": ModuleScope({"helpers.js": "..."})})


def test_nested_scope_with_slash_in_filename_raises() -> None:
    """§4 non-negotiable: nested scopes are flat. Subdirectories
    aren't representable — there's no filesystem, just a dict."""
    with pytest.raises(ValueError, match="contains '/'"):
        ModuleScope(
            {
                "@agent/fs": ModuleScope(
                    {
                        "index.js": "export default 1;",
                        "lib/helper.js": "export default 2;",
                    }
                )
            }
        )


def test_three_level_nesting_raises() -> None:
    """§3.1: two levels max. A nested scope's values must be source
    strings — it can't contain a further ModuleScope."""
    with pytest.raises(ValueError, match="Only two levels of nesting"):
        ModuleScope(
            {
                "@agent/fs": ModuleScope(
                    {
                        "index.js": ModuleScope({"index.js": "export default 1;"}),
                    }
                )
            }
        )


def test_top_level_key_starting_with_dot_slash_raises() -> None:
    """Top-level keys are import specifiers, not relative paths.
    './local' is a relative import, valid only inside a scope."""
    with pytest.raises(ValueError, match="relative specifier"):
        ModuleScope({"./local": "export default 1;"})


def test_top_level_key_starting_with_dot_dot_slash_raises() -> None:
    """Even more clearly wrong than ./ — ../ is never valid
    anywhere (§4: '../ is always an error')."""
    with pytest.raises(ValueError, match="relative specifier"):
        ModuleScope({"../parent": "export default 1;"})


def test_non_string_non_scope_value_raises() -> None:
    """Top-level values must be str | ModuleScope. Anything else is
    a programming error — not 'wrong JS,' wrong Python type."""
    with pytest.raises(TypeError, match="must be str or ModuleScope"):
        ModuleScope({"@agent/fs": 42})  # type: ignore[dict-item]


def test_nested_scope_with_non_string_file_source_raises() -> None:
    """Second-level values must be str — nothing else is a valid
    module source. (Three-level-nested ModuleScope is caught by a
    more specific message; this catches the non-ModuleScope,
    non-string case.)"""
    with pytest.raises(TypeError, match="must be str"):
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
    scope = ModuleScope({"@a": "export const X = 1;"})
    with pytest.raises(TypeError):
        scope.modules["@b"] = "export const Y = 2;"  # type: ignore[index]


def test_scope_instance_is_frozen_at_dataclass_level() -> None:
    """Dataclass(frozen=True) — reassigning the modules attribute
    itself also fails, not just mutating the dict contents."""
    scope = ModuleScope({"@a": "export const X = 1;"})
    with pytest.raises(FrozenInstanceError):
        scope.modules = {}  # type: ignore[misc]


def test_constructor_takes_defensive_copy() -> None:
    """Passing a dict in and mutating it afterwards must NOT affect
    the scope — validation happens once, at construction time, and
    then the scope snapshot is immutable."""
    raw: dict[str, object] = {"@a": "export const X = 1;"}
    scope = ModuleScope(raw)  # type: ignore[arg-type]
    raw["@sneaky"] = "this should not appear"
    assert "@sneaky" not in scope.modules


# ---- Composition patterns — §3.4 ------------------------------------


def test_dict_spread_extend() -> None:
    """Spread an existing scope's modules into a new dict to add
    entries. The new ModuleScope re-validates on construction."""
    base = ModuleScope({"@a": "export const X = 1;"})
    extended = ModuleScope({**base.modules, "@b": "export const Y = 2;"})
    assert set(extended.modules.keys()) == {"@a", "@b"}
    # Original untouched.
    assert set(base.modules.keys()) == {"@a"}


def test_dict_spread_override() -> None:
    """Later keys win in a dict spread — the canonical way to
    override one module in a composed scope."""
    base = ModuleScope({"@a": "export const X = 1;"})
    overridden = ModuleScope({**base.modules, "@a": "export const X = 99;"})
    assert overridden.modules["@a"].endswith("99;")


def test_dict_comprehension_filter() -> None:
    """Removal via comprehension — no dedicated remove method."""
    full = ModuleScope(
        {
            "@a": "export const X = 1;",
            "@b": "export const Y = 2;",
            "@c": "export const Z = 3;",
        }
    )
    filtered = ModuleScope(
        {k: v for k, v in full.modules.items() if k != "@b"}
    )
    assert set(filtered.modules.keys()) == {"@a", "@c"}


def test_merge_independent_scopes() -> None:
    """Two scopes authored independently can be combined by
    spreading both into a fresh ModuleScope."""
    team_a = ModuleScope({"@a/lib": "export const A = 1;"})
    team_b = ModuleScope({"@b/lib": "export const B = 2;"})
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
    extended = ModuleScope({**base.modules, "@new": "export const Z = 3;"})
    assert isinstance(extended.modules["@agent/utils"], ModuleScope)
    assert "helpers.js" in extended.modules["@agent/utils"].modules
