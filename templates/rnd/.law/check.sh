#!/usr/bin/env bash
# The project's gate (LAW runs it after every ticket): build the book, then check it (tools/verify.py).
# Both always run, so one pass lists every problem.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
built=0
bash tools/build.sh || built=1
PYTHONDONTWRITEBYTECODE=1 python3 tools/verify.py
checked=$?
[ "$built" -ne 0 ] && echo "check: FAIL (the build failed, see above)" >&2
exit $(( built || checked ))
