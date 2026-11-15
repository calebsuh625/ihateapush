#!/usr/bin/env bash
# Every check in one go: pytest, web unit tests, typecheck and every browser suite.
# One line per step and a final PASS or FAIL. Full output of each step: data/logs/check_<step>.log.
# The browser suites need the dev servers (`make dev`): API :8010, web :5173.
#
#   make check          (or: bash scripts/check.sh)
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
WEB_PORT=${WEB_PORT:-5173}
API_PORT=${API_PORT:-8010}
mkdir -p data/logs
failed=0

step() {
  name=$1
  shift
  log="data/logs/check_${name}.log"
  if "$@" > "$log" 2>&1; then
    last=$(grep -E 'passed|PASS|ℹ pass' "$log" | tail -1 | cut -c1-100)
    echo "  ok      ${name}: ${last}"
  else
    echo "  FAILED  ${name} (see ${log})"
    grep -E 'FAIL|Error|error' "$log" | head -3 | cut -c1-160
    failed=1
  fi
}

step pytest "$PY" -m pytest -q
step web-tests npm --prefix web test
step typecheck npm --prefix web run typecheck

if curl -s -o /dev/null -m 5 "http://127.0.0.1:${API_PORT}/health" && curl -s -o /dev/null -m 5 "http://localhost:${WEB_PORT}/"; then
  (cd web && npx playwright install chromium > /dev/null 2>&1) || true
  for suite in e2e e2e:states e2e:planner e2e:phase4; do
    step "$(echo "$suite" | tr ':' '_')" env WEB_URL="http://localhost:${WEB_PORT}" npm --prefix web run "$suite"
  done
else
  echo "  FAILED  browser suites: the dev servers are not answering (start them with 'make dev')"
  failed=1
fi

if [ "$failed" = 0 ]; then
  echo "check: PASS"
else
  echo "check: FAIL"
  exit 1
fi
