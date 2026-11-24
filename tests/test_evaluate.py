"""Tests for the evaluation script, on a tiny label set whose answers are worked out by hand.

Handle rules (see botaudit.handles):
  bots   zebra.9605848  digit-heavy + word.digits          -> digit rules yes, stitched no
         quail.87407895 digit-heavy + word.digits          -> digit rules yes, stitched no
         _.little_qf.y  adjacent "_." punctuation          -> digit rules no,  stitched yes
         mamamatii_     nothing unusual                    -> no rule fires
         chashayasha    nothing unusual                    -> no rule fires
  humans karinushul, ciprianciutreofficial, ana.maria24    -> no rule fires
         victor__macovei  adjacent "__" punctuation        -> stitched yes (a false alarm)
         tekk1177         50% digits                       -> digit rules yes (a false alarm)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from botaudit.evaluate import evaluate, format_report, main
from botaudit.labels import load_labels


def _lines(*items: str) -> str:
    return "\n".join(items) + "\n"


def _build(tmp_path: Path) -> tuple[Path, Path, Path]:
    bots = tmp_path / "verified_bots.csv"
    bots.write_text(
        _lines(
            "https://www.instagram.com/zebra.9605848/",
            "@quail.87407895",
            "_.little_qf.y",
            "mamamatii_",
            "chashayasha",
            "zebra.9605848",  # repeated line
            "ciprianciutreofficial",  # also a human: the owner rule makes it a human
        ),
        encoding="utf-8",
    )
    users = tmp_path / "verified_users.csv"
    users.write_text(
        _lines(
            "ciprianciutreofficial",
            "karinushul",
            "victor__macovei",
            "tekk1177",
            "https://www.instagram.com/ana.maria24/",
        ),
        encoding="utf-8",
    )
    checkpoints = tmp_path / "checkpoints"
    checkpoints.mkdir()

    def full(username: str, **numbers: Any) -> dict[str, Any]:
        row = {
            "username": username,
            "has_profile_photo": True,
            "account_private": False,
            "post_count": 42,
            "followers_count": 680,
            "following_count": 410,
            "bio_length": 58,
            "post_timestamps": [],
        }
        row.update(numbers)
        return row

    profiles = [
        # complete, written by the current schema
        full("zebra.9605848", schema_version=3, post_count=4, followers_count=18,
             following_count=1950, bio_length=0),
        full("karinushul", schema_version=3),
        # complete but saved by an older build: numbers cannot be proven real
        full("mamamatii_", deep_checked=True, post_count=0, followers_count=466,
             following_count=580, bio_length=2),
        # partial: not scoreable
        {"username": "chashayasha", "schema_version": 3, "has_profile_photo": True,
         "account_private": None, "post_count": None, "followers_count": None,
         "following_count": None, "bio_length": None, "post_timestamps": []},
        # placeholder numbers invented by an old build: scrubbed to unknown
        full("ana.maria24", deep_checked=False, post_count=24, followers_count=340,
             following_count=290, bio_length=42),
    ]
    (checkpoints / "artist_a.json").write_text(
        json.dumps({"artist": "artist_a", "profiles": profiles}), encoding="utf-8"
    )
    return bots, users, checkpoints


def test_handle_only_metrics_match_the_hand_calculation(tmp_path: Path) -> None:
    bots, users, checkpoints = _build(tmp_path)
    result = evaluate(load_labels(bots, users), checkpoints)

    assert result["labels"]["bots"] == 5
    assert result["labels"]["humans"] == 5
    assert result["labels"]["conflicts_counted_as_humans"] == ["ciprianciutreofficial"]
    assert result["labels"]["bot_duplicate_lines"] == ["zebra.9605848"]

    any_flag = result["handle_only"]["any handle flag"]
    assert (any_flag["tp"], any_flag["fn"], any_flag["fp"], any_flag["tn"]) == (3, 2, 2, 3)
    assert any_flag["sensitivity"]["text"] == "60.0% [23.1, 88.2] (3/5)"
    assert any_flag["youden_j"] == pytest.approx(0.2)
    assert any_flag["error_amplification"] == pytest.approx(5.0)

    digits = result["handle_only"]["digit rules only"]
    assert (digits["tp"], digits["fn"], digits["fp"], digits["tn"]) == (2, 3, 1, 4)

    stitched = result["handle_only"]["stitched rule only"]
    assert (stitched["tp"], stitched["fn"], stitched["fp"], stitched["tn"]) == (1, 4, 1, 4)


def test_coverage_separates_trusted_legacy_partial_and_missing(tmp_path: Path) -> None:
    bots, users, checkpoints = _build(tmp_path)
    coverage = evaluate(load_labels(bots, users), checkpoints)["coverage"]
    assert coverage == {
        "labeled": 10,
        "not_captured": 5,
        "list_only": 2,  # chashayasha (partial) and ana.maria24 (placeholder scrubbed)
        "complete_trusted": 2,  # zebra.9605848, karinushul
        "complete_legacy_unverified": 1,  # mamamatii_
    }


def test_the_app_scorer_runs_only_on_complete_rows_and_keeps_groups_apart(tmp_path: Path) -> None:
    bots, users, checkpoints = _build(tmp_path)
    scorer = evaluate(load_labels(bots, users), checkpoints)["app_scorer"]

    trusted = scorer["trusted"]
    assert (trusted["bots"], trusted["humans"]) == (1, 1)
    assert (trusted["tp"], trusted["tn"], trusted["fp"], trusted["fn"]) == (1, 1, 0, 0)  # 70 pts

    legacy = scorer["legacy_unverified"]
    assert (legacy["bots"], legacy["humans"]) == (1, 0)
    assert (legacy["tp"], legacy["fn"]) == (0, 1)  # 0 posts + empty bio = 35 pts: below the cutoff
    assert legacy["specificity"]["text"] == "n/a (0/0)"


def test_without_any_captured_numbers_there_is_nothing_to_claim(tmp_path: Path) -> None:
    bots, users, _ = _build(tmp_path)
    empty = tmp_path / "no_checkpoints"
    empty.mkdir()
    result = evaluate(load_labels(bots, users), empty)
    assert result["app_scorer"] == {"trusted": None, "legacy_unverified": None}
    assert result["coverage"]["not_captured"] == 10
    assert "none yet" in format_report(result)


def test_report_is_plain_ascii_and_states_what_95_percent_means(tmp_path: Path) -> None:
    bots, users, checkpoints = _build(tmp_path)
    report = format_report(evaluate(load_labels(bots, users), checkpoints))
    report.encode("ascii")  # must print on any Windows console
    assert "95% Wilson confidence interval" in report
    assert "ciprianciutreofficial" in report  # the conflict is shown, not hidden
    assert "60.0% [23.1, 88.2] (3/5)" in report
    assert "older-build rows (numbers cannot be proven real)" in report


def test_cli_prints_the_report_and_writes_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bots, users, checkpoints = _build(tmp_path)
    out_json = tmp_path / "out" / "evaluation.json"
    code = main(
        ["--bots", str(bots), "--users", str(users), "--checkpoints", str(checkpoints),
         "--json", str(out_json)]
    )
    assert code == 0
    assert "EVALUATION AGAINST YOUR CONFIRMED LABELS" in capsys.readouterr().out
    assert json.loads(out_json.read_text(encoding="utf-8"))["labels"]["total"] == 10


def test_cli_missing_label_file_is_a_friendly_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, users, checkpoints = _build(tmp_path)
    code = main(["--bots", str(tmp_path / "nope.csv"), "--users", str(users),
                 "--checkpoints", str(checkpoints)])
    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_the_locked_test_split_is_flagged_as_a_peek(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bots, users, checkpoints = _build(tmp_path)
    main(["--bots", str(bots), "--users", str(users), "--checkpoints", str(checkpoints),
          "--split", "test"])
    assert "counts as a peek" in capsys.readouterr().out
