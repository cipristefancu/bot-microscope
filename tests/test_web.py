"""Tests for the interactive Web UI backend and WSGI API (`src/botaudit/web.py`)."""

from __future__ import annotations

import io
import json
from typing import Any
from wsgiref.util import setup_testing_defaults

import pytest

from pathlib import Path

from botaudit.loader import CSVValidationError
from botaudit.web import (
    analyze_handle_forensics,
    compute_digit_ratio,
    create_wsgi_app,
    extract_username,
    load_preset_Audit,
    normalize_csv_text,
    run_audit_from_csv_text,
)


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.instagram.com/zebra.9605848/", "zebra.9605848"),
        ("https://instagram.com/ciprianciutreofficial?igsh=abc", "ciprianciutreofficial"),
        ("@musicfan_99", "musicfan_99"),
        ("  wired  ", "wired"),
        ("", ""),
    ],
)
def test_extract_username(raw: str, expected: str) -> None:
    """Clean handles are extracted from Instagram URLs and @mentions."""
    assert extract_username(raw) == expected


def test_analyze_handle_forensics_detects_farm_patterns() -> None:
    """Farm handles like zebra.9605848 trigger word.digits and digit-heavy flags."""
    farm = analyze_handle_forensics("zebra.9605848", digit_threshold=0.40)
    assert farm["is_digit_heavy"] is True
    assert farm["is_word_dot_digits"] is True
    assert farm["has_long_digit_run"] is True
    assert farm["digit_ratio"] == pytest.approx(7 / 13, abs=1e-4)

    human = analyze_handle_forensics("ciprianciutreofficial", digit_threshold=0.40)
    assert human["is_digit_heavy"] is False
    assert human["is_word_dot_digits"] is False
    assert human["has_long_digit_run"] is False
    assert human["digit_ratio"] == 0.0


def test_normalize_csv_computes_only_digit_ratio_never_invents_counts() -> None:
    """normalize_csv_text computes username_digit_ratio from username, but never invents counts."""
    complete_missing_ratio = (
        "artist,username,has_profile_photo,post_count,followers_count,"
        "following_count,bio_length,account_private,username_digit_ratio\n"
        "Target_Bots,mari0.256,false,0,10,2000,0,false,\n"
    )
    normalized = normalize_csv_text(complete_missing_ratio)
    result = run_audit_from_csv_text(normalized)
    assert result["total_rows_loaded"] == 1
    assert result["profiles"][0]["username"] == "mari0.256"
    assert result["profiles"][0]["username_digit_ratio"] == compute_digit_ratio("mari0.256")

    # Partial rows with blank counts are rejected (never auto-filled with fake defaults)
    partial = (
        "artist,username,has_profile_photo,post_count,followers_count,"
        "following_count,bio_length,account_private,username_digit_ratio\n"
        "Target_Bots,mari0.256,,,,,,,\n"
    )
    partial_res = run_audit_from_csv_text(partial)
    assert partial_res["total_rows_loaded"] == 0
    assert partial_res["total_rows_rejected"] == 1
    assert partial_res["profiles"] == []


def test_calibration_preset_is_permanently_removed() -> None:
    """Synthetic calibration preset is permanently removed and raises CSVValidationError."""
    with pytest.raises(CSVValidationError, match="permanently removed"):
        load_preset_Audit("calibration")


@pytest.mark.skipif(
    not Path("data/sample/verified_benchmarks.csv").is_file(),
    reason="Private local benchmark CSV is excluded from public clones",
)
def test_preset_verified_benchmarks_uses_real_observed_data() -> None:
    """The verified_benchmarks preset runs on real observed metrics without synthetic warnings."""
    result = load_preset_Audit("verified_benchmarks")
    assert not any("SYNTHETIC DEMO" in w for w in result["warnings"])
    artists = {a["artist"]: a for a in result["artists"]}
    assert "Target_Bots" in artists
    assert "Control_Users" in artists
    assert artists["Target_Bots"]["sample_size"] > 0
    assert artists["Control_Users"]["sample_size"] > 0


