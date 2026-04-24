#!/usr/bin/env python3
"""Enforce an explicit git-source policy for Cargo.lock."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

LOCKFILE = Path("Cargo.lock")
SOURCE_PREFIX = 'source = "'
GIT_PREFIX = "git+"
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")

# Keep this list intentionally small and explicit.
ALLOWED_GIT_REPOS = {
    "https://github.com/branchseer/oxidase",
    "https://github.com/branchseer/oxc",
}


def extract_sources(lockfile: Path) -> list[str]:
    sources: list[str] = []
    for line in lockfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith(SOURCE_PREFIX):
            continue
        # source lines are always `source = "<value>"`.
        sources.append(stripped[len(SOURCE_PREFIX) : -1])
    return sources


def parse_git_source(source: str) -> tuple[str, str]:
    parsed = urlparse(source[len(GIT_PREFIX) :])
    repo = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    rev = parsed.fragment
    return repo, rev


def main() -> int:
    if not LOCKFILE.exists():
        print(f"error: {LOCKFILE} not found", file=sys.stderr)
        return 1

    sources = extract_sources(LOCKFILE)
    git_sources = sorted({src for src in sources if src.startswith(GIT_PREFIX)})

    violations: list[str] = []
    for source in git_sources:
        repo, rev = parse_git_source(source)
        if repo not in ALLOWED_GIT_REPOS:
            violations.append(
                f"disallowed git repository '{repo}' in source '{source}'"
            )
        if not SHA1_RE.fullmatch(rev):
            violations.append(
                f"git source is not pinned to a full commit SHA: '{source}'"
            )

    if violations:
        print("Cargo.lock git-source policy violations detected:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        print(
            "Allowed repositories: "
            + ", ".join(sorted(ALLOWED_GIT_REPOS)),
            file=sys.stderr,
        )
        return 1

    print("Cargo.lock git-source policy check passed.")
    if git_sources:
        print("Observed git sources:")
        for source in git_sources:
            print(f"- {source}")
    else:
        print("No git sources found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
