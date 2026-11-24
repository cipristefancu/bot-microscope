"""Rules for profile rows captured by the browser extension (schema v3).

Rule R-NOFAKE: a number nobody observed is stored as None ("unknown"), never as a
guess. Older extension builds wrote made-up placeholder numbers for followers they
had not opened; `scrub_legacy_profile` turns those back into "unknown".

Data tiers (see docs/instagram-payload-schema.md):
    0 handle only | 1 + avatar/private flag | 2 + all four numbers | 3 + post timestamps

This tool never connects to the internet.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
STAT_FIELDS: tuple[str, ...] = ("post_count", "followers_count", "following_count", "bio_length")
HEADER_STAT_FIELDS: tuple[str, ...] = ("post_count", "followers_count", "following_count")

# (posts, followers, following, bio length) tuples that older builds invented for
# accounts they had not opened. Only used to recognise and remove them.
LEGACY_PLACEHOLDER_TUPLES: frozenset[tuple[int, int, int, int]] = frozenset(
    {(24, 340, 290, 42), (22, 320, 280, 40), (0, 18, 2150, 0), (3, 18, 1850, 0)}
)

# Verdicts that were computed from placeholder numbers are meaningless without them.
_DERIVED_FIELDS: tuple[str, ...] = (
    "bot_score",
    "score_pts",
    "bot_like",
    "flags",
    "same_day_post_burst",
)
_BOOKKEEPING_FIELDS: frozenset[str] = frozenset({"tier", "legacy_unverified", "legacy_scrubbed"})


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_legacy_placeholder(profile: dict[str, Any]) -> bool:
    """True for a row an old build filled with invented numbers (never opened by the owner)."""
    if profile.get("schema_version") is not None:
        return False
    if profile.get("deep_checked") is True:
        return False
    stats = tuple(profile.get(f) for f in STAT_FIELDS)
    return stats in LEGACY_PLACEHOLDER_TUPLES


def profile_tier(profile: dict[str, Any]) -> int:
    """Data tier 0-3 of a captured row, from what was actually observed."""
    if all(_is_count(profile.get(f)) for f in STAT_FIELDS):
        return 3 if profile.get("post_timestamps") else 2
    if isinstance(profile.get("has_profile_photo"), bool) or isinstance(
        profile.get("account_private"), bool
    ):
        return 1
    return 0


def is_scorable(profile: dict[str, Any]) -> bool:
    """True when every input the scorer needs was observed."""
    return (
        all(_is_count(profile.get(f)) for f in STAT_FIELDS)
        and isinstance(profile.get("has_profile_photo"), bool)
        and isinstance(profile.get("account_private"), bool)
    )


def has_header_counts(profile: dict[str, Any]) -> bool:
    """True when hover-card / header counts (posts, followers, following) were observed."""
    return all(_is_count(profile.get(f)) for f in HEADER_STAT_FIELDS)


def scrub_legacy_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with invented numbers turned back into None and the tier recomputed.

    Old rows that are not recognisable placeholders keep their numbers but are marked
    `legacy_unverified`: they pre-date provenance tracking, so they cannot be proven real.
    """
    out = dict(profile)
    if is_legacy_placeholder(out):
        for field in STAT_FIELDS:
            out[field] = None
        out["account_private"] = None
        out["deep_checked"] = False
        out["legacy_scrubbed"] = True
        for field in _DERIVED_FIELDS:
            out.pop(field, None)
    elif out.get("schema_version") is None and any(_is_count(out.get(f)) for f in STAT_FIELDS):
        out["legacy_unverified"] = True
    out["tier"] = profile_tier(out)
    return out


def _is_current(profile: dict[str, Any]) -> bool:
    return profile.get("schema_version") is not None


def merge_profiles(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Combine two captures of the same account without losing observed numbers.

    A value that is None never erases a known one. A number from a current-schema row
    (observed) always beats one from an older row (unproven), whichever order the rows
    arrive in; a number that only older rows hold stays marked `legacy_unverified`.
    """
    rows = sorted((scrub_legacy_profile(old), scrub_legacy_profile(new)), key=_is_current)
    merged: dict[str, Any] = {}
    observed: set[str] = set()

    for row in rows:  # older rows first, current rows last, so current values win
        for key, value in row.items():
            if key in _BOOKKEEPING_FIELDS:
                continue
            if key == "src":
                merged["src"] = {**merged.get("src", {}), **(value or {})}
            elif key == "post_timestamps":
                if value:
                    merged[key] = value
            elif value is not None:
                merged[key] = value
            else:
                merged.setdefault(key, None)
        if _is_current(row):
            observed |= {f for f in STAT_FIELDS if _is_count(row.get(f))}

    unverified = any(_is_count(merged.get(f)) and f not in observed for f in STAT_FIELDS)
    current_version = next((r["schema_version"] for r in reversed(rows) if _is_current(r)), None)
    merged["schema_version"] = None if unverified else current_version
    if unverified:
        merged["legacy_unverified"] = True
    merged["tier"] = profile_tier(merged)
    return merged


def capture_rank(profile: dict[str, Any]) -> int:
    """3 = complete and written by this schema, 2 = complete but legacy, 1 = partial, 0 = nothing."""
    if is_scorable(profile):
        return 3 if profile.get("schema_version") == SCHEMA_VERSION else 2
    return 1 if profile_tier(profile) >= 1 else 0


def iter_checkpoint_profiles(directory: Path) -> Iterator[dict[str, Any]]:
    """Yield every saved profile row (already scrubbed) from the checkpoint folder."""
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for raw in data.get("profiles") or []:
            if isinstance(raw, dict) and raw.get("username"):
                row = scrub_legacy_profile(raw)
                row["checkpoint"] = path.stem
                yield row


def best_profiles(directory: Path) -> dict[str, dict[str, Any]]:
    """The most complete saved row per lowercase username across all checkpoints."""
    best: dict[str, dict[str, Any]] = {}
    for row in iter_checkpoint_profiles(directory):
        key = str(row["username"]).lower()
        row_rank = (capture_rank(row), 1 if has_header_counts(row) else 0)
        if key not in best:
            best[key] = row
        else:
            prev = best[key]
            prev_rank = (capture_rank(prev), 1 if has_header_counts(prev) else 0)
            if row_rank > prev_rank:
                best[key] = row
    return best
