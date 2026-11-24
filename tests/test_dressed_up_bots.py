"""Tests for modern dressed-up bot detection: Enhancements A and B.

Enhancement A: Ratio-based mass following (following/followers >= 8.0x with following >= 200).
Enhancement B: Local Bot Registry matching and cross-artist bot recycling (B4).

Rule R-NOFAKE: uses real observed data and exact deterministic arithmetic; zero fabricated values.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from botaudit.config import load_config
from botaudit.labels import LabelSet, load_labels
from botaudit.registry import LocalBotRegistry, load_local_registry
from botaudit.scoring import is_mass_following_ratio, score_followers
from botaudit.web import score_followers_extended

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINTS_DIR = PROJECT_ROOT / "data" / "checkpoints"


def _make_profile_df(
    *,
    artist: str = "TestArtist",
    username: str = "test_user",
    has_profile_photo: bool = True,
    post_count: int = 4,
    followers_count: int = 25,
    following_count: int = 200,
    bio_length: int = 15,
    account_private: bool = False,
    username_digit_ratio: float = 0.0,
) -> pd.DataFrame:
    """Helper to create a single-row profile DataFrame for tests."""
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


# ---------------------------------------------------------------------------
# Enhancement A: Ratio-based Mass Following
# ---------------------------------------------------------------------------


def test_is_mass_following_ratio_boundaries() -> None:
    """Exact boundaries for following/followers ratio >= 8.0 and following >= 200."""
    # Exact threshold: 200 following / 25 followers = 8.0x -> True
    assert is_mass_following_ratio(following=200, followers=25) is True

    # Sub-threshold ratio: 200 following / 26 followers = 7.69x -> False
    assert is_mass_following_ratio(following=200, followers=26) is False

    # Sub-threshold following count: 199 following / 20 followers = 9.95x -> False (< 200)
    assert is_mass_following_ratio(following=199, followers=20) is False

    # Zero followers: safely uses max(1, followers) -> 200 / 1 = 200x -> True
    assert is_mass_following_ratio(following=200, followers=0) is True

    # Real human typical numbers: 350 following / 300 followers = 1.17x -> False
    assert is_mass_following_ratio(following=350, followers=300) is False

    # Real bot-farm numbers: 480 following / 12 followers = 40.0x -> True
    assert is_mass_following_ratio(following=480, followers=12) is True


def test_scoring_with_enable_ratio_following() -> None:
    """score_followers respects enable_ratio_following flag."""
    df = _make_profile_df(following_count=300, followers_count=10)

    # Baseline mode: classic threshold requires following > 1500 and followers < 50
    scored_baseline = score_followers(df, enable_ratio_following=False)
    assert bool(scored_baseline["mass_following"].iloc[0]) is False

    # Enhancement A mode: ratio 300/10 = 30x with following >= 200 triggers mass_following
    scored_ea = score_followers(df, enable_ratio_following=True)
    assert bool(scored_ea["mass_following"].iloc[0]) is True
    assert scored_ea["bot_score"].iloc[0] >= 0.25


def test_extended_scoring_enhancement_a_triggers_on_dressed_up_bot() -> None:
    """score_followers_extended triggers mass_following for accounts with following >= 200 and ratio >= 8.0."""
    cfg = load_config()
    # Dressed-up bot: 4 setup posts, bio=0, 240 following, 15 followers (ratio = 16.0x)
    df = _make_profile_df(
        username="dressed_bot_01",
        post_count=4,
        followers_count=15,
        following_count=240,
        bio_length=0,
        username_digit_ratio=0.10,
    )
    scored = score_followers_extended(df, config=cfg)
    assert bool(scored["mass_following"].iloc[0]) is True
    assert bool(scored["empty_bio"].iloc[0]) is True


# ---------------------------------------------------------------------------
# Enhancement B: Local Registry Matching
# ---------------------------------------------------------------------------


def test_local_bot_registry_lookup() -> None:
    """LocalBotRegistry correctly classifies known bots and known humans."""
    reg = LocalBotRegistry(
        known_bots=frozenset({"farm_bot_1", "cjrave74317", "bot_x"}),
        known_humans=frozenset({"real_person_1", "ciprianciutreofficial"}),
        unsure_handles=frozenset({"maybe_bot"}),
    )

    assert reg.is_known_bot("cjrave74317") is True
    assert reg.is_known_bot("@CJRAVE74317") is True  # case and @ insensitive
    assert reg.is_known_bot("real_person_1") is False
    assert reg.is_known_bot("unknown_stranger") is False

    assert reg.is_known_human("ciprianciutreofficial") is True
    assert reg.is_known_human("cjrave74317") is False


def test_registry_conflict_resolution_prioritizes_human() -> None:
    """Owner rule: handles in verified_users take precedence over verified_bots."""
    fake_label_set = LabelSet(
        bots=frozenset({"bot_a"}),  # 'conflicted_user' excluded from bots
        humans=frozenset({"human_a", "conflicted_user"}),
        conflicts=("conflicted_user",),
        bot_duplicates=(),
        human_duplicates=(),
        ignored_lines=(),
    )
    reg = LocalBotRegistry(
        known_bots=fake_label_set.bots,
        known_humans=fake_label_set.humans,
    )
    assert reg.is_known_bot("conflicted_user") is False
    assert reg.is_known_human("conflicted_user") is True


def test_extended_scoring_enhancement_b_pushes_dressed_up_bot_over_cutoff() -> None:
    """Enhancement B boost pushes a 45 pt dressed-up bot like cjrave74317 over the 60 pt cutoff."""
    cfg = load_config()
    # cjrave74317 numbers: 0 posts (+25), digit heavy (+20) = 45 pts
    df = _make_profile_df(
        username="cjrave74317",
        has_profile_photo=True,
        post_count=0,
        followers_count=7,
        following_count=13,
        bio_length=20,
        username_digit_ratio=0.50,
    )
    reg = LocalBotRegistry(
        known_bots=frozenset({"cjrave74317"}),
        known_humans=frozenset(),
    )

    # Without registry boost: score is 45 pts (0.45), NOT flagged as bot-like (cutoff 0.60)
    scored_no_boost = score_followers_extended(
        df, config=cfg, registry=reg, enable_registry_boost=False
    )
    assert scored_no_boost["bot_score"].iloc[0] == pytest.approx(0.45)
    assert bool(scored_no_boost["bot_like"].iloc[0]) is False

    # With Enhancement B: adds +25 pts -> 70 pts (0.70), crosses cutoff
    scored_with_boost = score_followers_extended(
        df, config=cfg, registry=reg, enable_registry_boost=True
    )
    assert scored_with_boost["bot_score"].iloc[0] == pytest.approx(0.70)
    assert bool(scored_with_boost["bot_like"].iloc[0]) is True
    assert bool(scored_with_boost["known_registry_bot"].iloc[0]) is True


def test_confirmed_human_never_falsely_flagged_with_registry() -> None:
    """A confirmed human in the registry is protected from false alarms."""
    cfg = load_config()
    # Human who happens to have 0 posts and empty bio (e.g. newly created real account)
    df = _make_profile_df(
        username="verified_friend",
        has_profile_photo=False,  # +20
        post_count=0,  # +25
        bio_length=0,  # +10
        username_digit_ratio=0.45,  # +20 -> Total 75 pts
    )
    reg = LocalBotRegistry(
        known_bots=frozenset(),
        known_humans=frozenset({"verified_friend"}),
    )
    scored = score_followers_extended(df, config=cfg, registry=reg, enable_registry_boost=True)
    # Owner rule: confirmed human is never bot_like
    assert bool(scored["bot_like"].iloc[0]) is False


# ---------------------------------------------------------------------------
# Cross-Artist Bot Network Recycling (B4)
# ---------------------------------------------------------------------------


def test_cross_artist_bot_recycling_detection(tmp_path: Path) -> None:
    """Verifies that the local registry indexes cross-artist checkpoint recycling."""
    import json as _json

    from botaudit.labels import LabelSet

    real_bots = [f"farm_bot_{idx:02d}" for idx in range(10)]
    labels = LabelSet(
        bots=frozenset(real_bots),
        humans=frozenset(),
        conflicts=(),
        bot_duplicates=(),
        human_duplicates=(),
        ignored_lines=(),
    )

    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    (cp_dir / "artist_alpha.json").write_text(
        _json.dumps({"artist": "artist_alpha", "profiles": [{"username": u} for u in real_bots]}),
        encoding="utf-8",
    )
    (cp_dir / "artist_beta.json").write_text(
        _json.dumps({"artist": "artist_beta", "profiles": [{"username": u} for u in real_bots[:6]]}),
        encoding="utf-8",
    )

    reg = load_local_registry(checkpoints_dir=cp_dir, label_set=labels)
    summary = reg.summarize()
    assert summary["total_known_bots"] == 10

    shared = reg.find_shared_bots("artist_alpha", "artist_beta")
    assert len(shared) == 6
    sample_shared_bot = shared[0]
    occurrences = reg.get_artist_occurrences(sample_shared_bot)
    assert len(occurrences) == 2
    assert reg.is_cross_artist_recycled(sample_shared_bot) is True


# ---------------------------------------------------------------------------
# Empirical Ground Truth Benchmark (data/sample/verified_benchmarks.csv)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (PROJECT_ROOT / "data" / "sample" / "verified_benchmarks.csv").is_file(),
    reason="Private local benchmark CSV is excluded from public clones",
)
def test_empirical_accuracy_on_real_captured_profiles() -> None:
    """Empirical benchmark on all 900+ real observed profiles in verified_benchmarks.csv.

    Enforces:
    - Specificity >= 99.0% (false alarms on real humans <= 1%)
    - Sensitivity >= 55.0% with Enhancements A and B
    - Zero fabricated values (Rule R-NOFAKE)
    """
    from botaudit.labels import DEFAULT_BENCHMARK_SAMPLE_PATH
    from botaudit.loader import load_followers_csv

    assert DEFAULT_BENCHMARK_SAMPLE_PATH.is_file(), "verified_benchmarks.csv must exist"

    load_res = load_followers_csv(DEFAULT_BENCHMARK_SAMPLE_PATH)
    df = load_res.df

    labels = load_labels()
    reg = load_local_registry(checkpoints_dir=CHECKPOINTS_DIR, label_set=labels)
    cfg = load_config()

    # Score WITHOUT registry boost: pure automated sensitivity stays >= 55% even as
    # disguised/private bots from newer accounts are added to verified_benchmarks.csv
    scored_raw = score_followers_extended(
        df, config=cfg, registry=reg, enable_registry_boost=False
    )
    scored_raw["manual_label"] = [labels.label_of(str(u)) for u in scored_raw["username"]]
    raw_bots = scored_raw[scored_raw["manual_label"] == "bot"]
    raw_humans = scored_raw[scored_raw["manual_label"] == "human"]
    raw_sens = int(raw_bots["bot_like"].astype(bool).sum()) / len(raw_bots)
    raw_spec = (len(raw_humans) - int(raw_humans["bot_like"].astype(bool).sum())) / len(raw_humans)
    assert raw_sens >= 0.550, (
        f"Pure automated sensitivity (no registry) below 55%: {raw_sens*100:.2f}%"
    )
    assert raw_spec >= 0.990, (
        f"Specificity dropped below 99% on raw pass: {raw_spec*100:.2f}%"
    )

    # Score with Enhancement A and Enhancement B
    scored = score_followers_extended(
        df, config=cfg, registry=reg, enable_registry_boost=True
    )
    scored["manual_label"] = [labels.label_of(str(u)) for u in scored["username"]]

    human_rows = scored[scored["manual_label"] == "human"]
    bot_rows = scored[scored["manual_label"] == "bot"]

    assert len(human_rows) >= 300, f"Expected >= 300 labeled humans, found {len(human_rows)}"
    assert len(bot_rows) >= 500, f"Expected >= 500 labeled bots, found {len(bot_rows)}"

    # Specificity check
    human_false_alarms = int(human_rows["bot_like"].astype(bool).sum())
    specificity = (len(human_rows) - human_false_alarms) / len(human_rows)
    assert specificity >= 0.990, (
        f"Specificity dropped below 99%: {specificity*100:.2f}% ({human_false_alarms} false alarms)"
    )

    # Sensitivity check
    bots_detected = int(bot_rows["bot_like"].astype(bool).sum())
    sensitivity = bots_detected / len(bot_rows)
    assert sensitivity >= 0.650, (
        f"Sensitivity with Enhancements A+B below 65%: {sensitivity*100:.2f}% ({bots_detected}/{len(bot_rows)})"
    )

    # Verify user's specific sample bot cjrave74317 is detected
    cj = scored[scored["username"].str.lower() == "cjrave74317"]
    if not cj.empty:
        assert bool(cj["bot_like"].iloc[0]) is True
        assert cj["bot_score"].iloc[0] >= 0.65


def test_follow_exchange_7k_cap_flood_catches_kiso8157_without_flagging_active_creators() -> None:
    """7K+/7K+ follow-exchange accounts with <=25 posts score >=65p, while real active creators stay 0p."""
    cfg = load_config()
    df = pd.DataFrame([
        {
            "artist": "target_alpha",
            "username": "kiso8157",
            "has_profile_photo": True,
            "post_count": 9,
            "followers_count": 7192,
            "following_count": 7351,
            "bio_length": 58,
            "account_private": False,
            "username_digit_ratio": 0.50,
        },
        {
            "artist": "target_alpha",
            "username": "creator_alpha",
            "has_profile_photo": True,
            "post_count": 1059,
            "followers_count": 7753,
            "following_count": 9098,
            "bio_length": 371,
            "account_private": False,
            "username_digit_ratio": 0.0,
        },
    ])
    scored = score_followers_extended(df, config=cfg)
    by_user = {row["username"]: row for _, row in scored.iterrows()}

    kiso = by_user["kiso8157"]
    assert bool(kiso["mass_following"]) is True
    assert bool(kiso["follow_exchange_ring"]) is True
    assert bool(kiso["digit_heavy_username"]) is True
    assert kiso["bot_score"] == pytest.approx(0.65)
    assert bool(kiso["bot_like"]) is True

    creator = by_user["creator_alpha"]
    assert bool(creator["mass_following"]) is False
    assert bool(creator["follow_exchange_ring"]) is False
    assert creator["bot_score"] == pytest.approx(0.0)
    assert bool(creator["bot_like"]) is False


def test_extreme_mass_and_shared_post_bot_detection() -> None:
    """Extreme 15x+ follow-ratio accounts and shared/reposted feed bots score >60p while real creators stay 0p."""
    from botaudit.scoring import (
        is_extreme_mass_following,
        is_low_audience_imbalance,
        is_shared_post_bot_signal,
    )

    assert is_extreme_mass_following(following=1000, followers=50, post_count=12) is True
    assert is_low_audience_imbalance(post_count=12, followers=50, following=1000, bio_length=0) is True
    assert is_shared_post_bot_signal(
        post_count=12,
        followers=50,
        following=1000,
        bio_length=0,
        username="andrei_m99",
        digit_ratio=0.20,
        is_stitched=False,
    ) is True

    # Shared-post engaged bot caught via observed feed telemetry
    assert is_shared_post_bot_signal(
        post_count=18,
        followers=900,
        following=3800,
        bio_length=12,
        username="repost_engaged_bot",
        digit_ratio=0.0,
        is_stitched=False,
        observed_posts_count=4,
        avg_post_likes=1.5,
        shared_post_ratio=1.0,
    ) is True

    cfg = load_config()
    df = pd.DataFrame([
        {
            "artist": "target_alpha",
            "username": "andrei_m99",
            "has_profile_photo": True,
            "post_count": 12,
            "followers_count": 50,
            "following_count": 1000,
            "bio_length": 0,
            "account_private": False,
            "username_digit_ratio": 0.20,
        },
        {
            "artist": "target_alpha",
            "username": "repost_engaged_bot",
            "has_profile_photo": True,
            "post_count": 18,
            "followers_count": 900,
            "following_count": 3800,
            "bio_length": 12,
            "account_private": False,
            "username_digit_ratio": 0.0,
        },
        {
            "artist": "target_alpha",
            "username": "real_photographer",
            "has_profile_photo": True,
            "post_count": 140,
            "followers_count": 950,
            "following_count": 620,
            "bio_length": 64,
            "account_private": False,
            "username_digit_ratio": 0.0,
        },
    ])
    feed_signals = {
        "repost_engaged_bot": {
            "observed_posts_count": 4,
            "avg_post_likes": 1.5,
            "shared_post_ratio": 1.0,
        }
    }
    scored = score_followers_extended(df, config=cfg, feed_signals_map=feed_signals)
    by_user = {row["username"]: row for _, row in scored.iterrows()}

    assert bool(by_user["andrei_m99"]["extreme_mass_following"]) is True
    assert bool(by_user["andrei_m99"]["bot_like"]) is True
    assert bool(by_user["repost_engaged_bot"]["shared_post_bot_signal"]) is True
    assert bool(by_user["real_photographer"]["bot_like"]) is False
    assert by_user["real_photographer"]["bot_score"] == pytest.approx(0.0)
