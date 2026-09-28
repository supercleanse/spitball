#!/usr/bin/env bash
# Spitball test runner: python unittest (discover) + node --test for Model.js.
# Usage: tests/run.sh [--live]
#   --live     also run tests/live/ (real PipeWire, ffmpeg, Deepgram, summary
#              model). Equivalent to SPITBALL_LIVE=1. Never run this on a
#              machine you don't want real mic/audio activity on.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

LIVE=0
for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
if [[ "${SPITBALL_LIVE:-0}" == "1" ]]; then
  LIVE=1
fi
export SPITBALL_LIVE=$LIVE

PY=${PYTHON:-python3}

echo "== Python tests (unittest discover) =="
"$PY" -m unittest discover -s tests -t . -v
py_status=$?

echo
echo "== Model.js tests (node --test) =="
if command -v node >/dev/null 2>&1; then
  node --test tests/js
  js_status=$?
else
  echo "node not found on PATH; skipping Model.js tests" >&2
  js_status=1
fi

echo
if [[ $LIVE -eq 1 ]]; then
  echo "Live tests: included (SPITBALL_LIVE=1)."
else
  echo "Live tests: skipped. Pass --live or set SPITBALL_LIVE=1 to include them."
fi

status=0
if [[ $py_status -ne 0 ]]; then
  echo "Python suite: FAILED"
  status=1
else
  echo "Python suite: passed"
fi
if [[ $js_status -ne 0 ]]; then
  echo "Model.js suite: FAILED"
  status=1
else
  echo "Model.js suite: passed"
fi

exit $status
