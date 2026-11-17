"""Check that `pyproject.toml` and `quickjs_rs/_version.py` agree on the version."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def read_version(path: Path, key: str) -> str | None:
    match = re.search(rf'^{key}\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def main() -> int:
    pyproject = read_version(ROOT / "pyproject.toml", "version")
    version_py = read_version(ROOT / "quickjs_rs" / "_version.py", "__version__")
    if pyproject is None or pyproject != version_py:
        print(f"Version mismatch: pyproject.toml={pyproject} quickjs_rs/_version.py={version_py}")
        return 1
    print(f"Version check passed: {pyproject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
