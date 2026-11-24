"""Measure the scoring rules against the owner's confirmed labels.

Usage:
    python run_evaluation.py
    python run_evaluation.py --split train --json output/evaluation.json

Reads data/verified_bots.csv, data/verified_users.csv and data/checkpoints/*.json.
This tool never connects to the internet.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from botaudit.evaluate import main

if __name__ == "__main__":
    sys.exit(main())
