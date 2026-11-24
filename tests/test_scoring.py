"""Tests for follower scoring logic (botaudit.scoring)."""

from __future__ import annotations

import pandas as pd
import pytest

from botaudit.config import load_config
from botaudit.scoring import FLAG_COLUMNS, score_followers


def _make_profile(
    *,
    artist: str = "TestArtist",
    username: str = "normal_fan",
    has_profile_photo: bool = True,
    post_count: int = 15,
    followers_count: int = 250,
    following_count: int = 300,
    bio_length: int = 30,
    account_private: bool = False,
    username_digit_ratio: float = 0.0,
) -> pd.DataFrame:
    """Helper to construct a single-row DataFrame with standard normal signals."""
    return pd.DataFrame([
        {
            "artist": artist,
            "username": username,
            "has_profile_photo": has_profile_photo,
            "post_count": post_count,
            "followers_count": followers_count,
            "following_count": following_count,
            "bio_length": bio_length,
            "account_private": account_private,
            "username_digit_ratio": username_digit_ratio,
        }
    ])


def test_input_dataframe_not_modified() -> None:
    """AC1: score_followers returns a new DataFrame and does not modify the input."""
    df = _make_profile()
    original_cols = list(df.columns)
    original_copy = df.copy(deep=True)

    scored = score_followers(df)

    assert scored is not df
    assert list(df.columns) == original_cols
    assert df.equals(original_copy)

    # Scored DataFrame contains original columns + flags + bot_score + bot_like
    for flag in FLAG_COLUMNS:
        assert flag in scored.columns
        assert scored[flag].dtype == bool
    assert "bot_score" in scored.columns
    assert "bot_like" in scored.columns
    assert scored["bot_like"].dtype == bool


def test_account_private_is_not_a_flag() -> None:
    """AC4: account_private is NOT a flag. A private account with normal signals scores 0."""
    df_public = _make_profile(account_private=False)
    df_private = _make_profile(account_private=True)

    scored_public = score_followers(df_public)
    scored_private = score_followers(df_private)

    assert scored_public["bot_score"].iloc[0] == 0.0
    assert bool(scored_public["bot_like"].iloc[0]) is False

    assert scored_private["bot_score"].iloc[0] == 0.0
    assert bool(scored_private["bot_like"].iloc[0]) is False
    assert "account_private" not in FLAG_COLUMNS


@pytest.mark.parametrize(
    ("following", "followers", "expected_mass"),
    [
        # Boundary at following_count == 1500 (threshold: > 1500)
        (1500, 40, False),
        (1501, 40, True),
        # Boundary at followers_count == 50 (threshold: < 50)
        (2000, 50, False),
        (2000, 49, True),
    ],
)
def test_mass_following_boundaries(following: int, followers: int, expected_mass: bool) -> None:
    """AC5: Exact boundaries for mass_following at 1500, 1501 and 49, 50."""
    df = _make_profile(following_count=following, followers_count=followers)
    scored = score_followers(df)
    assert bool(scored["mass_following"].iloc[0]) is expected_mass


@pytest.mark.parametrize(
    ("ratio", "expected_digit_heavy"),
    [
        # Boundary at username_digit_ratio == 0.40 (threshold: > 0.40)
        (0.40, False),
        (0.41, True),
        (0.39, False),
    ],
)
def test_digit_heavy_boundaries(ratio: float, expected_digit_heavy: bool) -> None:
    """AC5: Exact boundaries for digit_heavy_username at 0.40 and 0.41."""
    df = _make_profile(username_digit_ratio=ratio)
    scored = score_followers(df)
    assert bool(scored["digit_heavy_username"].iloc[0]) is expected_digit_heavy


