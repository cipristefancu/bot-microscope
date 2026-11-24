"""Tests for the capture rules (R-NOFAKE): unknown stays unknown, old guesses are removed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from botaudit.capture import (
    LEGACY_PLACEHOLDER_TUPLES,
    SCHEMA_VERSION,
    best_profiles,
    capture_rank,
    is_legacy_placeholder,
    is_scorable,
    merge_profiles,
    profile_tier,
    scrub_legacy_profile,
)


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "username": "someone",
        "has_profile_photo": True,
        "account_private": False,
        "post_count": 12,
        "followers_count": 300,
        "following_count": 250,
        "bio_length": 30,
    }
    row.update(overrides)
    return row


def _placeholder(stats: tuple[int, int, int, int], **overrides: Any) -> dict[str, Any]:
    keys = ("post_count", "followers_count", "following_count", "bio_length")
    return _row(
        **dict(zip(keys, stats, strict=True)),
        deep_checked=False,
        bot_like=True,
        bot_score=0.7,
        score_pts=70,
        flags={"zero_posts": True},
        same_day_post_burst=False,
        **overrides,
    )


@pytest.mark.parametrize("stats", sorted(LEGACY_PLACEHOLDER_TUPLES))
def test_every_known_placeholder_is_scrubbed_to_unknown(stats: tuple[int, int, int, int]) -> None:
    scrubbed = scrub_legacy_profile(_placeholder(stats))
    for field in ("post_count", "followers_count", "following_count", "bio_length"):
        assert scrubbed[field] is None
    assert scrubbed["account_private"] is None
    assert scrubbed["legacy_scrubbed"] is True
    assert scrubbed["deep_checked"] is False
    # Verdicts that were computed from the invented numbers are removed with them.
    for derived in ("bot_like", "bot_score", "score_pts", "flags", "same_day_post_burst"):
        assert derived not in scrubbed
    assert scrubbed["tier"] == 1  # the avatar flag is still known
    assert is_scorable(scrubbed) is False


def test_scrub_does_not_modify_its_input() -> None:
    original = _placeholder((24, 340, 290, 42))
    snapshot = dict(original)
    scrub_legacy_profile(original)
    assert original == snapshot


def test_deep_checked_row_is_kept_but_marked_unverified() -> None:
    row = _placeholder((24, 340, 290, 42))
    row["deep_checked"] = True
    kept = scrub_legacy_profile(row)
    assert kept["post_count"] == 24
    assert kept["legacy_unverified"] is True
    assert "legacy_scrubbed" not in kept
    assert is_legacy_placeholder(row) is False


def test_current_schema_rows_are_never_scrubbed() -> None:
    # A real account can happen to have these numbers; observed data is never second-guessed.
    row = _placeholder((24, 340, 290, 42), schema_version=SCHEMA_VERSION)
    kept = scrub_legacy_profile(row)
    assert kept["post_count"] == 24
    assert "legacy_unverified" not in kept
    assert "legacy_scrubbed" not in kept


def test_old_row_with_ordinary_numbers_is_kept_and_marked_unverified() -> None:
    kept = scrub_legacy_profile(_row(deep_checked=False))
    assert kept["post_count"] == 12
    assert kept["legacy_unverified"] is True


@pytest.mark.parametrize(
    ("overrides", "tier", "scorable"),
    [
        ({"has_profile_photo": None, "account_private": None, "post_count": None,
          "followers_count": None, "following_count": None, "bio_length": None}, 0, False),
        ({"post_count": None, "followers_count": None, "following_count": None,
          "bio_length": None}, 1, False),
        ({"bio_length": None}, 1, False),
        ({}, 2, True),
        ({"post_timestamps": [1715000000, 1715000300]}, 3, True),
        ({"account_private": None}, 2, False),
        ({"followers_count": True}, 1, False),  # a bool is not a count
        ({"post_count": -1}, 1, False),
    ],
)
def test_tier_and_scorable(overrides: dict[str, Any], tier: int, scorable: bool) -> None:
    row = _row(**overrides)
    assert profile_tier(row) == tier
    assert is_scorable(row) is scorable


def test_merge_never_erases_numbers_observed_earlier() -> None:
    old = _row(schema_version=SCHEMA_VERSION, src={"post_count": "api_json"})
    new = {
        "username": "someone",
        "schema_version": SCHEMA_VERSION,
        "has_profile_photo": None,
        "account_private": None,
        "post_count": None,
        "followers_count": None,
        "following_count": None,
        "bio_length": None,
        "post_timestamps": [],
    }
    merged = merge_profiles(old, new)
    assert merged["post_count"] == 12
    assert merged["followers_count"] == 300
    assert merged["tier"] == 2
    assert merged["src"] == {"post_count": "api_json"}
    assert "legacy_unverified" not in merged


def test_merge_new_observation_overrides_and_adds() -> None:
    old = _row(schema_version=SCHEMA_VERSION)
    new = _row(schema_version=SCHEMA_VERSION, post_count=99, src={"post_count": "dom_header"})
    merged = merge_profiles(old, new)
    assert merged["post_count"] == 99
    assert merged["followers_count"] == 300
    assert merged["src"]["post_count"] == "dom_header"


def test_merge_keeps_old_numbers_marked_unverified_when_new_data_is_partial() -> None:
    old = _row(deep_checked=True)  # no schema_version: saved by an older build
    new = {"username": "someone", "schema_version": SCHEMA_VERSION, "post_count": None}
    merged = merge_profiles(old, new)
    assert merged["post_count"] == 12
    assert merged["legacy_unverified"] is True
    assert merged["schema_version"] is None


def test_merge_complete_new_observation_replaces_legacy_numbers() -> None:
    old = _row(deep_checked=True)
    new = _row(schema_version=SCHEMA_VERSION, post_count=7, followers_count=8, following_count=9,
               bio_length=10)
    merged = merge_profiles(old, new)
    assert merged["post_count"] == 7
    assert merged["schema_version"] == SCHEMA_VERSION
    assert "legacy_unverified" not in merged


def test_merging_two_older_rows_keeps_them_marked_unverified() -> None:
    # What every live sync does: the extension re-sends the older rows it loaded.
    old = _row(deep_checked=True)
    merged = merge_profiles(old, dict(old))
    assert merged["schema_version"] is None
    assert merged["legacy_unverified"] is True


@pytest.mark.parametrize("current_first", [True, False])
def test_an_observed_number_beats_an_older_unproven_one_in_either_order(
    current_first: bool,
) -> None:
    current = _row(schema_version=SCHEMA_VERSION, post_count=999)
    older = _row(deep_checked=True, post_count=5)
    merged = merge_profiles(current, older) if current_first else merge_profiles(older, current)
    assert merged["post_count"] == 999
    assert merged["schema_version"] == SCHEMA_VERSION
    assert "legacy_unverified" not in merged


def test_numbers_only_older_rows_hold_stay_flagged_next_to_observed_ones() -> None:
    current = _row(
        schema_version=SCHEMA_VERSION,
        post_count=999,
        followers_count=None,
        following_count=None,
        bio_length=None,
    )
    merged = merge_profiles(current, _row(deep_checked=True))
    assert merged["post_count"] == 999  # observed
    assert merged["followers_count"] == 300  # kept, but unproven
    assert merged["legacy_unverified"] is True
    assert merged["schema_version"] is None


def test_capture_rank_orders_completeness() -> None:
    assert capture_rank(_row(schema_version=SCHEMA_VERSION)) == 3
    assert capture_rank(_row()) == 2
    assert capture_rank(_row(post_count=None)) == 1
    assert capture_rank({"username": "x"}) == 0


def test_best_profiles_prefers_the_most_complete_row_and_scrubs_placeholders(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.json").write_text(
        json.dumps(
            {
                "artist": "a",
                "profiles": [
                    _placeholder((0, 18, 2150, 0), username="Mixed.Case"),
                    _placeholder((24, 340, 290, 42), username="guess.only"),
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "b.json").write_text(
        json.dumps(
            {
                "artist": "b",
                "profiles": [_row(username="mixed.case", schema_version=SCHEMA_VERSION)],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    best = best_profiles(tmp_path)
    assert best["mixed.case"]["schema_version"] == SCHEMA_VERSION
    assert best["mixed.case"]["post_count"] == 12
    assert best["guess.only"]["post_count"] is None
    assert best["guess.only"]["checkpoint"] == "a"
