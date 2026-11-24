"""CSV loader with validation for follower profile datasets.

This module reads a CSV file provided by the user, validates every row,
and produces a clean pandas DataFrame. Any bad or duplicate row is recorded
with a clear reason and written to output/rejects.csv, never crashing the
pipeline and never silently dropping data.

This tool never connects to the internet.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from botaudit.config import load_config

REQUIRED_COLUMNS: tuple[str, ...] = (
    "artist",
    "username",
    "has_profile_photo",
    "post_count",
    "followers_count",
    "following_count",
    "bio_length",
    "account_private",
    "username_digit_ratio",
)

# Strict boolean parsing contract:
# Booleans accept true/false, 1/0, yes/no in any letter case. Anything else rejects the row.
TRUE_VALUES: frozenset[str] = frozenset({"true", "1", "yes"})
FALSE_VALUES: frozenset[str] = frozenset({"false", "0", "no"})


class CSVValidationError(ValueError):
    """Raised when the CSV file cannot be parsed or lacks required headers."""


@dataclass
class RejectedRow:
    """A row from the CSV that could not be validated."""

    row_number: int
    artist: str
    username: str
    reason: str
    raw_data: dict[str, str]


@dataclass
class LoadResult:
    """Result of loading a followers CSV file."""

    df: pd.DataFrame
    rejects: list[RejectedRow]
    warnings: list[str]

    def __iter__(self):
        """Allow tuple unpacking: `df, rejects = load_followers_csv(...)`."""
        return iter((self.df, self.rejects))


def _parse_bool(value: str) -> bool | None:
    """Parse boolean strings in any case. Returns None if invalid."""
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return None


def _parse_non_negative_int(value: str) -> int | None:
    """Parse a non-negative integer. Returns None if invalid."""
    stripped = value.strip()
    if not stripped:
        return None
    try:
        val = int(stripped)
        return val if val >= 0 else None
    except ValueError:
        return None


def _parse_ratio(value: str) -> float | None:
    """Parse a ratio between 0.0 and 1.0. Returns None if invalid."""
    stripped = value.strip()
    if not stripped:
        return None
    try:
        val = float(stripped)
        return val if 0.0 <= val <= 1.0 else None
    except ValueError:
        return None


def _write_rejects_csv(rejects: list[RejectedRow], destination: Path) -> None:
    """Write rejected rows to destination CSV."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["row_number", "artist", "username", "reason"]
    with destination.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rejects:
            writer.writerow({
                "row_number": r.row_number,
                "artist": r.artist,
                "username": r.username,
                "reason": r.reason,
            })


