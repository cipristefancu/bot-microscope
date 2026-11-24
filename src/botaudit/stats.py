"""Statistical calculations and Wilson score confidence intervals for audiences.

This module computes per-artist statistics, flag occurrence rates, and
honest 95% uncertainty intervals using the Wilson score method. It also
ranks artists and detects when overlapping intervals make relative ranking unclear.

This tool never connects to the internet.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from botaudit.config import WEIGHT_KEYS, load_config
from botaudit.loader import RejectedRow
from botaudit.scoring import FLAG_COLUMNS


def _get_z_critical(confidence_level: float) -> float:
    """Return standard normal critical value z for the given confidence level.

    Uses Python's built-in standard normal distribution quantile function.
    For a 95% confidence level, returns approximately 1.95996.
    """
    alpha = 1.0 - confidence_level
    p = 1.0 - (alpha / 2.0)
    return abs(statistics.NormalDist().inv_cdf(p))


def compute_wilson_ci(
    successes: int,
    total: int,
    confidence_level: float = 0.95,
) -> tuple[float, float]:
    """Calculate the Wilson score confidence interval for a proportion.

    Why Wilson score instead of the simple normal approximation (Wald interval)?
    The simple formula (p ± z * sqrt(p(1-p)/n)) fails dramatically on small
    samples or proportions near 0% or 100%, often giving impossible bounds like
    negative numbers or numbers above 100%.

    The Wilson score interval inverts the score test. It adjusts the center
    estimate slightly toward 0.5 (shrinkage) and produces valid, asymmetric
    bounds that strictly stay inside [0, 1] even when successes = 0 or successes = total.

    Formula:
      p = successes / total
      center = (p + z^2 / (2n)) / (1 + z^2 / n)
      margin = (z / (1 + z^2 / n)) * sqrt(p(1-p)/n + z^2 / (4n^2))
      lower = max(0.0, center - margin)
      upper = min(1.0, center + margin)

    Args:
      successes: Count of items showing the trait (e.g. bot-like profiles).
      total: Total sample size.
      confidence_level: Confidence probability (default 0.95 for 95% CI).

    Returns:
      Tuple of (ci_low, ci_high) rounded to 4 decimal places.
    """
    if total <= 0:
        return (0.0, 0.0)

    z = _get_z_critical(confidence_level)
    n = float(total)
    p = float(successes) / n
    z2 = z * z

    denominator = 1.0 + (z2 / n)
    center = (p + (z2 / (2.0 * n))) / denominator
    margin = (z / denominator) * math.sqrt(
        (p * (1.0 - p) / n) + (z2 / (4.0 * n * n))
    )

    ci_low = max(0.0, center - margin)
    ci_high = min(1.0, center + margin)

    return (round(ci_low, 4), round(ci_high, 4))


@dataclass
class ArtistStats:
    """Summary metrics and confidence interval for a single artist's audience."""

    artist: str
    sample_size: int
    mean_bot_score: float
    pct_bot_like: float
    ci_low: float
    ci_high: float
    flag_rates: dict[str, float] = field(default_factory=dict)
    rejected_rows: int = 0
    status: str = "ok"  # "ok", "low confidence", or "insufficient data"
    difference_not_clear: bool = False


def intervals_overlap(
    interval_a: tuple[float, float] | ArtistStats,
    interval_b: tuple[float, float] | ArtistStats,
) -> bool:
    """Return True if the confidence intervals of two artists overlap.

    When intervals overlap, the difference between the two artists cannot be
    concluded with statistical confidence at the chosen level.

    Args:
      interval_a: (ci_low, ci_high) tuple or ArtistStats object.
      interval_b: (ci_low, ci_high) tuple or ArtistStats object.
    """
    if isinstance(interval_a, ArtistStats):
        if interval_a.sample_size == 0:
            return False
        low_a, high_a = interval_a.ci_low, interval_a.ci_high
    else:
        low_a, high_a = interval_a

    if isinstance(interval_b, ArtistStats):
        if interval_b.sample_size == 0:
            return False
        low_b, high_b = interval_b.ci_low, interval_b.ci_high
    else:
        low_b, high_b = interval_b

    return max(low_a, low_b) <= min(high_a, high_b)


