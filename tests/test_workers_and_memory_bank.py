"""Tests for the 4-Worker Engine, Memory Bank endpoint, Live Status, and Glassmorphism History."""

from __future__ import annotations

import io
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from wsgiref.util import setup_testing_defaults

import pytest

import botaudit.labels as labels_mod
import botaudit.web as web_mod
from botaudit.web import (
    ThreadedWSGIServer,
    build_memory_bank_payload,
    create_wsgi_app,
)
from botaudit.workers import AuditWorkerEngine

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "index.html"


def _call_wsgi(
    method: str,
    path: str,
    query: str = "",
    json_body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
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


def _setup_isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    bots_csv = tmp_path / "verified_bots.csv"
    users_csv = tmp_path / "verified_users.csv"
    notes_csv = tmp_path / "label_notes.csv"
    bench_csv = tmp_path / "verified_benchmarks.csv"

    bots_csv.write_text(
        "Instagram Links\n"
        "https://www.instagram.com/bot_only_in_csv/\n"
        "https://www.instagram.com/shared_bot/\n",
        encoding="utf-8",
    )
    users_csv.write_text(
        "Instagram Links\n"
        "https://www.instagram.com/human_only_in_csv/\n"
        "https://www.instagram.com/shared_human/\n",
        encoding="utf-8",
    )
    notes_csv.write_text(
        "username,label,bot_type,labeler,labeled_on,evidence_note,status\n"
        "shared_bot,bot,farm_template,auditor,2026-03-01,note,available\n"
        "shared_human,human,,auditor,2026-03-01,note,available\n",
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
    monkeypatch.setattr(
        web_mod,
        "save_label_note",
        lambda note, path=notes_csv, auto_sync=False: labels_mod.save_label_note(
            note, path=notes_csv, auto_sync=False
        ),
    )
    return {
        "cp_dir": cp_dir,
        "bots_csv": bots_csv,
        "users_csv": users_csv,
        "notes_csv": notes_csv,
        "bench_csv": bench_csv,
    }


def test_memory_bank_unifies_csv_lists_and_label_notes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Handles present in verified_bots.csv or verified_users.csv but missing from label_notes.csv ARE preserved."""
    _setup_isolated_env(monkeypatch, tmp_path)

    payload = build_memory_bank_payload()
    assert payload["ok"] is True
    assert "bot_only_in_csv" in payload["verified_bots"]
    assert "shared_bot" in payload["verified_bots"]
    assert "human_only_in_csv" in payload["verified_users"]
    assert "shared_human" in payload["verified_users"]

    # In label_notes map, both shared entries exist
    assert payload["label_notes"]["shared_bot"] == "bot"
    assert payload["label_notes"]["shared_human"] == "human"

    # Total counts check
    totals = payload["totals"]
    assert totals["confirmed_bots"] >= 2
    assert totals["confirmed_humans"] >= 2
    assert totals["total_confirmed"] >= 4


def test_api_memory_bank_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """GET /api/memory-bank returns the unified JSON structure with cache headers."""
    _setup_isolated_env(monkeypatch, tmp_path)

    code, headers, body = _call_wsgi("GET", "/api/memory-bank")
    assert code == 200
    assert "application/json" in headers.get("content-type", "")
    assert "no-store" in headers.get("cache-control", "")
    data = json.loads(body.decode("utf-8"))
    assert data["ok"] is True
    assert "verified_bots" in data
    assert "verified_users" in data
    assert "totals" in data


def test_api_live_status_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """GET /api/live-status returns 4-worker telemetry, revision, and memory bank counts."""
    _setup_isolated_env(monkeypatch, tmp_path)

    code, _, body = _call_wsgi("GET", "/api/live-status")
    assert code == 200
    data = json.loads(body.decode("utf-8"))
    assert data["ok"] is True
    assert data["worker_count"] == 4
    assert isinstance(data["revision"], int)
    assert "memory_bank_totals" in data
    assert "latest_event" in data
    assert isinstance(data["recent_events"], list)


def test_audit_worker_engine_lifecycle_and_events(tmp_path: Path) -> None:
    """Worker engine starts 4 daemon threads, queues events, precomputes audits, and stops cleanly."""
    engine = AuditWorkerEngine()
    snapshot_init = engine.get_status_snapshot()
    assert snapshot_init["workers_running"] is False

    engine.start()
    snapshot_running = engine.get_status_snapshot()
    assert snapshot_running["workers_running"] is True
    assert snapshot_running["worker_count"] == 4

    # Record event
    engine.record_event(
        kind="manual_mark",
        artist="test_artist",
        username="test_user",
        summary_en="Manual check recorded for @test_user",
        summary_ro="Verificare manuală pentru @test_user",
    )
    time.sleep(0.05)
    snap = engine.get_status_snapshot()
    assert len(snap["recent_events"]) >= 1
    assert "test_user" in snap["recent_events"][0]["summary_en"]

    # Notify data changed
    engine.notify_data_changed(artist="artist_audit")
    time.sleep(0.05)
    snap2 = engine.get_status_snapshot()
    assert snap2["revision"] >= 1

    # Revision-safe history cache invalidation check
    rev_now = engine.get_status_snapshot()["revision"]
    engine.set_cached_history([{"artist": "stale_test"}], revision=rev_now - 1)
    assert engine.get_cached_history() is None
    engine.set_cached_history([{"artist": "fresh_test"}], revision=rev_now)
    assert engine.get_cached_history() == [{"artist": "fresh_test"}]
    engine.notify_data_changed(artist="fresh_test")
    assert engine.get_cached_history() is None

    # Revision-safe memory-bank cache invalidation check
    rev1 = engine.get_status_snapshot()["revision"]
    engine.notify_data_changed(artist="mb_test")
    rev2 = engine.get_status_snapshot()["revision"]
    assert rev2 > rev1
    engine.set_cached_memory_bank({"stale": True}, revision=rev1)
    assert engine.get_cached_memory_bank() is None
    engine.set_cached_memory_bank({"fresh": True}, revision=rev2)
    assert engine.get_cached_memory_bank() == {"fresh": True}
    engine.notify_data_changed(artist="mb_test")
    assert engine.get_cached_memory_bank() is None

    engine.stop()
    snapshot_stopped = engine.get_status_snapshot()
    assert snapshot_stopped["workers_running"] is False


def test_threaded_wsgi_server_attributes() -> None:
    """ThreadedWSGIServer must enable daemon_threads to prevent lingering sockets."""
    assert ThreadedWSGIServer.daemon_threads is True


def test_history_breakdown_enrichment_for_glass_cards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """GET /api/history delivers enriched fields for glassmorphic cards."""
    paths = _setup_isolated_env(monkeypatch, tmp_path)

    # Write a test checkpoint
    cp_file = paths["cp_dir"] / "test_band.json"
    cp_file.write_text(
        json.dumps({
            "artist": "test_band",
            "profiles": [
                {
                    "artist": "test_band",
                    "username": "bot_fan_1",
                    "has_profile_photo": False,
                    "post_count": 0,
                    "followers_count": 1,
                    "following_count": 2800,
                    "bio_length": 0,
                    "account_private": False,
                    "username_digit_ratio": 0.5,
                    "deep_checked": True,
                },
                {
                    "artist": "test_band",
                    "username": "real_fan_1",
                    "has_profile_photo": True,
                    "post_count": 55,
                    "followers_count": 480,
                    "following_count": 320,
                    "bio_length": 40,
                    "account_private": False,
                    "username_digit_ratio": 0.0,
                    "deep_checked": True,
                },
                {
                    "artist": "test_band",
                    "username": "unopened_fan",
                    "schema_version": 2,
                },
            ],
        }),
        encoding="utf-8",
    )

    code, _, body = _call_wsgi("GET", "/api/history")
    assert code == 200
    res = json.loads(body.decode("utf-8"))
    items = {h["artist"]: h for h in res["history"]}
    assert "test_band" in items
    item = items["test_band"]

    assert item["total_scanned"] == 3
    assert item["deep_checked"] == 2
    assert item["bot_like_count"] == 1
    assert item["real_count"] == 1
    assert item["bot_pct"] == 50.0
    assert item["photo_count"] == 1


def test_index_html_glass_and_worker_contract() -> None:
    """index.html must contain the Live Worker bar, Glass cards, lens avatars, and pass Node validation."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Required IDs and classes
    for token in (
        'id="liveWorkerBar"',
        'id="lwPulseDot"',
        'id="lwBotsCount"',
        'id="lwHumansCount"',
        'id="lwObservedCount"',
        'id="lwLatestEvent"',
        "hist-glass-grid",
        "hist-glass-card",
        "hist-remove-x",
        "hist-lens-dot",
        "insp-lens-avatar",
        "insp-gallery-pill",
        "function pollLiveWorkerStatus(",
    ):
        assert token in html, f"Missing {token} in index.html"

    # Validate JavaScript syntax
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.DOTALL)
    assert len(scripts) >= 1
    try:
        node_res = subprocess.run(
            ["node", "--check"],
            input=scripts[-1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert node_res.returncode == 0, f"JS syntax error: {node_res.stderr}"
    except FileNotFoundError:
        pass


def test_checkpoint_fast_path_and_global_observed_cache_invalidation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fast-path checkpoint skips duplicate live_counts and mtime cache invalidates on disk updates."""
    paths = _setup_isolated_env(monkeypatch, tmp_path)

    code, _, body = _call_wsgi(
        "POST",
        "/api/checkpoint",
        json_body={
            "artist": "fast_artist",
            "skip_live_counts": True,
            "defer_workers": True,
            "profiles": [
                {
                    "artist": "fast_artist",
                    "username": "fast_user_1",
                    "has_profile_photo": True,
                    "post_count": 10,
                    "followers_count": 100,
                    "following_count": 90,
                    "bio_length": 25,
                    "account_private": False,
                    "deep_checked": True,
                }
            ],
        },
    )
    assert code == 200
    cp_resp = json.loads(body.decode("utf-8"))
    assert cp_resp["total_saved"] == 1
    assert "live_counts" not in cp_resp

    # Warm the mtime-keyed global observed cache
    obs1 = web_mod._load_global_observed_profiles(cp_dir=paths["cp_dir"], benchmarks_path=paths["bench_csv"])
    assert "fast_user_1" in obs1
    assert "fast_user_2" not in obs1

    # Saving a second profile updates checkpoint mtime/size -> cache must auto-invalidate
    code2, _, _ = _call_wsgi(
        "POST",
        "/api/checkpoint",
        json_body={
            "artist": "fast_artist",
            "skip_live_counts": True,
            "defer_workers": True,
            "profiles": [
                {
                    "artist": "fast_artist",
                    "username": "fast_user_2",
                    "has_profile_photo": False,
                    "post_count": 0,
                    "followers_count": 2,
                    "following_count": 1800,
                    "bio_length": 0,
                    "account_private": False,
                    "deep_checked": True,
                }
            ],
        },
    )
    assert code2 == 200
    obs2 = web_mod._load_global_observed_profiles(cp_dir=paths["cp_dir"], benchmarks_path=paths["bench_csv"])
    assert "fast_user_1" in obs2
    assert "fast_user_2" in obs2

