"""Run every Python example in the docs site and check its printed output.

Each ```python block in docs/content is a standalone program. Lines starting with
`#>` are the output the block prints, in order. A block whose fence carries
`test="skip"` is shown but not run.

The prose checks enforce the house voice rules in docs/AGENTS.md that a regex can
catch.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

CONTENT = Path(__file__).resolve().parent.parent / "docs" / "content"
FENCE = re.compile(r"^```python([^\n]*)\n(.*?)^```", re.M | re.S)


def _examples() -> list[tuple[str, str]]:
    found = []
    for path in sorted(CONTENT.rglob("*.mdx")):
        text = path.read_text("utf-8")
        for m in FENCE.finditer(text):
            if 'test="skip"' in m.group(1):
                continue
            line = text[: m.start()].count("\n") + 1
            found.append((f"{path.relative_to(CONTENT)}:{line}", m.group(2)))
    return found


EXAMPLES = _examples()


@pytest.mark.skipif(not CONTENT.is_dir(), reason="docs/ not present")
@pytest.mark.parametrize(("where", "code"), EXAMPLES, ids=[w for w, _ in EXAMPLES])
def test_example(where: str, code: str) -> None:
    expected = [
        line.strip()[3:] if line.strip() != "#>" else ""
        for line in code.splitlines()
        if line.strip().startswith("#>")
    ]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(code, where, "exec"), {"__name__": "__docs__"})
    assert out.getvalue().splitlines() == expected, where


BANNED = {
    r" — |—": "em dash: use a comma, a colon, or two sentences",
    r"\b(simply|basically|obviously|just)\b": "filler word",
    r"\b(\w+n't|it's|that's|there's|you're|we're|let's|won't|isn't)\b": "contraction",
    r"\b(we|our|us)\b": "first person: address the reader or use the product name",
    r"\bIn short\b": "summary box: open with the plain statement instead",
}


def _prose(text: str) -> str:
    """Drop frontmatter, code fences, inline code, JSX tags and link targets."""
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    text = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    text = re.sub(r"<[^>\n]*>", "", text)
    text = re.sub(r"\]\([^)]*\)", "]", text)
    return text


@pytest.mark.skipif(not CONTENT.is_dir(), reason="docs/ not present")
@pytest.mark.parametrize(
    "path", sorted(CONTENT.rglob("*.mdx")), ids=lambda p: str(p.relative_to(CONTENT))
)
def test_prose(path: Path) -> None:
    prose = _prose(path.read_text("utf-8"))
    problems = []
    for line in prose.splitlines():
        for pattern, why in BANNED.items():
            for m in re.finditer(pattern, line, flags=re.I):
                problems.append(f"{why}: {m.group(0)!r} in {line.strip()[:80]!r}")
    assert not problems, "\n".join(problems)
