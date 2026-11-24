"""Bot scoring calculation based on configurable weights and thresholds.

This module scores follower profiles against the heuristic warning signs
defined in config.yaml. Each profile receives a transparent score between 0 and 1
and an indicator of whether it exceeds the bot-like audience cutoff.

This tool never connects to the internet.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from botaudit.config import WEIGHT_KEYS, load_config

FLAG_COLUMNS: tuple[str, ...] = WEIGHT_KEYS
_TRAILING_DIGITS_RE = re.compile(r"\d+$")


def is_mass_following_ratio(
    following: int,
    followers: int,
    min_following: int = 200,
    ratio_threshold: float = 8.0,
) -> bool:
    """True if following count meets minimum threshold and following/followers ratio >= threshold.

    Enhancement A for modern dressed-up bot detection:
    Accounts following 200+ accounts with a following/followers ratio of 8.0x or higher
    exhibit bot-farm mass-following behavior while evading classic >1500 ceilings.
    """
    if following < min_following:
        return False
    ratio = following / max(1, followers)
    return ratio >= ratio_threshold


def is_extended_mass_following(
    following: int,
    followers: int,
    post_count: int,
    min_following: int = 1500,
    max_followers: int = 50,
) -> bool:
    """Detect classic, ratio-based, ghost-shell, and 5K+/5K+ follow-exchange mass following (+25 pts)."""
    ratio = following / max(1, followers)
    return bool(
        (following > min_following and followers < max_followers)
        or (following >= 1500 and ratio >= 5.0)
        or (following >= 200 and ratio >= 8.0)
        or (following >= 50 and followers <= 20 and ratio >= 10.0)
        or (following >= 5000 and followers >= 3000 and post_count <= 25)
        or (following >= 6500 and post_count <= 20)
    )


def is_extreme_mass_following(
    following: int,
    followers: int,
    post_count: int,
) -> bool:
    """Detect extreme follow/follower imbalance on low/moderate post accounts (+30 pts)."""
    ratio = following / max(1, followers)
    return bool(
        (following >= 200 and ratio >= 15.0 and post_count <= 40)
        or (following >= 100 and followers <= 25 and ratio >= 12.0)
        or (following >= 500 and followers <= 50 and post_count <= 40)
    )


def is_follow_exchange_ring(
    following: int,
    followers: int,
    post_count: int,
) -> bool:
    """Detect high-cap follow-for-follow / bought exchange accounts (+20 pts)."""
    return bool(following >= 5000 and followers >= 5000 and post_count <= 25)


def is_low_audience_imbalance(
    post_count: int,
    followers: int,
    following: int,
    bio_length: int,
) -> bool:
    """Detect dressed-up low-post bots with tiny audiences or blank bios (+15 pts)."""
    ratio = following / max(1, followers)
    return bool(
        (post_count <= 25 and followers <= 80 and following >= 150)
        or (post_count <= 25 and followers <= 100 and bio_length <= 4)
        or (post_count <= 25 and following >= 3500 and ratio >= 2.5)
    )


def is_shared_post_bot_signal(
    post_count: int,
    followers: int,
    following: int,
    bio_length: int,
    username: str,
    digit_ratio: float,
    is_stitched: bool,
    *,
    observed_posts_count: int | None = None,
    avg_post_likes: float | None = None,
    shared_post_ratio: float | None = None,
) -> bool:
    """Detect engaged shared-post / repost bot accounts without penalizing real active creators (+20 pts)."""
    ratio = following / max(1, followers)
    m_trail = _TRAILING_DIGITS_RE.search(str(username or "").strip())
    trailing_digits = len(m_trail.group(0)) if m_trail else 0

    profile_signal = (
        (1 <= post_count <= 30 and followers <= 150 and following >= 300 and ratio >= 4.0)
        or (1 <= post_count <= 25 and bio_length <= 4 and following >= 1500 and ratio >= 3.0)
        or (
            7 <= post_count <= 35
            and followers <= 200
            and bio_length <= 4
            and (trailing_digits >= 3 or digit_ratio > 0.25 or is_stitched)
        )
    )
    if profile_signal:
        return True

    if observed_posts_count is not None and observed_posts_count >= 3:
        if shared_post_ratio is not None and shared_post_ratio >= 0.70 and post_count <= 60:
            return True
        if (
            followers >= 500
            and post_count >= 5
            and avg_post_likes is not None
            and (avg_post_likes / max(1, followers)) < 0.003
        ):
            return True

    return False


def score_followers(
    df: pd.DataFrame,
    config: dict[str, Any] | None = None,
    *,
    enable_ratio_following: bool = False,
    ratio_threshold: float = 8.0,
    ratio_min_following: int = 200,
) -> pd.DataFrame:
    """Score follower profiles using thresholds and weights from config.

    Args:
        df: Input DataFrame containing validated follower profile records.
            The input DataFrame is never modified.
        config: Optional configuration dictionary. If omitted, loads config.yaml.

    Returns:
        A new DataFrame containing the original columns plus:
        - boolean column for each flag (no_photo, zero_posts, mass_following,
          empty_bio, digit_heavy_username)
        - `bot_score`: float in [0.0, 1.0], sum of weights of triggered flags
        - `bot_like`: bool, True if bot_score is strictly greater than cutoff
    """
    if config is None:
        config = load_config()

    weights = config["weights"]
    min_following = config["mass_following_min_following"]
    max_followers = config["mass_following_max_followers"]
    digit_threshold = config["digit_ratio_threshold"]
    cutoff = config["bot_like_cutoff"]

    # Do not mutate input DataFrame
    out = df.copy()

    if out.empty:
        for flag in FLAG_COLUMNS:
            out[flag] = pd.Series(dtype=bool)
        out["bot_score"] = pd.Series(dtype=float)
        out["bot_like"] = pd.Series(dtype=bool)
        return out

    # Compute individual flag signals (AC2)
    # no_photo: has_profile_photo is false
    out["no_photo"] = ~out["has_profile_photo"].astype(bool)

    # zero_posts: post_count == 0
    out["zero_posts"] = out["post_count"] == 0

    # mass_following: following_count > min_following AND followers_count < max_followers
    out["mass_following"] = (out["following_count"] > min_following) & (
        out["followers_count"] < max_followers
    )
    if enable_ratio_following:
        ratio_series = out["following_count"] / out["followers_count"].clip(lower=1)
        ratio_mass = (out["following_count"] >= ratio_min_following) & (
            ratio_series >= ratio_threshold
        )
        out["mass_following"] = out["mass_following"] | ratio_mass

    # empty_bio: bio_length == 0
    out["empty_bio"] = out["bio_length"] == 0

    # digit_heavy_username: username_digit_ratio > digit_threshold
    out["digit_heavy_username"] = out["username_digit_ratio"] > digit_threshold

    # Sum triggered flag weights (AC3)
    # Use rounding to prevent floating-point representation error from altering boundary ties
    score = (
        out["no_photo"] * float(weights["no_photo"])
        + out["zero_posts"] * float(weights["zero_posts"])
        + out["mass_following"] * float(weights["mass_following"])
        + out["empty_bio"] * float(weights["empty_bio"])
        + out["digit_heavy_username"] * float(weights["digit_heavy_username"])
    ).round(4).clip(upper=1.0)

    out["bot_score"] = score

    # bot_like is strictly greater than cutoff (AC3)
    out["bot_like"] = out["bot_score"] > float(cutoff)

    return out

