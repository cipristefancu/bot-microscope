"""Tests for the Follower Profile Inspector mathematics, live tab/mark sync, and UI contract."""

from __future__ import annotations

import io
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from wsgiref.util import setup_testing_defaults

import pytest

import botaudit.labels as labels_mod
import botaudit.web as web_mod
from botaudit.labels import LabelNote
from botaudit.web import create_wsgi_app, run_audit_from_csv_text

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "index.html"
CONTENT_JS = REPO_ROOT / "browser-extension" / "content.js"


def _call_wsgi(
    method: str,
    path: str,
    query: str = "",
    json_body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Invoke the WSGI app in-memory without opening a network socket."""
    app = create_wsgi_app()
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ["REQUEST_METHOD"] = method.upper()
    environ["PATH_INFO"] = path
    environ["QUERY_STRING"] = query

    if json_body is not None:
        raw = json.dumps(json_body).encode("utf-8")
        environ["CONTENT_LENGTH"] = str(len(raw))
        environ["CONTENT_TYPE"] = "application/json"
        environ["wsgi.input"] = io.BytesIO(raw)
    else:
        environ["CONTENT_LENGTH"] = "0"
        environ["wsgi.input"] = io.BytesIO(b"")

    captured_status = "200 OK"
    captured_headers: list[tuple[str, str]] = []

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        nonlocal captured_status, captured_headers
        captured_status = status
        captured_headers = headers

    chunks = app(environ, start_response)
    body = b"".join(chunks)
    code = int(captured_status.split(" ", 1)[0])
    header_dict = {k.lower(): v for k, v in captured_headers}
    return code, header_dict, body


def _observed_profile(
    username: str,
    artist: str = "target_artist",
    *,
    has_profile_photo: bool = True,
    post_count: int = 12,
    followers_count: int = 240,
    following_count: int = 180,
    bio_length: int = 35,
    account_private: bool = False,
) -> dict[str, Any]:
    return {
        "artist": artist,
        "username": username,
        "has_profile_photo": has_profile_photo,
        "post_count": post_count,
        "followers_count": followers_count,
        "following_count": following_count,
        "bio_length": bio_length,
        "account_private": account_private,
        "deep_checked": True,
        "schema_version": 2,
    }


def _setup_isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Isolate checkpoints, labels, and benchmarks to tmp_path so real data/ is untouched."""
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    bots_csv = tmp_path / "verified_bots.csv"
    users_csv = tmp_path / "verified_users.csv"
    notes_csv = tmp_path / "label_notes.csv"
    bench_csv = tmp_path / "verified_benchmarks.csv"
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    bots_csv.write_text("Instagram Links\n", encoding="utf-8")
    users_csv.write_text("Instagram Links\n", encoding="utf-8")
    notes_csv.write_text(
        "username,label,bot_type,labeler,labeled_on,evidence_note,status\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(web_mod, "CHECKPOINTS_DIR", cp_dir)
    monkeypatch.setattr(web_mod, "DEFAULT_BENCHMARK_SAMPLE_PATH", bench_csv)
    monkeypatch.setattr(web_mod, "_LATEST_EXTENSION_RESULT", None)
    monkeypatch.setattr(web_mod, "_ACTIVE_EXTENSION_ARTIST", None)

    monkeypatch.setattr(
        web_mod,
        "load_labels",
        lambda: labels_mod.load_labels(bots_csv, users_csv, notes_path=notes_csv),
    )
    monkeypatch.setattr(
        web_mod,
        "load_label_notes",
        lambda: labels_mod.load_label_notes(notes_csv),
    )

    def _isolated_save_note(
        note: LabelNote,
        path: Path = notes_csv,
        auto_sync: bool = True,
    ) -> None:
        labels_mod.save_label_note(note, path=notes_csv, auto_sync=False)
        if auto_sync:
            labels_mod.sync_ground_truth_lists(
                notes_path=notes_csv,
                bots_path=bots_csv,
                users_path=users_csv,
                backup_dir=backups_dir,
            )
            labels_mod.sync_sample_benchmarks(
                checkpoints_dir=cp_dir,
                output_path=bench_csv,
                notes_path=notes_csv,
                bots_path=bots_csv,
                users_path=users_csv,
            )

    monkeypatch.setattr(web_mod, "save_label_note", _isolated_save_note)
    monkeypatch.setattr(
        web_mod,
        "sync_ground_truth_lists",
        lambda: labels_mod.sync_ground_truth_lists(
            notes_path=notes_csv,
            bots_path=bots_csv,
            users_path=users_csv,
        ),
    )
    monkeypatch.setattr(
        web_mod,
        "sync_sample_benchmarks",
        lambda: labels_mod.sync_sample_benchmarks(
            checkpoints_dir=cp_dir,
            output_path=bench_csv,
            notes_path=notes_csv,
            bots_path=bots_csv,
            users_path=users_csv,
        ),
    )
    return {
        "cp_dir": cp_dir,
        "bots_csv": bots_csv,
        "users_csv": users_csv,
        "notes_csv": notes_csv,
        "bench_csv": bench_csv,
    }


def test_live_tab_open_and_mark_invalidates_stale_extension_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Opening a follower tab (/api/checkpoint) or marking (/api/labels) refreshes live UI data."""
    _setup_isolated_env(monkeypatch, tmp_path)

    # 1. Initial Send-to-Audit batch with 1 observed follower + 1 handle-only follower
    code, _, body = _call_wsgi(
        "POST",
        "/api/extension-import",
        json_body={
            "artist": "live_target",
            "merge_checkpoint": True,
            "profiles": [
                _observed_profile("first_checked_human", "live_target"),
                {
                    "artist": "live_target",
                    "username": "second_opened_tab",
                    "has_profile_photo": True,
                    "post_count": None,
                    "followers_count": None,
                    "following_count": None,
                    "bio_length": None,
                    "account_private": None,
                    "deep_checked": False,
                    "schema_version": 2,
                },
            ],
        },
    )
    assert code == 200
    initial_data = json.loads(body.decode("utf-8"))
    assert initial_data["total_rows_loaded"] == 1

    # 2. User opens @second_opened_tab in a new Instagram tab and closes it WITHOUT clicking Mark:
    #    extension auto-saves via POST /api/checkpoint.
    code, _, _ = _call_wsgi(
        "POST",
        "/api/checkpoint",
        json_body={
            "artist": "live_target",
            "profiles": [
                _observed_profile(
                    "second_opened_tab",
                    "live_target",
                    has_profile_photo=False,
                    post_count=0,
                    followers_count=2,
                    following_count=2400,
                    bio_length=0,
                )
            ],
        },
    )
    assert code == 200

    # GET /api/extension-import must immediately reflect BOTH checked followers (not stale 1!)
    code, _, body = _call_wsgi("GET", "/api/extension-import")
    assert code == 200
    after_tab_open = json.loads(body.decode("utf-8"))
    assert after_tab_open["total_rows_loaded"] == 2
    loaded_usernames = {p["username"] for p in after_tab_open["profiles"]}
    assert loaded_usernames == {"first_checked_human", "second_opened_tab"}

    # 3. User clicks inline [Bot] in index.html or HUD via POST /api/labels
    code, _, _ = _call_wsgi(
        "POST",
        "/api/labels",
        json_body={
            "username": "second_opened_tab",
            "label": "bot",
            "artist": "live_target",
            "evidence_note": "Marked from Audit Table on @live_target",
        },
    )
    assert code == 200

    # Next GET /api/extension-import immediately reflects manual_label + ground_truth!
    code, _, body = _call_wsgi("GET", "/api/extension-import")
    assert code == 200
    after_mark = json.loads(body.decode("utf-8"))
    by_user = {p["username"]: p for p in after_mark["profiles"]}
    assert by_user["second_opened_tab"]["manual_label"] == "bot"
    assert after_mark["ground_truth"]["confirmed_bots"] == 1
    assert after_mark["ground_truth"]["confirmed_bots_detected"] == 1


def test_cross_checkpoint_and_benchmark_real_enrichment_matches_history_1_to_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Handle-only followers recover real observed numbers from disk and match History 1-to-1."""
    paths = _setup_isolated_env(monkeypatch, tmp_path)

    # Write a real observed row into verified_benchmarks.csv (backed by verified_bots.csv)
    paths["bots_csv"].write_text(
        "Instagram Links\nhttps://www.instagram.com/zebra.9605848/\n",
        encoding="utf-8",
    )
    paths["bench_csv"].write_text(
        "artist,username,has_profile_photo,post_count,followers_count,"
        "following_count,bio_length,account_private,username_digit_ratio\n"
        "Target_Bots,zebra.9605848,false,0,3,3100,0,false,0.5385\n",
        encoding="utf-8",
    )
    web_mod.save_artist_checkpoint(
        "other_artist",
        [_observed_profile("shared_real_fan", "other_artist", post_count=45, followers_count=510)],
    )

    # Save target_artist checkpoint where zebra.9605848 & shared_real_fan are handle-only,
    # plus 1 direct observed follower and 1 truly unobserved handle-only follower
    web_mod.save_artist_checkpoint(
        "target_artist",
        [
            _observed_profile("direct_checked_user", "target_artist"),
            {"artist": "target_artist", "username": "zebra.9605848", "schema_version": 2},
            {"artist": "target_artist", "username": "shared_real_fan", "schema_version": 2},
            {"artist": "target_artist", "username": "never_opened_user", "schema_version": 2},
        ],
    )

    # Also mark an unopened follower manually via HUD note -> goes to manual_unscored_profiles
    web_mod.save_label_note(
        LabelNote(
            username="hud_marked_unopened",
            label="bot",
            evidence_note="Marked from Instagram HUD on @target_artist",
        )
    )

    code, _, hist_body = _call_wsgi("GET", "/api/history")
    assert code == 200
    hist_items = {h["artist"]: h for h in json.loads(hist_body.decode("utf-8"))["history"]}
    target_hist = hist_items["target_artist"]

    code, _, imp_body = _call_wsgi("GET", "/api/extension-import", query="artist=target_artist")
    assert code == 200
    imp_data = json.loads(imp_body.decode("utf-8"))

    # 3 real observed profiles scored (direct + benchmark + cross-checkpoint), 2 unscored!
    assert imp_data["total_rows_loaded"] == 3
    assert target_hist["deep_checked"] == 3
    assert target_hist["total_scanned"] == 5
    assert target_hist["checkpoint_saved"] == 4
    # Effective bot rate in artist summary matches History card bot_pct 1-to-1
    assert imp_data["artists"][0]["effective_bot_count"] == target_hist["effective_bot_count"]
    assert round(imp_data["artists"][0]["effective_bot_rate"] * 100, 1) == target_hist["bot_pct"]

    scored_names = {p["username"] for p in imp_data["profiles"]}
    assert scored_names == {"direct_checked_user", "zebra.9605848", "shared_real_fan"}
    assert "never_opened_user" not in scored_names
    assert "hud_marked_unopened" not in scored_names

    manual_unscored_names = {p["username"] for p in imp_data["manual_unscored_profiles"]}
    assert manual_unscored_names == {"hud_marked_unopened"}
    assert imp_data["manual_unscored_profiles"][0]["post_count"] is None
    assert imp_data["manual_unscored_profiles"][0]["labeled_on"]
    assert imp_data["ground_truth"]["confirmed_bots_unscored"] == 1
    assert (
        imp_data["ground_truth"]["confirmed_bots_total"]
        == imp_data["ground_truth"]["confirmed_bots"] + 1
    )

    # GET / serves index.html with Cache-Control: no-store
    root_code, root_headers, _ = _call_wsgi("GET", "/")
    assert root_code == 200
    assert root_headers.get("cache-control") == "no-store"


def test_every_scored_profile_clue_weights_sum_identically_to_bot_score() -> None:
    """Every profile's active clue chips sum 100% identically to round(bot_score * 100)."""
    csv_text = (
        "artist,username,has_profile_photo,post_count,followers_count,"
        "following_count,bio_length,account_private,username_digit_ratio\n"
        # Case 1 (stitched._10_ style): no_photo(20)+zero_posts(25)+empty_bio(10)+stitched(10) = 65
        "target_alpha,stitched._10_,false,0,120,300,0,false,0.2000\n"
        # Case 2 (shellbotzzzz style): zero_posts(25)+shell(15)+ratio_cap(60)+bio(10)+stitched(10) = 120 -> 100
        "target_alpha,shellbotzzzz,true,0,5,2500,0,true,0.0000\n"
        # Case 3 (word.digits bot): no_photo(20)+zero(25)+shell(15)+ratio_cap(60)+bio(10)+word.digits(20) = 150 -> 100
        "target_alpha,sample.9605848,false,0,2,1900,0,false,0.5385\n"
        # Case 4 (dressed-up 3-post burst + digit_heavy): burst(15)+ratio_cap(60)+bio(10)+digits(20) = 105 -> 100
        "target_alpha,burst318685,true,3,12,2100,0,false,0.5000\n"
        # Case 5 (normal active human): 0 pts
        "target_alpha,organic_human,true,84,650,410,48,false,0.0000\n"
    )

    res = run_audit_from_csv_text(csv_text)
    by_user = {p["username"]: p for p in res["profiles"]}

    for uname, prof in by_user.items():
        expected_capped = min(100, prof["raw_score_pts"])
        actual_pts = round(prof["bot_score"] * 100)
        assert expected_capped == actual_pts, f"Mismatch on {uname}: {prof}"
        assert prof["flags"]["registry_boost_applied"] is False

    assert by_user["stitched._10_"]["raw_score_pts"] == 65
    assert by_user["stitched._10_"]["flags"]["stitched_username"] is True
    assert by_user["stitched._10_"]["handle_clue_reason"] == "stitched_pattern"

    assert by_user["shellbotzzzz"]["raw_score_pts"] == 120
    assert by_user["shellbotzzzz"]["ratio_cluster_raw_pts"] == 70
    assert by_user["shellbotzzzz"]["ratio_cluster_pts"] == 60
    assert by_user["shellbotzzzz"]["ratio_cluster_capped"] is True
    assert by_user["shellbotzzzz"]["flags"]["blank_shell_bonus"] is True
    assert by_user["shellbotzzzz"]["handle_clue_reason"] == "stitched_pattern"

    assert by_user["sample.9605848"]["raw_score_pts"] == 150
    assert round(by_user["sample.9605848"]["bot_score"] * 100) == 100
    assert by_user["sample.9605848"]["handle_clue_reason"] == "word_dot_digits"

    assert by_user["burst318685"]["raw_score_pts"] == 105
    assert by_user["burst318685"]["handle_clue_reason"] == "digit_heavy"

    assert by_user["organic_human"]["raw_score_pts"] == 0
    assert by_user["organic_human"]["handle_clue_reason"] == ""


def test_inspector_dom_placement_and_js_syntax_contract() -> None:
    """Verify Inspector HTML structure, pagination placement, extension sync, and JS syntax."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    ext_js = CONTENT_JS.read_text(encoding="utf-8")

    # 1. Pagination (#inspectorPageSize) MUST live inside #followerInspectorSection,
    #    NOT inside #rankingTable's section!
    rank_idx = html.index('id="rankingTable"')
    insp_idx = html.index('id="followerInspectorSection"')
    page_size_idx = html.index('id="inspectorPageSize"')
    custom_sec_idx = html.index('id="customAuditSection"')
    assert rank_idx < insp_idx < page_size_idx < custom_sec_idx

    # 2. Required upgraded Inspector elements and JS hooks exist
    for required_token in (
        'id="inspectorSummaryStrip"',
        'id="inspectorSort"',
        'id="pillDressedBots"',
        'id="pillUnlabeled"',
        'id="pillManualUnscored"',
        'id="btnLiveSyncNow"',
        "function buildExactClueChipsHtml(",
        "function isEffectiveBotProfile(",
        "function renderRankingTable(",
        "effectiveBotCount",
        "function quickMarkProfileFromTable(",
        "function refreshLiveAuditNow(",
        "insp-bento-stats",
        "insp-score-track",
        "insp-quick-mark",
        "insp-confirmed-pill",
        "insp-recheck-btn",
        "function toggleRowRecheck(",
        "hasConfirmedLabel && !isEditingRow",
    ):
        assert required_token in html, f"Missing {required_token} in index.html"

    # 3. Extension content.js must persist opened follower profiles automatically
    assert "function syncSingleProfileToServer(" in ext_js
    assert "function findProfileKeyInMap(" in ext_js

    # 4. Validate inline <script> syntax in index.html and content.js with Node if available
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.DOTALL)
    assert len(scripts) >= 1
    try:
        node_ver = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        node_ver = None

    if node_ver and node_ver.returncode == 0:
        chk_ext = subprocess.run(
            ["node", "--check", str(CONTENT_JS)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert chk_ext.returncode == 0, f"Syntax error in content.js: {chk_ext.stderr}"

        chk_inline = subprocess.run(
            ["node", "--check"],
            input=scripts[-1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert chk_inline.returncode == 0, f"Syntax error in index.html <script>: {chk_inline.stderr}"


def test_checkpoint_auto_recovers_orphaned_manual_labels_without_overwriting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """POST /api/checkpoint recovers any stranded manual_label safely without touching existing CSV labels."""
    paths = _setup_isolated_env(monkeypatch, tmp_path)

    # Pre-seed an existing human label for @protected_human
    paths["users_csv"].write_text(
        "Instagram Links\nhttps://www.instagram.com/protected_human/\n",
        encoding="utf-8",
    )

    code, _, _ = _call_wsgi(
        "POST",
        "/api/checkpoint",
        json_body={
            "artist": "recovery_artist",
            "profiles": [
                {**_observed_profile("stranded_bot_1", "recovery_artist"), "manual_label": "bot"},
                {**_observed_profile("protected_human", "recovery_artist"), "manual_label": "bot"},
            ],
        },
    )
    assert code == 200

    # @stranded_bot_1 is recovered into labels; @protected_human stays human!
    reloaded = labels_mod.load_labels(
        bots_path=paths["bots_csv"],
        users_path=paths["users_csv"],
        notes_path=paths["notes_csv"],
    )
    assert reloaded.label_of("stranded_bot_1") == "bot"
    assert reloaded.label_of("protected_human") == "human"


def test_checkpoint_and_enrichment_reject_self_handle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An audited target account must never be stored or enriched as its own follower."""
    paths = _setup_isolated_env(monkeypatch, tmp_path)

    saved = web_mod.save_artist_checkpoint(
        "target_alpha",
        [
            _observed_profile("target_alpha", "target_alpha", post_count=916, followers_count=7998),
            _observed_profile("real_follower_1", "target_alpha"),
        ],
    )
    assert saved["total_saved"] == 1
    assert [p["username"] for p in saved["profiles"]] == ["real_follower_1"]

    # Even if a label note mentions the target handle itself, _collect_enriched_artist_profiles excludes it
    web_mod.save_label_note(
        LabelNote(
            username="target_alpha",
            label="human",
            evidence_note="Marked from Instagram HUD on @target_alpha",
        )
    )
    notes_map = labels_mod.load_label_notes(paths["notes_csv"])
    enriched = web_mod._collect_enriched_artist_profiles(
        "target_alpha",
        [
            _observed_profile("target_alpha", "target_alpha"),
            _observed_profile("real_follower_1", "target_alpha"),
        ],
        global_observed={},
        label_notes=notes_map,
    )
    assert [p["username"] for p in enriched] == ["real_follower_1"]


def test_enrichment_never_resurrects_removed_audit_table_notes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Audit-Table notes do not re-inject removed handles, while HUD notes still load."""
    paths = _setup_isolated_env(monkeypatch, tmp_path)

    # Save a real follower into target_artist checkpoint
    web_mod.save_artist_checkpoint(
        "target_artist",
        [_observed_profile("kept_follower", "target_artist")],
    )
    web_mod.save_label_note(
        LabelNote(
            username="removed_audit_celeb",
            label="human",
            evidence_note="Marked from Audit Table on @target_artist",
        )
    )
    web_mod.save_label_note(
        LabelNote(
            username="hud_only_follower",
            label="bot",
            evidence_note="Marked from Instagram HUD on @target_artist",
        )
    )
    # Seed benchmark data for the removed non-follower celebrity so global_observed has its stats
    paths["bench_csv"].write_text(
        "artist,username,has_profile_photo,post_count,followers_count,following_count,bio_length,account_private,username_digit_ratio\n"
        "target_artist,removed_audit_celeb,true,900,5000000,100,80,false,0.0000\n",
        encoding="utf-8",
    )

    notes_map = labels_mod.load_label_notes(paths["notes_csv"])
    global_obs = web_mod._load_global_observed_profiles(
        cp_dir=paths["cp_dir"], benchmarks_path=paths["bench_csv"]
    )
    cp = web_mod.load_artist_checkpoint("target_artist")
    enriched = web_mod._collect_enriched_artist_profiles(
        "target_artist",
        cp["profiles"],
        global_observed=global_obs,
        label_notes=notes_map,
    )
    names = {p["username"] for p in enriched}
    assert "kept_follower" in names
    assert "hud_only_follower" in names
    assert "removed_audit_celeb" not in names


def test_checkpoint_exists_flag_and_extension_ghost_pruning_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """load_artist_checkpoint reports checkpoint_exists accurately and content.js prunes ghosts."""
    _setup_isolated_env(monkeypatch, tmp_path)

    empty_cp = web_mod.load_artist_checkpoint("nonexistent_artist")
    assert empty_cp["checkpoint_exists"] is False
    assert empty_cp["profiles"] == []

    web_mod.save_artist_checkpoint(
        "nonexistent_artist",
        [_observed_profile("fan_one", "nonexistent_artist")],
    )
    saved_cp = web_mod.load_artist_checkpoint("nonexistent_artist")
    assert saved_cp["checkpoint_exists"] is True
    assert [p["username"] for p in saved_cp["profiles"]] == ["fan_one"]

    ext_js = CONTENT_JS.read_text(encoding="utf-8")
    assert "botaudit_artist_v4_" in ext_js
    assert "checkpoint_exists" in ext_js
    assert "serverKeysLower" in ext_js
    assert "state.profilesMap.delete(" in ext_js

    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "const displayPct = rawPct === 0 ? \"0\" : rawPct.toFixed(1);" in html
    assert "item.checkpoint_saved ?? item.total_scanned" in html
    assert "Confirmed Bots (with numbers) on @" in html


def test_single_artist_report_chart_and_private_bots_use_effective_bots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Single-artist audit report, chart alt text, private_bots_count, and checkpoint_saved align with manual confirmations."""
    _setup_isolated_env(monkeypatch, tmp_path)

    # Create 2 real observed profiles for target_artist:
    # 1) an obvious auto bot (public)
    # 2) a private account scoring <= 60p that is manually confirmed as a bot
    web_mod.save_artist_checkpoint(
        "solo_artist",
        [
            _observed_profile(
                "auto_bot_public",
                "solo_artist",
                has_profile_photo=False,
                account_private=False,
                post_count=0,
                followers_count=2,
                following_count=2200,
                bio_length=0,
            ),
            _observed_profile(
                "dressed_private_bot",
                "solo_artist",
                has_profile_photo=True,
                account_private=True,
                post_count=18,
                followers_count=300,
                following_count=300,
                bio_length=40,
            ),
            {"artist": "solo_artist", "username": "unopened_handle_only", "schema_version": 3},
        ],
    )
    web_mod.save_label_note(
        LabelNote(
            username="dressed_private_bot",
            label="bot",
            evidence_note="Marked from Instagram HUD on @solo_artist",
        )
    )

    captured = web_mod.load_checkpoint_capture("solo_artist")
    assert captured is not None
    artist_row = captured["artists"][0]
    assert artist_row["sample_size"] == 2
    assert artist_row["effective_bot_count"] == 2
    assert artist_row["effective_bot_rate"] == 1.0
    assert artist_row["private_bots_count"] == 1
    assert artist_row["checkpoint_saved"] == 3
    assert captured["checkpoint_meta"]["checkpoint_saved"] == 3
    assert "100.0%" in captured["chart_alt_text"]
    assert "100.0%" in captured["report_html"]


def test_raw_false_alarms_reported_honestly_while_owner_override_protects_effective_rate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Raw checklist false alarms (>60p confirmed human) are exposed honestly on ground_truth."""
    _setup_isolated_env(monkeypatch, tmp_path)

    # Confirm 2 humans in registry:
    # 1) false_alarm_human scores >60p raw (no photo 20 + zero posts 25 + empty bio 10 + mass_following 25 = 80p)
    # 2) clean_normal_human scores 0p raw
    web_mod.save_label_note(
        LabelNote(
            username="false_alarm_human",
            label="human",
            evidence_note="Marked from Instagram HUD on @honest_target",
        )
    )
    web_mod.save_label_note(
        LabelNote(
            username="clean_normal_human",
            label="human",
            evidence_note="Marked from Instagram HUD on @honest_target",
        )
    )
    web_mod.save_artist_checkpoint(
        "honest_target",
        [
            _observed_profile(
                "false_alarm_human",
                "honest_target",
                has_profile_photo=False,
                account_private=False,
                post_count=0,
                followers_count=200,
                following_count=3500,
                bio_length=0,
            ),
            _observed_profile(
                "clean_normal_human",
                "honest_target",
                has_profile_photo=True,
                account_private=False,
                post_count=80,
                followers_count=600,
                following_count=450,
                bio_length=45,
            ),
        ],
    )

    captured = web_mod.load_checkpoint_capture("honest_target")
    assert captured is not None
    by_user = {p["username"]: p for p in captured["profiles"]}

    # Owner rule still prevents confirmed humans from inflating effective_bot_count
    assert by_user["false_alarm_human"]["bot_like"] is False
    assert by_user["false_alarm_human"]["raw_bot_like"] is True
    assert by_user["clean_normal_human"]["bot_like"] is False
    assert by_user["clean_normal_human"]["raw_bot_like"] is False

    artist_row = captured["artists"][0]
    assert artist_row["sample_size"] == 2
    assert artist_row["effective_bot_count"] == 0

    gt = captured["ground_truth"]
    assert gt["confirmed_humans"] == 2
    assert gt["confirmed_humans_normal"] == 1
    assert gt["confirmed_humans_false_alarms"] == 1
    assert gt["specificity_pct"] == 50.0
    assert gt["confirmed_humans_raw_normal"] == 1
    assert gt["confirmed_humans_raw_false_alarms"] == 1
    assert gt["raw_specificity_pct"] == 50.0
    assert gt["confirmed_humans_overridden"] == 1

    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "raw_bot_like" in html
    assert "RAW_FALSE_ALARMS" in html
    assert "btnViewRawFalseAlarms" in html


def test_handles_starting_with_http_prefix_are_preserved_while_raw_urls_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Valid handles like @https.zalexo42 score & match History 1-to-1; non-IG URLs are ignored."""
    _setup_isolated_env(monkeypatch, tmp_path)

    web_mod.save_artist_checkpoint(
        "prefix_artist",
        [
            _observed_profile("https.zalexo42", "prefix_artist", post_count=1, followers_count=865, following_count=886),
            _observed_profile("http_alex", "prefix_artist", post_count=12, followers_count=300, following_count=200),
            _observed_profile("https://example.com/not-an-ig-handle", "prefix_artist"),
        ],
    )

    hist_map = {h["artist"]: h for h in web_mod.get_checkpoints_history()}
    captured = web_mod.load_checkpoint_capture("prefix_artist")
    assert captured is not None

    loaded_names = {p["username"] for p in captured["profiles"]}
    assert loaded_names == {"https.zalexo42", "http_alex"}
    assert captured["artists"][0]["sample_size"] == hist_map["prefix_artist"]["deep_checked"] == 2


def test_content_js_hover_verdict_compact_storage_and_manual_override() -> None:
    """Extension helpers count conclusive hover profiles, preserve stats, and enforce manual precedence."""
    node_check = subprocess.run(["node", "--version"], capture_output=True, text=True, check=False)
    if node_check.returncode != 0:
        pytest.skip("Node.js not installed")

    parser_js = REPO_ROOT / "browser-extension" / "parser.js"
    script = f"""
    const fs = require("fs");
    const vm = require("vm");
    const noop = () => {{}};
    const dummyEl = () => ({{
      id: "", className: "", style: {{}}, dataset: {{}}, innerHTML: "", textContent: "",
      children: [], querySelectorAll: () => [], querySelector: () => null,
      addEventListener: noop, appendChild: noop, remove: noop, closest: () => null,
      getAttribute: () => "", getBoundingClientRect: () => ({{ left: 0, top: 0, width: 320, height: 200 }})
    }});
    const sandbox = {{
      console, Date, Math, JSON, Number, String, Boolean, Array, Object, Set, Map, RegExp, Error, Promise,
      setTimeout, clearTimeout,
      setInterval: () => 1, clearInterval: noop,
      localStorage: {{ _d: {{}}, getItem(k) {{ return this._d[k] || null; }}, setItem(k, v) {{ this._d[k] = String(v); }}, removeItem(k) {{ delete this._d[k]; }} }},
      document: {{
        hidden: true, activeElement: null,
        body: Object.assign(dummyEl(), {{ innerText: "" }}),
        head: dummyEl(), documentElement: dummyEl(),
        getElementById: () => null,
        createElement: () => dummyEl(),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: noop
      }},
      location: {{ pathname: "/_creator.beta_/followers/" }},
      addEventListener: noop,
      fetch: () => Promise.reject(new Error("offline")),
    }};
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync({json.dumps(str(parser_js))}, "utf8"), sandbox);
    vm.runInContext(fs.readFileSync({json.dumps(str(CONTENT_JS))}, "utf8"), sandbox);

    const P = sandbox.BotAuditParser;
    const H = sandbox.__BOT_AUDIT_UI_HELPERS__;

    const hoverBot = {{
      schema_version: P.SCHEMA_VERSION,
      artist: "_creator.beta_",
      username: "nawabayan111",
      has_profile_photo: true,
      account_private: true,
      post_count: 0,
      followers_count: 240,
      following_count: 5799,
      bio_length: null,
      collected_at: "2026-11-22"
    }};
    Object.assign(hoverBot, {{
      bot_like: P.scoreFollowerProfile(hoverBot).botLike,
      score_complete: P.scoreFollowerProfile(hoverBot).complete,
      tier: P.profileTier(hoverBot)
    }});

    const hoverHuman = {{
      schema_version: P.SCHEMA_VERSION,
      artist: "_creator.beta_",
      username: "_denisa.stancu_",
      has_profile_photo: true,
      account_private: false,
      post_count: 381,
      followers_count: 1450,
      following_count: 620,
      bio_length: null,
      collected_at: "2026-11-22"
    }};
    Object.assign(hoverHuman, {{
      bot_like: P.scoreFollowerProfile(hoverHuman).botLike,
      score_complete: P.scoreFollowerProfile(hoverHuman).complete,
      tier: P.profileTier(hoverHuman)
    }});

    const staleCachedBot = {{
      schema_version: P.SCHEMA_VERSION,
      artist: "_creator.beta_",
      username: "cutlaibapathan",
      has_profile_photo: null,
      account_private: null,
      post_count: 0,
      followers_count: 7,
      following_count: 6149,
      bio_length: null,
      bot_like: null
    }};
    const hoverCheck = {{
      schema_version: P.SCHEMA_VERSION,
      artist: "_creator.beta_",
      username: "rafa.a.14",
      has_profile_photo: true,
      account_private: false,
      post_count: 0,
      followers_count: 171,
      following_count: 260,
      bio_length: null
    }};
    const result = {{
      autoBot: H.classifyProfileVerdict(hoverBot, ""),
      overrideHuman: H.classifyProfileVerdict(hoverBot, "human"),
      autoHuman: H.classifyProfileVerdict(hoverHuman, ""),
      overrideBot: H.classifyProfileVerdict(hoverHuman, "bot"),
      staleBotNullFlags: H.classifyProfileVerdict(staleCachedBot, ""),
      hoverCheckUnlabeled: H.classifyProfileVerdict(hoverCheck, ""),
      hoverCheckManualBot: H.classifyProfileVerdict(hoverCheck, "bot"),
      compactHoverBot: H.buildCompactLocalProfile(hoverBot),
      handleOnly: H.classifyProfileVerdict({{ username: "rajourqw", tier: 1, has_profile_photo: true }}, "")
    }};
    console.log(JSON.stringify(result));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    res = json.loads(proc.stdout.strip().splitlines()[-1])

    assert res["autoBot"] == {
        "decided": True, "isBot": True, "verdict": "bot", "source": "auto",
        "overriddenAutoBot": False, "overriddenAutoHuman": False,
    }
    assert res["overrideHuman"] == {
        "decided": True, "isBot": False, "verdict": "human", "source": "manual",
        "overriddenAutoBot": True, "overriddenAutoHuman": False,
    }
    assert res["autoHuman"] == {
        "decided": True, "isBot": False, "verdict": "human", "source": "auto",
        "overriddenAutoBot": False, "overriddenAutoHuman": False,
    }
    assert res["overrideBot"] == {
        "decided": True, "isBot": True, "verdict": "bot", "source": "manual",
        "overriddenAutoBot": False, "overriddenAutoHuman": True,
    }
    assert res["staleBotNullFlags"] == {
        "decided": True, "isBot": True, "verdict": "bot", "source": "auto",
        "overriddenAutoBot": False, "overriddenAutoHuman": False,
    }
    assert res["hoverCheckUnlabeled"] == {
        "decided": False, "isBot": False, "verdict": None, "source": "unknown",
        "overriddenAutoBot": False, "overriddenAutoHuman": False,
    }
    assert res["hoverCheckManualBot"] == {
        "decided": True, "isBot": True, "verdict": "bot", "source": "manual",
        "overriddenAutoBot": False, "overriddenAutoHuman": False,
    }
    assert res["handleOnly"] == {
        "decided": False, "isBot": False, "verdict": None, "source": "unknown",
        "overriddenAutoBot": False, "overriddenAutoHuman": False,
    }
    # Compact local storage must NOT strip header counts from hover-card profiles
    compact = res["compactHoverBot"]
    assert compact["post_count"] == 0
    assert compact["followers_count"] == 240
    assert compact["following_count"] == 5799
    assert compact["bio_length"] is None

    # Symmetric Bots & Humans KPI markup must be present in both HUD and toolbar popup
    content_txt = CONTENT_JS.read_text(encoding="utf-8")
    popup_html_txt = (REPO_ROOT / "browser-extension" / "popup.html").read_text(encoding="utf-8")
    popup_js_txt = (REPO_ROOT / "browser-extension" / "popup.js").read_text(encoding="utf-8")
    assert "botaudit-kpi-bot" in content_txt and "botaudit-kpi-human" in content_txt
    assert "humansCount" in content_txt and "humansCount" in popup_js_txt
    assert 'id="val-bots"' in popup_html_txt and 'id="val-humans"' in popup_html_txt


def test_hover_card_profiles_counted_without_opening_tab_and_manual_human_overrides_auto_bot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Hover-card Bot/Human profiles score without opening tabs, and manual Human overrides auto-Bot everywhere."""
    _setup_isolated_env(monkeypatch, tmp_path)

    hover_bot = {
        "schema_version": 3,
        "artist": "hover_target",
        "username": "nawabayan111",
        "has_profile_photo": True,
        "account_private": True,
        "post_count": 0,
        "followers_count": 240,
        "following_count": 5799,
        "bio_length": None,
        "deep_checked": False,
    }
    hover_human = {
        "schema_version": 3,
        "artist": "hover_target",
        "username": "real_hover_user",
        "has_profile_photo": True,
        "account_private": False,
        "post_count": 381,
        "followers_count": 1450,
        "following_count": 620,
        "bio_length": None,
        "deep_checked": False,
    }
    hover_borderline = {
        "schema_version": 3,
        "artist": "hover_target",
        "username": "user_12345",
        "has_profile_photo": False,
        "account_private": False,
        "post_count": 10,
        "followers_count": 90,
        "following_count": 120,
        "bio_length": None,
        "deep_checked": False,
    }
    hover_check_ambiguous = {
        "schema_version": 3,
        "artist": "hover_target",
        "username": "rafa.a.14",
        "has_profile_photo": True,
        "account_private": False,
        "post_count": 0,
        "followers_count": 171,
        "following_count": 260,
        "bio_length": None,
        "deep_checked": False,
    }
    missing_priv_bot = {
        "schema_version": 3,
        "artist": "hover_target",
        "username": "missing_priv_bot",
        "has_profile_photo": True,
        "account_private": None,
        "post_count": 0,
        "followers_count": 7,
        "following_count": 6149,
        "bio_length": None,
        "deep_checked": False,
    }
    handle_only = {
        "schema_version": 3,
        "artist": "hover_target",
        "username": "unhovered_handle",
        "has_profile_photo": True,
        "account_private": None,
        "post_count": None,
        "followers_count": None,
        "following_count": None,
        "bio_length": None,
        "deep_checked": False,
    }

    code, _, body = _call_wsgi(
        "POST",
        "/api/extension-import",
        json_body={
            "artist": "hover_target",
            "merge_checkpoint": True,
            "profiles": [
                hover_bot,
                missing_priv_bot,
                hover_human,
                hover_borderline,
                hover_check_ambiguous,
                handle_only,
            ],
        },
    )
    assert code == 200
    imp = json.loads(body.decode("utf-8"))
    assert imp["total_rows_loaded"] == 3  # nawabayan111, missing_priv_bot, real_hover_user; CHECK & borderline & handle-only excluded!
    assert imp["artists"][0]["effective_bot_count"] == 2
    by_user = {p["username"]: p for p in imp["profiles"]}
    assert by_user["nawabayan111"]["bot_like"] is True
    assert by_user["nawabayan111"]["bio_length"] is None
    assert by_user["missing_priv_bot"]["bot_like"] is True
    assert by_user["missing_priv_bot"]["account_private"] is None
    assert by_user["real_hover_user"]["bot_like"] is False
    assert by_user["real_hover_user"]["bio_length"] is None
    assert "rafa.a.14" not in by_user

    # /api/history and /api/bot-network agree 1-to-1 before manual override
    _, _, hist_raw = _call_wsgi("GET", "/api/history")
    hist = {h["artist"]: h for h in json.loads(hist_raw.decode("utf-8"))["history"]}["hover_target"]
    assert hist["deep_checked"] == 3
    assert hist["effective_bot_count"] == 2
    assert hist["real_count"] == 1

    _, _, net_raw = _call_wsgi("GET", "/api/bot-network")
    net = json.loads(net_raw.decode("utf-8"))
    net_acc = {a["artist"]: a for a in net["accounts"]}["hover_target"]
    assert net_acc["deep_checked"] == 3
    assert net_acc["effective_bot_count"] == 2
    assert net_acc["real_count"] == 1
    assert {b["username"] for b in net["exclusive_bots_by_account"]["hover_target"]} == {
        "nawabayan111",
        "missing_priv_bot",
    }

    # Now owner verifies @nawabayan111 manually as HUMAN -> MUST override extension auto-Bot,
    # and manually confirms @rafa.a.14 as BOT -> immediately promotes CHECK row to Confirmed Bot!
    lbl_code, _, _ = _call_wsgi(
        "POST",
        "/api/labels",
        json_body={
            "username": "nawabayan111",
            "label": "human",
            "artist": "hover_target",
            "evidence_note": "Marked from Instagram HUD on @hover_target",
        },
    )
    assert lbl_code == 200
    lbl_code_2, _, _ = _call_wsgi(
        "POST",
        "/api/labels",
        json_body={
            "username": "rafa.a.14",
            "label": "bot",
            "artist": "hover_target",
            "evidence_note": "Marked from Instagram HUD on @hover_target",
        },
    )
    assert lbl_code_2 == 200

    _, _, after_raw = _call_wsgi("GET", "/api/extension-import", query="artist=hover_target")
    after_imp = json.loads(after_raw.decode("utf-8"))
    after_by_user = {p["username"]: p for p in after_imp["profiles"]}
    assert after_by_user["nawabayan111"]["manual_label"] == "human"
    assert after_by_user["nawabayan111"]["bot_like"] is False
    assert after_by_user["nawabayan111"]["raw_bot_like"] is True
    assert after_by_user["rafa.a.14"]["manual_label"] == "bot"
    assert after_imp["artists"][0]["effective_bot_count"] == 2  # missing_priv_bot + rafa.a.14
    assert after_imp["ground_truth"]["confirmed_humans_overridden"] == 1

    _, _, hist_after_raw = _call_wsgi("GET", "/api/history")
    hist_after = {h["artist"]: h for h in json.loads(hist_after_raw.decode("utf-8"))["history"]}["hover_target"]
    assert hist_after["deep_checked"] == 4
    assert hist_after["effective_bot_count"] == 2
    assert hist_after["real_count"] == 2

    _, _, net_after_raw = _call_wsgi("GET", "/api/bot-network")
    net_after = json.loads(net_after_raw.decode("utf-8"))
    net_acc_after = {a["artist"]: a for a in net_after["accounts"]}["hover_target"]
    assert net_acc_after["deep_checked"] == 4
    assert net_acc_after["effective_bot_count"] == 2
    assert net_acc_after["real_count"] == 2
    assert {b["username"] for b in net_after["exclusive_bots_by_account"]["hover_target"]} == {
        "missing_priv_bot",
        "rafa.a.14",
    }


def test_atomic_write_text_retries_transient_permission_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_atomic_write_text retries Windows-style PermissionError races and cleans temp files."""
    target = tmp_path / "sample_artist.json"
    orig_replace = Path.replace
    attempts = 0

    def flaky_replace(self: Path, dest: Path) -> Path:
        nonlocal attempts
        if self.parent == tmp_path and self.name.endswith(".tmp"):
            attempts += 1
            if attempts < 3:
                raise PermissionError(13, "Access is denied")
        return orig_replace(self, dest)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    web_mod._atomic_write_text(target, '{"ok":true}', max_attempts=6)
    assert attempts == 3
    assert target.read_text(encoding="utf-8") == '{"ok":true}'
    assert list(tmp_path.glob("*.tmp")) == []


def test_content_js_hud_render_memoization_delegated_confirm_and_visible_subcounters() -> None:
    """HUD avoids 2.5s innerHTML thrashing, handles delegated clicks, and shows confirmed vs auto."""
    node_check = subprocess.run(["node", "--version"], capture_output=True, text=True, check=False)
    if node_check.returncode != 0:
        pytest.skip("Node.js not installed")

    content_txt = CONTENT_JS.read_text(encoding="utf-8")
    popup_html_txt = (REPO_ROOT / "browser-extension" / "popup.html").read_text(encoding="utf-8")
    popup_js_txt = (REPO_ROOT / "browser-extension" / "popup.js").read_text(encoding="utf-8")
    assert "botaudit-kpi-sub" in content_txt
    assert 'id="sub-bots"' in popup_html_txt and 'id="sub-humans"' in popup_html_txt
    assert "sub-bots" in popup_js_txt and "confirmedBotsCount" in popup_js_txt

    parser_js = REPO_ROOT / "browser-extension" / "parser.js"
    script = f"""
    const fs = require("fs");
    const vm = require("vm");
    const listeners = {{}};
    let htmlWriteCount = 0;
    let currentHtml = "";
    const noop = () => {{}};
    const dummyChild = () => ({{ onclick: null, onchange: null, addEventListener: noop }});
    const dockEl = {{
      id: "botaudit-floating-dock",
      className: "botaudit-floating-dock",
      style: {{}},
      dataset: {{}},
      classList: {{ add: noop, remove: noop, contains: () => false }},
      get innerHTML() {{ return currentHtml; }},
      set innerHTML(v) {{ htmlWriteCount++; currentHtml = String(v); }},
      querySelector: (sel) => (sel === ".botaudit-avatar-img" ? null : dummyChild()),
      querySelectorAll: () => [],
      addEventListener(type, fn) {{
        listeners[type] = listeners[type] || [];
        listeners[type].push(fn);
      }},
      closest: () => null,
      getAttribute: () => "",
      getBoundingClientRect: () => ({{ left: 0, top: 0, width: 336, height: 420 }})
    }};
    const sandbox = {{
      console, Date, Math, JSON, Number, String, Boolean, Array, Object, Set, Map, RegExp, Error, Promise,
      setTimeout, clearTimeout,
      setInterval: () => 1, clearInterval: noop,
      localStorage: {{
        _d: {{}},
        getItem(k) {{ return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; }},
        setItem(k, v) {{ this._d[k] = String(v); }},
        removeItem(k) {{ delete this._d[k]; }}
      }},
      document: {{
        hidden: true,
        activeElement: {{ tagName: "BODY" }},
        body: {{ innerText: "", appendChild: noop }},
        head: {{ appendChild: noop }},
        documentElement: {{ appendChild: noop }},
        getElementById: (id) => (id === "botaudit-floating-dock" ? dockEl : null),
        createElement: () => ({{
          id: "", className: "", style: {{}}, dataset: {{}}, innerHTML: "",
          classList: {{ add: noop, remove: noop, contains: () => false }},
          querySelector: () => null, querySelectorAll: () => [], addEventListener: noop, getAttribute: () => ""
        }}),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: noop
      }},
      location: {{ pathname: "/anshu_1m_____/" }},
      addEventListener: noop,
      fetch: () => Promise.resolve({{ ok: true, status: 200, json: async () => ({{ ok: true }}) }}),
      chrome: {{
        runtime: {{
          lastError: null,
          getURL: (p) => `chrome-extension://test/${{p}}`,
          sendMessage(_msg, cb) {{ if (cb) cb({{ ok: true, closedTab: false }}); }},
          onMessage: {{ addListener: noop }}
        }}
      }}
    }};
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync({json.dumps(str(parser_js))}, "utf8"), sandbox);
    vm.runInContext(fs.readFileSync({json.dumps(str(CONTENT_JS))}, "utf8"), sandbox);

    const P = sandbox.BotAuditParser;
    const H = sandbox.__BOT_AUDIT_UI_HELPERS__;

    H.state.artist = "_creator.beta_";
    const autoBot = {{
      schema_version: P.SCHEMA_VERSION,
      artist: "_creator.beta_",
      username: "anshu_1m_____",
      has_profile_photo: true,
      account_private: false,
      post_count: 0,
      followers_count: 104,
      following_count: 2038,
      bio_length: 106,
      username_digit_ratio: 0.0769,
      deep_checked: true,
      collected_at: "2026-11-22"
    }};
    const scored = P.scoreFollowerProfile(autoBot);
    Object.assign(autoBot, {{
      bot_score: scored.botScore,
      score_pts: scored.scorePts,
      max_score_pts: scored.maxScorePts,
      bot_like: scored.botLike,
      score_conclusive: scored.conclusive,
      score_complete: scored.complete,
      flags: scored.flags,
      tier: P.profileTier(autoBot)
    }});

    H.state.profilesMap.set("anshu_1m_____", autoBot);
    H.state.inspectedPageProfile = Object.assign({{}}, autoBot);

    htmlWriteCount = 0;
    H.updateFloatingDock();
    const writesAfterFirst = htmlWriteCount;
    const htmlBeforeClick = currentHtml;

    // Repeated ticks with unchanged state MUST NOT rewrite innerHTML (fixes swallowed clicks)
    H.updateFloatingDock();
    H.updateFloatingDock();
    const writesAfterIdleTicks = htmlWriteCount;

    // Simulate user clicking #botaudit-mark-bot-btn via delegated dock listener
    for (const fn of listeners.click || []) {{
      fn({{
        preventDefault: noop,
        stopPropagation: noop,
        target: {{
          closest: (sel) => (sel === "#botaudit-mark-bot-btn" ? {{ id: "botaudit-mark-bot-btn" }} : null)
        }}
      }});
    }}
    const writesAfterClick = htmlWriteCount;
    const htmlAfterClick = currentHtml;
    const labelStored = H.state.manualLabelsMap.get("anshu_1m_____");
    const cpProf = H.state.profilesMap.get("anshu_1m_____");

    console.log(JSON.stringify({{
      writesAfterFirst,
      writesAfterIdleTicks,
      writesAfterClick,
      htmlBeforeClick,
      htmlAfterClick,
      labelStored: labelStored ? labelStored.label : null,
      cpLabel: cpProf ? cpProf.manual_label : null
    }}));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    res = json.loads(proc.stdout.strip().splitlines()[-1])

    assert res["writesAfterFirst"] == 1
    assert res["writesAfterIdleTicks"] == 1
    assert res["writesAfterClick"] == 2
    assert "✓ 0 confirmed · 1 auto" in res["htmlBeforeClick"]
    assert "🤖 Mark BOT" in res["htmlBeforeClick"]
    assert "✓ 1 confirmed · 0 auto" in res["htmlAfterClick"]
    assert "CONFIRMED BOT" in res["htmlAfterClick"]
    assert "✓ BOT" in res["htmlAfterClick"]
    assert res["labelStored"] == "bot"
    assert res["cpLabel"] == "bot"


def test_checkpoint_and_labels_return_authoritative_live_counts_matching_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """POST /api/checkpoint and POST /api/labels return exact live_counts matching /api/history."""
    _setup_isolated_env(monkeypatch, tmp_path)

    cp_code, _, cp_body = _call_wsgi(
        "POST",
        "/api/checkpoint",
        json_body={
            "artist": "wave_artist",
            "profiles": [
                _observed_profile(
                    "bot_auto_1",
                    "wave_artist",
                    has_profile_photo=False,
                    post_count=0,
                    followers_count=3,
                    following_count=2600,
                    bio_length=0,
                    account_private=True,
                ),
                _observed_profile(
                    "real_auto_1",
                    "wave_artist",
                    has_profile_photo=True,
                    post_count=42,
                    followers_count=620,
                    following_count=310,
                    bio_length=45,
                ),
                {
                    "artist": "wave_artist",
                    "username": "unopened_handle_only",
                    "has_profile_photo": True,
                    "post_count": None,
                    "followers_count": None,
                    "following_count": None,
                    "bio_length": None,
                    "deep_checked": False,
                    "schema_version": 3,
                },
            ],
        },
    )
    assert cp_code == 200
    cp_json = json.loads(cp_body.decode("utf-8"))
    lc1 = cp_json["live_counts"]
    assert lc1["artist"] == "wave_artist"
    assert lc1["total_saved"] == 3
    assert lc1["checked_count"] == 2
    assert lc1["bots_count"] == 1
    assert lc1["humans_count"] == 1
    assert lc1["auto_bots"] == 1
    assert lc1["confirmed_bots"] == 0
    assert lc1["auto_humans"] == 1
    assert lc1["confirmed_humans"] == 0
    assert lc1["priv_bots_count"] == 1

    # Override auto-bot as human via POST /api/labels -> live_counts flips cleanly
    lbl_code, _, lbl_body = _call_wsgi(
        "POST",
        "/api/labels",
        json_body={
            "username": "bot_auto_1",
            "label": "human",
            "artist": "wave_artist",
            "evidence_note": "Marked from Instagram HUD on @wave_artist",
        },
    )
    assert lbl_code == 200
    lc2 = json.loads(lbl_body.decode("utf-8"))["live_counts"]
    assert lc2["checked_count"] == 2
    assert lc2["bots_count"] == 0
    assert lc2["humans_count"] == 2
    assert lc2["confirmed_humans"] == 1
    assert lc2["auto_humans"] == 1

    _, _, hist_body = _call_wsgi("GET", "/api/history")
    hist = {h["artist"]: h for h in json.loads(hist_body.decode("utf-8"))["history"]}["wave_artist"]
    assert hist["deep_checked"] == lc2["checked_count"]
    assert hist["effective_bot_count"] == lc2["bots_count"]
    assert hist["real_count"] == lc2["humans_count"]


def test_multi_tab_15_wave_queue_live_deltas_and_inline_scouting_accelerators() -> None:
    """15-tab click queue retains all 15 handles, deltas merge cleanly, and inline confirm works."""
    node_check = subprocess.run(["node", "--version"], capture_output=True, text=True, check=False)
    if node_check.returncode != 0:
        pytest.skip("Node.js not installed")

    content_txt = CONTENT_JS.read_text(encoding="utf-8")
    css_txt = (REPO_ROOT / "browser-extension" / "content.css").read_text(encoding="utf-8")
    bg_txt = (REPO_ROOT / "browser-extension" / "background.js").read_text(encoding="utf-8")

    for tok in (".botaudit-row-actions", ".botaudit-row-act", ".botaudit-wave-strip", ".botaudit-row-confirmed"):
        assert tok in css_txt
        assert tok.lstrip(".") in content_txt
    assert "button:not(.botaudit-row-act)" in content_txt
    for bg_tok in ("BOTAUDIT_LIVE_DELTA", "BOTAUDIT_TABS_COUNT", "GET_OPEN_TABS_COUNT"):
        assert bg_tok in bg_txt and bg_tok in content_txt

    parser_js = REPO_ROOT / "browser-extension" / "parser.js"
    script = f"""
    const fs = require("fs");
    const vm = require("vm");
    const noop = () => {{}};
    let currentHtml = "";
    const fakeDialog = {{
      innerText: "Followers",
      querySelectorAll: () => [],
      querySelector: () => null,
      contains: () => true
    }};
    const dockEl = {{
      id: "botaudit-floating-dock",
      className: "botaudit-floating-dock",
      style: {{}},
      dataset: {{}},
      classList: {{ add: noop, remove: noop, contains: () => false }},
      get innerHTML() {{ return currentHtml; }},
      set innerHTML(v) {{ currentHtml = String(v); }},
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: noop,
      closest: () => null,
      getAttribute: () => "",
      getBoundingClientRect: () => ({{ left: 0, top: 0, width: 336, height: 420 }})
    }};
    const storageMap = {{
      botaudit_manual_labels_v1: JSON.stringify({{
        earlier_other_tab_user: {{ username: "earlier_other_tab_user", label: "bot", labeler: "extension_hud" }}
      }})
    }};
    const sandbox = {{
      console, Date, Math, JSON, Number, String, Boolean, Array, Object, Set, Map, RegExp, Error, Promise,
      setTimeout, clearTimeout,
      setInterval: () => 1, clearInterval: noop,
      localStorage: {{
        getItem(k) {{ return Object.prototype.hasOwnProperty.call(storageMap, k) ? storageMap[k] : null; }},
        setItem(k, v) {{ storageMap[k] = String(v); }},
        removeItem(k) {{ delete storageMap[k]; }}
      }},
      document: {{
        hidden: false,
        activeElement: {{ tagName: "BODY" }},
        body: {{ innerText: "", appendChild: noop }},
        head: {{ appendChild: noop }},
        documentElement: {{ appendChild: noop }},
        getElementById: (id) => (id === "botaudit-floating-dock" ? dockEl : null),
        createElement: () => ({{
          id: "", className: "", style: {{}}, dataset: {{}}, innerHTML: "",
          classList: {{ add: noop, remove: noop, contains: () => false }},
          querySelector: () => null, querySelectorAll: () => [], addEventListener: noop, getAttribute: () => ""
        }}),
        querySelector: (sel) => (sel === 'div[role="dialog"]' ? fakeDialog : null),
        querySelectorAll: () => [],
        addEventListener: noop
      }},
      location: {{ pathname: "/_creator.beta_/followers/" }},
      addEventListener: noop,
      fetch: () => Promise.resolve({{ ok: true, status: 200, json: async () => ({{ ok: true }}) }}),
      chrome: {{
        runtime: {{
          lastError: null,
          getURL: (p) => `chrome-extension://test/${{p}}`,
          sendMessage(_msg, cb) {{ if (cb) cb({{ ok: true, openFollowerTabs: 15 }}); }},
          onMessage: {{ addListener: noop }}
        }}
      }}
    }};
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync({json.dumps(str(parser_js))}, "utf8"), sandbox);
    vm.runInContext(fs.readFileSync({json.dumps(str(CONTENT_JS))}, "utf8"), sandbox);

    const P = sandbox.BotAuditParser;
    const H = sandbox.__BOT_AUDIT_UI_HELPERS__;
    H.state.artist = "_creator.beta_";

    // 1. Simulate middle-clicking 15 followers rapidly before any tab loads
    for (let i = 1; i <= 15; i++) {{
      const u = `wave_user_${{i}}`;
      const anchor = {{
        getAttribute: () => `/${{u}}/`,
        closest: () => null
      }};
      H.recordModalFollowerClick({{
        target: {{
          closest: (sel) => (sel.includes('a[href^="/"]') ? anchor : null)
        }}
      }});
    }}
    const all15Remembered = [];
    for (let i = 1; i <= 15; i++) {{
      all15Remembered.push(H.wasRecentlyClickedFromActiveFollowersModal(`wave_user_${{i}}`));
    }}

    // 2. Receive live deltas from Tab 1 (auto-bot overridden to human) and Tab 2 (bot)
    H.applyIncomingLiveDelta({{
      artist: "_creator.beta_",
      username: "wave_user_1",
      label: "human",
      openFollowerTabs: 12,
      source: "mark",
      ts: 1001,
      profile: {{
        schema_version: P.SCHEMA_VERSION,
        artist: "_creator.beta_",
        username: "wave_user_1",
        has_profile_photo: true,
        account_private: false,
        post_count: 0,
        followers_count: 40,
        following_count: 2500,
        bio_length: 0
      }}
    }});
    H.applyIncomingLiveDelta({{
      artist: "_creator.beta_",
      username: "wave_user_2",
      label: "bot",
      openFollowerTabs: 11,
      source: "mark",
      ts: 1002,
      profile: {{
        schema_version: P.SCHEMA_VERSION,
        artist: "_creator.beta_",
        username: "wave_user_2",
        has_profile_photo: false,
        account_private: true,
        post_count: 0,
        followers_count: 2,
        following_count: 1900,
        bio_length: 0
      }}
    }});

    // 3. Inline 1-click confirm on wave_user_3 directly in the Followers modal (without opening tab)
    H.state.profilesMap.set("wave_user_3", {{
      schema_version: P.SCHEMA_VERSION,
      artist: "_creator.beta_",
      username: "wave_user_3",
      has_profile_photo: true,
      account_private: false,
      post_count: 85,
      followers_count: 610,
      following_count: 340,
      bio_length: 44,
      bot_like: false,
      score_complete: true
    }});
    // Simulate duplicate delivery of wave_user_2 mark from background worker ~80ms later (different ts)
    H.applyIncomingLiveDelta({{
      artist: "_creator.beta_",
      username: "wave_user_2",
      label: "bot",
      openFollowerTabs: 11,
      source: "mark",
      ts: 1080
    }});
    // Pre-populate higher total_saved from an earlier snapshot to verify confirmFollowerInline clears stale serverLiveCounts
    H.state.serverLiveCounts = {{
      artist: "_creator.beta_",
      total_saved: 99,
      checked_count: 2,
      bots_count: 1,
      humans_count: 1,
      confirmed_bots: 1,
      auto_bots: 0,
      confirmed_humans: 1,
      auto_humans: 0,
      revision: 1
    }};
    H.confirmFollowerInline("wave_user_3", "human");
    const serverCountsClearedAfterInline = H.state.serverLiveCounts === null;
    // Re-apply a serverLiveCounts where only total_saved is higher (equal checked_count/confirmed): must NOT mask local verdicts
    H.state.serverLiveCounts = {{
      artist: "_creator.beta_",
      total_saved: 99,
      checked_count: 3,
      bots_count: 1,
      humans_count: 2,
      confirmed_bots: 0,
      auto_bots: 1,
      confirmed_humans: 0,
      auto_humans: 2,
      revision: 1
    }};

    const storedLabelsAfter = JSON.parse(storageMap.botaudit_manual_labels_v1 || "{{}}");
    H.updateFloatingDock(true);

    console.log(JSON.stringify({{
      all15Remembered,
      openFollowerTabs: H.state.openFollowerTabs,
      batchConfirmedCount: H.state.batchConfirmedCount,
      serverCountsClearedAfterInline,
      waveHtml: currentHtml,
      w1Label: H.state.profilesMap.get("wave_user_1").manual_label,
      w2Label: H.state.profilesMap.get("wave_user_2").manual_label,
      w3Label: H.state.profilesMap.get("wave_user_3").manual_label,
      preservedEarlierTabLabel: storedLabelsAfter.earlier_other_tab_user ? storedLabelsAfter.earlier_other_tab_user.label : null
    }}));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    res = json.loads(proc.stdout.strip().splitlines()[-1])

    assert len(res["all15Remembered"]) == 15
    assert all(res["all15Remembered"]) is True
    assert res["openFollowerTabs"] == 11
    assert res["batchConfirmedCount"] == 3  # deduplicated across BroadcastChannel + BOTAUDIT_LIVE_DELTA
    assert res["serverCountsClearedAfterInline"] is True
    assert "11" in res["waveHtml"] and "tabs open" in res["waveHtml"]
    assert "✓ 1 confirmed" in res["waveHtml"]  # 1 confirmed bot (wave_user_2)
    assert "✓ 2 confirmed" in res["waveHtml"]  # 2 confirmed humans (wave_user_1 + wave_user_3)
    assert res["w1Label"] == "human"
    assert res["w2Label"] == "bot"
    assert res["w3Label"] == "human"
    assert res["preservedEarlierTabLabel"] == "bot"


def test_three_tier_option_a_invariant_and_extension_hud_suspicious_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify Option A (Bots + Suspicious + Clean == Checked) across all APIs, index.html, and HUD JS."""
    _setup_isolated_env(monkeypatch, tmp_path)

    def _p(uname: str, photo: bool, posts: int, flwrs: int, flwing: int, bio: int) -> dict[str, Any]:
        return {
            "schema_version": 3,
            "artist": "tier_artist",
            "username": uname,
            "has_profile_photo": photo,
            "account_private": False,
            "post_count": posts,
            "followers_count": flwrs,
            "following_count": flwing,
            "bio_length": bio,
            "post_timestamps": [],
        }

    profiles = [
        _p("auto_bot_clear", False, 0, 5, 2200, 0),       # >60p auto bot -> bot
        _p("manual_dressed_bot", True, 0, 120, 90, 0),    # 35p unmarked, then manual bot -> bot
        _p("gray_zone_lurker", True, 0, 120, 90, 0),      # 35p unmarked -> suspicious
        _p("manual_real_override", False, 0, 5, 2200, 0), # >60p auto, overridden human -> clean
        _p("clean_normal_fan", True, 25, 450, 320, 42),   # 0p -> clean
    ]

    for uname, lbl in (("manual_dressed_bot", "bot"), ("manual_real_override", "human")):
        l_code, _, _ = _call_wsgi(
            "POST",
            "/api/labels",
            json_body={"username": uname, "label": lbl, "artist": "tier_artist"},
        )
        assert l_code == 200

    imp_code, _, imp_body = _call_wsgi(
        "POST",
        "/api/extension-import",
        json_body={"artist": "tier_artist", "include_control": False, "profiles": profiles},
    )
    assert imp_code == 200
    imp = json.loads(imp_body.decode("utf-8"))
    art = imp["artists"][0]
    assert art["effective_bot_count"] == 2
    assert art["suspicious_count"] == 1
    assert art["clean_count"] == 2
    assert art["effective_bot_count"] + art["suspicious_count"] + art["clean_count"] == art["sample_size"] == 5

    by_user = {p["username"]: p for p in imp["profiles"]}
    assert by_user["auto_bot_clear"]["verdict_tier"] == "bot"
    assert by_user["manual_dressed_bot"]["verdict_tier"] == "bot"
    assert by_user["gray_zone_lurker"]["verdict_tier"] == "suspicious"
    assert by_user["manual_real_override"]["verdict_tier"] == "clean"
    assert by_user["clean_normal_fan"]["verdict_tier"] == "clean"

    cp_code, _, cp_body = _call_wsgi(
        "POST",
        "/api/checkpoint",
        json_body={"artist": "tier_artist", "profiles": profiles},
    )
    assert cp_code == 200
    lc = json.loads(cp_body.decode("utf-8"))["live_counts"]
    assert lc["bots_count"] == 2
    assert lc["suspicious_count"] == 1
    assert lc["humans_count"] == 2
    assert lc["bots_count"] + lc["suspicious_count"] + lc["humans_count"] == lc["checked_count"] == 5

    live_code, _, live_body = _call_wsgi("GET", "/api/live-status")
    assert live_code == 200
    assert "worker_count" in json.loads(live_body.decode("utf-8"))

    hist_code, _, hist_body = _call_wsgi("GET", "/api/history")
    assert hist_code == 200
    hist = {h["artist"]: h for h in json.loads(hist_body.decode("utf-8"))["history"]}["tier_artist"]
    assert hist["effective_bot_count"] == 2
    assert hist["suspicious_count"] == 1
    assert hist["real_count"] == 2

    net_code, _, net_body = _call_wsgi("GET", "/api/bot-network")
    assert net_code == 200
    net_acc = {a["artist"]: a for a in json.loads(net_body.decode("utf-8"))["accounts"]}["tier_artist"]
    assert net_acc["suspicious_count"] == 1
    assert net_acc["effective_bot_count"] == 2
    assert net_acc["real_count"] == 2
    assert (
        net_acc["effective_bot_count"] + net_acc["suspicious_count"] + net_acc["real_count"]
        == net_acc["deep_checked"]
        == 5
    )

    # UI tokens in index.html & Option D 6-card Professor Mode guide
    html = INDEX_HTML.read_text(encoding="utf-8")
    for token in (
        'id="pillSuspicious"',
        "function getProfileVerdictTier(",
        "SUSPICIOUS",
        "guide5Tag",
        "guide6Tag",
        "97.7%",
        "97.6%",
        "1,417",
        'id="siteFooter"',
        "Made with love by",
        "https://freestate.ro",
    ):
        assert token in html

    # Extension HUD computeDisplayCounts & classifyProfileVerdict 3-tier check under Node
    if subprocess.run(["node", "--version"], capture_output=True, check=False).returncode == 0:
        parser_js = REPO_ROOT / "browser-extension" / "parser.js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const noop = () => {{}};
        const dummyEl = () => ({{
          id: "", className: "", style: {{}}, dataset: {{}}, innerHTML: "", textContent: "",
          children: [], querySelectorAll: () => [], querySelector: () => null,
          addEventListener: noop, appendChild: noop, remove: noop, closest: () => null,
          getAttribute: () => "", getBoundingClientRect: () => ({{ left: 0, top: 0, width: 320, height: 200 }})
        }});
        const sandbox = {{
          console, Date, Math, JSON, Number, String, Boolean, Array, Object, Set, Map, RegExp, Error, Promise,
          setTimeout, clearTimeout, setInterval: () => 1, clearInterval: noop,
          localStorage: {{ _d: {{}}, getItem(k) {{ return this._d[k] || null; }}, setItem(k, v) {{ this._d[k] = String(v); }}, removeItem(k) {{ delete this._d[k]; }} }},
          document: {{ hidden: true, activeElement: null, body: Object.assign(dummyEl(), {{ innerText: "" }}), head: dummyEl(), documentElement: dummyEl(), getElementById: () => null, createElement: () => dummyEl(), querySelector: () => null, querySelectorAll: () => [], addEventListener: noop }},
          location: {{ pathname: "/tier_artist/followers/" }},
          addEventListener: noop,
          fetch: () => Promise.reject(new Error("offline")),
        }};
        sandbox.window = sandbox;
        sandbox.globalThis = sandbox;
        vm.createContext(sandbox);
        vm.runInContext(fs.readFileSync({json.dumps(str(parser_js))}, "utf8"), sandbox);
        vm.runInContext(fs.readFileSync({json.dumps(str(CONTENT_JS))}, "utf8"), sandbox);
        const P = sandbox.BotAuditParser;
        const H = sandbox.__BOT_AUDIT_UI_HELPERS__;
        const rows = {json.dumps(profiles)};
        rows.forEach((r) => {{
          if (r.username === "manual_dressed_bot") r.manual_label = "bot";
          if (r.username === "manual_real_override") r.manual_label = "human";
          H.state.profilesMap.set(r.username, Object.assign({{}}, r, {{
            bot_like: P.scoreFollowerProfile(r).botLike,
            score_pts: P.scoreFollowerProfile(r).scorePts,
            max_score_pts: P.scoreFollowerProfile(r).maxScorePts,
            score_complete: P.scoreFollowerProfile(r).complete,
            tier: P.profileTier(r)
          }}));
        }});
        const grayVerdict = H.classifyProfileVerdict(H.state.profilesMap.get("gray_zone_lurker"));
        const disp = H.computeDisplayCounts();
        console.log(JSON.stringify({{ grayVerdict, disp }}));
        """
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
        out_js = json.loads(proc.stdout.strip().splitlines()[-1])
        assert out_js["grayVerdict"]["verdict"] == "suspicious"
        assert out_js["grayVerdict"]["decided"] is True
        assert out_js["disp"]["scoredBotsCount"] == 2
        assert out_js["disp"]["suspiciousCount"] == 1
        assert out_js["disp"]["scoredHumansCount"] == 2
        assert (
            out_js["disp"]["scoredBotsCount"]
            + out_js["disp"]["suspiciousCount"]
            + out_js["disp"]["scoredHumansCount"]
            == out_js["disp"]["checkedCount"]
            == 5
        )


def test_inline_confirm_buttons_appear_on_unsure_follower_rows() -> None:
    """Unsure follower rows get inline Bot/Human buttons in the modal and convert cleanly when clicked."""
    if subprocess.run(["node", "--version"], capture_output=True, check=False).returncode != 0:
        pytest.skip("Node.js is not installed")

    parser_js = REPO_ROOT / "browser-extension" / "parser.js"
    script = f"""
    const fs = require("fs");
    const vm = require("vm");
    const noop = () => {{}};

    function makeEl(tag, attrs, text) {{
      const classes = new Set();
      const el = {{
        tagName: String(tag || "div").toUpperCase(),
        attributes: Object.assign({{}}, attrs || {{}}),
        children: [],
        parentElement: null,
        style: {{}},
        dataset: {{}},
        textContent: text || "",
        _innerHTML: "",
        get className() {{
          return Array.from(classes).join(" ");
        }},
        set className(val) {{
          classes.clear();
          String(val || "")
            .split(/\\s+/)
            .filter(Boolean)
            .forEach((c) => classes.add(c));
        }},
        classList: {{
          add: (...cs) => cs.forEach((c) => classes.add(c)),
          remove: (...cs) => cs.forEach((c) => classes.delete(c)),
          contains: (c) => classes.has(c)
        }},
        get innerHTML() {{
          return this._innerHTML;
        }},
        set innerHTML(val) {{
          this._innerHTML = String(val || "");
        }},
        getAttribute(k) {{
          return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : "";
        }},
        setAttribute(k, v) {{
          this.attributes[k] = String(v);
        }},
        appendChild(child) {{
          child.parentElement = this;
          this.children.push(child);
          return child;
        }},
        insertAdjacentElement(pos, node) {{
          if (pos === "afterend" && this.parentElement) {{
            const siblings = this.parentElement.children;
            const idx = siblings.indexOf(this);
            node.parentElement = this.parentElement;
            siblings.splice(idx + 1, 0, node);
            return node;
          }}
          return this.appendChild(node);
        }},
        remove() {{
          if (!this.parentElement) return;
          const siblings = this.parentElement.children;
          const idx = siblings.indexOf(this);
          if (idx !== -1) siblings.splice(idx, 1);
          this.parentElement = null;
        }},
        get nextElementSibling() {{
          if (!this.parentElement) return null;
          const siblings = this.parentElement.children;
          const idx = siblings.indexOf(this);
          return idx >= 0 && idx + 1 < siblings.length ? siblings[idx + 1] : null;
        }},
        querySelectorAll(sel) {{
          const found = [];
          const walk = (n) => {{
            for (const c of n.children || []) {{
              if (sel === 'a[href^="/"][href$="/"]' && c.tagName === "A") {{
                const h = c.getAttribute("href");
                if (h.startsWith("/") && h.endsWith("/")) found.push(c);
              }} else if (sel === "button:not(.botaudit-row-act)" && c.tagName === "BUTTON" && !c.classList.contains("botaudit-row-act")) {{
                found.push(c);
              }}
              walk(c);
            }}
          }};
          walk(this);
          return found;
        }},
        querySelector(sel) {{
          return this.querySelectorAll(sel)[0] || null;
        }},
        addEventListener: noop,
        closest: () => null,
        getBoundingClientRect: () => ({{ left: 0, top: 0, width: 320, height: 200 }})
      }};
      return el;
    }}

    const dialog = makeEl("div", {{ role: "dialog" }}, "Followers");
    const users = ["miriam_148", "zalan_af", "heyday.photo"];
    const rowByUser = {{}};
    const anchorByUser = {{}};
    users.forEach((u) => {{
      const row = makeEl("div");
      const anchor = makeEl("a", {{ href: `/${{u}}/` }}, u);
      const followBtn = makeEl("button", {{}}, "Follow");
      row.appendChild(anchor);
      row.appendChild(followBtn);
      dialog.appendChild(row);
      rowByUser[u] = row;
      anchorByUser[u] = anchor;
    }});

    const sandbox = {{
      console, Date, Math, JSON, Number, String, Boolean, Array, Object, Set, Map, RegExp, Error, Promise,
      setTimeout, clearTimeout, setInterval: () => 1, clearInterval: noop,
      localStorage: {{ _d: {{}}, getItem(k) {{ return this._d[k] || null; }}, setItem(k, v) {{ this._d[k] = String(v); }}, removeItem(k) {{ delete this._d[k]; }} }},
      document: {{
        hidden: true,
        activeElement: null,
        body: makeEl("body"),
        head: makeEl("head"),
        documentElement: makeEl("html"),
        getElementById: () => null,
        createElement: (t) => makeEl(t),
        querySelector: (sel) => (sel === 'div[role="dialog"]' ? dialog : null),
        querySelectorAll: (sel) => (sel === 'div[role="dialog"]' ? [dialog] : []),
        addEventListener: noop
      }},
      location: {{ pathname: "/target_account/followers/" }},
      addEventListener: noop,
      fetch: () => Promise.reject(new Error("offline")),
    }};
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync({json.dumps(str(parser_js))}, "utf8"), sandbox);
    vm.runInContext(fs.readFileSync({json.dumps(str(CONTENT_JS))}, "utf8"), sandbox);

    const P = sandbox.BotAuditParser;
    const H = sandbox.__BOT_AUDIT_UI_HELPERS__;

    // Even an unsure profile that has only list/handle stats (no header counts yet) must get inline Bot/Human buttons
    const seedProfiles = [
      {{ username: "miriam_148", has_profile_photo: true, account_private: null, post_count: null, followers_count: null, following_count: null, bio_length: null, manual_label: "unsure" }},
      {{ username: "zalan_af", has_profile_photo: true, account_private: false, post_count: 4, followers_count: 180, following_count: 190, bio_length: 12, manual_label: "bot" }},
      {{ username: "heyday.photo", has_profile_photo: true, account_private: false, post_count: 42, followers_count: 680, following_count: 410, bio_length: 58, manual_label: "human" }}
    ];
    seedProfiles.forEach((p) => {{
      const s = P.scoreFollowerProfile(p);
      H.state.profilesMap.set(p.username, Object.assign({{}}, p, {{
        schema_version: P.SCHEMA_VERSION,
        tier: P.profileTier(p),
        bot_like: s.botLike,
        score_pts: s.scorePts,
        max_score_pts: s.maxScorePts,
        score_conclusive: s.conclusive,
        score_complete: s.complete
      }}));
      H.state.manualLabelsMap.set(p.username.toLowerCase(), {{ username: p.username.toLowerCase(), label: p.manual_label }});
    }});

    H.decorateFollowerRowsInDom();
    const snapshot = (u) => ({{
      badgeText: anchorByUser[u].nextElementSibling ? anchorByUser[u].nextElementSibling.textContent : "",
      hasActions: Boolean(
        anchorByUser[u].nextElementSibling &&
        anchorByUser[u].nextElementSibling.nextElementSibling &&
        anchorByUser[u].nextElementSibling.nextElementSibling.classList.contains("botaudit-row-actions")
      ),
      actionsHtml:
        anchorByUser[u].nextElementSibling &&
        anchorByUser[u].nextElementSibling.nextElementSibling &&
        anchorByUser[u].nextElementSibling.nextElementSibling.classList.contains("botaudit-row-actions")
          ? anchorByUser[u].nextElementSibling.nextElementSibling.innerHTML
          : "",
      confirmedClass: rowByUser[u].classList.contains("botaudit-row-confirmed")
    }});
    const beforeClick = {{
      unsure: snapshot("miriam_148"),
      bot: snapshot("zalan_af"),
      human: snapshot("heyday.photo")
    }};

    H.confirmFollowerInline("miriam_148", "human");
    const afterClick = snapshot("miriam_148");
    console.log(JSON.stringify({{ beforeClick, afterClick }}));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    res = json.loads(proc.stdout.strip().splitlines()[-1])

    unsure_before = res["beforeClick"]["unsure"]
    assert "UNSURE" in unsure_before["badgeText"]
    assert unsure_before["hasActions"] is True
    assert 'data-ba-act="bot"' in unsure_before["actionsHtml"]
    assert 'data-ba-act="human"' in unsure_before["actionsHtml"]
    assert unsure_before["confirmedClass"] is False

    assert res["beforeClick"]["bot"]["hasActions"] is False
    assert res["beforeClick"]["bot"]["confirmedClass"] is True
    assert res["beforeClick"]["human"]["hasActions"] is False
    assert res["beforeClick"]["human"]["confirmedClass"] is True

    after = res["afterClick"]
    assert "REAL PERSON" in after["badgeText"]
    assert after["hasActions"] is False
    assert after["confirmedClass"] is True