def test_wsgi_serves_index_html() -> None:
    """GET / serves index.html with 200 OK and both English & Romanian (EN | RO) support."""
    code, headers, body = _call_wsgi("GET", "/")
    assert code == 200
    assert "text/html" in headers["content-type"]
    text = body.decode("utf-8")
    assert "Bot Microscope" in text
    assert (
        "See what percentage of an Instagram audience is real people vs. fake bot accounts"
        in text
    )
    assert "setLanguage('ro')" in text
    assert "Audit de Audiență Instagram" in text
    assert (
        "Află ce procent din urmăritorii unui cont de Instagram sunt oameni reali vs. boți falși"
        in text
    )


def test_wsgi_api_config_and_audit_endpoints() -> None:
    """/api/config and /api/audit return valid JSON responses."""
    code, _, body = _call_wsgi("GET", "/api/config")
    assert code == 200
    cfg_data = json.loads(body.decode("utf-8"))
    assert "config" in cfg_data
    assert cfg_data["config"]["bot_like_cutoff"] == 0.60

    # Audit via POST /api/audit with complete 9-column CSV
    csv_payload = (
        "artist,username,has_profile_photo,post_count,followers_count,"
        "following_count,bio_length,account_private,username_digit_ratio\n"
        "Test_Cohort,zebra.9605848,false,0,10,2000,0,false,0.5385\n"
        "Test_Cohort,wired,true,500,10000,200,80,false,0.0000\n"
    )
    code, _, body = _call_wsgi(
        "POST",
        "/api/audit",
        json_body={
            "mode": "csv",
            "csv_text": csv_payload,
        },
    )
    assert code == 200
    audit_data = json.loads(body.decode("utf-8"))
    assert audit_data["total_rows_loaded"] == 2
    assert len(audit_data["artists"]) == 1
    assert audit_data["chart_base64"]
    assert audit_data["chart_base64_ro"]
    assert "bot-like audience" in audit_data["report_html"].lower()
    assert "raport de audit al calității audienței" in audit_data["report_html_ro"].lower()
    assert "clasamentul calității audienței" in audit_data["report_html_ro"].lower()


def test_same_day_post_burst_scores_dressed_up_bot_at_70_pts() -> None:
    """Dressed-up bot with photo + 4 same-day setup posts scores 0.70 (70 pts > 0.60 cutoff)."""
    csv_text = (
        "artist,username,has_profile_photo,post_count,followers_count,"
        "following_count,bio_length,account_private,username_digit_ratio\n"
        "Target_Artist,zebra.9605848,true,4,18,1950,0,false,0.5385\n"
        "Target_Artist,real_quiet_user,true,0,180,220,0,false,0.0000\n"
    )
    result = run_audit_from_csv_text(csv_text)
    profiles = {p["username"]: p for p in result["profiles"]}

    dressed_bot = profiles["zebra.9605848"]
    assert dressed_bot["flags"]["no_photo"] is False
    assert dressed_bot["flags"]["zero_posts"] is False
    assert dressed_bot["flags"]["same_day_post_burst"] is True
    assert dressed_bot["flags"]["mass_following"] is True
    assert dressed_bot["flags"]["extreme_mass_following"] is True
    assert dressed_bot["flags"]["low_audience_imbalance"] is True
    assert dressed_bot["flags"]["shared_post_bot_signal"] is True
    assert dressed_bot["flags"]["empty_bio"] is True
    assert dressed_bot["flags"]["digit_heavy_username"] is True
    assert dressed_bot["bot_score"] == pytest.approx(1.0)
    assert dressed_bot["bot_like"] is True

    quiet_human = profiles["real_quiet_user"]
    assert quiet_human["bot_score"] == pytest.approx(0.35)
    assert quiet_human["bot_like"] is False


def test_private_follower_bot_is_scored_and_caught() -> None:
    """Private follower bots are scored on their public counts and caught above 0.60."""
    csv_text = (
        "artist,username,has_profile_photo,post_count,followers_count,"
        "following_count,bio_length,account_private,username_digit_ratio\n"
        "Target_Artist,cheetah.26971659,true,3,12,2100,0,true,0.5000\n"
        "Target_Artist,normal_private_fan,true,18,340,290,35,true,0.0000\n"
    )
    result = run_audit_from_csv_text(csv_text)
    profiles = {p["username"]: p for p in result["profiles"]}

    priv_bot = profiles["cheetah.26971659"]
    assert priv_bot["account_private"] is True
    assert priv_bot["flags"]["same_day_post_burst"] is True
    assert priv_bot["flags"]["mass_following"] is True
    assert priv_bot["flags"]["empty_bio"] is True
    assert priv_bot["flags"]["digit_heavy_username"] is True
    assert priv_bot["bot_score"] == pytest.approx(1.0)
    assert priv_bot["bot_like"] is True

    priv_human = profiles["normal_private_fan"]
    assert priv_human["account_private"] is True
    assert priv_human["bot_score"] == pytest.approx(0.0)
    assert priv_human["bot_like"] is False