def test_hand_calculated_examples_arithmetic() -> None:
    """AC6: At least 8 hand-calculated examples showing exact arithmetic.

    Config weights:
      no_photo: 0.20
      zero_posts: 0.25
      mass_following: 0.25
      empty_bio: 0.10
      digit_heavy_username: 0.20
    Cutoff: 0.60 (strictly greater)
    """
    examples = [
        # Example 1: Clean profile with no flags
        # Arithmetic: 0.00 = 0.00 (not bot-like)
        (
            _make_profile(),
            0.00,
            False,
            "no flags",
        ),
        # Example 2: Every flag triggered
        # Arithmetic: 0.20 (photo) + 0.25 (posts) + 0.25 (mass) + 0.10 (bio) + 0.20 (digits) = 1.00 (bot-like)
        (
            _make_profile(
                has_profile_photo=False,
                post_count=0,
                following_count=2000,
                followers_count=10,
                bio_length=0,
                username_digit_ratio=0.50,
            ),
            1.00,
            True,
            "all flags",
        ),
        # Example 3: Only empty bio
        # Arithmetic: 0.10 (bio) = 0.10 (not bot-like)
        (
            _make_profile(bio_length=0),
            0.10,
            False,
            "empty bio only",
        ),
        # Example 4: No photo + digit heavy username
        # Arithmetic: 0.20 (photo) + 0.20 (digits) = 0.40 (not bot-like)
        (
            _make_profile(has_profile_photo=False, username_digit_ratio=0.45),
            0.40,
            False,
            "no photo + digits",
        ),
        # Example 5: No photo + zero posts + empty bio
        # Arithmetic: 0.20 (photo) + 0.25 (posts) + 0.10 (bio) = 0.55 (not bot-like)
        (
            _make_profile(has_profile_photo=False, post_count=0, bio_length=0),
            0.55,
            False,
            "photo + posts + bio",
        ),
        # Example 6: Empty bio + zero posts + mass following (EXACT CUTOFF TIE)
        # Arithmetic: 0.10 (bio) + 0.25 (posts) + 0.25 (mass) = 0.60
        # Cutoff rule: strictly greater than 0.60, so 0.60 is NOT bot-like
        (
            _make_profile(
                bio_length=0,
                post_count=0,
                following_count=1600,
                followers_count=30,
            ),
            0.60,
            False,
            "tie at 0.60 cutoff",
        ),
        # Example 7: No photo + zero posts + mass following
        # Arithmetic: 0.20 (photo) + 0.25 (posts) + 0.25 (mass) = 0.70 (bot-like)
        (
            _make_profile(
                has_profile_photo=False,
                post_count=0,
                following_count=1800,
                followers_count=20,
            ),
            0.70,
            True,
            "photo + posts + mass",
        ),
        # Example 8: Zero posts + mass following + digit-heavy username
        # Arithmetic: 0.25 (posts) + 0.25 (mass) + 0.20 (digits) = 0.70 (bot-like)
        (
            _make_profile(
                post_count=0,
                following_count=2200,
                followers_count=15,
                username_digit_ratio=0.60,
            ),
            0.70,
            True,
            "posts + mass + digits",
        ),
        # Example 9: No photo + zero posts + empty bio + digit-heavy username (from AC6 spec)
        # Arithmetic: 0.20 + 0.25 + 0.10 + 0.20 = 0.75 (bot-like)
        (
            _make_profile(
                has_profile_photo=False,
                post_count=0,
                bio_length=0,
                username_digit_ratio=0.45,
            ),
            0.75,
            True,
            "photo + posts + bio + digits",
        ),
        # Example 10: Mass following + empty bio + digit heavy
        # Arithmetic: 0.25 (mass) + 0.10 (bio) + 0.20 (digits) = 0.55 (not bot-like)
        (
            _make_profile(
                following_count=2500,
                followers_count=10,
                bio_length=0,
                username_digit_ratio=0.50,
            ),
            0.55,
            False,
            "mass + bio + digits",
        ),
    ]

    for df, expected_score, expected_bot_like, desc in examples:
        scored = score_followers(df)
        actual_score = scored["bot_score"].iloc[0]
        actual_bot_like = bool(scored["bot_like"].iloc[0])

        assert actual_score == pytest.approx(expected_score, abs=1e-4), (
            f"Failed on {desc}: score {actual_score} != {expected_score}"
        )
        assert actual_bot_like is expected_bot_like, (
            f"Failed on {desc}: bot_like {actual_bot_like} != {expected_bot_like}"
        )


def test_changing_config_changes_results() -> None:
    """AC7: Changing config changes results: zero weight removes effect; raising cutoff changes bot_like."""
    config = load_config()

    # Profile with no_photo + zero_posts + empty_bio + digit_heavy
    # Standard: 0.20 + 0.25 + 0.10 + 0.20 = 0.75 -> bot_like True (> 0.60)
    df = _make_profile(
        has_profile_photo=False,
        post_count=0,
        bio_length=0,
        username_digit_ratio=0.50,
    )

    scored_std = score_followers(df, config=config)
    assert scored_std["bot_score"].iloc[0] == 0.75
    assert bool(scored_std["bot_like"].iloc[0]) is True

    # 1. Setting digit_heavy_username weight to 0.0 removes its effect:
    # 0.20 + 0.25 + 0.10 + 0.00 = 0.55 -> bot_like False
    cfg_zero_weight = load_config()
    cfg_zero_weight["weights"]["digit_heavy_username"] = 0.0
    scored_zero = score_followers(df, config=cfg_zero_weight)
    assert scored_zero["bot_score"].iloc[0] == 0.55
    assert bool(scored_zero["bot_like"].iloc[0]) is False

    # 2. Raising bot_like_cutoff from 0.60 to 0.80 flips bot_like on 0.75 score:
    cfg_high_cutoff = load_config()
    cfg_high_cutoff["bot_like_cutoff"] = 0.80
    scored_high = score_followers(df, config=cfg_high_cutoff)
    assert scored_high["bot_score"].iloc[0] == 0.75
    assert bool(scored_high["bot_like"].iloc[0]) is False


