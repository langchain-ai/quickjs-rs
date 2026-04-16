#!/usr/bin/env bash
# Bump the vendored quickjs-ng submodule to a specific commit or tag, then
# rebuild and refresh the reproducibility baseline.
# See spec/implementation.md §4.1, §4.3.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$#" -lt 1 ]; then
    echo "usage: $(basename "$0") <commit-or-tag>" >&2
    exit 2
fi

target="$1"
cd "${REPO_ROOT}/vendor/quickjs-ng"
git fetch --tags origin
git checkout "${target}"

cd "${REPO_ROOT}"
git add vendor/quickjs-ng

./wasm/build.sh
git add quickjs_wasm/_resources/quickjs.wasm quickjs_wasm/_resources/quickjs.wasm.sha256

cat <<EOF
Staged the bump to ${target} and the rebuilt wasm. Review with \`git diff --cached\`,
then commit with:

    git commit -m "build: bump quickjs-ng to ${target}"

Remember to note the reason in the commit body (security fix, new feature, etc.).
EOF