def test_target_account_switcher_switches_active_artist_and_prevents_snapback() -> None:
    """HUD/Popup target switcher switches from previous artist (e.g. target_prev) to freestate.ai without snapback."""
    if subprocess.run(["node", "--version"], capture_output=True, check=False).returncode != 0:
        pytest.skip("Node.js is not installed")

    parser_js = REPO_ROOT / "browser-extension" / "parser.js"
    bg_txt = (REPO_ROOT / "browser-extension" / "background.js").read_text(encoding="utf-8")
    popup_html_txt = (REPO_ROOT / "browser-extension" / "popup.html").read_text(encoding="utf-8")
    popup_js_txt = (REPO_ROOT / "browser-extension" / "popup.js").read_text(encoding="utf-8")
    assert "GET_HISTORY" in bg_txt
    assert 'id="target-select"' in popup_html_txt and 'id="btn-audit-current"' in popup_html_txt
    assert "setTargetArtist" in popup_js_txt

    script = f"""
    const fs = require("fs");
    const vm = require("vm");
    const noop = () => {{}};
    const makeMockEl = () => ({{
      id: "", className: "", style: {{}}, dataset: {{}}, innerHTML: "", textContent: "",
      children: [], querySelectorAll: () => [], querySelector: () => null,
      addEventListener: noop, appendChild: noop, remove: noop, closest: () => null,
      classList: {{ add: noop, remove: noop, contains: () => false }},
      getAttribute: () => "", getBoundingClientRect: () => ({{ left: 0, top: 0, width: 320, height: 200 }})
    }});
    const mockDock = makeMockEl();
    mockDock.id = "botaudit-floating-dock";

    const store = {{
      botaudit_active_artist_v3: "target_prev",
      botaudit_recent_clicks_v2: JSON.stringify({{
        items: [{{ f: "freestate.ai", a: "target_prev", ts: Date.now() }}]
      }}),
      "botaudit_artist_v4_freestate.ai": JSON.stringify({{
        artist: "freestate.ai",
        profiles: [
          {{ username: "f1", bio_length: 10, score_conclusive: true }},
          {{ username: "f2", bio_length: 10, score_conclusive: true }},
          {{ username: "f3", bio_length: 10, score_conclusive: true }},
          {{ username: "f4", bio_length: 10, score_conclusive: true }}
        ]
      }})
    }};
    const storageApi = {{
      get length() {{ return Object.keys(store).length; }},
      key(i) {{ return Object.keys(store)[i] || null; }},
      getItem(k) {{ return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; }},
      setItem(k, v) {{ store[k] = String(v); }},
      removeItem(k) {{ delete store[k]; }}
    }};

    const sandbox = {{
      console, Date, Math, JSON, Number, String, Boolean, Array, Object, Set, Map, RegExp, Error, Promise,
      setTimeout, clearTimeout, setInterval: () => 1, clearInterval: noop,
      localStorage: storageApi,
      document: {{
        hidden: true, activeElement: null,
        body: Object.assign(makeMockEl(), {{ innerText: "" }}),
        head: makeMockEl(), documentElement: makeMockEl(),
        getElementById: (id) => (id === "botaudit-floating-dock" ? mockDock : null),
        createElement: () => makeMockEl(),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: noop
      }},
      location: {{ pathname: "/freestate.ai/" }},
      addEventListener: noop,
      fetch: () => Promise.reject(new Error("offline")),
    }};
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync({json.dumps(str(parser_js))}, "utf8"), sandbox);
    vm.runInContext(fs.readFileSync({json.dumps(str(CONTENT_JS))}, "utf8"), sandbox);

    const H = sandbox.__BOT_AUDIT_UI_HELPERS__;
    H.state.inspectedPageProfile = {{ username: "freestate.ai", bio_length: 148 }};
    H.updateFloatingDock(true);
    const beforeHtml = mockDock.innerHTML;
    const beforeArtist = H.state.artist;

    H.setActiveTargetArtist("freestate.ai");
    const afterFreestateArtist = H.state.artist;
    const afterFreestateDetect = H.detectCurrentArtist();
    const afterFreestateStored = store.botaudit_active_artist_v3;
    const afterFreestateInspected = H.state.inspectedPageProfile;
    const afterFreestateHtml = mockDock.innerHTML;

    H.setActiveTargetArtist("target_alpha");
    const afterTargetAlphaArtist = H.state.artist;
    const afterTargetAlphaDetect = H.detectCurrentArtist();
    const afterTargetAlphaHtml = mockDock.innerHTML;

    H.state.targetPickerOpen = true;
    H.updateFloatingDock(true);
    const pickerHtml = mockDock.innerHTML;
    const merged = H.getMergedKnownAccounts().map((a) => a.artist);

    console.log(JSON.stringify({{
      beforeArtist,
      beforeHtml,
      afterFreestateArtist,
      afterFreestateDetect,
      afterFreestateStored,
      afterFreestateInspected,
      afterFreestateHtml,
      afterTargetAlphaArtist,
      afterTargetAlphaDetect,
      afterTargetAlphaHtml,
      pickerHtml,
      merged
    }}));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    res = json.loads(proc.stdout.strip().splitlines()[-1])

    assert res["beforeArtist"] == "target_prev"
    assert 'id="botaudit-artist-btn"' in res["beforeHtml"]
    assert "@target_prev" in res["beforeHtml"]
    assert 'id="botaudit-switch-page-btn"' in res["beforeHtml"]
    assert 'data-target-artist="freestate.ai"' in res["beforeHtml"]

    assert res["afterFreestateArtist"] == "freestate.ai"
    assert res["afterFreestateDetect"] == "freestate.ai"
    assert res["afterFreestateStored"] == "freestate.ai"
    assert res["afterFreestateInspected"] is None
    assert "@freestate.ai" in res["afterFreestateHtml"]
    assert 'id="botaudit-switch-page-btn"' not in res["afterFreestateHtml"]

    # Manual target lock prevents snapping back to freestate.ai even when freestate.ai has >3 local profiles
    assert res["afterTargetAlphaArtist"] == "target_alpha"
    assert res["afterTargetAlphaDetect"] == "target_alpha"
    assert 'id="botaudit-switch-page-btn"' in res["afterTargetAlphaHtml"]

    assert 'id="botaudit-target-picker"' in res["pickerHtml"]
    assert 'id="botaudit-target-input"' in res["pickerHtml"]
    assert 'id="botaudit-target-go-btn"' in res["pickerHtml"]
    assert "target_alpha" in res["merged"] and "freestate.ai" in res["merged"]






