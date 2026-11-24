"""Measure how well the scoring rules agree with the owner's confirmed labels.

Run from the project folder:
    .\\.venv\\Scripts\\python.exe run_evaluation.py

Two measurements, kept apart so one cannot flatter the other:
  1. HANDLE-ONLY rules need nothing but the username, so every labeled account counts.
  2. THE APP'S SCORER runs only on accounts whose numbers were all observed. Rows written
     by older extension builds are listed separately: their numbers cannot be proven real.

Every percentage is shown with its 95% Wilson confidence interval.
This tool never connects to the internet.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd

from botaudit.capture import SCHEMA_VERSION, best_profiles, is_scorable
from botaudit.config import ConfigError, load_config
from botaudit.handles import analyze_handle_forensics, compute_digit_ratio
from botaudit.labels import (
    DEFAULT_BOTS_PATH,
    DEFAULT_USERS_PATH,
    SPLITS,
    LabelError,
    LabelSet,
    load_labels,
)
from botaudit.metrics import Confusion, Proportion, confusion, error_amplification
from botaudit.web import (
    detect_same_day_post_burst,
    score_followers_extended,
    validate_config_dict,
)

CHECKPOINTS_DIR = DEFAULT_BOTS_PATH.parent / "checkpoints"

HANDLE_RULES: dict[str, Callable[[dict[str, Any]], bool]] = {
    "any handle flag": lambda f: bool(
        f["is_digit_heavy"]
        or f["is_word_dot_digits"]
        or f["has_long_digit_run"]
        or f["is_stitched_handle"]
    ),
    "digit rules only": lambda f: bool(
        f["is_digit_heavy"] or f["is_word_dot_digits"] or f["has_long_digit_run"]
    ),
    "stitched rule only": lambda f: bool(f["is_stitched_handle"]),
}


def _prop(p: Proportion) -> dict[str, Any]:
    return {"successes": p.successes, "total": p.total, "text": p.format()}


def _summarize(c: Confusion) -> dict[str, Any]:
    return {
        "tp": c.tp,
        "fp": c.fp,
        "tn": c.tn,
        "fn": c.fn,
        "sensitivity": _prop(c.sensitivity()),
        "specificity": _prop(c.specificity()),
        "ppv": _prop(c.ppv()),
        "balanced_accuracy": c.balanced_accuracy,
        "youden_j": c.youden_j,
        "error_amplification": error_amplification(c.youden_j),
    }


def _score_group(
    rows: list[dict[str, Any]], labels: LabelSet, config: dict[str, Any]
) -> dict[str, Any] | None:
    """Run the app's scorer on rows whose numbers were all observed."""
    if not rows:
        return None
    threshold = float(config["digit_ratio_threshold"])
    frame = pd.DataFrame(
        [
            {
                "artist": "evaluation",
                "username": r["username"],
                "has_profile_photo": bool(r["has_profile_photo"]),
                "post_count": int(r["post_count"]),
                "followers_count": int(r["followers_count"]),
                "following_count": int(r["following_count"]),
                "bio_length": int(r["bio_length"]),
                "account_private": bool(r["account_private"]),
                "username_digit_ratio": compute_digit_ratio(r["username"]),
            }
            for r in rows
        ]
    )
    overrides = {
        r["username"]: detect_same_day_post_burst(
            post_count=int(r["post_count"]),
            post_timestamps=r["post_timestamps"],
            followers_count=int(r["followers_count"]),
            following_count=int(r["following_count"]),
            bio_length=int(r["bio_length"]),
            username=r["username"],
            digit_threshold=threshold,
        )
        for r in rows
        if r.get("post_timestamps")
    }
    scored = score_followers_extended(frame, config=config, burst_overrides=overrides)
    truth = [labels.label_of(u) == "bot" for u in scored["username"]]
    called = [bool(v) for v in scored["bot_like"]]
    result = _summarize(confusion(truth, called))
    result["bots"] = sum(truth)
    result["humans"] = len(truth) - sum(truth)
    tiers_summary: dict[str, dict[str, int]] = {
        "bot_gt_60": {"bots": 0, "humans": 0},
        "suspicious_35_60": {"bots": 0, "humans": 0},
        "clean_0_34": {"bots": 0, "humans": 0},
    }
    for is_b, v_tier in zip(truth, scored["verdict_tier"], strict=True):
        key_b = "bots" if is_b else "humans"
        if v_tier == "bot":
            tiers_summary["bot_gt_60"][key_b] += 1
        elif v_tier == "suspicious":
            tiers_summary["suspicious_35_60"][key_b] += 1
        else:
            tiers_summary["clean_0_34"][key_b] += 1
    result["tiers"] = tiers_summary
    return result


