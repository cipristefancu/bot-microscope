"""Tests for the cross-account Bot Connection Mindmap API and offline UI contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import botaudit.web as web_mod
from botaudit.labels import LabelNote
from tests.test_inspector_and_live_sync import (
    INDEX_HTML,
    _call_wsgi,
    _observed_profile,
    _setup_isolated_env,
)


def test_build_bot_network_payload_and_api_endpoint_parity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify /api/bot-network groups shared bots, archetypes, humans, and matches History 1-to-1."""
    _setup_isolated_env(monkeypatch, tmp_path)

    # Confirm manual labels for bots (covering every archetype) and a shared human
    confirmed_bots = [
        ("shared_priv_bot", "alpha"),
        ("shared_auto_bot", "alpha"),
        ("shared_mass_bot", "alpha"),
        ("shared_burst_bot", "alpha"),
        ("shared_dressed_bot", "alpha"),
        ("shared_unscored_bot", "alpha"),
        ("tri_shared_bot", "gamma"),
        ("exclusive_alpha_bot", "alpha"),
    ]
    for uname, art in confirmed_bots:
        web_mod.save_label_note(
            LabelNote(
                username=uname,
                label="bot",
                evidence_note=f"Marked from Instagram HUD on @{art}",
            )
        )
    web_mod.save_label_note(
        LabelNote(
            username="shared_real_human",
            label="human",
            evidence_note="Marked from Instagram HUD on @alpha",
        )
    )

    p_priv = _observed_profile(
        "shared_priv_bot",
        "alpha",
        has_profile_photo=True,
        account_private=True,
        post_count=12,
        followers_count=220,
        following_count=260,
        bio_length=28,
    )
    p_auto = _observed_profile(
        "shared_auto_bot",
        "alpha",
        has_profile_photo=False,
        account_private=False,
        post_count=0,
        followers_count=2,
        following_count=2600,
        bio_length=0,
    )
    p_mass = _observed_profile(
        "shared_mass_bot",
        "alpha",
        has_profile_photo=True,
        account_private=False,
        post_count=25,
        followers_count=320,
        following_count=1650,
        bio_length=45,
    )
    p_burst = _observed_profile(
        "shared_burst_bot",
        "alpha",
        has_profile_photo=True,
        account_private=False,
        post_count=3,
        followers_count=280,
        following_count=340,
        bio_length=55,
    )
    p_dressed = _observed_profile(
        "shared_dressed_bot",
        "alpha",
        has_profile_photo=True,
        account_private=False,
        post_count=32,
        followers_count=640,
        following_count=410,
        bio_length=80,
    )
    p_unscored = {"artist": "alpha", "username": "shared_unscored_bot", "schema_version": 3}
    p_tri = _observed_profile(
        "tri_shared_bot",
        "alpha",
        has_profile_photo=True,
        account_private=True,
        post_count=2,
        followers_count=110,
        following_count=900,
        bio_length=15,
    )
    p_excl = _observed_profile(
        "exclusive_alpha_bot",
        "alpha",
        has_profile_photo=True,
        account_private=False,
        post_count=40,
        followers_count=510,
        following_count=380,
        bio_length=65,
    )
    p_human = _observed_profile(
        "shared_real_human",
        "alpha",
        has_profile_photo=True,
        account_private=False,
        post_count=95,
        followers_count=900,
        following_count=420,
        bio_length=70,
    )

    web_mod.save_artist_checkpoint(
        "alpha",
        [p_priv, p_auto, p_mass, p_burst, p_dressed, p_unscored, p_tri, p_excl, p_human],
    )
    web_mod.save_artist_checkpoint(
        "beta",
        [
            dict(p_priv, artist="beta"),
            dict(p_auto, artist="beta"),
            dict(p_mass, artist="beta"),
            dict(p_burst, artist="beta"),
            dict(p_dressed, artist="beta"),
            {"artist": "beta", "username": "shared_unscored_bot", "schema_version": 3},
            dict(p_tri, artist="beta"),
            dict(p_human, artist="beta"),
        ],
    )
    web_mod.save_artist_checkpoint(
        "gamma",
        [dict(p_tri, artist="gamma"), dict(p_human, artist="gamma")],
    )
    web_mod.save_artist_checkpoint(
        "clean_delta",
        [_observed_profile("independent_person", "clean_delta", post_count=60, followers_count=500, following_count=300)],
    )

    hist_by_artist = {h["artist"]: h for h in web_mod.get_checkpoints_history()}

    code, _, body = _call_wsgi("GET", "/api/bot-network")
    assert code == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["ok"] is True

    summary = payload["summary"]
    assert summary["total_accounts"] == 4
    assert summary["connected_accounts_count"] == 3  # alpha, beta, gamma (clean_delta has 0 shared)
    assert summary["independent_bot_accounts_count"] == 0
    assert summary["clean_control_accounts_count"] == 1
    assert summary["total_clusters"] == 1
    assert summary["total_shared_bots"] == 7
    assert summary["total_unique_bots_all_accounts"] == 8  # 7 shared + 1 exclusive_alpha_bot
    assert summary["total_bridges"] == 2
    assert summary["total_shared_links"] == (6 * 2) + 3

    assert len(payload["clusters"]) == 1
    assert set(payload["clusters"][0]["accounts"]) == {"alpha", "beta", "gamma"}

    by_account = {a["artist"]: a for a in payload["accounts"]}
    assert by_account["alpha"]["account_role"] == "connected"
    assert by_account["alpha"]["cluster_id"] == 1
    assert by_account["clean_delta"]["account_role"] == "clean_control"
    assert by_account["clean_delta"]["cluster_id"] is None

    # Shared bots must NOT contain confirmed human shared_real_human
    by_bot = {b["username"]: b for b in payload["shared_bots"]}
    assert "shared_real_human" not in by_bot
    assert set(by_bot.keys()) == {
        "shared_priv_bot",
        "shared_auto_bot",
        "shared_mass_bot",
        "shared_burst_bot",
        "shared_dressed_bot",
        "shared_unscored_bot",
        "tri_shared_bot",
    }

    assert by_bot["shared_priv_bot"]["archetype"] == "private_shell"
    assert by_bot["shared_auto_bot"]["archetype"] == "auto_flagged"
    assert by_bot["shared_mass_bot"]["archetype"] == "mass_follower"
    assert by_bot["shared_burst_bot"]["archetype"] == "burst_or_zero"
    assert by_bot["shared_dressed_bot"]["archetype"] == "dressed_up_other"
    assert by_bot["shared_unscored_bot"]["archetype"] == "unscored_confirmed"
    assert by_bot["tri_shared_bot"]["account_count"] == 3
    assert by_bot["tri_shared_bot"]["bridge_id"] == "alpha+beta+gamma"

    # Verify bridges & shared human tracking
    by_bridge = {br["id"]: br for br in payload["bridges"]}
    assert by_bridge["alpha+beta"]["shared_bots_count"] == 6
    assert by_bridge["alpha+beta"]["shared_humans_count"] == 1
    assert by_bridge["alpha+beta+gamma"]["shared_bots_count"] == 1
    assert by_bridge["alpha+beta+gamma"]["shared_humans_count"] == 1

    # Verify exclusive bots separated cleanly
    assert [b["username"] for b in payload["exclusive_bots_by_account"]["alpha"]] == [
        "exclusive_alpha_bot"
    ]
    assert payload["exclusive_bots_by_account"]["clean_delta"] == []

    # Verify 1-to-1 parity between /api/bot-network accounts and /api/history, plus exact network sum reconciliation
    for acc in payload["accounts"]:
        hm = hist_by_artist[acc["artist"]]
        assert acc["checkpoint_saved"] == hm["checkpoint_saved"]
        assert acc["deep_checked"] == hm["deep_checked"]
        assert acc["effective_bot_count"] == hm["effective_bot_count"]
        assert acc["real_count"] == hm["real_count"]
        assert acc["bot_pct"] == hm["bot_pct"]
        assert acc["shared_bot_count"] + acc["exclusive_bot_count"] == acc["total_network_bots"]
        assert acc["effective_bot_count"] + acc["unscored_confirmed_bot_count"] == acc["total_network_bots"]

    assert by_account["alpha"]["unscored_confirmed_bot_count"] == 1  # shared_unscored_bot

    # Non-GET requests return 405
    bad_code, _, _ = _call_wsgi("POST", "/api/bot-network", json_body={})
    assert bad_code == 405


