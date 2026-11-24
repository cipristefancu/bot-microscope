#!/usr/bin/env bash
# scripts/check.sh - the "judge". Decides PASS or FAIL for the whole project.
#
# Run from anywhere:   bash scripts/check.sh
#
# Steps (stops at the first failure):
#   1. ruff   - lint: automatic check for code mistakes and sloppy style
#   2. pytest - runs every test in tests/ (including the no-network guardrail)
#   3. sample - runs make_sample_data.py to ensure sample generation works
#   4. audit  - runs the full pipeline on the sample data to produce HTML report
#   5. report - verifies that HTML report exists, is non-empty, contains required
#               phrases ("sample size", "bot-like audience") and no forbidden terms
#
# Ends with a final line "PASS" or "FAIL" and exits non-zero on any failure.
# Change control: this file may be made stricter, never weaker, without the
# owner's explicit approval (see MILESTONES.md).

set -euo pipefail

CURRENT_STEP="starting up"

# Runs when the script ends for any reason; prints the final verdict.
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

# Always work from the project folder, wherever the script is called from.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Use the project's private Python (the .venv folder) if it exists.
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

CURRENT_STEP="3/5 make_sample_data.py (sample data)"
echo
echo "--- Step $CURRENT_STEP ---"
"$PYTHON" make_sample_data.py --out data/sample/sample_data.csv --seed 42 --n-per-artist 300

CURRENT_STEP="4/5 run_audit.py (full pipeline on sample data)"
echo
echo "--- Step $CURRENT_STEP ---"
"$PYTHON" run_audit.py --input data/sample/sample_data.csv --output output/report.html --rejects output/rejects.csv

CURRENT_STEP="5/5 report verification (content and safety check)"
echo
echo "--- Step $CURRENT_STEP ---"
"$PYTHON" -c "
from pathlib import Path
import sys
report_path = Path('output/report.html')
if not report_path.is_file():
    print('Error: output/report.html was not generated', file=sys.stderr)
    sys.exit(1)
if report_path.stat().st_size == 0:
    print('Error: output/report.html is empty', file=sys.stderr)
    sys.exit(1)

content = report_path.read_text(encoding='utf-8')

# Check required phrasing and forbidden words using the single source of truth in botaudit.report
sys.path.insert(0, 'src')
from botaudit.report import validate_report_content
try:
    validate_report_content(content)
except ValueError as err:
    print(f'Report validation error: {err}', file=sys.stderr)
    sys.exit(1)

print('HTML report verified: non-empty, required phrases present, zero forbidden words.')
"

CURRENT_STEP="done"