def load_followers_csv(
    path: str | Path,
    config: dict[str, Any] | None = None,
    rejects_path: str | Path = Path("output/rejects.csv"),
) -> LoadResult:
    """Load and validate a CSV file of sampled follower profiles.

    Args:
        path: Path to the input CSV file. URLs are forbidden.
        config: Optional configuration dictionary. If omitted, loads config.yaml.
        rejects_path: Where to write rejected rows (default: output/rejects.csv).

    Returns:
        LoadResult containing clean DataFrame, rejected rows list, and warnings list.

    Raises:
        CSVValidationError: If file is missing, empty, header-only, lacks required
            columns, or is a remote URL.
    """
    path_str = str(path)
    if path_str.startswith(("http://", "https://", "ftp://")):
        raise CSVValidationError("URLs are not supported; input must be a local CSV file.")

    file_path = Path(path)
    if not file_path.is_file():
        raise CSVValidationError(f"File not found: {file_path}")

    try:
        content = file_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as err:
        raise CSVValidationError(
            f"File {file_path} is not valid UTF-8 text: {err}"
        ) from err

    # Check for empty file
    if not content.strip():
        raise CSVValidationError(f"CSV file is empty: {file_path}")

    # Load configuration if not provided (thresholds live only in config.yaml)
    if config is None:
        config = load_config()

    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise CSVValidationError(f"CSV file has no header row: {file_path}")

    # Trim whitespace on headers
    cleaned_fieldnames = [
        h.strip() for h in reader.fieldnames if h is not None
    ]
    missing_columns = [
        col for col in REQUIRED_COLUMNS if col not in cleaned_fieldnames
    ]
    if missing_columns:
        raise CSVValidationError(
            f"CSV file is missing required columns: {', '.join(missing_columns)}"
        )

    warnings: list[str] = []
    extra_columns = [
        col for col in cleaned_fieldnames if col not in REQUIRED_COLUMNS
    ]
    if extra_columns:
        warnings.append(f"Ignored extra columns: {', '.join(extra_columns)}")

    valid_rows: list[dict[str, Any]] = []
    rejects: list[RejectedRow] = []
    seen_pairs: set[tuple[str, str]] = set()

    # Track total and rejected rows per artist
    artist_total_count: dict[str, int] = {}
    artist_reject_count: dict[str, int] = {}

    row_count = 0
    for raw_row in reader:
        row_count += 1
        row_idx = reader.line_num  # Physical line number in source file

        # Trim whitespace on all values
        row = {
            k.strip(): (v.strip() if v is not None else "")
            for k, v in raw_row.items()
            if k is not None
        }

        artist = row.get("artist", "")
        username = row.get("username", "")

        if not artist:
            rejects.append(
                RejectedRow(
                    row_number=row_idx,
                    artist="",
                    username=username,
                    reason="artist name is empty",
                    raw_data=raw_row,
                )
            )
            continue

        artist_total_count[artist] = artist_total_count.get(artist, 0) + 1

        if not username:
            artist_reject_count[artist] = artist_reject_count.get(artist, 0) + 1
            rejects.append(
                RejectedRow(
                    row_number=row_idx,
                    artist=artist,
                    username="",
                    reason="username is empty",
                    raw_data=raw_row,
                )
            )
            continue

        # Check duplicate (artist, username) pair
        pair_key = (artist.lower(), username.lower())
        if pair_key in seen_pairs:
            artist_reject_count[artist] = artist_reject_count.get(artist, 0) + 1
            rejects.append(
                RejectedRow(
                    row_number=row_idx,
                    artist=artist,
                    username=username,
                    reason="duplicate",
                    raw_data=raw_row,
                )
            )
            continue
        seen_pairs.add(pair_key)

        # Validate booleans
        has_photo = _parse_bool(row.get("has_profile_photo", ""))
        if has_photo is None:
            artist_reject_count[artist] = artist_reject_count.get(artist, 0) + 1
            rejects.append(
                RejectedRow(
                    row_number=row_idx,
                    artist=artist,
                    username=username,
                    reason=f"invalid boolean for has_profile_photo: '{row.get('has_profile_photo', '')}'",
                    raw_data=raw_row,
                )
            )
            continue

        is_private = _parse_bool(row.get("account_private", ""))
        if is_private is None:
            artist_reject_count[artist] = artist_reject_count.get(artist, 0) + 1
            rejects.append(
                RejectedRow(
                    row_number=row_idx,
                    artist=artist,
                    username=username,
                    reason=f"invalid boolean for account_private: '{row.get('account_private', '')}'",
                    raw_data=raw_row,
                )
            )
            continue

        # Validate non-negative integer counts
        post_count = _parse_non_negative_int(row.get("post_count", ""))
        if post_count is None:
            artist_reject_count[artist] = artist_reject_count.get(artist, 0) + 1
            rejects.append(
                RejectedRow(
                    row_number=row_idx,
                    artist=artist,
                    username=username,
                    reason=f"invalid post_count: '{row.get('post_count', '')}' (must be whole number >= 0)",
                    raw_data=raw_row,
                )
            )
            continue

        followers_count = _parse_non_negative_int(row.get("followers_count", ""))
        if followers_count is None:
            artist_reject_count[artist] = artist_reject_count.get(artist, 0) + 1
            rejects.append(
                RejectedRow(
                    row_number=row_idx,
                    artist=artist,
                    username=username,
                    reason=f"invalid followers_count: '{row.get('followers_count', '')}' (must be whole number >= 0)",
                    raw_data=raw_row,
                )
            )
            continue

        following_count = _parse_non_negative_int(row.get("following_count", ""))
        if following_count is None:
            artist_reject_count[artist] = artist_reject_count.get(artist, 0) + 1
            rejects.append(
                RejectedRow(
                    row_number=row_idx,
                    artist=artist,
                    username=username,
                    reason=f"invalid following_count: '{row.get('following_count', '')}' (must be whole number >= 0)",
                    raw_data=raw_row,
                )
            )
            continue

        bio_length = _parse_non_negative_int(row.get("bio_length", ""))
        if bio_length is None:
            artist_reject_count[artist] = artist_reject_count.get(artist, 0) + 1
            rejects.append(
                RejectedRow(
                    row_number=row_idx,
                    artist=artist,
                    username=username,
                    reason=f"invalid bio_length: '{row.get('bio_length', '')}' (must be whole number >= 0)",
                    raw_data=raw_row,
                )
            )
            continue

        # Validate ratio
        digit_ratio = _parse_ratio(row.get("username_digit_ratio", ""))
        if digit_ratio is None:
            artist_reject_count[artist] = artist_reject_count.get(artist, 0) + 1
            rejects.append(
                RejectedRow(
                    row_number=row_idx,
                    artist=artist,
                    username=username,
                    reason=f"invalid username_digit_ratio: '{row.get('username_digit_ratio', '')}' (must be between 0 and 1)",
                    raw_data=raw_row,
                )
            )
            continue

        valid_rows.append({
            "artist": artist,
            "username": username,
            "has_profile_photo": has_photo,
            "post_count": post_count,
            "followers_count": followers_count,
            "following_count": following_count,
            "bio_length": bio_length,
            "account_private": is_private,
            "username_digit_ratio": digit_ratio,
        })

    if row_count == 0:
        raise CSVValidationError(
            f"CSV file contains only a header and no data rows: {file_path}"
        )

    # Check max_reject_fraction per artist
    max_reject = float(config["max_reject_fraction"])
    for artist, total in artist_total_count.items():
        rejected = artist_reject_count.get(artist, 0)
        fraction = rejected / total if total > 0 else 0.0
        if fraction > max_reject:
            warnings.append(
                f"Artist '{artist}' has {rejected}/{total} rejected rows ({fraction:.1%}), "
                f"exceeding max allowed reject fraction of {max_reject:.1%}."
            )

    # Write rejects CSV
    _write_rejects_csv(rejects, Path(rejects_path))

    # Construct DataFrame with proper types
    if valid_rows:
        df = pd.DataFrame(valid_rows).astype({
            "artist": "string",
            "username": "string",
            "has_profile_photo": "bool",
            "post_count": "int64",
            "followers_count": "int64",
            "following_count": "int64",
            "bio_length": "int64",
            "account_private": "bool",
            "username_digit_ratio": "float64",
        })
    else:
        df = pd.DataFrame(columns=list(REQUIRED_COLUMNS)).astype({
            "artist": "string",
            "username": "string",
            "has_profile_photo": "bool",
            "post_count": "int64",
            "followers_count": "int64",
            "following_count": "int64",
            "bio_length": "int64",
            "account_private": "bool",
            "username_digit_ratio": "float64",
        })

    return LoadResult(df=df, rejects=rejects, warnings=warnings)