def test_wsgi_extension_import_and_har_json_mode() -> None:
    """WSGI /api/extension-import and /api/audit (mode='har_json') parse timestamps and profiles."""
    ext_payload = {
        "artist": "Test_Artist",
        "include_control": False,
        "rate_limit_status": {"zone": "green", "checksLastHour": 14},
        "profiles": [
            {
                "username": "zebra.9605848",
                "has_profile_photo": True,
                "post_count": 4,
                "followers_count": 15,
                "following_count": 1900,
                "bio_length": 0,
                "account_private": True,
                "post_timestamps": [1710000000, 1710000200, 1710000400, 1710000800],
            },
            {
                "username": "ciprianciutreofficial",
                "has_profile_photo": True,
                "post_count": 42,
                "followers_count": 820,
                "following_count": 410,
                "bio_length": 55,
                "account_private": False,
                "post_timestamps": [1680000000, 1690000000, 1700000000, 1710000000],
            },
        ],
    }

    code, _, body = _call_wsgi("POST", "/api/extension-import", json_body=ext_payload)
    assert code == 200
    data = json.loads(body.decode("utf-8"))
    assert data["source"] == "extension"
    assert data["total_rows_loaded"] == 2

    code_get, _, body_get = _call_wsgi("GET", "/api/extension-import")
    assert code_get == 200
    fetched = json.loads(body_get.decode("utf-8"))
    assert fetched["total_rows_loaded"] == 2

    # Also test .har archive format via /api/audit mode="har_json"
    har_archive = {
        "log": {
            "entries": [
                {
                    "response": {
                        "content": {
                            "text": json.dumps(
                                {
                                    "data": {
                                        "user": {
                                            "username": "iguana.8470829",
                                            "is_private": True,
                                            "has_anonymous_profile_picture": False,
                                            "biography": "",
                                            "edge_followed_by": {"count": 11},
                                            "edge_follow": {"count": 2050},
                                            "edge_owner_to_timeline_media": {
                                                "count": 3,
                                                "edges": [
                                                    {"node": {"taken_at_timestamp": 1715000000}},
                                                    {"node": {"taken_at_timestamp": 1715000300}},
                                                    {"node": {"taken_at_timestamp": 1715000600}},
                                                ],
                                            },
                                        }
                                    }
                                }
                            )
                        }
                    }
                }
            ]
        }
    }
    code_har, _, body_har = _call_wsgi(
        "POST",
        "/api/audit",
        json_body={
            "mode": "har_json",
            "artist": "Har_Artist",
            "include_control": False,
            "raw_text": json.dumps(har_archive),
        },
    )
    assert code_har == 200
    har_res = json.loads(body_har.decode("utf-8"))
    assert har_res["total_rows_loaded"] == 1
    prof = har_res["profiles"][0]
    assert prof["username"] == "iguana.8470829"
    assert prof["account_private"] is True
    assert prof["flags"]["same_day_post_burst"] is True
    assert prof["bot_score"] == pytest.approx(1.0)
    assert prof["bot_like"] is True


def test_multi_day_artist_checkpoint_merges_without_duplicates() -> None:
    """/api/checkpoint saves Day 1 + Day 2 batches per artist without re-collecting duplicates."""
    artist = "test_multiday_artist"
    _call_wsgi("POST", "/api/checkpoint", json_body={"artist": artist, "clear": True})

    # Day 1: Collect 2 accounts (slice "a")
    code1, _, body1 = _call_wsgi(
        "POST",
        "/api/checkpoint",
        json_body={
            "artist": artist,
            "completed_slices": ["a"],
            "last_username": "cheetah.26971659",
            "profiles": [
                {"username": "zebra.9605848", "post_count": 0},
                {"username": "cheetah.26971659", "post_count": 4},
            ],
        },
    )
    assert code1 == 200
    res1 = json.loads(body1.decode("utf-8"))
    assert res1["total_saved"] == 2
    assert res1["newly_added"] == 2

    # Day 2: Resume with 1 overlap ("cheetah.26971659") + 1 brand-new ("iguana.8470829", slice "b")
    code2, _, body2 = _call_wsgi(
        "POST",
        "/api/checkpoint",
        json_body={
            "artist": artist,
            "completed_slices": ["b"],
            "last_username": "iguana.8470829",
            "profiles": [
                {"username": "cheetah.26971659", "post_count": 4},
                {"username": "iguana.8470829", "post_count": 3},
            ],
        },
    )
    assert code2 == 200
    res2 = json.loads(body2.decode("utf-8"))
    assert res2["total_saved"] == 3
    assert res2["newly_added"] == 1
    assert res2["completed_slices"] == ["a", "b"]
    assert res2["last_username"] == "iguana.8470829"

    # Clean up test checkpoint
    _call_wsgi("POST", "/api/checkpoint", json_body={"artist": artist, "clear": True})


