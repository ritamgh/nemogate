#!/usr/bin/env bash
# SC-002: build the wheel, install it in a fresh venv, run the README quickstart, time it.
# Needs uv and, for uv, the network to fetch the pinned dependencies. No model call, no key.
set -euo pipefail

LIMIT=600
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
START=$SECONDS
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP:?}"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "build the wheel"
uv build --wheel --out-dir "$TMP/dist" "$REPO" >/dev/null
WHEEL="$(ls "$TMP"/dist/nemogate-*.whl)"

echo "fresh venv and install of $(basename "$WHEEL")"
uv venv --quiet "$TMP/venv"
PY="$TMP/venv/bin/python"
uv pip install --quiet --python "$PY" "$WHEEL"

# The package contree-sdk imports as contree_sdk; check that name and the bare one.
for module in contree contree_sdk; do
    if (cd "$TMP" && "$PY" -I -c "import $module") 2>/dev/null; then
        fail "a core install must not bring the sandbox SDK (import $module worked)"
    fi
done
echo "ok: the sandbox SDK is not installed"

echo "README code block matches examples/quickstart/app.py"
awk '/^```python$/ {if (!done) {on=1; next}} on && /^```$/ {on=0; done=1} on' \
    "$REPO/README.md" > "$TMP/readme_app.py"
diff "$TMP/readme_app.py" "$REPO/examples/quickstart/app.py" \
    || fail "the README code block differs from examples/quickstart/app.py"
echo "ok: identical"

echo "run the quickstart"
cp -r "$REPO/examples/quickstart" "$TMP/quickstart"
OUT="$(cd "$TMP/quickstart" && "$PY" -I app.py)" || fail "the quickstart exited non-zero"
echo "$OUT"
grep -q "^write_file -> error: Blocked by rule no-migrations: " <<<"$OUT" \
    || fail "the quickstart output shows no blocked call"

ELAPSED=$((SECONDS - START))
echo "elapsed: ${ELAPSED}s (limit ${LIMIT}s)"
[ "$ELAPSED" -le "$LIMIT" ] || fail "took ${ELAPSED}s, over ${LIMIT}s"
echo "PASS"