def test_no_flags_scores_zero_and_all_flags_scores_one() -> None:
    """AC8: Profile with no flags scores 0; profile with every flag scores 1.0."""
    clean_df = _make_profile()
    dirty_df = _make_profile(
        has_profile_photo=False,
        post_count=0,
        following_count=3000,
        followers_count=10,
        bio_length=0,
        username_digit_ratio=0.80,
    )

    scored_clean = score_followers(clean_df)
    scored_dirty = score_followers(dirty_df)

    assert scored_clean["bot_score"].iloc[0] == 0.0
    assert bool(scored_clean["bot_like"].iloc[0]) is False

    assert scored_dirty["bot_score"].iloc[0] == 1.0
    assert bool(scored_dirty["bot_like"].iloc[0]) is True


def test_empty_dataframe_handled() -> None:
    """Empty DataFrame input produces empty DataFrame with all expected columns."""
    df_empty = pd.DataFrame(columns=[
        "artist", "username", "has_profile_photo", "post_count",
        "followers_count", "following_count", "bio_length",
        "account_private", "username_digit_ratio"
    ])
    scored = score_followers(df_empty)
    assert len(scored) == 0
    for flag in FLAG_COLUMNS:
        assert flag in scored.columns
    assert "bot_score" in scored.columns
    assert "bot_like" in scored.columns


def test_multi_row_dataframe_scoring() -> None:
    """Multi-row DataFrame preserves row order, custom index, and computes row-level flags."""
    rows = [
        # Row 0: Clean profile
        {"artist": "BandA", "username": "clean_fan", "has_profile_photo": True,
         "post_count": 25, "followers_count": 300, "following_count": 200,
         "bio_length": 40, "account_private": False, "username_digit_ratio": 0.0},
        # Row 1: Bot profile
        {"artist": "BandA", "username": "bot94820", "has_profile_photo": False,
         "post_count": 0, "followers_count": 15, "following_count": 2500,
         "bio_length": 0, "account_private": False, "username_digit_ratio": 0.625},
        # Row 2: Lurker fan (0.55 score)
        {"artist": "BandB", "username": "quiet_music", "has_profile_photo": False,
         "post_count": 0, "followers_count": 80, "following_count": 120,
         "bio_length": 0, "account_private": False, "username_digit_ratio": 0.0},
    ]
    df = pd.DataFrame(rows, index=[100, 200, 300])
    scored = score_followers(df)

    # Preserves index and row count
    assert list(scored.index) == [100, 200, 300]
    assert len(scored) == 3

    # Row 0: Clean -> score 0.0, bot_like False
    assert scored.loc[100, "bot_score"] == 0.0
    assert bool(scored.loc[100, "bot_like"]) is False

    # Row 1: Bot -> score 1.0, bot_like True
    assert scored.loc[200, "bot_score"] == 1.0
    assert bool(scored.loc[200, "bot_like"]) is True

    # Row 2: Lurker -> score 0.55, bot_like False
    assert scored.loc[300, "bot_score"] == 0.55
    assert bool(scored.loc[300, "bot_like"]) is False


def test_clip_above_one_capping() -> None:
    """AC3: bot_score sums above 1.0 are properly capped at 1.0."""
    cfg = load_config()
    # Boost weights so the sum exceeds 1.0 (e.g. 0.50 + 0.50 + 0.25 = 1.25)
    cfg["weights"]["no_photo"] = 0.50
    cfg["weights"]["zero_posts"] = 0.50
    cfg["weights"]["empty_bio"] = 0.25

    df = _make_profile(has_profile_photo=False, post_count=0, bio_length=0)
    scored = score_followers(df, config=cfg)

    # Without capping, sum would be 1.25; with capping, must be exactly 1.0
    assert scored["bot_score"].iloc[0] == 1.0
    assert bool(scored["bot_like"].iloc[0]) is True
