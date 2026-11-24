"""Tests for per-artist statistics and Wilson score confidence intervals (botaudit.stats)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from botaudit.loader import RejectedRow, load_followers_csv
from botaudit.stats import (
    ArtistStats,
    compute_artist_stats,
    compute_wilson_ci,
    intervals_overlap,
)


def test_wilson_formula_docstring_plain_language() -> None:
    """AC2: Formula and rationale explained in a plain-language docstring."""
    doc = compute_wilson_ci.__doc__
    assert doc is not None
    assert "Wilson score" in doc
    assert "normal approximation" in doc
    assert "Formula:" in doc
    assert "center" in doc
    assert "margin" in doc


def test_wilson_hand_calculated_example() -> None:
    """AC3: Hand-calculated test: 30 bot-like out of 100 at 95% gives [0.219, 0.396].

    Arithmetic:
      n = 100, k = 30, p = 0.30
      For 95% confidence, z = 1.95996
      z^2 = 3.84146
      1 + z^2/n = 1 + 3.84146/100 = 1.038415
      center = (0.30 + 3.84146/200) / 1.038415 = 0.319207 / 1.038415 = 0.307399
      margin = (1.95996 / 1.038415) * sqrt(0.30*0.70/100 + 3.84146/40000)
             = 1.88745 * sqrt(0.0021 + 0.000096) = 1.88745 * 0.046862 = 0.088450
      ci_low = 0.307399 - 0.088450 = 0.218949 ~ 0.219
      ci_high = 0.307399 + 0.088450 = 0.395849 ~ 0.396
    """
    ci_low, ci_high = compute_wilson_ci(30, 100, confidence_level=0.95)

    assert ci_low == pytest.approx(0.219, abs=0.002)
    assert ci_high == pytest.approx(0.396, abs=0.002)


def test_wilson_edge_cases_zero_and_hundred() -> None:
    """AC4: Edge cases 0/100 and 100/100 stay strictly inside [0, 1]; 0/100 upper bound near 0.037."""
    low_zero, high_zero = compute_wilson_ci(0, 100, confidence_level=0.95)
    assert low_zero == 0.0
    assert high_zero == pytest.approx(0.037, abs=0.002)
    assert 0.0 <= low_zero <= high_zero <= 1.0

    low_full, high_full = compute_wilson_ci(100, 100, confidence_level=0.95)
    assert low_full == pytest.approx(0.963, abs=0.002)
    assert high_full == 1.0
    assert 0.0 <= low_full <= high_full <= 1.0


def test_sample_size_zero_insufficient_data() -> None:
    """AC4: A sample of 0 marks the artist 'insufficient data' without crashing or dividing by zero."""
    # Artist has only rejected rows
    reject = RejectedRow(
        row_number=2, artist="GhostArtist", username="ghost",
        reason="invalid boolean", raw_data={}
    )
    empty_df = pd.DataFrame(columns=["artist", "bot_score", "bot_like"])

    stats = compute_artist_stats(empty_df, rejects=[reject])

    assert len(stats) == 1
    s = stats[0]
    assert s.artist == "GhostArtist"
    assert s.sample_size == 0
    assert s.mean_bot_score == 0.0
    assert s.pct_bot_like == 0.0
    assert s.ci_low == 0.0
    assert s.ci_high == 0.0
    assert s.status == "insufficient data"
    assert s.rejected_rows == 1


def test_low_confidence_badge_for_small_sample() -> None:
    """AC5: If sample_size < min_sample_size, artist is marked 'low confidence'."""
    # Create 50 rows for ArtistSmall (< default min_sample_size of 100)
    rows = [
        {"artist": "ArtistSmall", "bot_score": 0.20, "bot_like": False,
         "no_photo": False, "zero_posts": False, "mass_following": False,
         "empty_bio": False, "digit_heavy_username": False}
        for _ in range(50)
    ]
    df = pd.DataFrame(rows)
    stats = compute_artist_stats(df, config={"min_sample_size": 100, "confidence_level": 0.95})

    assert len(stats) == 1
    assert stats[0].sample_size == 50
    assert stats[0].status == "low confidence"

    # With min_sample_size=40, it should be marked 'ok'
    stats_ok = compute_artist_stats(df, config={"min_sample_size": 40, "confidence_level": 0.95})
    assert stats_ok[0].status == "ok"


def test_ranking_sort_order_and_tie_breaking() -> None:
    """AC6: Ranking sorted by pct_bot_like descending, ties broken deterministically by artist name."""
    rows = [
        # ArtistC: 80% bot-like
        {"artist": "ArtistC", "bot_score": 0.80, "bot_like": True},
        {"artist": "ArtistC", "bot_score": 0.80, "bot_like": True},
        {"artist": "ArtistC", "bot_score": 0.80, "bot_like": True},
        {"artist": "ArtistC", "bot_score": 0.80, "bot_like": True},
        {"artist": "ArtistC", "bot_score": 0.00, "bot_like": False},

        # ArtistA: 50% bot-like (Tie with ArtistB on pct_bot_like)
        {"artist": "ArtistA", "bot_score": 0.70, "bot_like": True},
        {"artist": "ArtistA", "bot_score": 0.00, "bot_like": False},

        # ArtistB: 50% bot-like (Tie with ArtistA on pct_bot_like)
        {"artist": "ArtistB", "bot_score": 0.70, "bot_like": True},
        {"artist": "ArtistB", "bot_score": 0.00, "bot_like": False},

        # ArtistD: 10% bot-like
        {"artist": "ArtistD", "bot_score": 0.70, "bot_like": True},
        {"artist": "ArtistD", "bot_score": 0.00, "bot_like": False},
        {"artist": "ArtistD", "bot_score": 0.00, "bot_like": False},
        {"artist": "ArtistD", "bot_score": 0.00, "bot_like": False},
        {"artist": "ArtistD", "bot_score": 0.00, "bot_like": False},
        {"artist": "ArtistD", "bot_score": 0.00, "bot_like": False},
        {"artist": "ArtistD", "bot_score": 0.00, "bot_like": False},
        {"artist": "ArtistD", "bot_score": 0.00, "bot_like": False},
        {"artist": "ArtistD", "bot_score": 0.00, "bot_like": False},
        {"artist": "ArtistD", "bot_score": 0.00, "bot_like": False},
    ]
    df = pd.DataFrame(rows)
    stats = compute_artist_stats(df)

    # Expected order:
    # 1. ArtistC (80%)
    # 2. ArtistA (50% - alphabetical tie breaker before ArtistB)
    # 3. ArtistB (50%)
    # 4. ArtistD (10%)
    ranked_names = [s.artist for s in stats]
    assert ranked_names == ["ArtistC", "ArtistA", "ArtistB", "ArtistD"]


def test_intervals_overlap_helper_and_marking() -> None:
    """AC7: intervals_overlap helper detects overlaps and marks 'difference not clear'."""
    # Test helper directly
    assert intervals_overlap((0.20, 0.40), (0.35, 0.55)) is True
    assert intervals_overlap((0.20, 0.40), (0.45, 0.65)) is False
    assert intervals_overlap((0.20, 0.40), (0.40, 0.60)) is True  # Boundary contact is overlap

    # Test marking in rankings
    stat1 = ArtistStats("Band1", 100, 0.5, 0.35, 0.25, 0.45)
    stat2 = ArtistStats("Band2", 100, 0.4, 0.30, 0.21, 0.40)
    assert intervals_overlap(stat1, stat2) is True

    # Construct DataFrame with overlapping artists (30% vs 35% on N=100)
    rows_a = [{"artist": "Alpha", "bot_score": 0.7, "bot_like": (i < 35)} for i in range(100)]
    rows_b = [{"artist": "Beta", "bot_score": 0.7, "bot_like": (i < 30)} for i in range(100)]
    df = pd.DataFrame(rows_a + rows_b)

    ranked = compute_artist_stats(df)
    assert len(ranked) == 2
    # Alpha CI ~ [0.26, 0.45], Beta CI ~ [0.22, 0.40] -> Overlap!
    assert ranked[0].difference_not_clear is True
    assert ranked[1].difference_not_clear is True


def test_per_artist_metrics_complete() -> None:
    """AC1: All metrics (sample_size, mean_bot_score, pct_bot_like, ci_low, ci_high, flag_rates, rejected_rows)."""
    rows = [
        {"artist": "BandX", "bot_score": 0.70, "bot_like": True,
         "no_photo": True, "zero_posts": True, "mass_following": False,
         "empty_bio": False, "digit_heavy_username": True},
        {"artist": "BandX", "bot_score": 0.10, "bot_like": False,
         "no_photo": False, "zero_posts": False, "mass_following": False,
         "empty_bio": True, "digit_heavy_username": False},
    ]
    df = pd.DataFrame(rows)
    reject = RejectedRow(2, "BandX", "bad_user", "bad count", {})

    stats = compute_artist_stats(df, rejects=[reject])
    assert len(stats) == 1
    s = stats[0]

    assert s.artist == "BandX"
    assert s.sample_size == 2
    assert s.mean_bot_score == 0.40
    assert s.pct_bot_like == 0.50
    assert 0.0 <= s.ci_low <= s.ci_high <= 1.0
    assert s.rejected_rows == 1
    assert s.flag_rates["no_photo"] == 0.50
    assert s.flag_rates["zero_posts"] == 0.50
    assert s.flag_rates["mass_following"] == 0.00
    assert s.flag_rates["empty_bio"] == 0.50
    assert s.flag_rates["digit_heavy_username"] == 0.50


@pytest.mark.skipif(
    not Path("data/sample/verified_benchmarks.csv").is_file(),
    reason="Private local benchmark CSV is excluded from public clones",
)
def test_verified_benchmarks_real_data_ordering() -> None:
    """Running on real observed verified_benchmarks.csv ranks Target_Bots strictly above Control_Users."""
    from botaudit.config import load_config
    from botaudit.registry import load_local_registry
    from botaudit.web import score_followers_extended, validate_config_dict

    cfg = validate_config_dict(load_config())
    load_res = load_followers_csv("data/sample/verified_benchmarks.csv", config=cfg)
    scored_df = score_followers_extended(load_res.df, config=cfg, registry=load_local_registry())
    stats = compute_artist_stats(scored_df, rejects=load_res.rejects, config=cfg)

    assert len(stats) == 2
    assert stats[0].artist == "Target_Bots"
    assert stats[1].artist == "Control_Users"

    assert stats[0].pct_bot_like > 0.25
    assert stats[1].pct_bot_like == 0.0
    assert stats[1].ci_high < 0.02

    assert intervals_overlap(stats[0], stats[1]) is False
    assert stats[0].difference_not_clear is False
    assert stats[1].difference_not_clear is False