def compute_artist_stats(
    df: pd.DataFrame,
    rejects: list[RejectedRow] | None = None,
    config: dict[str, Any] | None = None,
) -> list[ArtistStats]:
    """Aggregate follower scores into per-artist statistics and deterministic rankings.

    Args:
      df: DataFrame containing scored follower rows (with bot_score, bot_like,
          and flag boolean columns).
      rejects: Optional list of RejectedRow items from the loader.
      config: Optional configuration dictionary. If omitted, loads config.yaml.

    Returns:
      List of ArtistStats sorted by pct_bot_like descending, ties broken by artist name.
    """
    if config is None:
        config = load_config()

    min_sample = int(config.get("min_sample_size", 100))
    conf_level = float(config.get("confidence_level", 0.95))

    # Tally rejects per artist
    reject_counts: dict[str, int] = {}
    if rejects:
        for r in rejects:
            if r.artist:
                reject_counts[r.artist] = reject_counts.get(r.artist, 0) + 1

    # Gather all artists present in df or in rejects
    artists_in_df = (
        set(df["artist"].dropna().unique())
        if not df.empty and "artist" in df.columns
        else set()
    )
    all_artists = sorted(artists_in_df.union(reject_counts.keys()))

    stats_list: list[ArtistStats] = []

    for artist in all_artists:
        sub_df = (
            df[df["artist"] == artist]
            if not df.empty and "artist" in df.columns
            else pd.DataFrame()
        )
        n = len(sub_df)
        rejected_count = reject_counts.get(artist, 0)

        if n == 0:
            # Insufficient data: artist had only rejected rows or 0 rows
            stats = ArtistStats(
                artist=artist,
                sample_size=0,
                mean_bot_score=0.0,
                pct_bot_like=0.0,
                ci_low=0.0,
                ci_high=0.0,
                flag_rates={k: 0.0 for k in FLAG_COLUMNS},
                rejected_rows=rejected_count,
                status="insufficient data",
                difference_not_clear=False,
            )
            stats_list.append(stats)
            continue

        mean_score = round(float(sub_df["bot_score"].mean()), 4)
        k = int(sub_df["bot_like"].sum())
        pct_bot_like = round(float(k) / float(n), 4)

        ci_low, ci_high = compute_wilson_ci(k, n, confidence_level=conf_level)

        flag_rates = {
            flag: round(float(sub_df[flag].mean()), 4)
            for flag in FLAG_COLUMNS
            if flag in sub_df.columns
        }
        # Ensure all standard flags are represented
        for flag in WEIGHT_KEYS:
            if flag not in flag_rates:
                flag_rates[flag] = 0.0

        status = "low confidence" if n < min_sample else "ok"

        stats = ArtistStats(
            artist=artist,
            sample_size=n,
            mean_bot_score=mean_score,
            pct_bot_like=pct_bot_like,
            ci_low=ci_low,
            ci_high=ci_high,
            flag_rates=flag_rates,
            rejected_rows=rejected_count,
            status=status,
            difference_not_clear=False,
        )
        stats_list.append(stats)

    # Sort deterministically: pct_bot_like descending, ties broken by artist name (AC6)
    stats_list.sort(key=lambda s: (-s.pct_bot_like, s.artist))

    # Mark overlapping intervals between adjacent ranks (AC7)
    for i in range(len(stats_list)):
        current = stats_list[i]
        if current.sample_size == 0:
            continue

        overlap_prev = (
            i > 0
            and stats_list[i - 1].sample_size > 0
            and intervals_overlap(current, stats_list[i - 1])
        )
        overlap_next = (
            i < len(stats_list) - 1
            and stats_list[i + 1].sample_size > 0
            and intervals_overlap(current, stats_list[i + 1])
        )

        if overlap_prev or overlap_next:
            current.difference_not_clear = True

    return stats_list