def test_effective_bot_rate_avoids_double_rounding_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify effective_bot_rate preserves precision so 68.551% rounds to 68.6% consistently."""
    _setup_isolated_env(monkeypatch, tmp_path)
    bots = [
        _observed_profile(f"b_{i}", "precise_acc", has_profile_photo=False, account_private=False, post_count=0, followers_count=2, following_count=2200, bio_length=0)
        for i in range(194)
    ]
    humans = [
        _observed_profile(f"h_{i}", "precise_acc", has_profile_photo=True, account_private=False, post_count=60, followers_count=500, following_count=300, bio_length=40)
        for i in range(89)
    ]
    # 194 / 283 = 0.685512367... -> round(..., 4) was 0.6855 (68.5%); round(..., 6) is 0.685512 (68.6%)
    web_mod.save_artist_checkpoint("precise_acc", bots + humans)
    cap = web_mod.load_checkpoint_capture("precise_acc")
    assert cap is not None
    a0 = cap["artists"][0]
    assert round(a0["effective_bot_rate"] * 100, 1) == 68.6
    assert a0["effective_bot_pct"] == 68.6


def test_unrelated_high_bot_account_and_multi_cluster_separation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify brand-new unrelated high-bot accounts are not mislabeled Clean Controls & clusters isolate cleanly."""
    _setup_isolated_env(monkeypatch, tmp_path)

    # Cluster 1: alpha + beta sharing 5 private/auto bots
    c1_bots = [
        _observed_profile(f"c1_bot_{i}", "alpha", has_profile_photo=False, account_private=False, post_count=0, followers_count=3, following_count=2400, bio_length=0)
        for i in range(5)
    ]
    web_mod.save_artist_checkpoint("alpha", c1_bots)
    web_mod.save_artist_checkpoint("beta", [dict(p, artist="beta") for p in c1_bots])

    # Cluster 2: delta + epsilon sharing 2 auto bots (completely disjoint from Cluster 1)
    c2_bots = [
        _observed_profile(f"c2_bot_{i}", "delta", has_profile_photo=False, account_private=False, post_count=0, followers_count=2, following_count=2200, bio_length=0)
        for i in range(2)
    ]
    web_mod.save_artist_checkpoint("delta", c2_bots)
    web_mod.save_artist_checkpoint("epsilon", [dict(p, artist="epsilon") for p in c2_bots])

    # Brand-new unrelated account: 60% auto bots (6 bots, 4 real humans), 0 shared handles with anyone
    unrelated_bots = [
        _observed_profile(f"unrelated_bot_{i}", "unrelated_dj", has_profile_photo=False, account_private=False, post_count=0, followers_count=4, following_count=2300, bio_length=0)
        for i in range(6)
    ]
    unrelated_humans = [
        _observed_profile(f"unrelated_real_{i}", "unrelated_dj", has_profile_photo=True, account_private=False, post_count=85, followers_count=620, following_count=310, bio_length=45)
        for i in range(4)
    ]
    web_mod.save_artist_checkpoint("unrelated_dj", unrelated_bots + unrelated_humans)

    # Clean control account: 0 bots
    web_mod.save_artist_checkpoint(
        "clean_ctrl",
        [_observed_profile("clean_real_1", "clean_ctrl", post_count=90, followers_count=700, following_count=350)],
    )

    code, _, body = _call_wsgi("GET", "/api/bot-network")
    assert code == 200
    payload = json.loads(body.decode("utf-8"))
    summary = payload["summary"]
    assert summary["connected_accounts_count"] == 4
    assert summary["independent_bot_accounts_count"] == 1
    assert summary["clean_control_accounts_count"] == 1
    assert summary["total_clusters"] == 2

    clusters = payload["clusters"]
    assert len(clusters) == 2
    cluster_sets = [set(c["accounts"]) for c in clusters]
    assert {"alpha", "beta"} in cluster_sets
    assert {"delta", "epsilon"} in cluster_sets

    by_acc = {a["artist"]: a for a in payload["accounts"]}
    dj = by_acc["unrelated_dj"]
    assert dj["account_role"] == "independent_bot_affected"
    assert dj["shared_bot_count"] == 0
    assert dj["exclusive_bot_count"] == 6
    assert dj["cluster_id"] is None
    assert dj["all_bot_archetypes"].get("auto_flagged") == 6
    # Same bot tactic archetype yields high cosine similarity even with 0 overlapping handles
    assert len(dj["pattern_matches"]) >= 1
    assert dj["pattern_matches"][0]["similarity_pct"] == 100.0
    assert dj["pattern_matches"][0]["shared_bots"] == 0

    assert by_acc["clean_ctrl"]["account_role"] == "clean_control"


