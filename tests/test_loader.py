"""Tests for CSV follower data loader and validator (botaudit.loader)."""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

from botaudit.loader import (
    CSVValidationError,
    LoadResult,
    RejectedRow,
    load_followers_csv,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_load_valid_basic_with_whitespace_and_case(tmp_path: Path) -> None:
    """AC1, AC4, AC5: Load clean data, parse mixed-case booleans, and trim whitespace."""
    rejects_file = tmp_path / "rejects.csv"
    res = load_followers_csv(FIXTURES_DIR / "valid_basic.csv", rejects_path=rejects_file)

    assert isinstance(res, LoadResult)
    # Test tuple unpacking
    df, rejects = res
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 4
    assert len(rejects) == 0
    assert rejects_file.is_file()

    # AC5: Whitespace trimming check
    assert df["artist"].iloc[0] == "ArtistA"
    assert df["username"].iloc[0] == "user_one"

    # AC4: Case-insensitive boolean parsing (TRUE / False / YES / No / 1 / 0)
    assert bool(df["has_profile_photo"].iloc[0]) is True
    assert bool(df["account_private"].iloc[0]) is False
    assert bool(df["has_profile_photo"].iloc[1]) is True
    assert bool(df["has_profile_photo"].iloc[2]) is True
    assert bool(df["account_private"].iloc[2]) is False

    # Column dtypes
    assert df["has_profile_photo"].dtype == bool
    assert df["post_count"].dtype == "int64"
    assert df["username_digit_ratio"].dtype == "float64"


def test_missing_required_columns() -> None:
    """AC2: Missing required columns raise friendly error naming missing columns."""
    with pytest.raises(CSVValidationError) as err:
        load_followers_csv(FIXTURES_DIR / "missing_columns.csv")

    msg = str(err.value)
    assert "missing required columns" in msg
    assert "bio_length" in msg
    assert "post_count" in msg


def test_empty_file_error() -> None:
    """AC3: Empty file raises clear CSVValidationError."""
    with pytest.raises(CSVValidationError, match="CSV file is empty"):
        load_followers_csv(FIXTURES_DIR / "empty_file.csv")


def test_header_only_file_error() -> None:
    """AC3: Header-only file raises clear CSVValidationError."""
    with pytest.raises(CSVValidationError, match="contains only a header and no data rows"):
        load_followers_csv(FIXTURES_DIR / "header_only.csv")


def test_file_not_found_error() -> None:
    """Non-existent file raises clear CSVValidationError."""
    with pytest.raises(CSVValidationError, match="File not found"):
        load_followers_csv("tests/fixtures/non_existent.csv")


def test_invalid_booleans_rejected_and_structure_checked(tmp_path: Path) -> None:
    """AC1 & AC4: Strict booleans; bad booleans rejected with RejectedRow details."""
    rejects_file = tmp_path / "rejects.csv"
    res = load_followers_csv(FIXTURES_DIR / "invalid_booleans.csv", rejects_path=rejects_file)

    # 1 good row, 3 rejected rows
    assert len(res.df) == 1
    assert len(res.rejects) == 3

    # Check RejectedRow attributes
    first_reject = res.rejects[0]
    assert isinstance(first_reject, RejectedRow)
    assert first_reject.row_number == 3
    assert first_reject.artist == "ArtistA"
    assert first_reject.username == "bad_photo"
    assert "has_profile_photo" in first_reject.reason
    assert isinstance(first_reject.raw_data, dict)

    assert any("has_profile_photo" in r.reason for r in res.rejects)
    assert any("account_private" in r.reason for r in res.rejects)
    assert any(r.username == "numeric_two" for r in res.rejects)

    # Verify rejects.csv written
    assert rejects_file.is_file()
    with rejects_file.open(newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
    assert len(reader) == 3
    assert reader[0]["artist"] == "ArtistA"


def test_invalid_counts_and_ratios_rejected(tmp_path: Path) -> None:
    """AC5: Negative counts, decimal counts, ratio > 1, empty username rejected."""
    rejects_file = tmp_path / "rejects.csv"
    res = load_followers_csv(
        FIXTURES_DIR / "invalid_counts_and_ratios.csv", rejects_path=rejects_file
    )

    # 1 valid row ("good_user"), 8 invalid rows rejected
    assert len(res.df) == 1
    assert len(res.rejects) == 8

    assert any(r.username == "negative_posts" for r in res.rejects)
    assert any(r.username == "decimal_posts" for r in res.rejects)
    assert any(r.username == "text_followers" for r in res.rejects)
    assert any(r.username == "negative_following" for r in res.rejects)
    assert any(r.username == "negative_bio" for r in res.rejects)
    assert any(r.username == "high_ratio" for r in res.rejects)
    assert any(r.username == "negative_ratio" for r in res.rejects)
    assert any(r.reason == "username is empty" for r in res.rejects)


def test_duplicates_rejected_and_first_kept(tmp_path: Path) -> None:
    """AC6: Duplicate (artist, username) pairs: keep first occurrence, reject subsequent."""
    rejects_file = tmp_path / "rejects.csv"
    res = load_followers_csv(FIXTURES_DIR / "duplicates.csv", rejects_path=rejects_file)

    # ArtistA has clara_music twice (lower) and CLARA_MUSIC once (upper).
    # First row: post_count 10, has_profile_photo True.
    # Second row: post_count 0. Third row: post_count 12.
    # Fourth row: ArtistB with clara_music (different artist, kept).
    assert len(res.df) == 2
    assert len(res.rejects) == 2

    # Verify FIRST occurrence's data was retained!
    artist_a_row = res.df[res.df["artist"] == "ArtistA"].iloc[0]
    assert artist_a_row["username"] == "clara_music"
    assert artist_a_row["post_count"] == 10
    assert bool(artist_a_row["has_profile_photo"]) is True

    assert res.rejects[0].reason == "duplicate"
    assert res.rejects[1].reason == "duplicate"
    assert res.rejects[0].row_number == 3
    assert res.rejects[1].row_number == 4


def test_extra_columns_ignored_and_utf8_bom(tmp_path: Path) -> None:
    """AC7: Extra columns ignored with warning; UTF-8 BOM verified and non-English usernames load."""
    fixture_path = FIXTURES_DIR / "extra_columns_and_bom.csv"

    # Explicitly verify the fixture file actually contains the UTF-8 BOM bytes
    raw_bytes = fixture_path.read_bytes()
    assert raw_bytes.startswith(b"\xef\xbb\xbf"), "Fixture must start with UTF-8 BOM bytes"

    rejects_file = tmp_path / "rejects.csv"
    res = load_followers_csv(fixture_path, rejects_path=rejects_file)

    assert len(res.rejects) == 0
    assert len(res.df) == 4

    # Extra columns ignored with warning
    assert any("Ignored extra columns" in w for w in res.warnings)
    assert "notes" not in res.df.columns
    assert "extra_tag" not in res.df.columns

    # Non-English usernames loaded properly
    usernames = list(res.df["username"])
    assert "elena_música" in usernames
    assert "саша_1998" in usernames
    assert "τάκης" in usernames
    assert "タロウ" in usernames


def test_high_reject_fraction_warning(tmp_path: Path) -> None:
    """AC8: Artist with reject share > max_reject_fraction triggers warning."""
    rejects_file = tmp_path / "rejects.csv"
    res = load_followers_csv(
        FIXTURES_DIR / "high_reject_fraction.csv",
        config={"max_reject_fraction": 0.20},
        rejects_path=rejects_file,
    )

    assert len(res.rejects) == 3
    assert len(res.df) == 3

    # Check warning specifically names ArtistA and shows percentage
    artist_a_warnings = [w for w in res.warnings if "ArtistA" in w]
    assert len(artist_a_warnings) == 1
    assert "75.0%" in artist_a_warnings[0]

    # No warning for ArtistB
    assert not any("ArtistB" in w for w in res.warnings)


def test_default_config_loading() -> None:
    """AC8: When config is omitted, max_reject_fraction is loaded from config.yaml."""
    res = load_followers_csv(FIXTURES_DIR / "high_reject_fraction.csv")
    assert any("ArtistA" in w and "75.0%" in w for w in res.warnings)


def test_reject_rows_written_to_destination(tmp_path: Path) -> None:
    """AC9: Rejected rows are properly written to output CSV file."""
    rejects_file = tmp_path / "custom_rejects.csv"
    load_followers_csv(FIXTURES_DIR / "invalid_booleans.csv", rejects_path=rejects_file)

    assert rejects_file.is_file()
    with rejects_file.open(newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))

    assert len(reader) == 3
    assert list(reader[0].keys()) == ["row_number", "artist", "username", "reason"]
    assert reader[0]["row_number"] == "3"
    assert reader[0]["artist"] == "ArtistA"
    assert reader[0]["username"] == "bad_photo"


def test_remote_url_rejected() -> None:
    """Security check: URLs are rejected to prevent network access."""
    with pytest.raises(CSVValidationError, match="URLs are not supported"):
        load_followers_csv("https://evil.com/fake.csv")
