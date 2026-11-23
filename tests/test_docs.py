"""Run every Python example in the docs and check its printed output.

Each ```python block in README.md and the docs/ pages is a standalone program. Lines starting with
`#>` are the output the block prints, in order. A block whose fence carries
`test="skip"` is shown but not run.

The prose checks enforce the docs voice rules that a regex can catch. The link check
resolves every relative link and #anchor between pages.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "docs"
REPO_BLOB = "https://github.com/langchain-ai/quickjs-rs/blob/main/"
# The top-level README is the docs landing page.
PAGES = [ROOT / "README.md", *sorted(CONTENT.rglob("*.md"))] if CONTENT.is_dir() else []
FENCE = re.compile(r"^```python([^\n]*)\n(.*?)^```", re.M | re.S)


def _examples() -> list[tuple[str, str]]:
    found = []
    for path in PAGES:
        text = path.read_text("utf-8")
        for m in FENCE.finditer(text):
            if 'test="skip"' in m.group(1):
                continue
            line = text[: m.start()].count("\n") + 1
            found.append((f"{path.relative_to(ROOT)}:{line}", m.group(2)))
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
@pytest.mark.parametrize("path", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_prose(path: Path) -> None:
    prose = _prose(path.read_text("utf-8"))
    problems = []
    for line in prose.splitlines():
        for pattern, why in BANNED.items():
            for m in re.finditer(pattern, line, flags=re.I):
                problems.append(f"{why}: {m.group(0)!r} in {line.strip()[:80]!r}")
    assert not problems, "\n".join(problems)


def _without_code(text: str) -> str:
    return re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)


def _anchors(path: Path) -> set[str]:
    """GitHub's heading ids for a page, de-duplicated as `foo`, `foo-1`."""
    seen: dict[str, int] = {}
    ids = set()
    for m in re.finditer(r"^#{1,6} +(.+?) *#*$", _without_code(path.read_text("utf-8")), re.M):
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(1)).replace("`", "")
        base = re.sub(r"[^\w\s-]", "", text.lower().strip()).replace(" ", "-")
        n = seen.get(base)
        seen[base] = 0 if n is None else n + 1
        ids.add(base if n is None else f"{base}-{n + 1}")
    return ids


@pytest.mark.skipif(not CONTENT.is_dir(), reason="docs/ not present")
@pytest.mark.parametrize("path", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_links(path: Path) -> None:
    problems = []
    for m in re.finditer(r"\]\(([^)\s]+)\)", _without_code(path.read_text("utf-8"))):
        target = m.group(1)
        # README.md links into the repo by URL so they work on PyPI too; check them as local files.
        if target.startswith(REPO_BLOB):
            file, _, anchor = target[len(REPO_BLOB) :].partition("#")
            dest = ROOT / file
        elif re.match(r"[a-z]+:", target):
            continue
        else:
            file, _, anchor = target.partition("#")
            dest = (path.parent / file).resolve() if file else path
        if not dest.exists():
            problems.append(f"missing file: {target}")
        elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
            problems.append(f"missing anchor: {target}")
    assert not problems, "\n".join(problems)
