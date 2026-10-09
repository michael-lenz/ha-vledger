#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
#
# Cut a release: scripts/release.sh X.Y.Z [--dry-run] [--yes]
#
# Does what docs/releasing.md, "Every release", lists by hand, after
# checking everything that would make it go wrong: the version is
# well-formed and greater than the current one, the three version strings
# agree, the tree is clean, on main and in step with origin, neither the
# tag nor the PyPI version exists, and the tests and ruff are green on the
# bumped tree. Then: commit "Release X.Y.Z", tag vX.Y.Z, push both. The
# tag starts .github/workflows/release.yml, which publishes to PyPI and
# creates the GitHub release HACS reads (ADR-0002).
#
# --dry-run  check, bump and test, then put the files back; nothing is
#            committed or pushed
# --yes      do not ask before committing and pushing
#
# The Python used is $PYTHON, else .venv/bin/python, else python3; it
# needs the ".[dev]" extra (pytest, ruff), and ".[dev,ha]" to run the
# integration's tests as well (docs/developing.md).

set -euo pipefail

usage() {
    echo "usage: scripts/release.sh X.Y.Z [--dry-run] [--yes]" >&2
    exit 2
}

die() {
    echo "release: $*" >&2
    exit 1
}

new=""
dry_run=0
assume_yes=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        --yes) assume_yes=1 ;;
        -h|--help) usage ;;
        -*) usage ;;
        *) [[ -z "$new" ]] || usage; new="$arg" ;;
    esac
done
[[ -n "$new" ]] || usage
new="${new#v}"
[[ "$new" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
    || die "'$new' is not a version X.Y.Z"
tag="v$new"

cd "$(git rev-parse --show-toplevel)"

if [[ -n "${PYTHON:-}" ]]; then
    py="$PYTHON"
elif [[ -x .venv/bin/python ]]; then
    py=.venv/bin/python
else
    py=python3
fi
"$py" -c "import pytest, ruff" 2>/dev/null \
    || die "$py lacks pytest or ruff: pip install -e '.[dev]' (docs/developing.md)"

# The files that carry the version, and nothing else may be touched.
files=(pyproject.toml src/vledger/__init__.py custom_components/vledger/manifest.json)

# --- the repository --------------------------------------------------------

branch="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
[[ "$branch" == main ]] || die "on '${branch:-a detached HEAD}', not main"
[[ -z "$(git status --porcelain)" ]] || die "the working tree is not clean"
git fetch --quiet origin main --tags
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] \
    || die "main is not origin/main — pull or push first"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag exists already"
[[ -z "$(git ls-remote --tags origin "refs/tags/$tag")" ]] || die "tag $tag exists on origin"

# --- the version -----------------------------------------------------------

current="$("$py" - <<'EOF'
import json, re, sys, tomllib
from pathlib import Path
lib = re.search(r'^__version__ = "([^"]+)"', Path("src/vledger/__init__.py").read_text(), re.M)
pyproject = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
manifest = json.loads(Path("custom_components/vledger/manifest.json").read_text())
found = {lib.group(1) if lib else None, pyproject, manifest["version"]}
if len(found) != 1 or manifest["requirements"] != [f"vledger=={pyproject}"]:
    sys.exit(f"the version strings disagree: {sorted(map(str, found))}, "
             f"requirements {manifest['requirements']}")
print(pyproject)
EOF
)" || die "fix the version strings first (tests/test_version.py says the same)"

"$py" -c "import sys; a, b = (tuple(map(int, v.split('.'))) for v in sys.argv[1:]); sys.exit(a <= b)" \
    "$new" "$current" || die "$new is not greater than the current version $current"

status="$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/vledger/$new/json" || true)"
case "$status" in
    404) ;;
    200) die "vledger $new is on PyPI already — a version is never uploaded twice" ;;
    *) echo "release: could not ask PyPI (HTTP ${status:-none}); the workflow will refuse a duplicate" >&2 ;;
esac

echo "release: $current -> $new"

# --- bump and test ---------------------------------------------------------

restore() { git checkout --quiet -- "${files[@]}"; }

"$py" - "$current" "$new" <<'EOF'
import sys
from pathlib import Path
old, new = sys.argv[1:]
edits = {
    "pyproject.toml": [(f'version = "{old}"', f'version = "{new}"')],
    "src/vledger/__init__.py": [(f'__version__ = "{old}"', f'__version__ = "{new}"')],
    "custom_components/vledger/manifest.json": [
        (f'"version": "{old}"', f'"version": "{new}"'),
        (f'"vledger=={old}"', f'"vledger=={new}"'),
    ],
}
for name, pairs in edits.items():
    path = Path(name)
    text = path.read_text()
    for a, b in pairs:
        if text.count(a) != 1:
            sys.exit(f"{name}: expected {a!r} exactly once")
        text = text.replace(a, b)
    path.write_text(text)
EOF

# This tree's library, whatever checkout the interpreter's install points at.
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
if ! "$py" -m pytest -q || ! "$py" -m ruff check src tests custom_components; then
    restore
    die "tests or ruff failed; the files are put back"
fi
if ! "$py" -c "import homeassistant" 2>/dev/null; then
    echo "release: no Home Assistant in $py — tests/ha did not run here; the release workflow does not run them either" >&2
fi

git --no-pager diff --stat

if (( dry_run )); then
    restore
    echo "release: dry run — $tag would be released; nothing committed or pushed"
    exit 0
fi

if (( ! assume_yes )); then
    read -r -p "Commit, tag $tag and push to origin? [y/N] " answer
    [[ "$answer" == y || "$answer" == Y ]] || { restore; die "stopped; the files are put back"; }
fi

# --- commit, tag, push -----------------------------------------------------

git commit --quiet -m "Release $new" -- "${files[@]}"
git tag -a "$tag" -m "vledger $new"
git push --quiet origin main
git push --quiet origin "$tag"

echo "release: $tag pushed. The release workflow now publishes to PyPI and creates the GitHub release:"
echo "  https://github.com/michael-lenz/ha-vledger/actions"
echo "  https://pypi.org/project/vledger/$new/"
