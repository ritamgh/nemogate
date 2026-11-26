#!/usr/bin/env bash
# Spike for the drop-in rule guard (PRD assumptions AS-1..AS-5, FR-002). Offline: a scripted chat
# model, no key, no credit. Prints four lines, one per question, each "PASS: ..." or "FAIL: ...".
# A FAIL records a finding and still exits 0; the exit code is non-zero only if the spike crashed.
#
#   Q1 Python 3.11: imports and `pytest gate oracle schemas` in a 3.11 venv built from uv.lock
#   Q2 an outside Deep Agents app (no refapp) runs offline        } scripts/spike_311.py
#   Q3 which agents a middleware list reaches                     } (on 3.12, and again on 3.11
#   Q4 the thread_id the middleware sees at each delegation level }  to check it runs there too)
#
# SPIKE_VERBOSE=1 prints the raw observations of Q2 to Q4 on stderr. SPIKE_311_VENV sets where the
# 3.11 venv lives (default ~/.cache/nemogate-spike-311); it is built outside the repo, so
# pyproject.toml and uv.lock stay untouched.

set -euo pipefail
cd "$(dirname "$0")/.."

VENV="${SPIKE_311_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/nemogate-spike-311}"
LOG="$(mktemp -d)"
trap 'rm -rf "${LOG:?}"' EXIT

one_line() { tr '\n' ' ' | sed 's/  */ /g; s/ $//'; }

# --- Q1 ---------------------------------------------------------------------------------------
q1_fail=""
direct="ok"
if ! uv run --python 3.11 pytest gate oracle schemas -q >"$LOG/direct.txt" 2>&1; then
    direct="refused ($(grep -m1 '^error:' "$LOG/direct.txt" | cut -c1-220))"
fi

if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 11))'; then
    rm -rf "${VENV:?}"
    uv venv --python 3.11 "$VENV" >"$LOG/venv.txt" 2>&1 || q1_fail="venv: $(tail -1 "$LOG/venv.txt")"
fi
if [ -z "$q1_fail" ]; then
    uv export --frozen --no-hashes --no-emit-project -o "$LOG/req.txt" >"$LOG/export.txt" 2>&1
    uv pip install --python "$VENV/bin/python" -r "$LOG/req.txt" >"$LOG/install.txt" 2>&1 \
        || q1_fail="dependency install: $(grep -m1 -i 'error\|no solution\|not found' "$LOG/install.txt" | cut -c1-200)"
fi
versions=""
if [ -z "$q1_fail" ]; then
    "$VENV/bin/python" -c '
import deepagents, langchain, langgraph  # noqa: F401
import nemogate.gate.middleware, oracle.evaluate, schemas.ledger, schemas.run, schemas.trace  # noqa: F401
from importlib.metadata import version
print(" ".join(f"{n} {version(n)}" for n in ("deepagents", "langgraph", "langchain")))
' >"$LOG/imports.txt" 2>&1 || q1_fail="import: $(tail -1 "$LOG/imports.txt" | cut -c1-200)"
    versions="$(cat "$LOG/imports.txt")"
fi
tests=""
if [ -z "$q1_fail" ]; then
    if "$VENV/bin/python" -m pytest gate oracle schemas -q -p no:cacheprovider >"$LOG/pytest.txt" 2>&1; then
        tests="$(tail -1 "$LOG/pytest.txt")"
    else
        q1_fail="pytest: $(tail -1 "$LOG/pytest.txt"); $(grep '^FAILED\|^ERROR' "$LOG/pytest.txt" | head -5 | cut -c1-120 | one_line)"
    fi
fi
probe311=""
if [ -z "$q1_fail" ]; then
    if "$VENV/bin/python" scripts/spike_311.py >"$LOG/probe311.txt" 2>"$LOG/probe311.err"; then
        probe311="$(cut -c1-4 "$LOG/probe311.txt" | one_line)"
    else
        q1_fail="probe on 3.11: $(tail -1 "$LOG/probe311.err" | cut -c1-200)"
    fi
fi
if [ -z "$q1_fail" ]; then
    echo "PASS: Q1 Python 3.11.x (venv from uv.lock; $versions) imports deepagents, langchain, langgraph, gate, oracle and schemas, and \`pytest gate oracle schemas\` gives: $tests; the probe on 3.11 gives: $probe311; plain \`uv run --python 3.11\` is $direct because pyproject pins requires-python ==3.12.*."
else
    echo "FAIL: Q1 Python 3.11 failed: $q1_fail; plain \`uv run --python 3.11\` is $direct."
fi

# --- Q2 to Q4 ---------------------------------------------------------------------------------
uv run python scripts/spike_311.py