def test_stitched_handles_and_mass_follow_inflation_bots() -> None:
    """Catch _queen._s_7 (0 posts, 106 flw, 3036 following, emoji bio) and _.little_qf.y."""
    hf_queen = analyze_handle_forensics("_queen._s_7")
    assert hf_queen["is_stitched_handle"] is True

    hf_little = analyze_handle_forensics("_.little_qf.y")
    assert hf_little["is_stitched_handle"] is True

    # _queen._s_7 has every number observed; _.little_qf.y carries the placeholder numbers an
    # older build invented, which must be treated as unknown and left out of the scoring.
    code, _, body = _call_wsgi(
        "POST",
        "/api/extension-import",
        json_body={
            "artist": "Test_Inflation_Bots",
            "include_control": False,
            "profiles": [
                {
                    "username": "_queen._s_7",
                    "has_profile_photo": True,
                    "post_count": 0,
                    "followers_count": 106,
                    "following_count": 3036,
                    "bio_length": 2,
                    "account_private": True,
                    "deep_checked": True,
                },
                {
                    "username": "_.little_qf.y",
                    "has_profile_photo": True,
                    "post_count": 22,
                    "followers_count": 320,
                    "following_count": 280,
                    "bio_length": 40,
                    "account_private": False,
                    "deep_checked": False,
                },
            ],
        },
    )
    assert code == 200
    data = json.loads(body.decode("utf-8"))
    by_user = {p["username"]: p for p in data["profiles"]}

    queen = by_user["_queen._s_7"]
    assert queen["flags"]["zero_posts"] is True
    assert queen["flags"]["mass_following"] is True
    assert queen["flags"]["empty_bio"] is True
    assert queen["flags"]["digit_heavy_username"] is False
    assert queen["flags"]["stitched_username"] is True
    assert queen["flags"]["ratio_cluster_capped"] is False
    assert queen["ratio_cluster_pts"] == 55
    assert queen["bot_score"] == pytest.approx(1.0)
    assert queen["bot_like"] is True

    assert "_.little_qf.y" not in by_user
    coverage = data["coverage"]["Test_Inflation_Bots"]
    assert coverage == {"scanned": 2, "complete": 1, "incomplete": 1}
    assert any("NOT scored" in w for w in data["warnings"])


def _full_profile(username: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_version": 3,
        "username": username,
        "has_profile_photo": True,
        "account_private": False,
        "post_count": 42,
        "followers_count": 680,
        "following_count": 410,
        "bio_length": 58,
        "post_timestamps": [],
    }
    row.update(overrides)
    return row


def _handle_only(username: str) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "username": username,
        "has_profile_photo": None,
        "account_private": None,
        "post_count": None,
        "followers_count": None,
        "following_count": None,
        "bio_length": None,
        "post_timestamps": [],
    }


