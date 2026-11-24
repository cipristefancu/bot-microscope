"""Run audience quality audit on a follower CSV and produce an HTML report.

Usage:
    python run_audit.py --input data/examples/_vlad.dumitrache_.csv --output output/report.html

This tool never connects to the internet. Input is only a CSV provided by the owner.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure src is on sys.path so botaudit can be imported directly
_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from botaudit.config import ConfigError, load_config
from botaudit.loader import CSVValidationError, load_followers_csv
from botaudit.report import write_html_report
from botaudit.scoring import score_followers
from botaudit.stats import compute_artist_stats


def main() -> int:
    """Execute the full audit pipeline from CSV input to HTML report."""
    parser = argparse.ArgumentParser(
        description="Analyze a CSV of follower profiles and generate an HTML audience quality report."
    )
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        required=True,
        help="Path to the input CSV file containing sampled follower profiles.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("output/report.html"),
        help="Path where the self-contained HTML report will be saved (default: output/report.html).",
    )
    parser.add_argument(
        "--rejects",
        type=Path,
        default=Path("output/rejects.csv"),
        help="Path where invalid or rejected rows will be saved (default: output/rejects.csv).",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        help="Path to custom config.yaml settings file (optional, defaults to config.yaml).",
    )
    parser.add_argument(
        "--lang",
        "-l",
        choices=["en", "ro"],
        default="en",
        help="Report language: 'en' (English, default) or 'ro' (Romanian).",
    )

    args = parser.parse_args()

    # Friendly error handling for missing input file (AC1)
    if not args.input.is_file():
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        print("Please check the path and provide a valid CSV file.", file=sys.stderr)
        return 1

    try:
        # Load custom config if specified, otherwise default config.yaml
        cfg = load_config(args.config) if args.config else load_config()

        print(f"Loading and validating follower profiles from {args.input}...")
        load_result = load_followers_csv(args.input, rejects_path=args.rejects, config=cfg)
        total_loaded = len(load_result.df)
        total_rejected = len(load_result.rejects)
        print(f"Loaded {total_loaded} valid rows ({total_rejected} rejected).")

        if load_result.warnings:
            for w in load_result.warnings:
                print(f"Warning: {w}")

        print("Computing heuristic bot scores...")
        scored_df = score_followers(load_result.df, config=cfg)

        print("Calculating Wilson score confidence intervals and rankings...")
        artist_stats = compute_artist_stats(scored_df, rejects=load_result.rejects, config=cfg)

        print(f"Writing self-contained HTML report to {args.output}...")
        write_html_report(
            destination=args.output,
            artists=artist_stats,
            total_rows_loaded=total_loaded,
            total_rows_rejected=total_rejected,
            warnings=load_result.warnings,
            config=cfg,
            lang=args.lang,
        )

        print("=" * 50)
        print("AUDIT COMPLETE")
        print(f"Report saved: {args.output.resolve()}")
        if total_rejected > 0:
            print(f"Rejects saved: {args.rejects.resolve()}")
        print("=" * 50)
        return 0

    except ConfigError as err:
        print(f"Configuration Error: {err}", file=sys.stderr)
        return 1
    except CSVValidationError as err:
        print(f"CSV Validation Error: {err}", file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError) as err:
        print(f"Error during audit pipeline: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