def evaluate(
    labels: LabelSet,
    checkpoints_dir: Path = CHECKPOINTS_DIR,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure the rules against `labels`; returns plain JSON-serialisable data."""
    cfg = validate_config_dict(config if config is not None else load_config())
    threshold = float(cfg["digit_ratio_threshold"])
    handles = sorted(labels.bots | labels.humans)
    truth = [labels.label_of(h) == "bot" for h in handles]

    forensics = {h: analyze_handle_forensics(h, digit_threshold=threshold) for h in handles}
    handle_only = {
        name: _summarize(confusion(truth, [rule(forensics[h]) for h in handles]))
        for name, rule in HANDLE_RULES.items()
    }

    captured = best_profiles(checkpoints_dir) if Path(checkpoints_dir).is_dir() else {}
    coverage = {
        "labeled": len(handles),
        "not_captured": 0,
        "list_only": 0,
        "complete_trusted": 0,
        "complete_legacy_unverified": 0,
    }
    groups: dict[str, list[dict[str, Any]]] = {"trusted": [], "legacy_unverified": []}
    for handle in handles:
        row = captured.get(handle)
        if row is None:
            coverage["not_captured"] += 1
        elif not is_scorable(row):
            coverage["list_only"] += 1
        elif row.get("schema_version") == SCHEMA_VERSION:
            coverage["complete_trusted"] += 1
            groups["trusted"].append(row)
        else:
            coverage["complete_legacy_unverified"] += 1
            groups["legacy_unverified"].append(row)

    return {
        "labels": {
            "bots": len(labels.bots),
            "humans": len(labels.humans),
            "total": len(labels),
            "conflicts_counted_as_humans": list(labels.conflicts),
            "bot_duplicate_lines": list(labels.bot_duplicates),
            "human_duplicate_lines": list(labels.human_duplicates),
            "ignored_lines": list(labels.ignored_lines),
        },
        "handle_only": handle_only,
        "coverage": coverage,
        "app_scorer": {name: _score_group(rows, labels, cfg) for name, rows in groups.items()},
    }


def _fmt(value: float | None, spec: str = ".2f") -> str:
    return "n/a" if value is None else format(value, spec)


def _metric_lines(summary: dict[str, Any], indent: str = "  ") -> list[str]:
    j = _fmt(summary["youden_j"])
    amplification = _fmt(summary["error_amplification"], ".1f")
    return [
        f"{indent}bots caught (sensitivity)  : {summary['sensitivity']['text']}",
        f"{indent}humans left alone (spec.)  : {summary['specificity']['text']}",
        f"{indent}called bot-like, really bot: {summary['ppv']['text']}",
        f"{indent}Youden J {j}  (audience-% error is multiplied by x{amplification})",
    ]


def format_report(result: dict[str, Any], split: str = "all") -> str:
    """Plain-text (ASCII) report of an `evaluate` result."""
    lab, cov = result["labels"], result["coverage"]
    out = [
        "BOT MICROSCOPE - EVALUATION AGAINST YOUR CONFIRMED LABELS",
        '"95%" means a 95% Wilson confidence interval. Bot-like is the positive class.',
        "",
        f"LABELS (split: {split})",
        f"  bots: {lab['bots']}   humans: {lab['humans']}   total: {lab['total']}",
    ]
    if lab["conflicts_counted_as_humans"]:
        out.append(
            f"  in both files (owner rule: counted as humans): "
            f"{', '.join(lab['conflicts_counted_as_humans'])}"
        )
    if lab["bot_duplicate_lines"] or lab["human_duplicate_lines"]:
        out.append(
            f"  repeated lines: bots {lab['bot_duplicate_lines'] or '-'}  "
            f"users {lab['human_duplicate_lines'] or '-'}"
        )
    if lab["ignored_lines"]:
        out.append(f"  ignored (not a handle): {lab['ignored_lines']}")
    if split == "test":
        out.append("  NOTE: this is the locked test split; every look at it counts as a peek.")

    out += ["", "1. HANDLE-ONLY RULES (username only; every labeled account counts)"]
    for name, summary in result["handle_only"].items():
        out.append(f" {name}")
        out += _metric_lines(summary, indent="    ")

    out += [
        "",
        "2. HOW MANY LABELED ACCOUNTS HAVE CAPTURED NUMBERS",
        f"  not captured at all                         : {cov['not_captured']}",
        f"  list only / partial (no scoreable numbers)  : {cov['list_only']}",
        f"  complete, written by this extension version : {cov['complete_trusted']}",
        f"  complete, from older builds (unverified)    : {cov['complete_legacy_unverified']}",
        "",
        "3. THE APP'S SCORER, only on accounts with every number observed",
    ]
    for key, title in (
        ("trusted", "trusted rows"),
        ("legacy_unverified", "older-build rows (numbers cannot be proven real)"),
    ):
        summary = result["app_scorer"][key]
        if summary is None:
            out.append(f" {title}: none yet")
            continue
        out.append(f" {title}: {summary['bots']} bots, {summary['humans']} humans")
        out += _metric_lines(summary, indent="    ")
        if summary.get("tiers"):
            t = summary["tiers"]
            out.append("    3-tier score breakdown:")
            for tk, tlbl in (
                ("bot_gt_60", "61-100p Auto-Bot  "),
                ("suspicious_35_60", "35-60p  Suspicious"),
                ("clean_0_34", "0-34p   Clean     "),
            ):
                row_t = t[tk]
                tot_t = row_t["bots"] + row_t["humans"]
                pct_t = f"{100 * row_t['bots'] / tot_t:5.1f}%" if tot_t else "  n/a "
                out.append(
                    f"      {tlbl}: {row_t['bots']:4d} bots, {row_t['humans']:4d} humans (bot share: {pct_t})"
                )
    out += [
        "",
        "READ THIS: a Wilson interval measures sampling error only. With few rows it is wide;",
        "with no rows there is nothing to claim. It does not say the labels or numbers are right.",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the bot-like scoring rules against your confirmed labels."
    )
    parser.add_argument("--bots", type=Path, default=DEFAULT_BOTS_PATH, help="bot labels file")
    parser.add_argument("--users", type=Path, default=DEFAULT_USERS_PATH, help="human labels file")
    parser.add_argument(
        "--checkpoints", type=Path, default=CHECKPOINTS_DIR, help="saved captures folder"
    )
    parser.add_argument("--split", choices=SPLITS, default="all", help="which labels to use")
    parser.add_argument("--json", type=Path, default=None, help="also write the result as JSON")
    args = parser.parse_args(argv)

    try:
        labels = load_labels(args.bots, args.users).only(args.split)
        result = evaluate(labels, args.checkpoints)
    except (LabelError, ConfigError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    print(format_report(result, args.split))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nJSON written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