def _post_extension_batch(profiles: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    code, _, body = _call_wsgi(
        "POST",
        "/api/extension-import",
        json_body={"artist": "Honest_Artist", "include_control": True, "profiles": profiles},
    )
    return code, json.loads(body.decode("utf-8"))


def test_extension_import_scores_only_rows_with_every_number_observed() -> None:
    """Handle-only rows are counted as scanned but never scored or guessed."""
    code, data = _post_extension_batch(
        [_full_profile("real.person"), _handle_only("only.a.handle"), _handle_only("another_handle")]
    )
    assert code == 200
    assert [p["username"] for p in data["profiles"]] == ["real.person"]
    assert data["coverage"]["Honest_Artist"] == {"scanned": 3, "complete": 1, "incomplete": 2}

    artist = data["artists"][0]
    assert artist["sample_size"] == 1
    assert artist["scanned_total"] == 3
    assert artist["incomplete_rows"] == 2
    assert any("NOT scored" in w and "1 of 3" in w for w in data["warnings"])


def test_extension_import_adds_no_invented_control_rows() -> None:
    """The old synthetic 'Control_Users' cohort (made-up numbers) is no longer appended."""
    code, data = _post_extension_batch([_full_profile("real.person")])
    assert code == 200
    assert [a["artist"] for a in data["artists"]] == ["Honest_Artist"]
    assert "Control_Users" not in data["coverage"]


def test_extension_import_with_only_handles_explains_why_nothing_is_scored() -> None:
    """Nothing to score is an explained error, not an audit of invented numbers."""
    code, data = _post_extension_batch([_handle_only("a.b"), _handle_only("c_d")])
    assert code == 400
    assert "none has all of its numbers" in data["error"]


def test_old_placeholder_numbers_are_treated_as_unknown() -> None:
    """A row an older build filled with placeholder numbers is not scored."""
    placeholder = {
        "username": "legacy.guess",
        "has_profile_photo": True,
        "account_private": False,
        "post_count": 24,
        "followers_count": 340,
        "following_count": 290,
        "bio_length": 42,
        "deep_checked": False,
    }
    code, data = _post_extension_batch([_full_profile("real.person"), placeholder])
    assert code == 200
    assert [p["username"] for p in data["profiles"]] == ["real.person"]
    assert data["coverage"]["Honest_Artist"]["incomplete"] == 1


def test_same_day_burst_claim_from_a_client_is_ignored_without_timestamps() -> None:
    """The server decides the burst clue itself; a client-supplied flag is never trusted."""
    row = _full_profile("claims.burst", post_count=3, same_day_post_burst=True)
    code, data = _post_extension_batch([row])
    assert code == 200
    assert data["profiles"][0]["flags"]["same_day_post_burst"] is False


def test_checkpoint_merge_keeps_numbers_observed_earlier() -> None:
    """A later handle-only sighting never erases numbers that were already observed."""
    artist = "test_merge_keeps_numbers"
    _call_wsgi("POST", "/api/checkpoint", json_body={"artist": artist, "clear": True})
    _call_wsgi(
        "POST",
        "/api/checkpoint",
        json_body={"artist": artist, "profiles": [_full_profile("kept.numbers")]},
    )
    _call_wsgi(
        "POST",
        "/api/checkpoint",
        json_body={"artist": artist, "profiles": [_handle_only("kept.numbers")]},
    )
    code, _, body = _call_wsgi("GET", "/api/checkpoint", query=f"artist={artist}")
    assert code == 200
    saved = json.loads(body.decode("utf-8"))["profiles"][0]
    assert saved["post_count"] == 42
    assert saved["followers_count"] == 680
    assert saved["tier"] == 2
    _call_wsgi("POST", "/api/checkpoint", json_body={"artist": artist, "clear": True})


def test_url_only_audit_mode_is_rejected() -> None:
    """URL-only mode with assumed numbers is permanently rejected with HTTP 400."""
    code, _, body = _call_wsgi(
        "POST",
        "/api/audit",
        json_body={
            "mode": "urls",
            "groups": [{"artist": "What_If", "urls_text": "https://www.instagram.com/wired/"}],
        },
    )
    assert code == 400
    assert "removed" in json.loads(body.decode("utf-8"))["error"]


def test_blank_csv_columns_are_rejected_not_assumed() -> None:
    """Blank profile columns are rejected into rejects rather than filled with assumed defaults."""
    partial = (
        "artist,username,has_profile_photo,post_count,followers_count,"
        "following_count,bio_length,account_private,username_digit_ratio\n"
        "A,mari0.256,,,,,,,\n"
    )
    code, _, body = _call_wsgi("POST", "/api/audit", json_body={"mode": "csv", "csv_text": partial})
    assert code == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["total_rows_loaded"] == 0
    assert payload["total_rows_rejected"] == 1
    assert payload["profiles"] == []


def test_checkpoint_sync_reply_is_a_small_summary() -> None:
    """The extension syncs every few seconds, so the reply must not carry every profile."""
    artist = "test_small_reply"
    code, _, body = _call_wsgi(
        "POST",
        "/api/checkpoint",
        json_body={"artist": artist, "profiles": [_full_profile("a.b"), _handle_only("c.d")]},
    )
    assert code == 200
    reply = json.loads(body.decode("utf-8"))
    assert reply["total_saved"] == 2
    assert reply["newly_added"] == 2
    assert "profiles" not in reply
    assert "seen_usernames" not in reply
    _call_wsgi("POST", "/api/checkpoint", json_body={"artist": artist, "clear": True})


def test_labels_api_get_post_and_options(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test /api/labels endpoint in isolated tmp_path without touching real data files."""
    import botaudit.labels as labels_mod
    import botaudit.web as web_mod

    tmp_notes = tmp_path / "label_notes.csv"
    tmp_bots = tmp_path / "verified_bots.csv"
    tmp_users = tmp_path / "verified_users.csv"
    tmp_bench = tmp_path / "verified_benchmarks.csv"
    tmp_cp = tmp_path / "checkpoints"
    tmp_backups = tmp_path / "backups"
    tmp_bots.write_text("https://www.instagram.com/existing_bot/\n", encoding="utf-8")
    tmp_users.write_text("https://www.instagram.com/existing_human/\n", encoding="utf-8")

    def _isolated_save_note(note: Any, auto_sync: bool = True) -> Any:
        res = labels_mod.save_label_note(note, path=tmp_notes, auto_sync=False)
        if auto_sync:
            labels_mod.sync_ground_truth_lists(
                notes_path=tmp_notes,
                bots_path=tmp_bots,
                users_path=tmp_users,
                backup_dir=tmp_backups,
            )
            labels_mod.sync_sample_benchmarks(
                checkpoints_dir=tmp_cp,
                output_path=tmp_bench,
                notes_path=tmp_notes,
                bots_path=tmp_bots,
                users_path=tmp_users,
            )
        return res

    monkeypatch.setattr(web_mod, "save_label_note", _isolated_save_note)
    monkeypatch.setattr(
        web_mod,
        "load_labels",
        lambda: labels_mod.load_labels(tmp_bots, tmp_users, notes_path=tmp_notes),
    )
    monkeypatch.setattr(
        web_mod,
        "load_label_notes",
        lambda: labels_mod.load_label_notes(tmp_notes),
    )

    code, _, _ = _call_wsgi("OPTIONS", "/api/labels")
    assert code == 200

    # Missing username
    code, _, body = _call_wsgi("POST", "/api/labels", json_body={})
    assert code == 400
    assert "Missing" in json.loads(body.decode("utf-8"))["error"]

    # Invalid label
    code, _, body = _call_wsgi(
        "POST", "/api/labels", json_body={"username": "valid_user", "label": "robot"}
    )
    assert code == 400
    assert "Invalid label" in json.loads(body.decode("utf-8"))["error"]

    # Invalid status
    code, _, body = _call_wsgi(
        "POST",
        "/api/labels",
        json_body={"username": "valid_user", "status": "destroyed"},
    )
    assert code == 400
    assert "Invalid status" in json.loads(body.decode("utf-8"))["error"]

    # Valid POST (isolated to tmp_path)
    code, _, body = _call_wsgi(
        "POST",
        "/api/labels",
        json_body={
            "username": "@isolated_test_bot",
            "label": "bot",
            "bot_type": "farm_template",
            "evidence_note": "isolated test",
            "status": "available",
        },
    )
    assert code == 200
    res = json.loads(body.decode("utf-8"))
    assert res["ok"] is True
    assert res["username"] == "isolated_test_bot"
    assert res["label"] == "bot"
    assert res["verified_bots_count"] == 2
    assert res["verified_users_count"] == 1

    # GET returns the isolated note
    code, _, body = _call_wsgi("GET", "/api/labels")
    assert code == 200
    get_res = json.loads(body.decode("utf-8"))
    assert get_res["count"] == 1
    usernames = [n["username"] for n in get_res["notes"]]
    assert "isolated_test_bot" in usernames


def test_presets_api_endpoint() -> None:
    """GET /api/presets lists ONLY real-data presets (example_bot_farm and verified_benchmarks)."""
    code, _, body = _call_wsgi("GET", "/api/presets")
    assert code == 200
    res = json.loads(body.decode("utf-8"))
    preset_ids = [p["id"] for p in res["presets"]]
    assert preset_ids == ["example_bot_farm", "verified_benchmarks"]
    assert "calibration" not in preset_ids

    ex_meta = res["presets"][0]
    assert ex_meta["artist"] == "_vlad.dumitrache_"
    assert ex_meta["instagram_url"] == "https://www.instagram.com/_vlad.dumitrache_/"
    assert ex_meta["available"] is True

    bad_code, _, _ = _call_wsgi("GET", "/api/presets", query="name=calibration")
    assert bad_code == 400


def test_starter_example_bot_farm_preset_and_csv_work_on_fresh_clone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bundled @_vlad.dumitrache_ example loads cleanly with real metrics and zero manual-label bias."""
    import botaudit.labels as labels_mod
    import botaudit.web as web_mod
    from botaudit.labels import LabelSet
    from botaudit.loader import load_followers_csv

    ex_json = Path("data/examples/_vlad.dumitrache_.json")
    ex_csv = Path("data/examples/_vlad.dumitrache_.csv")
    assert ex_json.is_file() and ex_csv.is_file()

    # Strip manual_label in bundled starter JSON so fresh users see pure automated scoring + 35-60p review queue
    raw_ex = json.loads(ex_json.read_text(encoding="utf-8"))
    assert raw_ex["artist"] == "_vlad.dumitrache_"
    assert raw_ex["source_url"] == "https://www.instagram.com/_vlad.dumitrache_/"
    assert len(raw_ex["profiles"]) >= 1200
    assert all("manual_label" not in p for p in raw_ex["profiles"])

    csv_loaded = load_followers_csv(ex_csv)
    assert len(csv_loaded.df) >= 500
    assert len(csv_loaded.rejects) == 0

    # Simulate an isolated fresh clone (empty checkpoints and empty label CSVs)
    empty_cp = tmp_path / "checkpoints"
    empty_cp.mkdir()
    monkeypatch.setattr(web_mod, "CHECKPOINTS_DIR", empty_cp)
    monkeypatch.setattr(
        web_mod,
        "load_labels",
        lambda *a, **kw: LabelSet(
            bots=frozenset(),
            humans=frozenset(),
            conflicts=(),
            bot_duplicates=(),
            human_duplicates=(),
            ignored_lines=0,
        ),
    )
    monkeypatch.setattr(web_mod, "load_label_notes", lambda *a, **kw: {})
    monkeypatch.setattr(labels_mod, "DEFAULT_BENCHMARK_SAMPLE_PATH", tmp_path / "missing_bench.csv")

    res = load_preset_Audit("example_bot_farm")
    assert res["source"] == "example_bot_farm"
    assert res["example_url"] == "https://www.instagram.com/_vlad.dumitrache_/"
    assert res["artists"][0]["artist"] == "_vlad.dumitrache_"
    assert res["artists"][0]["sample_size"] >= 1100
    assert res["artists"][0]["effective_bot_pct"] >= 70.0
    assert res["artists"][0]["suspicious_count"] > 0

    post_code, _, post_body = _call_wsgi(
        "POST",
        "/api/audit",
        json_body={"mode": "preset", "preset": "example_bot_farm"},
    )
    assert post_code == 200
    post_res = json.loads(post_body.decode("utf-8"))
    assert post_res["artists"][0]["artist"] == "_vlad.dumitrache_"


def test_history_api_and_artist_param_import() -> None:
    """Test /api/history endpoint and /api/extension-import?artist= query parameter."""
    code, _, body = _call_wsgi("GET", "/api/history")
    assert code == 200
    res = json.loads(body.decode("utf-8"))
    assert "history" in res
    assert isinstance(res["history"], list)
    if res["history"]:
        item = res["history"][0]
        assert "artist" in item
        assert "total_scanned" in item
        assert "deep_checked" in item
        assert "last_modified" in item

    # Query with non-existent artist
    code, _, body = _call_wsgi("GET", "/api/extension-import", query="artist=__non_existent_artist__")
    assert code == 404
    assert "No checkpoint found" in json.loads(body.decode("utf-8"))["error"]

    # Verify ground_truth structure in audit
    code, data = _post_extension_batch([_full_profile("real.person")])
    assert code == 200
    assert "ground_truth" in data
    assert "total_labeled" in data["ground_truth"]
    assert "confirmed_bots" in data["ground_truth"]
    assert "confirmed_humans" in data["ground_truth"]


def test_history_api_delete_and_clear() -> None:
    """Test DELETE /api/history?artist= and POST /api/history delete action."""
    from botaudit.web import CHECKPOINTS_DIR, save_artist_checkpoint

    save_artist_checkpoint("test_delete_artist", [_full_profile("test_del_user")])
    target_cp = CHECKPOINTS_DIR / "test_delete_artist.json"
    assert target_cp.is_file()

    code, _, body = _call_wsgi("DELETE", "/api/history", query="artist=test_delete_artist")
    assert code == 200
    res = json.loads(body.decode("utf-8"))
    assert res["ok"] is True
    assert not target_cp.is_file()

    save_artist_checkpoint("test_delete_artist_2", [_full_profile("test_del_user_2")])
    target_cp2 = CHECKPOINTS_DIR / "test_delete_artist_2.json"
    assert target_cp2.is_file()

    code, _, body = _call_wsgi(
        "POST", "/api/history", json_body={"action": "delete", "artist": "test_delete_artist_2"}
    )
    assert code == 200
    res = json.loads(body.decode("utf-8"))
    assert res["ok"] is True
    assert not target_cp2.is_file()


def test_option_c_split_handle_and_ratio_cluster_cap_protects_single_dimension_users() -> None:
    """Verify Option C1 (+10p stitched vs +20p digits) and C2 (60p ratio cluster cap + Option A 3-tier verdicts)."""
    csv_text = (
        "artist,username,has_profile_photo,post_count,followers_count,"
        "following_count,bio_length,account_private,username_digit_ratio\n"
        # 1. C1 stitched handle only (+10p), clean real stats -> 10p Clean
        "tier_group,ana.popescu._x,true,22,420,380,45,false,0.0000\n"
        # 2. C1 true digit-heavy handle (+20p), clean real stats -> 20p Clean
        "tier_group,user_98765432,true,22,420,380,45,false,0.6154\n"
        # 3. C2 single-dimension high-ratio active user (mass 25 + ext 30 + low 15 + shared 20 = 90p raw -> capped 60p) -> Suspicious
        "tier_group,quietlistener,true,15,20,2200,80,false,0.0000\n"
        # 4. C2 same high-ratio + 1 independent witness (empty bio +10p) -> 70p Bot
        "tier_group,quietemptybio,true,15,20,2200,0,false,0.0000\n"
    )
    res = run_audit_from_csv_text(csv_text)
    by_user = {p["username"]: p for p in res["profiles"]}

    stitched_prof = by_user["ana.popescu._x"]
    assert stitched_prof["flags"]["stitched_username"] is True
    assert stitched_prof["flags"]["digit_heavy_username"] is False
    assert round(stitched_prof["bot_score"] * 100) == stitched_prof["raw_score_pts"] == 10
    assert stitched_prof["verdict_tier"] == "clean"
    assert stitched_prof["bot_like"] is False

    digit_prof = by_user["user_98765432"]
    assert digit_prof["flags"]["digit_heavy_username"] is True
    assert digit_prof["flags"]["stitched_username"] is False
    assert round(digit_prof["bot_score"] * 100) == digit_prof["raw_score_pts"] == 20
    assert digit_prof["verdict_tier"] == "clean"
    assert digit_prof["bot_like"] is False

    capped_only = by_user["quietlistener"]
    assert capped_only["ratio_cluster_raw_pts"] == 90
    assert capped_only["ratio_cluster_pts"] == 60
    assert capped_only["ratio_cluster_capped"] is True
    assert capped_only["flags"]["ratio_cluster_capped"] is True
    assert round(capped_only["bot_score"] * 100) == capped_only["raw_score_pts"] == 60
    assert capped_only["bot_like"] is False
    assert capped_only["verdict_tier"] == "suspicious"

    two_witnesses = by_user["quietemptybio"]
    assert two_witnesses["ratio_cluster_raw_pts"] == 90
    assert two_witnesses["ratio_cluster_pts"] == 60
    assert round(two_witnesses["bot_score"] * 100) == two_witnesses["raw_score_pts"] == 70
    assert two_witnesses["bot_like"] is True
    assert two_witnesses["verdict_tier"] == "bot"

    art = res["artists"][0]
    assert art["effective_bot_count"] == 1
    assert art["suspicious_count"] == 1
    assert art["clean_count"] == 2
    assert art["effective_bot_count"] + art["suspicious_count"] + art["clean_count"] == art["sample_size"] == 4



