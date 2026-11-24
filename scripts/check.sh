#!/usr/bin/env bash
# scripts/check.sh - Project quality & guardrail verification runner.
#
# Run from anywhere:   bash scripts/check.sh
#
# Steps (stops at the first failure):
#   1. ruff      - lint and formatting hygiene checks
#   2. pytest    - unit, WSGI, Mindmap, guardrail, and JS contract tests
#   3. node      - syntax & headless contract validation for browser-extension/
#   4. run_audit - offline CSV -> self-contained HTML report on bundled starter example
#   5. report    - validates output/report.html required phrases and zero forbidden terms

set -euo pipefail

CURRENT_STEP="starting up"

finish() {
    local exit_code=$?
    echo
    if [ "$exit_code" -eq 0 ]; then
        echo "========================================"
        echo "PASS: all checks succeeded"
        echo "========================================"
    else
        echo "========================================"
        echo "FAIL: step '$CURRENT_STEP' failed (exit code $exit_code)"
        echo "========================================"
    fi
}
trap finish EXIT

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -x ".venv/Scripts/python.exe" ]; then
    PYTHON=".venv/Scripts/python.exe"      # Windows
elif [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"              # macOS / Linux
else
    PYTHON="python"
fi
echo "Using Python: $PYTHON ($("$PYTHON" --version 2>&1))"

CURRENT_STEP="1/5 ruff (lint)"
echo
echo "--- Step $CURRENT_STEP ---"
"$PYTHON" -m ruff check .

CURRENT_STEP="2/5 pytest (tests)"
echo
echo "--- Step $CURRENT_STEP ---"
"$PYTHON" -m pytest -q

CURRENT_STEP="3/5 node (extension syntax & contracts)"
echo
echo "--- Step $CURRENT_STEP ---"
if command -v node >/dev/null 2>&1; then
    node --check browser-extension/parser.js
    node --check browser-extension/content.js
    node --check browser-extension/background.js
    node --check browser-extension/popup.js
    node --check browser-extension/injected-interceptor.js
    node tests/js/parser_contract.js >/dev/null
    node tests/js/hud_inspector_contract.js >/dev/null
    node tests/js/background_contract.js >/dev/null
    echo "Node syntax and JS contracts passed."
else
    echo "Node.js not found on PATH; skipping standalone JS checks."
fi

CURRENT_STEP="4/5 run_audit.py (full pipeline on starter example CSV)"
echo
echo "--- Step $CURRENT_STEP ---"
"$PYTHON" run_audit.py --input data/examples/_vlad.dumitrache_.csv --output output/report.html --rejects output/rejects.csv

CURRENT_STEP="5/5 report verification (content and safety check)"
echo
echo "--- Step $CURRENT_STEP ---"
"$PYTHON" -c "
from pathlib import Path
import sys
report_path = Path('output/report.html')
if not report_path.is_file() or report_path.stat().st_size == 0:
    print('Error: output/report.html is missing or empty', file=sys.stderr)
    sys.exit(1)

content = report_path.read_text(encoding='utf-8')
sys.path.insert(0, 'src')
from botaudit.report import validate_report_content
validate_report_content(content)
print('HTML report verified: non-empty, required phrases present, zero forbidden words.')
"

CURRENT_STEP="done"
