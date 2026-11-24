"""Launch the interactive Bot Microscope web interface (`index.html` + local API).

Usage:
    python app.py --port 8000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from botaudit.web import run_server


def main() -> int:
    """Parse CLI arguments and launch the local WSGI server."""
    parser = argparse.ArgumentParser(
        description="Start the local Bot Microscope web application."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=8000,
        help="Port number to serve on (default: 8000).",
    )
    args = parser.parse_args()
    try:
        run_server(host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\nServer stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

