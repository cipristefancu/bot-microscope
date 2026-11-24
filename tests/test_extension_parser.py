"""Runs the extension parser under Node and checks the unknown-not-invented contract (R-NOFAKE)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = PROJECT_ROOT / "tests" / "js" / "parser_contract.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")

NUMBER_FIELDS = ("post_count", "followers_count", "following_count", "bio_length")


@pytest.fixture(scope="module")
def out() -> dict[str, Any]:
    done = subprocess.run(
        ["node", str(CONTRACT)], capture_output=True, text=True, check=True, timeout=60
    )
    return json.loads(done.stdout)


def test_a_followers_list_row_keeps_every_number_unknown(out: dict[str, Any]) -> None:
    row = out["list_row"]
    assert all(row[f] is None for f in NUMBER_FIELDS)
    assert row["has_profile_photo"] is True  # observed in the list data
    assert row["account_private"] is False  # observed in the list data
    assert row["tier"] == 1
    assert row["deep_checked"] is False
    assert row["bot_like"] is None  # no verdict without the numbers
    assert row["score_complete"] is False
    assert row["schema_version"] == 3


def test_a_bare_handle_knows_nothing_else(out: dict[str, Any]) -> None:
    row = out["bare_row"]
    assert all(row[f] is None for f in (*NUMBER_FIELDS, "has_profile_photo", "account_private"))
    assert row["tier"] == 0
    assert row["bot_like"] is None


def test_a_full_profile_is_scored_like_the_python_scorer(out: dict[str, Any]) -> None:
    row = out["full_row"]
    assert [row[f] for f in NUMBER_FIELDS] == [4, 18, 1950, 0]
    assert row["tier"] == 2
    assert row["deep_checked"] is True
    assert row["score_pts"] == 100
    assert row["bot_like"] is True
    assert row["src"]["post_count"] == "api_json"


def test_a_later_bare_sighting_does_not_erase_observed_numbers(out: dict[str, Any]) -> None:
    assert [out["merged_row"][f] for f in NUMBER_FIELDS] == [4, 18, 1950, 0]
    assert out["merged_row"]["score_pts"] == 100


def test_scoring_is_tri_state(out: dict[str, Any]) -> None:
    nothing = out["score_nothing_known"]
    assert nothing["botLike"] is None and nothing["complete"] is False
    assert "no_photo" in nothing["unknownFlags"]

    photo_only = out["score_handle_and_photo_only"]
    assert photo_only["scorePts"] == 40  # no photo 20 + digit-heavy handle 20
    assert photo_only["botLike"] is None  # not enough to call, and not enough to clear

    conclusive = out["score_partial_but_conclusive"]
    assert conclusive["scorePts"] == 65  # unknown clues can only add points
    assert conclusive["botLike"] is True and conclusive["complete"] is False

    human = out["score_complete_human"]
    assert human["scorePts"] == 0 and human["botLike"] is False and human["complete"] is True


def test_numbers_invented_by_older_builds_become_unknown(out: dict[str, Any]) -> None:
    gone = out["scrub_placeholder"]
    assert all(gone[f] is None for f in NUMBER_FIELDS)
    assert gone["account_private"] is None
    assert gone["legacy_scrubbed"] is True
    assert "bot_like" not in gone and "flags" not in gone

    deep = out["scrub_keeps_deep_checked"]
    assert deep["post_count"] == 24 and deep["legacy_unverified"] is True

    current = out["scrub_keeps_current_schema"]
    assert current["post_count"] == 24 and "legacy_unverified" not in current

    real = out["scrub_keeps_real_numbers"]
    assert real["post_count"] == 5 and real["legacy_unverified"] is True


def test_csv_export_contains_only_fully_observed_rows(out: dict[str, Any]) -> None:
    lines = out["csv"].strip().split("\n")
    assert len(lines) == 2
    assert lines[1] == "artist_x,zebra.9605848,true,4,18,1950,0,false,0.5385"


def test_number_parsing_returns_unknown_instead_of_zero(out: dict[str, Any]) -> None:
    assert out["counts"] == [1200, 1200000, 4321, None, None]
    assert out["header_stats_partial"] == {
        "post_count": 12,
        "followers_count": 340,
        "following_count": None,
    }


def test_burst_clue_is_unknown_without_numbers_or_timestamps(out: dict[str, Any]) -> None:
    assert out["burst_unknown"] is None
    assert out["burst_from_timestamps"] is True


def test_enhancement_a_ratio_in_javascript_parser(out: dict[str, Any]) -> None:
    """Enhancement A (ratio >= 8.0 with following >= 200) triggers in JS parser."""
    scored = out["score_enhancement_a_ratio"]
    assert scored["flags"]["mass_following"] is True
    assert scored["scorePts"] == 55
    assert scored["botLike"] is False


def test_enhancement_b_registry_match_in_javascript_parser(out: dict[str, Any]) -> None:
    """Enhancement B registry match adds +25 pts and pushes bot over cutoff in JS parser."""
    scored = out["score_enhancement_b_registry"]
    assert scored["flags"]["known_registry_bot"] is True
    assert scored["flags"]["zero_posts"] is True
    assert scored["flags"]["digit_heavy_username"] is True
    assert scored["scorePts"] == 70  # 25 (zero_p) + 20 (digits) + 25 (reg) = 70
    assert scored["botLike"] is True


def test_follow_exchange_ring_in_javascript_parser(out: dict[str, Any]) -> None:
    """Low-post 7K+/7K+ follow-exchange bots trigger 65 pts in JS, while active creators stay 0p."""
    kiso = out["score_follow_exchange_kiso"]
    assert kiso["flags"]["mass_following"] is True
    assert kiso["flags"]["follow_exchange_ring"] is True
    assert kiso["flags"]["digit_heavy_username"] is True
    assert kiso["scorePts"] == 65
    assert kiso["botLike"] is True

    real_creator = out["score_follow_exchange_real_creator"]
    assert real_creator["flags"]["mass_following"] is False
    assert real_creator["flags"]["follow_exchange_ring"] is False
    assert real_creator["scorePts"] == 0
    assert real_creator["botLike"] is False


def test_extreme_mass_and_shared_post_feed_extraction_in_javascript_parser(out: dict[str, Any]) -> None:
    """Extreme 15x+ bots and shared/reposted timeline bots trigger accurately in JS."""
    ext12 = out["score_extreme_mass_12_posts"]
    assert ext12["flags"]["mass_following"] is True
    assert ext12["flags"]["extreme_mass_following"] is True
    assert ext12["flags"]["low_audience_imbalance"] is True
    assert ext12["flags"]["shared_post_bot_signal"] is True
    assert ext12["flags"]["ratio_cluster_capped"] is True
    assert ext12["ratioClusterRawPts"] == 90
    assert ext12["ratioClusterPts"] == 60
    assert ext12["scorePts"] == 70
    assert ext12["botLike"] is True

    shared_row = out["shared_post_feed_row"]
    assert shared_row["observed_posts_count"] == 4
    assert shared_row["shared_post_ratio"] == 1.0
    assert shared_row["avg_post_likes"] == 1.5
    assert shared_row["flags"]["shared_post_bot_signal"] is True
    assert shared_row["flags"]["low_audience_imbalance"] is True


def test_strict_follower_guards_reject_following_suggestions_and_self(out: dict[str, Any]) -> None:
    """Strict URL/modal guards and extractor filters block self, co-authors, and suggested celebs."""
    urls = out["url_classifications"]
    assert urls["followers"] == "followers_list"
    assert urls["following"] == "following_list"
    assert urls["graphql"] == "profile_detail"
    assert urls["web_profile"] == "profile_detail"
    assert urls["other"] == "ignore"

    modals = out["modal_text_checks"]
    assert modals["en_followers"] is True
    assert modals["ro_followers"] is True
    assert modals["followers_with_search"] is True
    assert modals["en_following"] is False
    assert modals["ro_following"] is False
    assert modals["likes"] is False
    assert modals["comments"] is False

    assert out["strict_extract_keys"] == ["known_fan"]
    assert out["strict_known_fan"] is not None
    assert out["strict_known_fan"]["followers_count"] == 220
    assert out["strict_known_fan"]["observed_posts_count"] == 1


def test_hover_card_profiles_interval_bounds_without_fabricating_bio(out: dict[str, Any]) -> None:
    """Hover-card profiles (bio_length=null) decide Bot/Human when interval cannot cross 60p."""
    h_bot = out["hover_conclusive_bot"]
    assert h_bot["scorePts"] >= 95
    assert h_bot["botLike"] is True
    assert h_bot["conclusive"] is True
    assert h_bot["complete"] is False

    h_human = out["hover_conclusive_human"]
    assert h_human["scorePts"] == 0
    assert h_human["maxScorePts"] == 10
    assert h_human["botLike"] is False
    assert h_human["conclusive"] is True
    assert h_human["complete"] is False

    h_border = out["hover_borderline"]
    assert h_border["scorePts"] == 40
    assert h_border["maxScorePts"] == 85
    assert h_border["botLike"] is None
    assert h_border["conclusive"] is False

    hf = out["hover_flags"]
    assert hf["bot_has_header"] is True
    assert hf["bot_is_scorable"] is False
    assert hf["bot_missing_flags_has_header"] is True
    assert hf["human_has_header"] is True
    assert hf["list_has_header"] is False

    # Hover CHECK (35-60p) stays uncounted until tab opened or manually labeled
    h_check = out["hover_check_ambiguous"]
    assert h_check["scorePts"] == 35
    assert h_check["maxScorePts"] == 45
    assert h_check["botLike"] is None
    assert h_check["conclusive"] is False

    # Clear BOT with null privacy/photo still counts as Bot; clear REAL (max <= 34) still counts as Human
    h_bot_nf = out["hover_bot_missing_flags"]
    assert h_bot_nf["scorePts"] > 60
    assert h_bot_nf["botLike"] is True
    assert h_bot_nf["conclusive"] is True

    h_hum_np = out["hover_human_missing_photo"]
    assert h_hum_np["scorePts"] == 0
    assert h_hum_np["maxScorePts"] == 30
    assert h_hum_np["botLike"] is False
    assert h_hum_np["conclusive"] is True

    h_hum_stitched = out["hover_human_missing_photo_stitched"]
    assert h_hum_stitched["scorePts"] == 10
    assert h_hum_stitched["maxScorePts"] == 40
    assert h_hum_stitched["botLike"] is None
    assert h_hum_stitched["conclusive"] is False


def test_js_parser_option_c_ratio_cap_stitched_and_3tier_parity() -> None:
    """JS scoreFollowerProfile matches Python Option C1/C2 & Option A 3-tier verdicts."""
    from botaudit.config import load_config
    from botaudit.web import score_followers_extended

    cfg = load_config(PROJECT_ROOT / "config.yaml")
    parser_js = PROJECT_ROOT / "browser-extension" / "parser.js"
    cases = [
        {
            "username": "ana.popescu._x",
            "has_profile_photo": True,
            "account_private": False,
            "post_count": 22,
            "followers_count": 420,
            "following_count": 380,
            "bio_length": 45,
        },
        {
            "username": "quietlistener",
            "has_profile_photo": True,
            "account_private": False,
            "post_count": 15,
            "followers_count": 20,
            "following_count": 2200,
            "bio_length": 80,
        },
        {
            "username": "quietemptybio",
            "has_profile_photo": True,
            "account_private": False,
            "post_count": 15,
            "followers_count": 20,
            "following_count": 2200,
            "bio_length": 0,
        },
        {
            "username": "quietlistener",
            "has_profile_photo": True,
            "account_private": False,
            "post_count": 12,
            "followers_count": 2,
            "following_count": 2500,
            "bio_length": None,
        },
    ]

    node_code = f"""
    const fs = require("fs");
    const vm = require("vm");
    const sandbox = {{ console, Date, Math, JSON, Number, String, Boolean, Array, Object, Set, Map, RegExp }};
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync({json.dumps(str(parser_js))}, "utf8"), sandbox);
    const P = sandbox.BotAuditParser;
    const cases = {json.dumps(cases)};
    console.log(JSON.stringify(cases.map((c) => P.scoreFollowerProfile(c))));
    """
    proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=True, timeout=30)
    js_results = json.loads(proc.stdout.strip().splitlines()[-1])

    stitched_js, capped_js, bot_js, hover_capped_js = js_results
    assert stitched_js["flags"]["stitched_username"] is True
    assert stitched_js["flags"]["digit_heavy_username"] is False
    assert stitched_js["scorePts"] == 10
    assert stitched_js["verdictTier"] == "clean"

    assert capped_js["ratioClusterRawPts"] == 90
    assert capped_js["ratioClusterPts"] == 60
    assert capped_js["ratioClusterCapped"] is True
    assert capped_js["scorePts"] == 60
    assert capped_js["botLike"] is False
    assert capped_js["verdictTier"] == "suspicious"

    assert bot_js["scorePts"] == 70
    assert bot_js["botLike"] is True
    assert bot_js["verdictTier"] == "bot"

    # Partial hover on high-ratio user: ratio cap already reached at 60, only unknown bio (+10) adds -> maxScorePts=70
    assert hover_capped_js["ratioClusterRawPts"] == 90
    assert hover_capped_js["ratioClusterPts"] == 60
    assert hover_capped_js["scorePts"] == 60
    assert hover_capped_js["maxScorePts"] == 70
    assert hover_capped_js["botLike"] is None
    assert hover_capped_js["verdictTier"] == "unknown"

    # Exact Python <-> JS parity on complete cases
    import pandas as pd

    py_df = pd.DataFrame([
        {
            "artist": "parity",
            "username": c["username"],
            "has_profile_photo": c["has_profile_photo"],
            "account_private": c["account_private"],
            "post_count": c["post_count"],
            "followers_count": c["followers_count"],
            "following_count": c["following_count"],
            "bio_length": c["bio_length"],
            "username_digit_ratio": 0.0,
        }
        for c in cases[:3]
    ])
    py_res = score_followers_extended(py_df, config=cfg)
    for idx in range(3):
        assert js_results[idx]["scorePts"] == round(float(py_res.iloc[idx]["bot_score"]) * 100)
        assert js_results[idx]["botLike"] is bool(py_res.iloc[idx]["bot_like"])