def test_bot_network_mindmap_html_contract() -> None:
    """Verify Mindmap HTML controls, stage fullscreen, unified Bot Web rail, wheel guard & removed Radial mode."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for token in (
        'id="botNetworkMindmapSection"',
        'id="btnJumpToMindmap"',
        'id="botMindmapSvg"',
        'id="mmModeWebBtn"',
        'id="mmBtnStageFullscreen"',
        'id="mmStageFsBar"',
        "is-stage-fullscreen",
        'mode: "web"',
        'id="mmFocusBanner"',
        'id="mmStoryStrip"',
        'id="mmAccountScopeSelect"',
        'id="mmWheelHint"',
        'id="mmBtnWheelLock"',
        "function loadBotNetworkMindmap(",
        "function onMindmapAccountScopeChange(",
        "function mmBuildSingleAccountSlice(",
        "function renderSingleAccountMindmapSvg(",
        "function renderMindmapTreeSvg(",
        "function renderBotWebSvg(",
        "function renderBotMatrixHtml(",
        "function mmGroupConnectedComponents(",
        "function mmRenderIndependentLane(",
        "function mmRenderAccountDnaCard(",
        "function openSingleAccountMap(",
        "function mmToggleWheelZoom(",
        "function mmZoomAt(",
        "function mmFitToBounds(",
        "function focusBotOnMindmap(",
        "function mmBuildExportSvgString(",
        "function exportMindmapSvg(",
        "function exportMindmapPng(",
        "function exportMindmapCsv(",
        "function mmQuickSelectAccount(",
        "function mmToggleDrawerShowAll(",
        "function mmToggleMatrixShowAll(",
        "mm-explorer-row",
        '<option value="ALL">Show All bots (no pages)</option>',
    ):
        assert token in html, f"Missing {token} in index.html"

    # Radial Mindmap mode removed as visually unfeasible
    assert 'id="mmModeRadialBtn"' not in html
    assert "function renderMindmapRadialSvg(" not in html
    assert "function mmBuildRadialClusterBlock(" not in html

    # Single-account map must use structured Left-to-Right layout (no radial Math.cos/sin)
    solo_section = html.split("function renderSingleAccountMindmapSvg(", 1)[1].split("function mmBuildSearchFallbackGraph(", 1)[0]
    assert "Math.cos" not in solo_section and "Math.sin" not in solo_section
    assert "mmBuildSingleAccountSlice" in solo_section
    assert "SHARED BRIDGE PARTNERS" in solo_section

    # Unified left account rail & bridge hubs + exclusive-bot visual clue in renderBotWebSvg
    web_section = html.split("function renderBotWebSvg(", 1)[1].split("function mmHighlightFocus(", 1)[0]
    assert "rightAccs" not in web_section
    assert "orderedBridgeEntries" in web_section
    assert 'data-kind="exclusive-cta"' in web_section

    # Single-account Matrix & CSV support
    matrix_section = html.split("function mmBuildMatrixDataset(", 1)[1].split("function mmRenderBotRowItem(", 1)[0]
    assert "botNetworkState.singleAccountMap" in matrix_section
    csv_section = html.split("function exportMindmapCsv(", 1)[1].split("(function initMindmapPanZoom()", 1)[0]
    assert "mmCollectAccountAllBots" in csv_section

    # Story Strip must show all audited accounts and all bridges (no 2-pill cap)
    story_section = html.split("function mmRenderStoryStrip(", 1)[1].split("function renderBotMindmap(", 1)[0]
    assert "allAccounts.forEach" in story_section
    assert "scopedBridges.map" in story_section
    assert "bridges[0]" not in story_section and "bridges[1]" not in story_section

    # Must not unconditionally hijack mouse-wheel page scroll
    wheel_section = html.split('svg.addEventListener("wheel",', 1)[1][:350]
    assert "e.ctrlKey || e.metaKey" in wheel_section

    hist_idx = html.index('id="historySection"')
    mm_idx = html.index('id="botNetworkMindmapSection"')
    rank_idx = html.index('id="rankingTable"')
    assert hist_idx < mm_idx < rank_idx
