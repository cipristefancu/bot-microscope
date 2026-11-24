"""Web application backend and API endpoints for interactive audience auditing.

Provides a local WSGI web server (`wsgiref.simple_server`) and JSON API
endpoints that connect `index.html` to the core `botaudit` pipeline
(loader, heuristic scoring, Wilson confidence intervals, handle forensics,
and standalone HTML report generation).

This tool never connects to the internet.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIServer, make_server

import pandas as pd

from botaudit.capture import (
    SCHEMA_VERSION,
    best_profiles,
    has_header_counts,
    is_legacy_placeholder,
    is_scorable,
    merge_profiles,
    scrub_legacy_profile,
)
from botaudit.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    _check_names,
    _check_values,
    load_config,
)
from botaudit.handles import (
    analyze_handle_forensics,
    compute_digit_ratio,
    extract_username,
)
from botaudit.labels import (
    DEFAULT_BENCHMARK_SAMPLE_PATH,
    VALID_LABELS,
    VALID_STATUSES,
    LabelError,
    LabelNote,
    load_label_notes,
    load_labels,
    save_label_note,
    sync_ground_truth_lists,
    sync_sample_benchmarks,
)
from botaudit.loader import REQUIRED_COLUMNS, CSVValidationError, load_followers_csv
from botaudit.registry import load_local_registry
from botaudit.report import generate_chart_base64, render_html_report
from botaudit.scoring import (
    FLAG_COLUMNS,
    is_extended_mass_following,
    is_extreme_mass_following,
    is_follow_exchange_ring,
    is_low_audience_imbalance,
    is_shared_post_bot_signal,
    score_followers,
)
from botaudit.stats import compute_artist_stats, compute_wilson_ci
from botaudit.workers import AuditWorkerEngine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML_PATH = PROJECT_ROOT / "index.html"
CHECKPOINTS_DIR = PROJECT_ROOT / "data" / "checkpoints"

_SAFE_ARTIST_FILE_RE = re.compile(r"[^a-zA-Z0-9._-]")
logger = logging.getLogger(__name__)
_STORAGE_LOCK = threading.RLock()
_BENCHMARK_SYNC_LOCK = threading.Lock()
_GLOBAL_OBS_CACHE_LOCK = threading.Lock()
_GLOBAL_OBS_CACHE: dict[str, Any] = {"sig": None, "data": None}
_WORKER_ENGINE = AuditWorkerEngine()


class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    """Multi-threaded local WSGI server so rapid tab closes and syncs never block each other."""

    daemon_threads = True


# In-memory store for the latest batch pushed by the local Browser Extension
_LATEST_EXTENSION_RESULT: dict[str, Any] | None = None
_ACTIVE_EXTENSION_ARTIST: str | None = None
_KNOWN_BOT_HANDLES_BY_ARTIST: dict[str, set[str]] = {}


def _checkpoint_path(artist: str) -> Path:
    """Return a safe disk path for an artist's multi-day collection checkpoint."""
    clean = _SAFE_ARTIST_FILE_RE.sub("_", artist.strip().lstrip("@")) or "Audited_Account"
    return CHECKPOINTS_DIR / f"{clean}.json"


def _atomic_write_text(target_path: Path, text: str, max_attempts: int = 12) -> None:
    """Write text to a unique temp file and atomically replace target_path with Windows retry."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(
        f"{target_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    try:
        tmp_path.write_text(text, encoding="utf-8")
        for attempt in range(max_attempts):
            try:
                tmp_path.replace(target_path)
                return
            except PermissionError:
                if attempt >= max_attempts - 1:
                    raise
                time.sleep(0.015 * (attempt + 1))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_artist_checkpoint(artist: str) -> dict[str, Any]:
    """Load an artist's saved multi-day follower checkpoint from disk if it exists."""
    path = _checkpoint_path(artist)
    with _STORAGE_LOCK:
        if not path.is_file():
            return {
                "artist": artist,
                "checkpoint_exists": False,
                "profiles": [],
                "seen_usernames": [],
                "completed_slices": [],
                "last_username": "",
                "next_max_id": "",
            }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["checkpoint_exists"] = True
                data["profiles"] = [
                    scrub_legacy_profile(p)
                    for p in data.get("profiles") or []
                    if isinstance(p, dict)
                ]
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "artist": artist,
        "checkpoint_exists": False,
        "profiles": [],
        "seen_usernames": [],
        "completed_slices": [],
        "last_username": "",
        "next_max_id": "",
    }


def save_artist_checkpoint(
    artist: str,
    new_profiles: list[dict[str, Any]],
    completed_slices: list[str] | None = None,
    last_username: str = "",
    next_max_id: str = "",
) -> dict[str, Any]:
    """Merge new_profiles into an artist's multi-day checkpoint on disk without duplicates."""
    with _STORAGE_LOCK:
        CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
        existing = load_artist_checkpoint(artist)
        artist_handle_low = extract_username(str(artist or "")).lower()
        if artist_handle_low == "audited_account":
            artist_handle_low = ""
        by_uname: dict[str, dict[str, Any]] = {}

        for p in existing.get("profiles") or []:
            if isinstance(p, dict) and p.get("username"):
                key = str(p["username"]).lower()
                if artist_handle_low and key == artist_handle_low:
                    continue
                p["artist"] = artist
                by_uname[key] = p

        added_count = 0
        for p in new_profiles:
            if isinstance(p, dict) and p.get("username"):
                p["artist"] = artist
                key = str(p["username"]).lower()
                if artist_handle_low and key == artist_handle_low:
                    continue
                if key in by_uname:
                    by_uname[key] = merge_profiles(by_uname[key], p)
                    by_uname[key]["artist"] = artist
                else:
                    added_count += 1
                    scrubbed = scrub_legacy_profile(p)
                    scrubbed["artist"] = artist
                    by_uname[key] = scrubbed

        merged_profiles = list(by_uname.values())
        prev_slices = set(existing.get("completed_slices") or [])
        if completed_slices:
            prev_slices.update(str(s) for s in completed_slices if s)

        final_last = last_username or str(existing.get("last_username") or "")
        if not final_last and merged_profiles:
            final_last = str(merged_profiles[-1].get("username") or "")

        payload = {
            "artist": artist,
            "total_saved": len(merged_profiles),
            "newly_added": added_count,
            "last_username": final_last,
            "next_max_id": next_max_id or str(existing.get("next_max_id") or ""),
            "completed_slices": sorted(prev_slices),
            "seen_usernames": [
                str(p.get("username")) for p in merged_profiles if p.get("username")
            ],
            "profiles": merged_profiles,
        }
        target_path = _checkpoint_path(artist)
        _atomic_write_text(target_path, json.dumps(payload, separators=(",", ":")))
        return payload


def reconcile_checkpoint_manual_labels(
    artist: str,
    profiles: list[dict[str, Any]],
) -> int:
    """Recover any orphaned manual_label values from checkpoint profiles into label_notes/CSVs.

    Never overwrites handles already present in load_labels() (verified_bots/users/label_notes).
    Returns the number of missing labels recovered.
    """
    if not profiles:
        return 0
    clean_artist = re.sub(r"[^\w\-.]", "_", (artist or "").strip().lstrip("@")) or "Audited_Account"
    artist_handle_low = extract_username(clean_artist).lower()
    if artist_handle_low == "audited_account":
        artist_handle_low = ""
    recovered = 0
    with _STORAGE_LOCK:
        labels_set = load_labels()
        seen: set[str] = set()
        for raw_p in profiles:
            if not isinstance(raw_p, dict):
                continue
            cp_lbl = str(raw_p.get("manual_label") or "").strip().lower()
            if cp_lbl not in VALID_LABELS:
                continue
            uname = extract_username(str(raw_p.get("username") or "")).lower()
            if not uname or uname in seen:
                continue
            if artist_handle_low and uname == artist_handle_low:
                continue
            if labels_set.label_of(uname) is not None:
                continue
            seen.add(uname)
            raw_date = str(raw_p.get("collected_at") or "").strip()
            labeled_on = raw_date if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date) else None
            note = LabelNote(
                username=uname,
                label=cp_lbl,
                labeler="owner",
                labeled_on=labeled_on,
                evidence_note=f"Recovered from @{clean_artist} checkpoint",
                status="available",
            )
            save_label_note(note, auto_sync=False)
            recovered += 1
        if recovered > 0:
            sync_ground_truth_lists()
    return recovered


def detect_same_day_post_burst(
    post_count: int,
    post_timestamps: list[int | float] | None = None,
    followers_count: int = 100,
    following_count: int = 100,
    bio_length: int = 20,
    username: str = "",
    digit_threshold: float = 0.40,
) -> bool:
    """Detect whether an account with 1 to 6 posts exhibits a 'Same-Day Setup Burst'.

    Triggers when `1 <= post_count <= 6` AND either:
      1. Explicit post timestamps exist and all fall within a 48-hour (172,800s) window, OR
      2. No timestamps were provided, `1 <= post_count <= 6`, and the account exhibits
         converging setup-bot traits (high following-to-follower ratio or blank bio + bot handle).
    """
    if post_count < 1 or post_count > 6:
        return False

    if post_timestamps and len(post_timestamps) >= 2:
        clean_ts = [float(t) for t in post_timestamps if isinstance(t, (int, float)) and t > 0]
        if len(clean_ts) >= 2:
            span_seconds = max(clean_ts) - min(clean_ts)
            return span_seconds <= 172800.0  # Within 48 hours (1-day / same-day setup burst)

    ratio = following_count / max(1, followers_count)
    hf = analyze_handle_forensics(username, digit_threshold=digit_threshold)
    if (following_count >= 350 and ratio >= 8.0) or (
        followers_count <= 40 and following_count >= 80
    ):
        return True
    return bool(
        bio_length <= 4
        and (
            hf["is_word_dot_digits"]
            or hf["is_digit_heavy"]
            or hf["has_long_digit_run"]
            or hf["is_stitched_handle"]
        )
    )


def validate_config_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Validate an in-memory configuration dictionary using botaudit.config rules.

    Also preserves extended web settings:
        - same_day_burst_weight: float in [0.0, 0.20) (default 0.15 = 15 pts, strictly < 20 pts)
        - score_private_accounts: bool (default True)
    """
    if not isinstance(data, dict):
        raise ConfigError("Configuration must be a JSON object of named settings.")

    base_copy = dict(data)
    burst_weight = float(base_copy.pop("same_day_burst_weight", 0.15))
    score_private = bool(base_copy.pop("score_private_accounts", True))
    optional_ext = {
        k: base_copy.pop(k)
        for k in (
            "follow_exchange_ring_weight",
            "extreme_mass_following_weight",
            "low_audience_imbalance_weight",
            "shared_post_bot_weight",
            "stitched_username_weight",
            "ratio_cluster_cap",
            "suspicious_cutoff",
            "known_registry_bot_boost",
            "enable_registry_boost",
        )
        if k in base_copy
    }

    if burst_weight < 0.0 or burst_weight >= 0.20:
        raise ConfigError("same_day_burst_weight must be between 0.00 and 0.19 (under 20 pts).")

    problems = _check_names(base_copy)
    if not problems:
        problems = _check_values(base_copy)
    if problems:
        bullet_list = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(f"Invalid configuration settings:\n{bullet_list}")

    base_copy["same_day_burst_weight"] = round(burst_weight, 4)
    base_copy["score_private_accounts"] = score_private
    base_copy.update(optional_ext)
    return base_copy


def normalize_csv_text(csv_text: str) -> str:
    """Compute username_digit_ratio from real username if blank; never invent profile counts."""
    stripped = csv_text.strip()
    if not stripped:
        return csv_text

    lines = stripped.splitlines()
    first_line = lines[0].strip()

    if "," not in first_line and (
        "instagram.com/" in first_line.lower() or first_line.startswith("@")
    ):
        raise CSVValidationError(
            "URL-only lists without real observed profile metrics are not allowed. "
            "Provide a complete 9-column CSV or capture real profiles via the Browser Extension."
        )

    reader = csv.DictReader(io.StringIO(stripped))
    if not reader.fieldnames:
        return csv_text

    headers = [h.strip() for h in reader.fieldnames if h is not None]
    if "artist" not in headers or "username" not in headers:
        return csv_text

    out_buf = io.StringIO()
    all_headers = list(REQUIRED_COLUMNS)
    writer = csv.DictWriter(out_buf, fieldnames=all_headers, lineterminator="\n")
    writer.writeheader()

    for raw_row in reader:
        row = {k.strip(): (v.strip() if v is not None else "") for k, v in raw_row.items() if k}
        username = extract_username(row.get("username", ""))
        artist = row.get("artist", "").strip()
        ratio_str = row.get("username_digit_ratio", "").strip()
        if not ratio_str and username:
            ratio_str = f"{compute_digit_ratio(username):.4f}"

        writer.writerow(
            {
                "artist": artist,
                "username": username,
                "has_profile_photo": row.get("has_profile_photo", ""),
                "post_count": row.get("post_count", ""),
                "followers_count": row.get("followers_count", ""),
                "following_count": row.get("following_count", ""),
                "bio_length": row.get("bio_length", ""),
                "account_private": row.get("account_private", ""),
                "username_digit_ratio": ratio_str,
            }
        )

    return out_buf.getvalue()


def score_followers_extended(
    df: Any,
    config: dict[str, Any],
    burst_overrides: dict[str, bool] | None = None,
    registry: Any | None = None,
    enable_registry_boost: bool | None = None,
    feed_signals_map: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """Apply extended scoring: same-day burst, ratio/extreme mass-following (A), low-audience & shared-post clues, and registry matching (B)."""
    scored = score_followers(df, config=config)
    if scored.empty:
        scored["same_day_post_burst"] = []
        scored["blank_shell_bonus"] = []
        scored["extreme_mass_following"] = []
        scored["follow_exchange_ring"] = []
        scored["low_audience_imbalance"] = []
        scored["shared_post_bot_signal"] = []
        scored["stitched_username"] = []
        scored["ratio_cluster_capped"] = []
        scored["ratio_cluster_raw_pts"] = []
        scored["ratio_cluster_pts"] = []
        scored["verdict_tier"] = []
        scored["known_registry_bot"] = []
        scored["registry_boost_applied"] = []
        scored["raw_bot_like"] = []
        return scored

    weights = config["weights"]
    burst_weight = float(config.get("same_day_burst_weight", 0.15))
    extreme_mass_weight = float(config.get("extreme_mass_following_weight", 0.30))
    follow_exchange_weight = float(config.get("follow_exchange_ring_weight", 0.20))
    low_audience_weight = float(config.get("low_audience_imbalance_weight", 0.15))
    shared_post_weight = float(config.get("shared_post_bot_weight", 0.20))
    stitched_weight = float(config.get("stitched_username_weight", 0.10))
    ratio_cluster_cap = float(config.get("ratio_cluster_cap", 0.60))
    suspicious_floor_pts = round(float(config.get("suspicious_cutoff", 0.35)) * 100)
    registry_boost = float(config.get("known_registry_bot_boost", 0.25))
    enable_reg = (
        enable_registry_boost
        if enable_registry_boost is not None
        else bool(config.get("enable_registry_boost", False))
    )
    score_private = bool(config.get("score_private_accounts", True))
    min_following = int(config["mass_following_min_following"])
    max_followers = int(config["mass_following_max_followers"])
    digit_threshold = float(config["digit_ratio_threshold"])
    cutoff = float(config["bot_like_cutoff"])
    overrides = burst_overrides or {}
    feed_map = feed_signals_map or {}

    same_day_col: list[bool] = []
    shell_bonus_col: list[bool] = []
    zero_posts_col: list[bool] = []
    empty_bio_col: list[bool] = []
    mass_follow_col: list[bool] = []
    extreme_mass_col: list[bool] = []
    follow_exchange_col: list[bool] = []
    low_audience_col: list[bool] = []
    shared_post_col: list[bool] = []
    digit_col: list[bool] = []
    stitched_col: list[bool] = []
    ratio_capped_col: list[bool] = []
    ratio_raw_pts_col: list[int] = []
    ratio_eff_pts_col: list[int] = []
    verdict_tier_col: list[str] = []
    no_photo_col: list[bool] = []
    known_reg_col: list[bool] = []
    reg_boost_col: list[bool] = []
    bot_scores: list[float] = []
    bot_likes: list[bool] = []
    raw_bot_likes: list[bool] = []

    for _, row in scored.iterrows():
        uname = str(row["username"])
        u_low = uname.lower()
        is_priv = bool(row["account_private"])
        p_count = int(row["post_count"])
        flwrs = int(row["followers_count"])
        flwing = int(row["following_count"])
        bio_len = int(row["bio_length"])
        has_pic = bool(row["has_profile_photo"])
        d_ratio = float(row["username_digit_ratio"])
        hf = analyze_handle_forensics(uname, digit_threshold=digit_threshold)

        no_photo = not has_pic
        # C1: High-precision digit patterns (+20p) vs stitched punctuation-only (+10p)
        digit_heavy = (
            (d_ratio > digit_threshold)
            or hf["is_word_dot_digits"]
            or hf["has_long_digit_run"]
        )
        stitched_only = bool(hf["is_stitched_handle"]) and not digit_heavy

        is_known_bot = registry.is_known_bot(uname) if registry else False
        is_known_human = registry.is_known_human(uname) if registry else False

        if is_priv and not score_private:
            zero_p = False
            burst_p = False
            empty_b = False
            mass_f = False
            extreme_mass = False
            follow_exchange = False
            low_aud = False
            shared_post = False
            shell_bonus = 0.0
        else:
            zero_p = p_count == 0
            if zero_p:
                burst_p = False
            elif uname in overrides:
                burst_p = bool(overrides[uname]) and (1 <= p_count <= 6)
            else:
                burst_p = detect_same_day_post_burst(
                    post_count=p_count,
                    followers_count=flwrs,
                    following_count=flwing,
                    bio_length=bio_len,
                    username=uname,
                    digit_threshold=digit_threshold,
                )
            empty_b = bio_len <= 4
            mass_f = is_extended_mass_following(
                following=flwing,
                followers=flwrs,
                post_count=p_count,
                min_following=min_following,
                max_followers=max_followers,
            )
            extreme_mass = is_extreme_mass_following(
                following=flwing,
                followers=flwrs,
                post_count=p_count,
            )
            follow_exchange = is_follow_exchange_ring(
                following=flwing,
                followers=flwrs,
                post_count=p_count,
            )
            low_aud = is_low_audience_imbalance(
                post_count=p_count,
                followers=flwrs,
                following=flwing,
                bio_length=bio_len,
            )
            fsig = feed_map.get(u_low) or {}
            shared_post = is_shared_post_bot_signal(
                post_count=p_count,
                followers=flwrs,
                following=flwing,
                bio_length=bio_len,
                username=uname,
                digit_ratio=d_ratio,
                is_stitched=hf["is_stitched_handle"],
                observed_posts_count=fsig.get("observed_posts_count"),
                avg_post_likes=fsig.get("avg_post_likes"),
                shared_post_ratio=fsig.get("shared_post_ratio"),
            )
            shell_bonus = burst_weight if (zero_p and mass_f and flwing >= 1000) else 0.0

        # Enhancement B: Local Registry Matching boost (+25 pts)
        reg_bonus = registry_boost if (is_known_bot and enable_reg) else 0.0

        # C2: Two-Independent-Witnesses Ratio-Cluster Cap (max 60p from follow-ratio signals alone)
        ratio_cluster_raw = (
            (float(weights["mass_following"]) if mass_f else 0.0)
            + (extreme_mass_weight if extreme_mass else 0.0)
            + (low_audience_weight if low_aud else 0.0)
            + (shared_post_weight if shared_post else 0.0)
        )
        ratio_cluster_effective = min(ratio_cluster_cap, ratio_cluster_raw)
        ratio_capped = ratio_cluster_raw > (ratio_cluster_cap + 1e-9)

        raw_score = (
            ratio_cluster_effective
            + (float(weights["no_photo"]) if no_photo else 0.0)
            + (float(weights["zero_posts"]) if zero_p else 0.0)
            + (burst_weight if burst_p else 0.0)
            + shell_bonus
            + (follow_exchange_weight if follow_exchange else 0.0)
            + (float(weights["empty_bio"]) if empty_b else 0.0)
            + (float(weights["digit_heavy_username"]) if digit_heavy else 0.0)
            + (stitched_weight if stitched_only else 0.0)
            + reg_bonus
        )
        final_score = round(min(1.0, raw_score), 6)
        score_pts_val = round(final_score * 100)

        raw_bot_like = final_score > cutoff
        # Confirmed humans in registry never exceed cutoff (owner rule)
        is_bot_like = raw_bot_like and not is_known_human
        if is_known_human:
            v_tier = "clean"
        elif is_bot_like or is_known_bot:
            v_tier = "bot"
        elif score_pts_val >= suspicious_floor_pts:
            v_tier = "suspicious"
        else:
            v_tier = "clean"

        no_photo_col.append(no_photo)
        zero_posts_col.append(zero_p)
        same_day_col.append(burst_p)
        shell_bonus_col.append(shell_bonus > 0.0)
        mass_follow_col.append(mass_f)
        extreme_mass_col.append(extreme_mass)
        follow_exchange_col.append(follow_exchange)
        low_audience_col.append(low_aud)
        shared_post_col.append(shared_post)
        empty_bio_col.append(empty_b)
        digit_col.append(digit_heavy)
        stitched_col.append(stitched_only)
        ratio_capped_col.append(ratio_capped)
        ratio_raw_pts_col.append(round(ratio_cluster_raw * 100))
        ratio_eff_pts_col.append(round(ratio_cluster_effective * 100))
        verdict_tier_col.append(v_tier)
        known_reg_col.append(is_known_bot)
        reg_boost_col.append(reg_bonus > 0.0)
        bot_scores.append(final_score)
        bot_likes.append(is_bot_like)
        raw_bot_likes.append(raw_bot_like)

    scored["no_photo"] = no_photo_col
    scored["zero_posts"] = zero_posts_col
    scored["same_day_post_burst"] = same_day_col
    scored["blank_shell_bonus"] = shell_bonus_col
    scored["mass_following"] = mass_follow_col
    scored["extreme_mass_following"] = extreme_mass_col
    scored["follow_exchange_ring"] = follow_exchange_col
    scored["low_audience_imbalance"] = low_audience_col
    scored["shared_post_bot_signal"] = shared_post_col
    scored["empty_bio"] = empty_bio_col
    scored["digit_heavy_username"] = digit_col
    scored["stitched_username"] = stitched_col
    scored["ratio_cluster_capped"] = ratio_capped_col
    scored["ratio_cluster_raw_pts"] = ratio_raw_pts_col
    scored["ratio_cluster_pts"] = ratio_eff_pts_col
    scored["verdict_tier"] = verdict_tier_col
    scored["known_registry_bot"] = known_reg_col
    scored["registry_boost_applied"] = reg_boost_col
    scored["bot_score"] = bot_scores
    scored["bot_like"] = bot_likes
    scored["raw_bot_like"] = raw_bot_likes
    return scored


def _is_valid_profile_username(uname: str) -> bool:
    """Return True when uname is non-empty and not an unparsed http(s):// URL."""
    return bool(uname) and not uname.lower().startswith(("http://", "https://"))


def _extract_profiles_from_json_obj(
    obj: Any,
    default_artist: str,
    collected: dict[str, dict[str, Any]],
) -> None:
    """Recursively inspect parsed JSON objects (Instagram API or Extension format) for profiles."""
    if isinstance(obj, list):
        for item in obj:
            _extract_profiles_from_json_obj(item, default_artist, collected)
        return

    if not isinstance(obj, dict):
        return

    # Format A: Extension or direct profile object with username
    uname_raw = obj.get("username")
    if isinstance(uname_raw, str) and uname_raw.strip():
        uname = extract_username(uname_raw)
        if _is_valid_profile_username(uname):
            entry = collected.setdefault(
                uname,
                {
                    "artist": str(obj.get("artist") or default_artist),
                    "username": uname,
                    "has_profile_photo": None,
                    "post_count": None,
                    "followers_count": None,
                    "following_count": None,
                    "bio_length": None,
                    "account_private": None,
                    "post_timestamps": [],
                    "schema_version": None,
                },
            )
            if obj.get("has_profile_photo") is not None:
                entry["has_profile_photo"] = bool(obj["has_profile_photo"])
            elif obj.get("has_anonymous_profile_picture") is not None:
                entry["has_profile_photo"] = not bool(obj["has_anonymous_profile_picture"])

            if "post_count" in obj and obj["post_count"] is not None:
                entry["post_count"] = int(obj["post_count"])
            elif isinstance(obj.get("edge_owner_to_timeline_media"), dict):
                media = obj["edge_owner_to_timeline_media"]
                if "count" in media and media["count"] is not None:
                    entry["post_count"] = int(media["count"])
                edges = media.get("edges")
                if isinstance(edges, list):
                    ts_list: list[int] = []
                    like_vals: list[int] = []
                    shared_hits = 0
                    obs_posts = 0
                    u_low = uname.lower()
                    for e in edges:
                        node = e.get("node") if isinstance(e, dict) and isinstance(e.get("node"), dict) else e
                        if not isinstance(node, dict):
                            continue
                        obs_posts += 1
                        ts_v = node.get("taken_at_timestamp")
                        if not isinstance(ts_v, (int, float)):
                            ts_v = node.get("taken_at")
                        if isinstance(ts_v, (int, float)) and ts_v > 0:
                            ts_list.append(int(ts_v))
                        lc = node.get("like_count")
                        if lc is None and isinstance(node.get("edge_liked_by"), dict):
                            lc = node["edge_liked_by"].get("count")
                        if isinstance(lc, (int, float)) and lc >= 0:
                            like_vals.append(int(lc))
                        owner_obj = node.get("owner") or node.get("user")
                        owner_uname = (
                            str(owner_obj.get("username") or "").strip().lower()
                            if isinstance(owner_obj, dict)
                            else ""
                        )
                        coauthors = node.get("coauthor_producers")
                        is_shared_post = bool(
                            node.get("is_repost")
                            or node.get("repost_context")
                            or (isinstance(coauthors, list) and len(coauthors) > 0)
                            or (owner_uname and owner_uname != u_low)
                        )
                        if is_shared_post:
                            shared_hits += 1
                    if ts_list:
                        entry["post_timestamps"] = ts_list
                    if obs_posts > 0:
                        entry["observed_posts_count"] = obs_posts
                        if like_vals:
                            entry["avg_post_likes"] = round(sum(like_vals) / len(like_vals), 2)
                        entry["shared_post_ratio"] = round(shared_hits / obs_posts, 4)

            for fk in ("observed_posts_count", "avg_post_likes", "shared_post_ratio"):
                if fk in obj and obj[fk] is not None:
                    entry[fk] = float(obj[fk]) if fk != "observed_posts_count" else int(obj[fk])

            if "followers_count" in obj and obj["followers_count"] is not None:
                entry["followers_count"] = int(obj["followers_count"])
            elif isinstance(obj.get("edge_followed_by"), dict):
                cnt = obj["edge_followed_by"].get("count")
                if cnt is not None:
                    entry["followers_count"] = int(cnt)

            if "following_count" in obj and obj["following_count"] is not None:
                entry["following_count"] = int(obj["following_count"])
            elif isinstance(obj.get("edge_follow"), dict):
                cnt = obj["edge_follow"].get("count")
                if cnt is not None:
                    entry["following_count"] = int(cnt)

            if "bio_length" in obj and obj["bio_length"] is not None:
                entry["bio_length"] = int(obj["bio_length"])
            elif isinstance(obj.get("biography"), str):
                entry["bio_length"] = len(obj["biography"].strip())
            elif isinstance(obj.get("biography_with_entities"), dict) and isinstance(
                obj["biography_with_entities"].get("raw_text"), str
            ):
                entry["bio_length"] = len(obj["biography_with_entities"]["raw_text"].strip())

            if "account_private" in obj and obj["account_private"] is not None:
                entry["account_private"] = bool(obj["account_private"])
            elif "is_private" in obj and obj["is_private"] is not None:
                entry["account_private"] = bool(obj["is_private"])

            if isinstance(obj.get("post_timestamps"), list) and obj["post_timestamps"]:
                entry["post_timestamps"] = obj["post_timestamps"]
            if obj.get("schema_version") is not None:
                entry["schema_version"] = obj["schema_version"]
            if "deep_checked" in obj and obj["deep_checked"] is not None:
                entry["deep_checked"] = bool(obj["deep_checked"])
            if isinstance(obj.get("collected_at"), str) and obj["collected_at"].strip():
                entry["collected_at"] = obj["collected_at"].strip()

    for val in obj.values():
        if isinstance(val, (dict, list)):
            _extract_profiles_from_json_obj(val, default_artist, collected)


@dataclass
class ParsedCapture:
    """A capture split into rows that can be scored and rows that cannot yet."""

    csv_text: str
    burst_overrides: dict[str, bool]
    unscored: list[dict[str, Any]]
    warnings: list[str]
    collected_at_map: dict[str, str] = field(default_factory=dict)
    feed_signals_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    hover_profiles: list[dict[str, Any]] = field(default_factory=list)


def parse_har_or_json_to_rows(
    raw_text: str,
    default_artist: str = "Audited_Account",
) -> ParsedCapture:
    """Parse an offline .har file, Instagram JSON, or an extension batch.

    Rule R-NOFAKE: only rows whose numbers were all observed are written to the CSV and
    scored. Rows with unknown numbers come back in `unscored`; nothing is ever filled in.
    """
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as err:
        raise CSVValidationError(f"Could not parse JSON/.HAR file: {err}") from err

    collected: dict[str, dict[str, Any]] = {}

    if isinstance(parsed, dict) and isinstance(parsed.get("log"), dict):
        entries = parsed["log"].get("entries") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            resp = entry.get("response") or {}
            content = resp.get("content") or {}
            text_body = content.get("text")
            if isinstance(text_body, str) and text_body.strip().startswith(("{", "[")):
                try:
                    sub_json = json.loads(text_body)
                    _extract_profiles_from_json_obj(sub_json, default_artist, collected)
                except json.JSONDecodeError:
                    continue
    else:
        _extract_profiles_from_json_obj(parsed, default_artist, collected)

    if not collected:
        raise CSVValidationError(
            "No Instagram follower profiles found in the provided JSON/.HAR data."
        )

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(REQUIRED_COLUMNS), lineterminator="\n")
    writer.writeheader()
    burst_overrides: dict[str, bool] = {}
    hover_profiles: list[dict[str, Any]] = []
    unscored: list[dict[str, Any]] = []
    collected_at_map: dict[str, str] = {}
    feed_signals_map: dict[str, dict[str, Any]] = {}
    legacy_unverified = 0

    for uname, raw_profile in collected.items():
        if isinstance(raw_profile.get("collected_at"), str) and raw_profile["collected_at"].strip():
            collected_at_map[uname.lower()] = raw_profile["collected_at"].strip()
        p = scrub_legacy_profile(raw_profile)
        if not is_scorable(p):
            if has_header_counts(p):
                hover_profiles.append(p)
            else:
                unscored.append(p)
            continue
        if p.get("legacy_unverified"):
            legacy_unverified += 1
        p_count = int(p["post_count"])
        flwrs = int(p["followers_count"])
        flwing = int(p["following_count"])
        bio_len = int(p["bio_length"])
        ts_list = p.get("post_timestamps") or []
        if ts_list:
            burst_overrides[uname] = detect_same_day_post_burst(
                post_count=p_count,
                post_timestamps=ts_list,
                followers_count=flwrs,
                following_count=flwing,
                bio_length=bio_len,
                username=uname,
            )
        if any(
            p.get(fk) is not None
            for fk in ("observed_posts_count", "avg_post_likes", "shared_post_ratio")
        ):
            feed_signals_map[uname.lower()] = {
                "observed_posts_count": (
                    int(p["observed_posts_count"])
                    if p.get("observed_posts_count") is not None
                    else None
                ),
                "avg_post_likes": (
                    float(p["avg_post_likes"]) if p.get("avg_post_likes") is not None else None
                ),
                "shared_post_ratio": (
                    float(p["shared_post_ratio"])
                    if p.get("shared_post_ratio") is not None
                    else None
                ),
            }
        writer.writerow(
            {
                "artist": p.get("artist") or default_artist,
                "username": uname,
                "has_profile_photo": "true" if p["has_profile_photo"] else "false",
                "post_count": p_count,
                "followers_count": flwrs,
                "following_count": flwing,
                "bio_length": bio_len,
                "account_private": "true" if p["account_private"] else "false",
                "username_digit_ratio": f"{compute_digit_ratio(uname):.4f}",
            }
        )

    if len(unscored) == len(collected):
        raise CSVValidationError(
            f"{len(collected)} profiles were captured, but none has all of its numbers yet "
            "(posts, followers, following, bio). Handle-only rows are never scored or guessed. "
            "Open each follower's profile with the extension active so it can read those numbers."
        )

    warnings: list[str] = []
    return ParsedCapture(
        out.getvalue(),
        burst_overrides,
        unscored,
        warnings,
        collected_at_map,
        feed_signals_map,
        hover_profiles,
    )


def _audit_capture(parsed: ParsedCapture, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the audit on a parsed capture, keeping track of the rows that were not scored."""
    return run_audit_from_csv_text(
        parsed.csv_text,
        config=config,
        burst_overrides=parsed.burst_overrides,
        unscored=parsed.unscored,
        extra_warnings=parsed.warnings,
        collected_at_map=parsed.collected_at_map,
        feed_signals_map=parsed.feed_signals_map,
        hover_profiles=parsed.hover_profiles,
    )


def _build_coverage(scored_df: Any, unscored: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per artist: how many scanned followers were scored and how many were not."""
    coverage: dict[str, dict[str, int]] = {}
    if not scored_df.empty:
        for artist, count in scored_df["artist"].astype(str).value_counts().items():
            coverage[str(artist)] = {"scanned": int(count), "complete": int(count), "incomplete": 0}
    for profile in unscored:
        bucket = coverage.setdefault(
            str(profile.get("artist") or "Audited_Account"),
            {"scanned": 0, "complete": 0, "incomplete": 0},
        )
        bucket["scanned"] += 1
        bucket["incomplete"] += 1
    return coverage


def _load_global_observed_profiles(
    cp_dir: Path | None = None,
    benchmarks_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Collect 100% real observed scorable profiles across checkpoints and verified_benchmarks.csv."""
    target_cp_dir = cp_dir if cp_dir is not None else CHECKPOINTS_DIR
    target_bench = benchmarks_path if benchmarks_path is not None else DEFAULT_BENCHMARK_SAMPLE_PATH

    bench_sig: tuple[int, int] | None = None
    try:
        if target_bench.is_file():
            st_b = target_bench.stat()
            bench_sig = (st_b.st_mtime_ns, st_b.st_size)
    except OSError:
        bench_sig = None

    cp_sig_list: list[tuple[str, int, int]] = []
    try:
        if target_cp_dir.is_dir():
            for p in sorted(target_cp_dir.glob("*.json")):
                try:
                    st_p = p.stat()
                    cp_sig_list.append((p.name, st_p.st_mtime_ns, st_p.st_size))
                except OSError:
                    continue
    except OSError:
        cp_sig_list = []

    sig = (str(target_cp_dir), str(target_bench), bench_sig, tuple(cp_sig_list))
    with _GLOBAL_OBS_CACHE_LOCK:
        if _GLOBAL_OBS_CACHE["sig"] == sig and _GLOBAL_OBS_CACHE["data"] is not None:
            return dict(_GLOBAL_OBS_CACHE["data"])

    observed: dict[str, dict[str, Any]] = {}

    if target_bench.is_file():
        try:
            with target_bench.open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    uname = extract_username(str(row.get("username") or "")).lower()
                    if not uname:
                        continue
                    try:
                        candidate: dict[str, Any] = {
                            "schema_version": SCHEMA_VERSION,
                            "username": uname,
                            "has_profile_photo": str(row["has_profile_photo"]).strip().lower()
                            == "true",
                            "post_count": int(row["post_count"]),
                            "followers_count": int(row["followers_count"]),
                            "following_count": int(row["following_count"]),
                            "bio_length": int(row["bio_length"]),
                            "account_private": str(row["account_private"]).strip().lower()
                            == "true",
                        }
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not is_legacy_placeholder(candidate) and is_scorable(candidate):
                        observed[uname] = candidate
        except OSError:
            pass

    if target_cp_dir.is_dir():
        for uname, best_row in best_profiles(target_cp_dir).items():
            if not is_legacy_placeholder(best_row) and (
                is_scorable(best_row) or has_header_counts(best_row)
            ):
                if uname in observed:
                    observed[uname] = merge_profiles(observed[uname], best_row)
                else:
                    observed[uname] = dict(best_row)

    with _GLOBAL_OBS_CACHE_LOCK:
        _GLOBAL_OBS_CACHE["sig"] = sig
        _GLOBAL_OBS_CACHE["data"] = dict(observed)

    return dict(observed)


def _collect_enriched_artist_profiles(
    artist: str,
    raw_profiles: list[dict[str, Any]],
    global_observed: dict[str, dict[str, Any]],
    label_notes: dict[str, LabelNote] | None = None,
) -> list[dict[str, Any]]:
    """Enrich an artist's captured followers with any real observed metrics already on disk."""
    clean_artist = re.sub(r"[^\w\-.]", "_", artist.strip().lstrip("@")) or "Audited_Account"
    artist_handle_low = clean_artist.lower()
    if artist_handle_low == "audited_account":
        artist_handle_low = ""
    by_uname: dict[str, dict[str, Any]] = {}

    for raw in raw_profiles:
        if not isinstance(raw, dict):
            continue
        uname = extract_username(str(raw.get("username") or ""))
        if not _is_valid_profile_username(uname):
            continue
        key = uname.lower()
        if artist_handle_low and key == artist_handle_low:
            continue
        scrubbed = scrub_legacy_profile(raw)
        scrubbed["artist"] = clean_artist
        scrubbed["username"] = uname
        if key in by_uname:
            merged = merge_profiles(by_uname[key], scrubbed)
            merged["artist"] = clean_artist
            by_uname[key] = merged
        else:
            by_uname[key] = scrubbed

    if label_notes:
        artist_mention_re = re.compile(rf"(?i)@{re.escape(clean_artist)}(?![a-z0-9._])")
        for note in label_notes.values():
            ev = note.evidence_note or ""
            # Audit Table labels annotate profiles already inside a checkpoint; never resurrect
            # removed non-followers from label_notes if they are absent from raw_profiles.
            if "marked from audit table" in ev.lower():
                continue
            if artist_mention_re.search(ev):
                key = note.username.lower()
                if artist_handle_low and key == artist_handle_low:
                    continue
                if key not in by_uname:
                    by_uname[key] = {
                        "schema_version": SCHEMA_VERSION,
                        "artist": clean_artist,
                        "username": note.username,
                        "has_profile_photo": None,
                        "post_count": None,
                        "followers_count": None,
                        "following_count": None,
                        "bio_length": None,
                        "account_private": None,
                        "post_timestamps": [],
                    }

    for key, prof in list(by_uname.items()):
        if not is_scorable(prof) and key in global_observed:
            obs = dict(global_observed[key])
            obs["artist"] = clean_artist
            obs["username"] = prof.get("username") or obs.get("username") or key
            enriched = merge_profiles(obs, prof)
            enriched["artist"] = clean_artist
            enriched["username"] = prof.get("username") or key
            by_uname[key] = enriched

    return list(by_uname.values())


def load_checkpoint_capture(
    artist: str, config: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Load an artist's checkpoint from disk and execute the audit capture on it."""
    clean_artist = re.sub(r"[^\w\-.]", "_", artist.strip().lstrip("@"))
    target_file = CHECKPOINTS_DIR / f"{clean_artist}.json"
    if not target_file.is_file():
        return None
    cp_data = load_artist_checkpoint(clean_artist)
    raw_profiles = cp_data.get("profiles") or []
    if not raw_profiles:
        return None

    global_obs = _load_global_observed_profiles()
    try:
        notes_map = load_label_notes()
    except (OSError, LabelError):
        notes_map = {}

    profiles = _collect_enriched_artist_profiles(
        clean_artist,
        raw_profiles,
        global_observed=global_obs,
        label_notes=notes_map,
    )
    if not profiles:
        return None

    try:
        parsed = parse_har_or_json_to_rows(
            json.dumps(profiles),
            default_artist=clean_artist,
        )
    except CSVValidationError:
        return None

    res = _audit_capture(parsed, config=config)
    res["source"] = "extension"
    res["checkpoint_meta"] = {
        "artist": clean_artist,
        "filename": target_file.name,
        "last_modified": datetime.datetime.fromtimestamp(
            target_file.stat().st_mtime, tz=datetime.UTC
        ).strftime("%Y-%m-%d %H:%M UTC"),
        "total_scanned": len(profiles),
        "checkpoint_saved": len(raw_profiles),
        "deep_checked": res.get("total_rows_loaded", 0),
    }
    for a_entry in res.get("artists") or []:
        if str(a_entry.get("artist") or "").lower() == clean_artist.lower():
            a_entry["checkpoint_saved"] = len(raw_profiles)
    return res


def build_memory_bank_payload() -> dict[str, Any]:
    """Build Step-0 unified memory bank: all confirmed Bots, Real People, and observed profiles."""
    labels_map: dict[str, dict[str, Any]] = {}
    try:
        lbls = load_labels()
        for h in sorted(lbls.humans):
            labels_map[h] = {
                "username": h,
                "label": "human",
                "status": "available",
                "labeled_on": "",
                "bot_type": None,
                "source": "verified_users",
            }
        for b in sorted(lbls.bots):
            labels_map[b] = {
                "username": b,
                "label": "bot",
                "status": "available",
                "labeled_on": "",
                "bot_type": None,
                "source": "verified_bots",
            }
        for uname_low, note in lbls.notes.items():
            if note.label in VALID_LABELS:
                labels_map[uname_low] = {
                    "username": uname_low,
                    "label": note.label,
                    "status": note.status,
                    "labeled_on": note.labeled_on or "",
                    "bot_type": note.bot_type,
                    "source": "label_notes",
                }
    except (OSError, LabelError):
        pass

    global_obs = _load_global_observed_profiles()
    observed_compact: dict[str, dict[str, Any]] = {}
    for uname_low, p in global_obs.items():
        bio_raw = p.get("bio_length")
        bio_val = (
            int(bio_raw)
            if isinstance(bio_raw, int) and not isinstance(bio_raw, bool) and bio_raw >= 0
            else None
        )
        pic_raw = p.get("has_profile_photo")
        priv_raw = p.get("account_private")
        observed_compact[uname_low] = {
            "has_profile_photo": pic_raw if isinstance(pic_raw, bool) else None,
            "account_private": priv_raw if isinstance(priv_raw, bool) else None,
            "post_count": int(p["post_count"]),
            "followers_count": int(p["followers_count"]),
            "following_count": int(p["following_count"]),
            "bio_length": bio_val,
        }

    bots_n = sum(1 for item in labels_map.values() if item["label"] == "bot")
    humans_n = sum(1 for item in labels_map.values() if item["label"] == "human")
    unsure_n = sum(1 for item in labels_map.values() if item["label"] == "unsure")

    return {
        "ok": True,
        "totals": {
            "confirmed_bots": bots_n,
            "confirmed_humans": humans_n,
            "confirmed_unsure": unsure_n,
            "total_confirmed": len(labels_map),
            "observed_profiles_count": len(observed_compact),
            "verified_bots": bots_n,
            "verified_users": humans_n,
            "observed_profiles": len(observed_compact),
        },
        "verified_bots": sorted([u for u, it in labels_map.items() if it["label"] == "bot"]),
        "verified_users": sorted([u for u, it in labels_map.items() if it["label"] == "human"]),
        "label_notes": {u: it["label"] for u, it in labels_map.items() if it.get("source") == "label_notes"},
        "labels": labels_map,
        "observed_profiles": observed_compact,
    }


_ARCHETYPE_ORDER: tuple[str, ...] = (
    "private_shell",
    "auto_flagged",
    "dressed_up_other",
    "mass_follower",
    "burst_or_zero",
    "unscored_confirmed",
)


def _evaluate_profile_score(
    p: dict[str, Any],
    active_config: dict[str, Any],
    known_bots: set[str],
    known_humans: set[str],
) -> dict[str, Any]:
    """Score one scorable or header-observed profile identically across Audit, History, and Mindmap."""
    weights = active_config["weights"]
    burst_weight = float(active_config.get("same_day_burst_weight", 0.15))
    extreme_mass_weight = float(active_config.get("extreme_mass_following_weight", 0.30))
    follow_exchange_weight = float(active_config.get("follow_exchange_ring_weight", 0.20))
    low_audience_weight = float(active_config.get("low_audience_imbalance_weight", 0.15))
    shared_post_weight = float(active_config.get("shared_post_bot_weight", 0.20))
    stitched_weight = float(active_config.get("stitched_username_weight", 0.10))
    ratio_cluster_cap = float(active_config.get("ratio_cluster_cap", 0.60))
    suspicious_floor_pts = round(float(active_config.get("suspicious_cutoff", 0.35)) * 100)
    registry_boost = float(active_config.get("known_registry_bot_boost", 0.25))
    enable_reg = bool(active_config.get("enable_registry_boost", False))
    score_private = bool(active_config.get("score_private_accounts", True))
    min_following = int(active_config["mass_following_min_following"])
    max_followers = int(active_config["mass_following_max_followers"])
    digit_threshold = float(active_config["digit_ratio_threshold"])
    cutoff = float(active_config["bot_like_cutoff"])

    uname = str(p.get("username") or "")
    u_low = uname.lower()
    pic_raw = p.get("has_profile_photo")
    has_pic: bool | None = pic_raw if isinstance(pic_raw, bool) else None
    priv_raw = p.get("account_private")
    is_priv: bool | None = priv_raw if isinstance(priv_raw, bool) else None
    p_count = int(p["post_count"])
    flwrs = int(p["followers_count"])
    flwing = int(p["following_count"])
    bio_raw = p.get("bio_length")
    bio_len: int | None = (
        int(bio_raw)
        if isinstance(bio_raw, int) and not isinstance(bio_raw, bool) and bio_raw >= 0
        else None
    )
    d_ratio = compute_digit_ratio(uname)
    hf = analyze_handle_forensics(uname, digit_threshold=digit_threshold)

    no_photo_tri: bool | None = None if has_pic is None else (not has_pic)
    digit_heavy = (
        (d_ratio > digit_threshold)
        or hf["is_word_dot_digits"]
        or hf["has_long_digit_run"]
    )
    stitched_only = bool(hf["is_stitched_handle"]) and not digit_heavy
    is_known_bot = u_low in known_bots
    is_known_human = u_low in known_humans

    other_unknown_score = float(weights["no_photo"]) if no_photo_tri is None else 0.0
    if (is_priv is None) and (not score_private):
        zero_p_tri: bool | None = None
        burst_p: bool | None = None
        empty_b: bool | None = None
        mass_f_tri: bool | None = None
        extreme_mass_tri: bool | None = None
        follow_exchange_tri: bool | None = None
        low_aud: bool | None = None
        shared_post: bool | None = None
        shell_bonus = 0.0
        zero_burst_cap = max(
            float(weights["zero_posts"]) + (burst_weight if flwing >= 1000 else 0.0),
            burst_weight,
        )
        other_unknown_score += (
            zero_burst_cap
            + follow_exchange_weight
            + float(weights["empty_bio"])
        )
    elif is_priv is True and not score_private:
        zero_p_tri = False
        burst_p = False
        empty_b = False
        mass_f_tri = False
        extreme_mass_tri = False
        follow_exchange_tri = False
        low_aud = False
        shared_post = False
        shell_bonus = 0.0
    else:
        zero_p_tri = p_count == 0
        ts_raw = p.get("post_timestamps")
        ts_list = ts_raw if isinstance(ts_raw, list) else None
        if zero_p_tri:
            burst_p = False
        elif p_count < 1 or p_count > 6:
            burst_p = False
        elif bio_len is not None:
            burst_p = detect_same_day_post_burst(
                post_count=p_count,
                post_timestamps=ts_list,
                followers_count=flwrs,
                following_count=flwing,
                bio_length=bio_len,
                username=uname,
                digit_threshold=digit_threshold,
            )
        else:
            clean_ts = (
                [float(t) for t in ts_list if isinstance(t, (int, float)) and t > 0]
                if ts_list
                else []
            )
            if len(clean_ts) >= 2:
                burst_p = (max(clean_ts) - min(clean_ts)) <= 172800.0
            else:
                f_ratio = flwing / max(1, flwrs)
                if (flwing >= 350 and f_ratio >= 8.0) or (flwrs <= 40 and flwing >= 80):
                    burst_p = True
                elif not (
                    hf["is_word_dot_digits"]
                    or hf["is_digit_heavy"]
                    or hf["has_long_digit_run"]
                    or hf["is_stitched_handle"]
                ):
                    burst_p = False
                else:
                    burst_p = None

        empty_b = None if bio_len is None else (bio_len <= 4)
        mass_f_tri = is_extended_mass_following(
            following=flwing,
            followers=flwrs,
            post_count=p_count,
            min_following=min_following,
            max_followers=max_followers,
        )
        extreme_mass_tri = is_extreme_mass_following(
            following=flwing,
            followers=flwrs,
            post_count=p_count,
        )
        follow_exchange_tri = is_follow_exchange_ring(
            following=flwing,
            followers=flwrs,
            post_count=p_count,
        )

        f_ratio = flwing / max(1, flwrs)
        if bio_len is not None:
            low_aud = is_low_audience_imbalance(
                post_count=p_count,
                followers=flwrs,
                following=flwing,
                bio_length=bio_len,
            )
        else:
            non_bio_low_aud = (
                (p_count <= 25 and flwrs <= 80 and flwing >= 150)
                or (p_count <= 25 and flwing >= 3500 and f_ratio >= 2.5)
            )
            bio_low_aud_cond = p_count <= 25 and flwrs <= 100
            if non_bio_low_aud:
                low_aud = True
            elif not bio_low_aud_cond:
                low_aud = False
            else:
                low_aud = None

        obs_posts = (
            int(p["observed_posts_count"]) if p.get("observed_posts_count") is not None else None
        )
        avg_likes = float(p["avg_post_likes"]) if p.get("avg_post_likes") is not None else None
        shared_ratio = (
            float(p["shared_post_ratio"]) if p.get("shared_post_ratio") is not None else None
        )
        if bio_len is not None:
            shared_post = is_shared_post_bot_signal(
                post_count=p_count,
                followers=flwrs,
                following=flwing,
                bio_length=bio_len,
                username=uname,
                digit_ratio=d_ratio,
                is_stitched=hf["is_stitched_handle"],
                observed_posts_count=obs_posts,
                avg_post_likes=avg_likes,
                shared_post_ratio=shared_ratio,
            )
        else:
            m_trail = re.search(r"\d+$", uname.strip())
            trailing_digits = len(m_trail.group(0)) if m_trail else 0
            non_bio_profile_sig = (
                1 <= p_count <= 30 and flwrs <= 150 and flwing >= 300 and f_ratio >= 4.0
            )
            bio_profile_cond = (
                (1 <= p_count <= 25 and flwing >= 1500 and f_ratio >= 3.0)
                or (
                    7 <= p_count <= 35
                    and flwrs <= 200
                    and (trailing_digits >= 3 or d_ratio > 0.25 or hf["is_stitched_handle"])
                )
            )
            feed_sig = (
                obs_posts is not None
                and obs_posts >= 3
                and (
                    (shared_ratio is not None and shared_ratio >= 0.70 and p_count <= 60)
                    or (
                        flwrs >= 500
                        and p_count >= 5
                        and avg_likes is not None
                        and (avg_likes / max(1, flwrs)) < 0.003
                    )
                )
            )
            if non_bio_profile_sig or feed_sig:
                shared_post = True
            elif not bio_profile_cond:
                shared_post = False
            else:
                shared_post = None

        shell_bonus = burst_weight if (zero_p_tri is True and mass_f_tri is True and flwing >= 1000) else 0.0
        if burst_p is None:
            other_unknown_score += burst_weight
        if empty_b is None:
            other_unknown_score += float(weights["empty_bio"])

    zero_p = bool(zero_p_tri is True)
    mass_f = bool(mass_f_tri is True)
    extreme_mass = bool(extreme_mass_tri is True)
    follow_exchange = bool(follow_exchange_tri is True)
    no_photo = bool(no_photo_tri is True)

    ratio_known_raw = 0.0
    ratio_unknown_raw = 0.0
    for r_flag, r_wt in (
        (mass_f_tri, float(weights["mass_following"])),
        (extreme_mass_tri, extreme_mass_weight),
        (low_aud, low_audience_weight),
        (shared_post, shared_post_weight),
    ):
        if r_flag is True:
            ratio_known_raw += r_wt
        elif r_flag is None:
            ratio_unknown_raw += r_wt
    ratio_cluster_effective = min(ratio_cluster_cap, ratio_known_raw)
    ratio_cluster_max_eff = min(ratio_cluster_cap, ratio_known_raw + ratio_unknown_raw)
    ratio_unknown_eff = max(0.0, ratio_cluster_max_eff - ratio_cluster_effective)
    unknown_score = other_unknown_score + ratio_unknown_eff
    ratio_cluster_capped = ratio_known_raw > (ratio_cluster_cap + 1e-9)

    reg_bonus = registry_boost if (is_known_bot and enable_reg) else 0.0
    raw_score = (
        ratio_cluster_effective
        + (float(weights["no_photo"]) if no_photo else 0.0)
        + (float(weights["zero_posts"]) if zero_p else 0.0)
        + (burst_weight if burst_p is True else 0.0)
        + shell_bonus
        + (follow_exchange_weight if follow_exchange else 0.0)
        + (float(weights["empty_bio"]) if empty_b is True else 0.0)
        + (float(weights["digit_heavy_username"]) if digit_heavy else 0.0)
        + (stitched_weight if stitched_only else 0.0)
        + reg_bonus
    )
    final_score = round(min(1.0, raw_score), 6)
    score_pts = round(final_score * 100)
    max_score = round(min(1.0, raw_score + unknown_score), 6)
    max_score_pts = round(max_score * 100)
    is_full_complete = (bio_len is not None) and (has_pic is not None) and (is_priv is not None)
    raw_bot_like = final_score > cutoff
    # On passive hover without full observation, stay uncommitted for CHECK (>=35p) cases.
    conclusive_auto = (
        raw_bot_like
        or is_full_complete
        or (max_score_pts <= 34 and max_score <= cutoff)
    )
    conclusive = conclusive_auto or is_known_bot or is_known_human
    is_auto = raw_bot_like and not is_known_human
    is_effective_bot = (is_auto or is_known_bot) and not is_known_human
    if is_known_human:
        verdict_tier = "clean"
    elif is_effective_bot:
        verdict_tier = "bot"
    elif not conclusive:
        verdict_tier = "unknown"
    elif is_full_complete and score_pts >= suspicious_floor_pts:
        verdict_tier = "suspicious"
    else:
        verdict_tier = "clean"

    traits: list[str] = []
    if is_auto:
        traits.append("auto_gt60")
    if is_priv is True:
        traits.append("private")
    if flwing >= 1000 or mass_f or extreme_mass:
        traits.append("mass_following")
    if zero_p or p_count == 0:
        traits.append("zero_posts")
    elif 1 <= p_count <= 6:
        traits.append("burst_1_6")
    if digit_heavy or stitched_only:
        traits.append("digit_heavy")
    if no_photo:
        traits.append("no_photo")

    flags_dict = {
        "no_photo": bool(no_photo),
        "zero_posts": bool(zero_p),
        "mass_following": bool(mass_f),
        "empty_bio": bool(empty_b is True),
        "digit_heavy_username": bool(digit_heavy),
        "stitched_username": bool(stitched_only),
        "same_day_post_burst": bool(burst_p is True),
        "blank_shell_bonus": bool(shell_bonus > 0.0),
        "extreme_mass_following": bool(extreme_mass),
        "follow_exchange_ring": bool(follow_exchange),
        "low_audience_imbalance": bool(low_aud is True),
        "shared_post_bot_signal": bool(shared_post is True),
        "ratio_cluster_capped": bool(ratio_cluster_capped),
        "known_registry_bot": bool(is_known_bot),
        "registry_boost_applied": bool(reg_bonus > 0.0),
    }

    return {
        "final_score": final_score,
        "score_pts": score_pts,
        "max_score": max_score,
        "max_score_pts": max_score_pts,
        "raw_score_pts": round(raw_score * 100),
        "ratio_cluster_raw_pts": round(ratio_known_raw * 100),
        "ratio_cluster_pts": round(ratio_cluster_effective * 100),
        "ratio_cluster_capped": bool(ratio_cluster_capped),
        "verdict_tier": verdict_tier,
        "is_suspicious": bool(verdict_tier == "suspicious"),
        "conclusive": conclusive,
        "conclusive_auto": conclusive_auto,
        "raw_bot_like": raw_bot_like,
        "is_auto": is_auto,
        "is_effective_bot": is_effective_bot,
        "is_known_bot": is_known_bot,
        "is_known_human": is_known_human,
        "has_pic": has_pic,
        "is_priv": is_priv,
        "post_count": p_count,
        "followers_count": flwrs,
        "following_count": flwing,
        "bio_length": bio_len,
        "flags": flags_dict,
        "forensics": hf,
        "traits": traits,
    }


_ARCHETYPE_KEYS: tuple[str, ...] = (
    "private_shell",
    "auto_flagged",
    "dressed_up_other",
    "mass_follower",
    "burst_or_zero",
    "unscored_confirmed",
)


def _classify_bot_archetype(eval_res: dict[str, Any] | None) -> str:
    """Assign each bot a clear primary archetype for mindmap clustering."""
    if eval_res is None:
        return "unscored_confirmed"
    if eval_res["is_auto"]:
        return "auto_flagged"
    if eval_res["is_priv"] is True:
        return "private_shell"
    if eval_res["following_count"] >= 1000:
        return "mass_follower"
    if eval_res["post_count"] <= 6:
        return "burst_or_zero"
    return "dressed_up_other"


def _classify_network_account_role(
    shared_bot_count: int,
    bot_pct: float | None,
    effective_bot_count: int,
) -> str:
    """Classify an audited account without mislabeling high-bot newcomers as clean controls."""
    if shared_bot_count > 0:
        return "connected"
    if (bot_pct is not None and bot_pct >= 10.0) or effective_bot_count >= 5:
        return "independent_bot_affected"
    return "clean_control"


def _archetype_cosine_similarity(
    vec_a: dict[str, int],
    vec_b: dict[str, int],
    min_bots: int = 5,
) -> float | None:
    """Compute cosine similarity between two accounts' bot-archetype distributions."""
    total_a = sum(int(vec_a.get(k, 0)) for k in _ARCHETYPE_KEYS)
    total_b = sum(int(vec_b.get(k, 0)) for k in _ARCHETYPE_KEYS)
    if total_a < min_bots or total_b < min_bots:
        return None
    dot = sum(int(vec_a.get(k, 0)) * int(vec_b.get(k, 0)) for k in _ARCHETYPE_KEYS)
    norm_a = math.sqrt(sum(int(vec_a.get(k, 0)) ** 2 for k in _ARCHETYPE_KEYS))
    norm_b = math.sqrt(sum(int(vec_b.get(k, 0)) ** 2 for k in _ARCHETYPE_KEYS))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return None
    return dot / (norm_a * norm_b)


def _compute_history_breakdown(
    enriched: list[dict[str, Any]],
    active_config: dict[str, Any],
    known_bots: set[str],
    known_humans: set[str],
) -> dict[str, Any]:
    """Compute exact 3-tier breakdown (Bots, Suspicious 35-60p, Clean) and confirmed counts."""
    complete_count = 0
    bot_like_count = 0
    effective_bot_count = 0
    suspicious_count = 0
    photo_count = 0
    conf_bots = 0
    conf_humans = 0

    for p in enriched:
        uname = str(p.get("username") or "")
        u_low = uname.lower()
        if u_low in known_bots:
            conf_bots += 1
        elif u_low in known_humans:
            conf_humans += 1

        if not (is_scorable(p) or has_header_counts(p)):
            continue

        ev = _evaluate_profile_score(p, active_config, known_bots, known_humans)
        if not ev["conclusive"]:
            continue
        complete_count += 1
        if ev["has_pic"] is True:
            photo_count += 1
        if ev["is_auto"]:
            bot_like_count += 1
        if ev["verdict_tier"] == "bot":
            effective_bot_count += 1
        elif ev["verdict_tier"] == "suspicious":
            suspicious_count += 1

    real_count = max(0, complete_count - effective_bot_count - suspicious_count)
    bot_pct = round(effective_bot_count / complete_count * 100, 1) if complete_count > 0 else None
    suspicious_pct = round(suspicious_count / complete_count * 100, 1) if complete_count > 0 else None
    clean_pct = round(real_count / complete_count * 100, 1) if complete_count > 0 else None
    auto_bot_pct = round(bot_like_count / complete_count * 100, 1) if complete_count > 0 else None
    return {
        "deep_checked": complete_count,
        "bot_like_count": bot_like_count,
        "effective_bot_count": effective_bot_count,
        "suspicious_count": suspicious_count,
        "real_count": real_count,
        "bot_pct": bot_pct,
        "suspicious_pct": suspicious_pct,
        "clean_pct": clean_pct,
        "auto_bot_pct": auto_bot_pct,
        "photo_count": photo_count,
        "confirmed_bots": conf_bots,
        "confirmed_humans": conf_humans,
    }


def _compute_artist_live_counts(
    artist: str,
    *,
    preloaded_labels: Any = None,
) -> dict[str, Any]:
    """Compute authoritative single-artist 3-tier KPI breakdown for fast multi-tab extension sync."""
    clean_artist = (artist or "").strip().lstrip("@")
    if not clean_artist or clean_artist.lower() == "audited_account":
        return {}
    cp_data = load_artist_checkpoint(clean_artist)
    raw_profiles = cp_data.get("profiles") or []
    raw_list = list(raw_profiles.values()) if isinstance(raw_profiles, dict) else list(raw_profiles)
    if preloaded_labels is not None:
        notes_map = getattr(preloaded_labels, "notes", {}) or {}
        known_bots = set(getattr(preloaded_labels, "bots", ()) or ())
        known_humans = set(getattr(preloaded_labels, "humans", ()) or ())
        try:
            active_cfg = validate_config_dict(load_config())
        except ConfigError:
            active_cfg = None
    else:
        notes_map, known_bots, known_humans, _lbls, active_cfg = _load_labels_and_config_safe()
    global_obs = _load_global_observed_profiles()
    enriched = _collect_enriched_artist_profiles(
        clean_artist,
        raw_list,
        global_observed=global_obs,
        label_notes=notes_map,
    )
    checked_count = 0
    bots_count = 0
    suspicious_count = 0
    confirmed_bots = 0
    auto_bots = 0
    confirmed_humans = 0
    burst_count = 0
    priv_bots_count = 0
    if active_cfg is not None:
        for p in enriched:
            if not (is_scorable(p) or has_header_counts(p)):
                continue
            ev = _evaluate_profile_score(p, active_cfg, known_bots, known_humans)
            if not ev["conclusive"]:
                continue
            checked_count += 1
            if ev["verdict_tier"] == "bot":
                bots_count += 1
                if ev["is_known_bot"]:
                    confirmed_bots += 1
                elif ev["is_auto"]:
                    auto_bots += 1
                if ev["is_priv"] is True:
                    priv_bots_count += 1
            elif ev["verdict_tier"] == "suspicious":
                suspicious_count += 1
            elif ev["is_known_human"]:
                confirmed_humans += 1
            if ev["flags"].get("same_day_post_burst") is True:
                burst_count += 1
    humans_count = max(0, checked_count - bots_count - suspicious_count)
    auto_humans = max(0, humans_count - confirmed_humans)
    rev = _WORKER_ENGINE.get_status_snapshot()["revision"]
    return {
        "artist": clean_artist,
        "total_saved": len(raw_list),
        "checked_count": checked_count,
        "bots_count": bots_count,
        "suspicious_count": suspicious_count,
        "humans_count": humans_count,
        "confirmed_bots": confirmed_bots,
        "auto_bots": auto_bots,
        "confirmed_humans": confirmed_humans,
        "auto_humans": auto_humans,
        "burst_count": burst_count,
        "priv_bots_count": priv_bots_count,
        "revision": rev,
    }


def _load_labels_and_config_safe() -> tuple[dict[str, LabelNote], set[str], set[str], Any, dict[str, Any] | None]:
    """Load labels, notes, and validated config with safe fallbacks."""
    try:
        lbls = load_labels()
        notes_map = lbls.notes
        known_bots = set(lbls.bots)
        known_humans = set(lbls.humans)
    except (OSError, LabelError):
        lbls = None
        notes_map = {}
        known_bots = set()
        known_humans = set()
    try:
        active_cfg = validate_config_dict(load_config())
    except ConfigError:
        active_cfg = None
    return notes_map, known_bots, known_humans, lbls, active_cfg


def build_bot_network_payload() -> dict[str, Any]:
    """Build cross-account shared-bot & exclusive-bot mindmap graph from real checkpoints."""
    generated_at = datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    if not CHECKPOINTS_DIR.is_dir():
        return {
            "ok": True,
            "summary": {
                "total_accounts": 0,
                "connected_accounts_count": 0,
                "independent_bot_accounts_count": 0,
                "clean_control_accounts_count": 0,
                "total_clusters": 0,
                "total_shared_bots": 0,
                "total_unique_bots_all_accounts": 0,
                "total_bridges": 0,
                "total_shared_links": 0,
                "generated_at": generated_at,
            },
            "accounts": [],
            "clusters": [],
            "bridges": [],
            "shared_bots": [],
            "exclusive_bots_by_account": {},
        }

    cp_files = sorted(
        CHECKPOINTS_DIR.glob("*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    global_obs = _load_global_observed_profiles()
    notes_map, known_bots, known_humans, lbls, active_cfg = _load_labels_and_config_safe()
    conf_level = float(active_cfg["confidence_level"]) if active_cfg is not None else 0.95

    handle_accounts: dict[str, set[str]] = {}
    bot_accounts: dict[str, set[str]] = {}
    human_accounts: dict[str, set[str]] = {}
    display_username: dict[str, str] = {}
    best_eval: dict[str, dict[str, Any] | None] = {}
    raw_accounts: dict[str, dict[str, Any]] = {}

    for cp_file in cp_files:
        try:
            cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_profiles = cp_data.get("profiles") or []
        if isinstance(raw_profiles, dict):
            raw_list = list(raw_profiles.values())
        else:
            raw_list = list(raw_profiles)
        artist_name = str(cp_data.get("artist") or cp_file.stem)
        enriched = _collect_enriched_artist_profiles(
            artist_name,
            raw_list,
            global_observed=global_obs,
            label_notes=notes_map,
        )

        if active_cfg is not None:
            breakdown = _compute_history_breakdown(
                enriched, active_cfg, known_bots, known_humans
            )
        else:
            complete_count = sum(1 for p in enriched if is_scorable(p))
            breakdown = {
                "deep_checked": complete_count,
                "bot_like_count": 0,
                "effective_bot_count": 0,
                "real_count": complete_count,
                "bot_pct": 0.0 if complete_count > 0 else None,
                "auto_bot_pct": None,
                "photo_count": 0,
                "confirmed_bots": sum(
                    1
                    for p in enriched
                    if str(p.get("username") or "").lower() in known_bots
                ),
                "confirmed_humans": sum(
                    1
                    for p in enriched
                    if str(p.get("username") or "").lower() in known_humans
                ),
            }

        deep_checked = int(breakdown["deep_checked"])
        eff_bots = int(breakdown["effective_bot_count"])
        ci_low, ci_high = compute_wilson_ci(
            eff_bots, deep_checked, confidence_level=conf_level
        )
        raw_accounts[artist_name] = {
            "artist": artist_name,
            "checkpoint_saved": len(raw_list),
            "deep_checked": deep_checked,
            "effective_bot_count": eff_bots,
            "confirmed_bot_count": int(breakdown["confirmed_bots"]),
            "auto_bot_count": int(breakdown["bot_like_count"]),
            "suspicious_count": int(breakdown.get("suspicious_count", 0)),
            "real_count": int(breakdown["real_count"]),
            "bot_pct": breakdown["bot_pct"],
            "suspicious_pct": breakdown.get("suspicious_pct"),
            "clean_pct": breakdown.get("clean_pct"),
            "ci_low": round(ci_low * 100, 1),
            "ci_high": round(ci_high * 100, 1),
        }

        for p in enriched:
            uname = str(p.get("username") or "").strip()
            if not uname:
                continue
            u_low = uname.lower()
            display_username.setdefault(u_low, uname)
            handle_accounts.setdefault(u_low, set()).add(artist_name)
            if u_low in known_humans:
                human_accounts.setdefault(u_low, set()).add(artist_name)

            ev: dict[str, Any] | None = None
            if active_cfg is not None and (is_scorable(p) or has_header_counts(p)):
                cand_ev = _evaluate_profile_score(p, active_cfg, known_bots, known_humans)
                if cand_ev["conclusive"]:
                    ev = cand_ev
                    prev_ev = best_eval.get(u_low)
                    cand_complete = (
                        ev.get("bio_length") is not None
                        and ev.get("has_pic") is not None
                        and ev.get("is_priv") is not None
                    )
                    prev_complete = bool(
                        prev_ev is not None
                        and prev_ev.get("bio_length") is not None
                        and prev_ev.get("has_pic") is not None
                        and prev_ev.get("is_priv") is not None
                    )
                    if prev_ev is None or (cand_complete and not prev_complete):
                        best_eval[u_low] = ev
            elif u_low not in best_eval:
                best_eval[u_low] = None

            is_bot_here = bool(
                (ev is not None and ev["is_effective_bot"])
                or (u_low in known_bots and u_low not in known_humans)
            )
            if is_bot_here:
                bot_accounts.setdefault(u_low, set()).add(artist_name)

    bridges_grouped: dict[tuple[str, ...], list[str]] = {}
    shared_bots: list[dict[str, Any]] = []
    exclusive_bots_by_account: dict[str, list[dict[str, Any]]] = {
        art: [] for art in raw_accounts
    }
    exclusive_archetypes: dict[str, dict[str, int]] = {art: {} for art in raw_accounts}
    all_bot_archetypes: dict[str, dict[str, int]] = {art: {} for art in raw_accounts}
    shared_count_by_account: dict[str, int] = {art: 0 for art in raw_accounts}

    def _bot_record(u_low: str, accounts_list: list[str], bridge_id: str | None) -> dict[str, Any]:
        ev = best_eval.get(u_low)
        arch = _classify_bot_archetype(ev)
        note_obj = notes_map.get(u_low)
        manual_lbl = lbls.label_of(u_low) if lbls else None
        is_conf_bot = u_low in known_bots
        base = {
            "username": display_username.get(u_low, u_low),
            "archetype": arch,
            "manual_label": manual_lbl,
            "is_confirmed_bot": is_conf_bot,
            "is_auto_bot": bool(ev["is_auto"]) if ev else False,
            "score_pts": int(ev["score_pts"]) if ev else None,
            "post_count": int(ev["post_count"]) if ev else None,
            "followers_count": int(ev["followers_count"]) if ev else None,
            "following_count": int(ev["following_count"]) if ev else None,
            "bio_length": (
                int(ev["bio_length"]) if (ev and ev.get("bio_length") is not None) else None
            ),
            "account_private": (
                ev["is_priv"] if (ev and isinstance(ev.get("is_priv"), bool)) else None
            ),
            "has_profile_photo": (
                ev["has_pic"] if (ev and isinstance(ev.get("has_pic"), bool)) else None
            ),
            "labeled_on": (note_obj.labeled_on or "") if note_obj else "",
            "traits": list(ev["traits"]) if ev else [],
        }
        if bridge_id is not None:
            base["accounts"] = accounts_list
            base["account_count"] = len(accounts_list)
            base["bridge_id"] = bridge_id
        return base

    for u_low, arts in bot_accounts.items():
        arch_key = _classify_bot_archetype(best_eval.get(u_low))
        for art in arts:
            art_arch = all_bot_archetypes.setdefault(art, {})
            art_arch[arch_key] = art_arch.get(arch_key, 0) + 1
        if len(arts) >= 2:
            bridge_key = tuple(sorted(arts))
            bridge_id = "+".join(bridge_key)
            bridges_grouped.setdefault(bridge_key, []).append(u_low)
            for art in bridge_key:
                shared_count_by_account[art] = shared_count_by_account.get(art, 0) + 1
            shared_bots.append(_bot_record(u_low, list(bridge_key), bridge_id))
        elif len(arts) == 1:
            sole_art = next(iter(arts))
            rec = _bot_record(u_low, [sole_art], None)
            exclusive_bots_by_account.setdefault(sole_art, []).append(rec)
            arch_map = exclusive_archetypes.setdefault(sole_art, {})
            arch_map[rec["archetype"]] = arch_map.get(rec["archetype"], 0) + 1

    shared_bots.sort(
        key=lambda b: (
            -int(b["account_count"]),
            -(b["score_pts"] if b["score_pts"] is not None else -1),
            b["username"].lower(),
        )
    )
    for art, blist in exclusive_bots_by_account.items():
        blist.sort(
            key=lambda b: (
                -(b["score_pts"] if b["score_pts"] is not None else -1),
                b["username"].lower(),
            )
        )

    bridges: list[dict[str, Any]] = []
    for bridge_key, u_list in bridges_grouped.items():
        bridge_id = "+".join(bridge_key)
        key_set = set(bridge_key)
        arch_counts: dict[str, int] = {}
        for u_low in u_list:
            arch = _classify_bot_archetype(best_eval.get(u_low))
            arch_counts[arch] = arch_counts.get(arch, 0) + 1
        shared_humans_cnt = sum(
            1 for h_arts in human_accounts.values() if key_set.issubset(h_arts)
        )
        shared_total_cnt = sum(
            1 for all_arts in handle_accounts.values() if key_set.issubset(all_arts)
        )
        bridges.append(
            {
                "id": bridge_id,
                "accounts": list(bridge_key),
                "shared_bots_count": len(u_list),
                "shared_humans_count": shared_humans_cnt,
                "shared_total_count": shared_total_cnt,
                "archetypes": arch_counts,
            }
        )
    bridges.sort(key=lambda br: (-int(br["shared_bots_count"]), str(br["id"])))

    all_account_names = list(raw_accounts.keys())
    conn_adj: dict[str, set[str]] = {
        art: set() for art in all_account_names if shared_count_by_account.get(art, 0) > 0
    }
    for br in bridges:
        br_accs = [a for a in br["accounts"] if a in conn_adj]
        for a in br_accs:
            for b in br_accs:
                if a != b:
                    conn_adj[a].add(b)

    raw_clusters: list[dict[str, Any]] = []
    visited_conn: set[str] = set()
    ordered_conn = sorted(
        conn_adj.keys(),
        key=lambda a: (
            -shared_count_by_account.get(a, 0),
            -int(raw_accounts[a]["effective_bot_count"]),
            a,
        ),
    )
    for start_art in ordered_conn:
        if start_art in visited_conn:
            continue
        comp_accs: list[str] = []
        queue: deque[str] = deque([start_art])
        visited_conn.add(start_art)
        while queue:
            cur = queue.popleft()
            comp_accs.append(cur)
            for nxt in sorted(conn_adj[cur]):
                if nxt not in visited_conn:
                    visited_conn.add(nxt)
                    queue.append(nxt)
        comp_accs.sort(
            key=lambda a: (
                -shared_count_by_account.get(a, 0),
                -int(raw_accounts[a]["effective_bot_count"]),
                a,
            )
        )
        comp_set = set(comp_accs)
        comp_bridges = [br for br in bridges if comp_set.intersection(br["accounts"])]
        raw_clusters.append(
            {
                "accounts": comp_accs,
                "bridges_count": len(comp_bridges),
                "shared_bots_count": sum(int(br["shared_bots_count"]) for br in comp_bridges),
            }
        )

    raw_clusters.sort(
        key=lambda c: (-int(c["shared_bots_count"]), -len(c["accounts"]), c["accounts"][0])
    )
    clusters: list[dict[str, Any]] = []
    cluster_id_by_account: dict[str, int] = {}
    for idx, cl in enumerate(raw_clusters, start=1):
        clusters.append({"id": idx, **cl})
        for art in cl["accounts"]:
            cluster_id_by_account[art] = idx

    role_by_account: dict[str, str] = {
        art: _classify_network_account_role(
            shared_count_by_account.get(art, 0),
            raw_accounts[art]["bot_pct"],
            int(raw_accounts[art]["effective_bot_count"]),
        )
        for art in all_account_names
    }

    accounts_payload: list[dict[str, Any]] = []
    for art in all_account_names:
        info = dict(raw_accounts[art])
        connected_to: list[dict[str, Any]] = []
        pair_shared_bots: dict[str, int] = {}
        for other in all_account_names:
            if other == art:
                continue
            pair = {art, other}
            sh_bots = sum(1 for b_arts in bot_accounts.values() if pair.issubset(b_arts))
            pair_shared_bots[other] = sh_bots
            sh_humans = sum(1 for h_arts in human_accounts.values() if pair.issubset(h_arts))
            sh_total = sum(1 for a_arts in handle_accounts.values() if pair.issubset(a_arts))
            if sh_bots > 0 or sh_total > 0:
                connected_to.append(
                    {
                        "artist": other,
                        "shared_bots": sh_bots,
                        "shared_humans": sh_humans,
                        "shared_total": sh_total,
                    }
                )
        connected_to.sort(key=lambda c: (-int(c["shared_bots"]), -int(c["shared_total"]), c["artist"]))

        pattern_matches: list[dict[str, Any]] = []
        vec_a = all_bot_archetypes.get(art, {})
        for other in all_account_names:
            if other == art:
                continue
            vec_b = all_bot_archetypes.get(other, {})
            sim = _archetype_cosine_similarity(vec_a, vec_b)
            if sim is None:
                continue
            dom_arch = max(vec_b.items(), key=lambda kv: (kv[1], kv[0]))[0] if vec_b else None
            pattern_matches.append(
                {
                    "artist": other,
                    "similarity_pct": round(sim * 100, 1),
                    "shared_bots": pair_shared_bots.get(other, 0),
                    "other_bot_pct": raw_accounts[other]["bot_pct"],
                    "other_role": role_by_account[other],
                    "dominant_archetype": dom_arch,
                }
            )
        pattern_matches.sort(
            key=lambda m: (-float(m["similarity_pct"]), -int(m["shared_bots"]), str(m["artist"]))
        )

        shared_cnt = shared_count_by_account.get(art, 0)
        excl_cnt = len(exclusive_bots_by_account.get(art, []))
        total_net_bots = shared_cnt + excl_cnt
        unscored_conf_bots = max(0, total_net_bots - int(info["effective_bot_count"]))
        info["shared_bot_count"] = shared_cnt
        info["exclusive_bot_count"] = excl_cnt
        info["total_network_bots"] = total_net_bots
        info["unscored_confirmed_bot_count"] = unscored_conf_bots
        info["exclusive_archetypes"] = exclusive_archetypes.get(art, {})
        info["all_bot_archetypes"] = all_bot_archetypes.get(art, {})
        info["account_role"] = role_by_account[art]
        info["cluster_id"] = cluster_id_by_account.get(art)
        info["connected_to"] = connected_to
        info["pattern_matches"] = pattern_matches[:3]
        accounts_payload.append(info)

    accounts_payload.sort(
        key=lambda a: (-int(a["shared_bot_count"]), -int(a["effective_bot_count"]), a["artist"])
    )

    return {
        "ok": True,
        "summary": {
            "total_accounts": len(accounts_payload),
            "connected_accounts_count": sum(
                1 for a in accounts_payload if a["shared_bot_count"] > 0
            ),
            "independent_bot_accounts_count": sum(
                1 for a in accounts_payload if a["account_role"] == "independent_bot_affected"
            ),
            "clean_control_accounts_count": sum(
                1 for a in accounts_payload if a["account_role"] == "clean_control"
            ),
            "total_clusters": len(clusters),
            "total_shared_bots": len(shared_bots),
            "total_unique_bots_all_accounts": len(bot_accounts),
            "total_bridges": len(bridges),
            "total_shared_links": sum(int(b["account_count"]) for b in shared_bots),
            "generated_at": generated_at,
        },
        "accounts": accounts_payload,
        "clusters": clusters,
        "bridges": bridges,
        "shared_bots": shared_bots,
        "exclusive_bots_by_account": exclusive_bots_by_account,
    }


def get_checkpoints_history(active_artist: str | None = None) -> list[dict[str, Any]]:
    """Scan data/checkpoints and return summary metadata for all audited accounts."""
    if not CHECKPOINTS_DIR.is_dir():
        return []
    history: list[dict[str, Any]] = []
    cp_files = sorted(
        CHECKPOINTS_DIR.glob("*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    global_obs = _load_global_observed_profiles()
    try:
        lbls = load_labels()
        notes_map = lbls.notes
        known_bots = set(lbls.bots)
        known_humans = set(lbls.humans)
    except (OSError, LabelError):
        notes_map = {}
        known_bots = set()
        known_humans = set()

    try:
        active_cfg = validate_config_dict(load_config())
    except ConfigError:
        active_cfg = None

    for cp_file in cp_files:
        try:
            cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
            raw_profiles = cp_data.get("profiles") or []
            artist_name = str(cp_data.get("artist") or cp_file.stem)
            mtime = cp_file.stat().st_mtime
            mtime_iso = datetime.datetime.fromtimestamp(
                mtime, tz=datetime.UTC
            ).strftime("%Y-%m-%d %H:%M UTC")

            enriched = _collect_enriched_artist_profiles(
                artist_name,
                raw_profiles,
                global_observed=global_obs,
                label_notes=notes_map,
            )
            if active_cfg is not None:
                breakdown = _compute_history_breakdown(
                    enriched, active_cfg, known_bots, known_humans
                )
            else:
                complete_count = sum(1 for p in enriched if is_scorable(p))
                breakdown = {
                    "deep_checked": complete_count,
                    "bot_like_count": 0,
                    "effective_bot_count": 0,
                    "real_count": complete_count,
                    "bot_pct": None,
                    "auto_bot_pct": None,
                    "photo_count": 0,
                    "confirmed_bots": 0,
                    "confirmed_humans": 0,
                }

            is_active = (
                (artist_name.lower() == active_artist.lower())
                if active_artist
                else False
            )

            history.append(
                {
                    "artist": artist_name,
                    "filename": cp_file.name,
                    "total_scanned": len(enriched),
                    "checkpoint_saved": len(raw_profiles),
                    "deep_checked": breakdown["deep_checked"],
                    "bot_like_count": breakdown["bot_like_count"],
                    "effective_bot_count": breakdown.get(
                        "effective_bot_count", breakdown["bot_like_count"]
                    ),
                    "suspicious_count": breakdown.get("suspicious_count", 0),
                    "real_count": breakdown["real_count"],
                    "bot_pct": breakdown["bot_pct"],
                    "suspicious_pct": breakdown.get("suspicious_pct"),
                    "clean_pct": breakdown.get("clean_pct"),
                    "auto_bot_pct": breakdown.get("auto_bot_pct"),
                    "photo_count": breakdown["photo_count"],
                    "confirmed_bots": breakdown["confirmed_bots"],
                    "confirmed_humans": breakdown["confirmed_humans"],
                    "last_modified": mtime_iso,
                    "is_active": is_active,
                }
            )
        except (OSError, json.JSONDecodeError):
            continue
    return history


def run_audit_from_csv_text(
    csv_text: str,
    config: dict[str, Any] | None = None,
    burst_overrides: dict[str, bool] | None = None,
    unscored: list[dict[str, Any]] | None = None,
    extra_warnings: list[str] | None = None,
    collected_at_map: dict[str, str] | None = None,
    feed_signals_map: dict[str, dict[str, Any]] | None = None,
    hover_profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute the full botaudit pipeline on real observed CSV text and return JSON results."""
    active_config = validate_config_dict(config) if config is not None else validate_config_dict(load_config())
    prepared_csv = normalize_csv_text(csv_text)

    labels_set = None
    try:
        labels_set = load_labels()
    except (OSError, LabelError):
        labels_set = None

    local_reg = None
    try:
        local_reg = load_local_registry(
            checkpoints_dir=CHECKPOINTS_DIR,
            label_set=labels_set,
        )
    except (OSError, LabelError):
        local_reg = None

    has_csv_rows = len([ln for ln in prepared_csv.splitlines() if ln.strip()]) > 1
    if has_csv_rows or not hover_profiles:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            csv_path = tmp_path / "input.csv"
            rejects_path = tmp_path / "rejects.csv"
            csv_path.write_text(prepared_csv, encoding="utf-8")

            load_result = load_followers_csv(
                csv_path,
                config=active_config,
                rejects_path=rejects_path,
            )

        scored_df = score_followers_extended(
            load_result.df,
            config=active_config,
            burst_overrides=burst_overrides,
            registry=local_reg,
            feed_signals_map=feed_signals_map,
        )
        load_rejects = load_result.rejects
        load_warnings = load_result.warnings
    else:
        scored_df = pd.DataFrame()
        load_rejects = []
        load_warnings = []

    known_bots_reg = (
        set(local_reg.known_bots)
        if local_reg
        else (set(labels_set.bots) if labels_set else set())
    )
    known_humans_reg = (
        set(local_reg.known_humans)
        if local_reg
        else (set(labels_set.humans) if labels_set else set())
    )

    conclusive_hover_records: list[dict[str, Any]] = []
    effective_unscored = list(unscored or [])
    for hp in hover_profiles or []:
        ev = _evaluate_profile_score(hp, active_config, known_bots_reg, known_humans_reg)
        if not ev["conclusive"]:
            effective_unscored.append(hp)
            continue
        uname_hp = str(hp.get("username") or "")
        artist_hp = str(hp.get("artist") or "Audited_Account")
        rec: dict[str, Any] = {
            "artist": artist_hp,
            "username": uname_hp,
            "has_profile_photo": ev["has_pic"] if isinstance(ev.get("has_pic"), bool) else None,
            "post_count": int(ev["post_count"]),
            "followers_count": int(ev["followers_count"]),
            "following_count": int(ev["following_count"]),
            "bio_length": None,
            "account_private": ev["is_priv"] if isinstance(ev.get("is_priv"), bool) else None,
            "username_digit_ratio": round(float(ev["forensics"]["digit_ratio"]), 4),
            "bot_score": float(ev["final_score"]),
            "bot_like": bool(ev["is_auto"]),
            "raw_bot_like": bool(ev["raw_bot_like"]),
            "ratio_cluster_raw_pts": int(ev["ratio_cluster_raw_pts"]),
            "ratio_cluster_pts": int(ev["ratio_cluster_pts"]),
            "verdict_tier": ev["verdict_tier"],
            "hover_scored": True,
        }
        rec.update(ev["flags"])
        conclusive_hover_records.append(rec)

    if scored_df.empty and not conclusive_hover_records and bool(hover_profiles):
        raise CSVValidationError(
            "No captured profiles had enough observed numbers yet (posts, followers, following, bio): "
            "none has all of its numbers or a conclusive hover-card verdict."
        )

    if conclusive_hover_records:
        hover_df = pd.DataFrame(conclusive_hover_records)
        if scored_df.empty:
            scored_df = hover_df
        else:
            for col_obj in ("bio_length", "has_profile_photo", "account_private"):
                if col_obj in scored_df.columns:
                    scored_df[col_obj] = scored_df[col_obj].astype(object)
            scored_df = pd.concat([scored_df, hover_df], ignore_index=True)

    artist_stats = compute_artist_stats(
        scored_df,
        rejects=load_rejects,
        config=active_config,
    )
    coverage = _build_coverage(scored_df, effective_unscored)

    digit_threshold = float(active_config["digit_ratio_threshold"])
    all_flag_cols = (
        *FLAG_COLUMNS,
        "stitched_username",
        "same_day_post_burst",
        "blank_shell_bonus",
        "extreme_mass_following",
        "follow_exchange_ring",
        "low_audience_imbalance",
        "shared_post_bot_signal",
        "ratio_cluster_capped",
        "known_registry_bot",
        "registry_boost_applied",
    )
    weights_cfg = active_config["weights"]
    w_no_photo_pts = round(float(weights_cfg["no_photo"]) * 100)
    w_zero_posts_pts = round(float(weights_cfg["zero_posts"]) * 100)
    w_burst_pts = round(float(active_config.get("same_day_burst_weight", 0.15)) * 100)
    w_mass_pts = round(float(weights_cfg["mass_following"]) * 100)
    w_ext_mass_pts = round(float(active_config.get("extreme_mass_following_weight", 0.30)) * 100)
    w_f4f_pts = round(float(active_config.get("follow_exchange_ring_weight", 0.20)) * 100)
    w_low_aud_pts = round(float(active_config.get("low_audience_imbalance_weight", 0.15)) * 100)
    w_shared_pts = round(float(active_config.get("shared_post_bot_weight", 0.20)) * 100)
    w_bio_pts = round(float(weights_cfg["empty_bio"]) * 100)
    w_digit_pts = round(float(weights_cfg["digit_heavy_username"]) * 100)
    w_stitched_pts = round(float(active_config.get("stitched_username_weight", 0.10)) * 100)
    w_ratio_cap_pts = round(float(active_config.get("ratio_cluster_cap", 0.60)) * 100)
    suspicious_floor_pts = round(float(active_config.get("suspicious_cutoff", 0.35)) * 100)
    w_reg_pts = round(float(active_config.get("known_registry_bot_boost", 0.25)) * 100)

    # Compute handle forensics per artist and per profile
    forensics_by_artist: dict[str, dict[str, Any]] = {}
    profiles_list: list[dict[str, Any]] = []
    manual_unscored_profiles: list[dict[str, Any]] = []

    if not scored_df.empty:
        for _, row in scored_df.iterrows():
            uname = str(row["username"])
            artist_name = str(row["artist"])
            hf = analyze_handle_forensics(uname, digit_threshold=digit_threshold)

            bucket = forensics_by_artist.setdefault(
                artist_name,
                {
                    "total": 0,
                    "digit_heavy": 0,
                    "word_dot_digits": 0,
                    "long_digit_run": 0,
                    "same_day_burst": 0,
                    "private_bots": 0,
                    "known_registry_bots": 0,
                    "cross_artist_recycled": 0,
                },
            )
            manual_label = labels_set.label_of(uname) if labels_set else None
            note_obj = labels_set.notes.get(uname.lower()) if labels_set else None
            manual_status = note_obj.status if note_obj else "available"
            manual_bot_type = note_obj.bot_type if note_obj else None
            is_eff_bot_row = (manual_label == "bot") or (
                bool(row["bot_like"]) and manual_label != "human"
            )

            bucket["total"] += 1
            if hf["is_digit_heavy"]:
                bucket["digit_heavy"] += 1
            if hf["is_word_dot_digits"]:
                bucket["word_dot_digits"] += 1
            if hf["has_long_digit_run"]:
                bucket["long_digit_run"] += 1
            if bool(row.get("same_day_post_burst", False)):
                bucket["same_day_burst"] += 1
            raw_priv = row["account_private"]
            priv_val = bool(raw_priv) if pd.notna(raw_priv) else None
            if priv_val is True and is_eff_bot_row:
                bucket["private_bots"] += 1
            if bool(row.get("known_registry_bot", False)):
                bucket["known_registry_bots"] += 1

            seen_artists = local_reg.get_artist_occurrences(uname) if local_reg else []
            cross_recycled = len(seen_artists) >= 2
            if cross_recycled:
                bucket["cross_artist_recycled"] += 1

            flwrs_i = int(row["followers_count"])
            flwing_i = int(row["following_count"])
            follow_ratio = round(flwing_i / max(1, flwrs_i), 2)
            flags_dict = {flag: bool(row.get(flag, False)) for flag in all_flag_cols}

            if flags_dict["digit_heavy_username"]:
                if hf["is_word_dot_digits"]:
                    handle_clue_reason = "word_dot_digits"
                elif hf["is_digit_heavy"]:
                    handle_clue_reason = "digit_heavy"
                else:
                    handle_clue_reason = "long_digit_run"
            elif flags_dict["stitched_username"]:
                handle_clue_reason = "stitched_pattern"
            else:
                handle_clue_reason = ""

            ratio_raw_pts = (
                (w_mass_pts if flags_dict["mass_following"] else 0)
                + (w_ext_mass_pts if flags_dict["extreme_mass_following"] else 0)
                + (w_low_aud_pts if flags_dict["low_audience_imbalance"] else 0)
                + (w_shared_pts if flags_dict["shared_post_bot_signal"] else 0)
            )
            ratio_eff_pts = min(w_ratio_cap_pts, ratio_raw_pts)
            raw_score_pts = (
                ratio_eff_pts
                + (w_no_photo_pts if flags_dict["no_photo"] else 0)
                + (w_zero_posts_pts if flags_dict["zero_posts"] else 0)
                + (w_burst_pts if flags_dict["same_day_post_burst"] else 0)
                + (w_burst_pts if flags_dict["blank_shell_bonus"] else 0)
                + (w_f4f_pts if flags_dict["follow_exchange_ring"] else 0)
                + (w_bio_pts if flags_dict["empty_bio"] else 0)
                + (w_digit_pts if flags_dict["digit_heavy_username"] else 0)
                + (w_stitched_pts if flags_dict["stitched_username"] else 0)
                + (w_reg_pts if flags_dict["registry_boost_applied"] else 0)
            )
            score_pts_i = round(float(row["bot_score"]) * 100)
            is_hover_row = (
                bool(row["hover_scored"])
                if "hover_scored" in row and pd.notna(row["hover_scored"])
                else False
            )
            if manual_label == "human":
                verdict_tier = "clean"
            elif manual_label == "bot" or bool(row["bot_like"]):
                verdict_tier = "bot"
            elif is_hover_row:
                verdict_tier = "clean"
            elif score_pts_i >= suspicious_floor_pts:
                verdict_tier = "suspicious"
            else:
                verdict_tier = "clean"

            raw_pic = row["has_profile_photo"]
            pic_val = bool(raw_pic) if pd.notna(raw_pic) else None
            raw_bio = row["bio_length"]
            bio_val = int(raw_bio) if pd.notna(raw_bio) else None
            profiles_list.append(
                {
                    "artist": artist_name,
                    "username": uname,
                    "has_profile_photo": pic_val,
                    "post_count": int(row["post_count"]),
                    "followers_count": flwrs_i,
                    "following_count": flwing_i,
                    "follow_ratio": follow_ratio,
                    "bio_length": bio_val,
                    "hover_scored": is_hover_row,
                    "account_private": priv_val,
                    "username_digit_ratio": round(float(row["username_digit_ratio"]), 4),
                    "bot_score": round(float(row["bot_score"]), 4),
                    "raw_score_pts": raw_score_pts,
                    "ratio_cluster_raw_pts": ratio_raw_pts,
                    "ratio_cluster_pts": ratio_eff_pts,
                    "ratio_cluster_capped": bool(flags_dict["ratio_cluster_capped"]),
                    "verdict_tier": verdict_tier,
                    "raw_bot_like": bool(
                        row.get(
                            "raw_bot_like",
                            float(row["bot_score"]) > float(active_config["bot_like_cutoff"]),
                        )
                    ),
                    "bot_like": bool(row["bot_like"]),
                    "manual_label": manual_label,
                    "manual_status": manual_status,
                    "manual_bot_type": manual_bot_type,
                    "labeled_on": (note_obj.labeled_on or "") if note_obj else "",
                    "collected_at": (collected_at_map or {}).get(uname.lower(), ""),
                    "known_registry_bot": bool(row.get("known_registry_bot", False)),
                    "cross_artist_recycled": cross_recycled,
                    "seen_in_artists": seen_artists,
                    "handle_clue_reason": handle_clue_reason,
                    "flags": flags_dict,
                    "forensics": hf,
                }
            )

    if effective_unscored and labels_set:
        for u_prof in effective_unscored:
            if not isinstance(u_prof, dict):
                continue
            uname = extract_username(str(u_prof.get("username") or ""))
            if not uname:
                continue
            m_label = labels_set.label_of(uname)
            if not m_label:
                continue
            note_obj = labels_set.notes.get(uname.lower())
            seen_artists = local_reg.get_artist_occurrences(uname) if local_reg else []
            manual_unscored_profiles.append(
                {
                    "artist": str(u_prof.get("artist") or "Audited_Account"),
                    "username": uname,
                    "has_profile_photo": (
                        u_prof["has_profile_photo"]
                        if isinstance(u_prof.get("has_profile_photo"), bool)
                        else None
                    ),
                    "post_count": None,
                    "followers_count": None,
                    "following_count": None,
                    "follow_ratio": None,
                    "bio_length": None,
                    "account_private": (
                        u_prof["account_private"]
                        if isinstance(u_prof.get("account_private"), bool)
                        else None
                    ),
                    "username_digit_ratio": round(compute_digit_ratio(uname), 4),
                    "bot_score": None,
                    "raw_score_pts": None,
                    "raw_bot_like": False,
                    "bot_like": False,
                    "unscored_manual": True,
                    "manual_label": m_label,
                    "manual_status": note_obj.status if note_obj else "available",
                    "manual_bot_type": note_obj.bot_type if note_obj else None,
                    "labeled_on": (note_obj.labeled_on or "") if note_obj else "",
                    "collected_at": (collected_at_map or {}).get(
                        uname.lower(), str(u_prof.get("collected_at") or "")
                    ),
                    "known_registry_bot": bool(local_reg and local_reg.is_known_bot(uname)),
                    "cross_artist_recycled": len(seen_artists) >= 2,
                    "seen_in_artists": seen_artists,
                    "handle_clue_reason": "",
                    "flags": {},
                    "forensics": analyze_handle_forensics(uname, digit_threshold=digit_threshold),
                }
            )

    conf_level = float(active_config["confidence_level"])
    profs_by_artist: dict[str, list[dict[str, Any]]] = {}
    for prof_row in profiles_list:
        profs_by_artist.setdefault(prof_row["artist"], []).append(prof_row)

    artists_payload: list[dict[str, Any]] = []
    for stat in artist_stats:
        stat_dict = asdict(stat)
        artist_cov = coverage.get(stat.artist, {})
        stat_dict["scanned_total"] = artist_cov.get("scanned", stat.sample_size)
        stat_dict["incomplete_rows"] = artist_cov.get("incomplete", 0)
        artist_profs = profs_by_artist.get(stat.artist, [])
        auto_bot_cnt = sum(1 for pr in artist_profs if pr.get("bot_like"))
        manual_extra_bots = sum(
            1
            for pr in artist_profs
            if pr.get("manual_label") == "bot" and not pr.get("bot_like")
        )
        effective_bot_cnt = auto_bot_cnt + manual_extra_bots
        eff_lo, eff_hi = compute_wilson_ci(
            effective_bot_cnt, stat.sample_size, confidence_level=conf_level
        )
        stat_dict["auto_bot_count"] = auto_bot_cnt
        stat_dict["manual_extra_bots"] = manual_extra_bots
        stat_dict["effective_bot_count"] = effective_bot_cnt
        stat_dict["effective_bot_rate"] = (
            round(effective_bot_cnt / stat.sample_size, 6) if stat.sample_size else 0.0
        )
        stat_dict["effective_bot_pct"] = (
            round(effective_bot_cnt / stat.sample_size * 100, 1) if stat.sample_size else 0.0
        )
        suspicious_cnt = sum(1 for pr in artist_profs if pr.get("verdict_tier") == "suspicious")
        clean_cnt = max(0, stat.sample_size - effective_bot_cnt - suspicious_cnt)
        stat_dict["suspicious_count"] = suspicious_cnt
        stat_dict["suspicious_rate"] = (
            round(suspicious_cnt / stat.sample_size, 6) if stat.sample_size else 0.0
        )
        stat_dict["clean_count"] = clean_cnt
        stat_dict["clean_rate"] = (
            round(clean_cnt / stat.sample_size, 6) if stat.sample_size else 0.0
        )
        stat_dict["effective_ci_low"] = round(eff_lo, 4)
        stat_dict["effective_ci_high"] = round(eff_hi, 4)
        fb = forensics_by_artist.get(
            stat.artist,
            {
                "total": 0,
                "digit_heavy": 0,
                "word_dot_digits": 0,
                "long_digit_run": 0,
                "same_day_burst": 0,
                "private_bots": 0,
            },
        )
        total_n = max(1, fb["total"])
        stat_dict["same_day_burst_count"] = fb["same_day_burst"]
        stat_dict["same_day_burst_rate"] = (
            round(fb["same_day_burst"] / total_n, 4) if fb["total"] else 0.0
        )
        stat_dict["private_bots_count"] = fb["private_bots"]
        stat_dict["handle_forensics"] = {
            "digit_heavy_count": fb["digit_heavy"],
            "digit_heavy_rate": round(fb["digit_heavy"] / total_n, 4) if fb["total"] else 0.0,
            "word_dot_digits_count": fb["word_dot_digits"],
            "word_dot_digits_rate": (
                round(fb["word_dot_digits"] / total_n, 4) if fb["total"] else 0.0
            ),
            "long_digit_run_count": fb["long_digit_run"],
            "long_digit_run_rate": (
                round(fb["long_digit_run"] / total_n, 4) if fb["total"] else 0.0
            ),
            "same_day_burst_count": fb["same_day_burst"],
            "same_day_burst_rate": (
                round(fb["same_day_burst"] / total_n, 4) if fb["total"] else 0.0
            ),
        }
        artists_payload.append(stat_dict)

    rejects_payload = [
        {
            "row_number": r.row_number,
            "artist": r.artist,
            "username": r.username,
            "reason": r.reason,
        }
        for r in load_rejects
    ]

    warnings = [*load_warnings, *(extra_warnings or [])]
    for artist_name, cov in sorted(coverage.items()):
        if cov["incomplete"]:
            warnings.append(
                f"{artist_name}: {cov['complete']} of {cov['scanned']} scanned followers are scored; "
                f"{cov['incomplete']} are handle-only and are NOT scored."
            )

    # Empirical ground-truth validation against manual labels
    confirmed_bots = [p for p in profiles_list if p.get("manual_label") == "bot"]
    confirmed_humans = [p for p in profiles_list if p.get("manual_label") == "human"]
    confirmed_unsure = [p for p in profiles_list if p.get("manual_label") == "unsure"]
    confirmed_bots_unscored = sum(
        1 for p in manual_unscored_profiles if p.get("manual_label") == "bot"
    )
    confirmed_humans_unscored = sum(
        1 for p in manual_unscored_profiles if p.get("manual_label") == "human"
    )
    confirmed_unsure_unscored = sum(
        1 for p in manual_unscored_profiles if p.get("manual_label") == "unsure"
    )

    bots_detected = sum(1 for p in confirmed_bots if p.get("bot_like"))
    bots_missed = sum(1 for p in confirmed_bots if not p.get("bot_like"))
    bots_in_suspicious = sum(
        1
        for p in confirmed_bots
        if not p.get("bot_like") and round(float(p.get("bot_score") or 0.0) * 100) >= suspicious_floor_pts
    )
    cutoff_val = float(active_config["bot_like_cutoff"])
    humans_raw_false_alarms = sum(
        1
        for p in confirmed_humans
        if bool(p.get("raw_bot_like", float(p.get("bot_score") or 0.0) > cutoff_val))
    )
    humans_in_suspicious = sum(
        1
        for p in confirmed_humans
        if not bool(p.get("raw_bot_like", float(p.get("bot_score") or 0.0) > cutoff_val))
        and round(float(p.get("bot_score") or 0.0) * 100) >= suspicious_floor_pts
    )
    humans_raw_normal = len(confirmed_humans) - humans_raw_false_alarms

    sens_pct = round(bots_detected / len(confirmed_bots) * 100, 1) if confirmed_bots else None
    spec_pct = (
        round(humans_raw_normal / len(confirmed_humans) * 100, 1)
        if confirmed_humans
        else None
    )

    ground_truth_stats = {
        "total_labeled": len(confirmed_bots) + len(confirmed_humans) + len(confirmed_unsure),
        "total_labeled_all": (
            len(confirmed_bots)
            + len(confirmed_humans)
            + len(confirmed_unsure)
            + len(manual_unscored_profiles)
        ),
        "confirmed_bots": len(confirmed_bots),
        "confirmed_bots_unscored": confirmed_bots_unscored,
        "confirmed_bots_total": len(confirmed_bots) + confirmed_bots_unscored,
        "confirmed_bots_detected": bots_detected,
        "confirmed_bots_missed": bots_missed,
        "confirmed_bots_in_suspicious": bots_in_suspicious,
        "sensitivity_pct": sens_pct,
        "confirmed_humans": len(confirmed_humans),
        "confirmed_humans_unscored": confirmed_humans_unscored,
        "confirmed_humans_total": len(confirmed_humans) + confirmed_humans_unscored,
        "confirmed_humans_normal": humans_raw_normal,
        "confirmed_humans_false_alarms": humans_raw_false_alarms,
        "confirmed_humans_in_suspicious": humans_in_suspicious,
        "specificity_pct": spec_pct,
        "confirmed_humans_raw_normal": humans_raw_normal,
        "confirmed_humans_raw_false_alarms": humans_raw_false_alarms,
        "raw_specificity_pct": spec_pct,
        "confirmed_humans_overridden": humans_raw_false_alarms,
        "confirmed_unsure": len(confirmed_unsure),
        "confirmed_unsure_unscored": confirmed_unsure_unscored,
    }

    report_stats = list(artist_stats)
    if (
        len(report_stats) == 1
        and artists_payload
        and int(artists_payload[0].get("manual_extra_bots") or 0) > 0
    ):
        a0 = artists_payload[0]
        report_stats = [
            replace(
                report_stats[0],
                pct_bot_like=float(a0["effective_bot_rate"]),
                ci_low=float(a0["effective_ci_low"]),
                ci_high=float(a0["effective_ci_high"]),
            )
        ]

    chart_base64, chart_alt_text = generate_chart_base64(report_stats, lang="en")
    chart_base64_ro, chart_alt_text_ro = generate_chart_base64(report_stats, lang="ro")
    report_html = render_html_report(
        artists=report_stats,
        total_rows_loaded=len(scored_df),
        total_rows_rejected=len(load_rejects),
        warnings=load_warnings,
        config=active_config,
        lang="en",
    )
    report_html_ro = render_html_report(
        artists=report_stats,
        total_rows_loaded=len(scored_df),
        total_rows_rejected=len(load_rejects),
        warnings=load_warnings,
        config=active_config,
        lang="ro",
    )

    return {
        "artists": artists_payload,
        "total_rows_loaded": len(scored_df),
        "total_rows_rejected": len(load_rejects),
        "warnings": warnings,
        "coverage": coverage,
        "rejects": rejects_payload,
        "chart_base64": chart_base64,
        "chart_alt_text": chart_alt_text,
        "chart_base64_ro": chart_base64_ro,
        "chart_alt_text_ro": chart_alt_text_ro,
        "profiles": profiles_list,
        "manual_unscored_profiles": manual_unscored_profiles,
        "report_html": report_html,
        "report_html_ro": report_html_ro,
        "enriched_csv": prepared_csv,
        "config": active_config,
        "ground_truth": ground_truth_stats,
    }


def load_preset_Audit(
    preset_name: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the verified empirical benchmark preset using 100% real observed profile metrics."""
    if preset_name in ("verified_benchmarks", "verified", "empirical"):
        benchmarks_path = DEFAULT_BENCHMARK_SAMPLE_PATH
        if not benchmarks_path.is_file():
            sync_sample_benchmarks()
        if not benchmarks_path.is_file():
            raise CSVValidationError(
                "Empirical benchmark data (data/sample/verified_benchmarks.csv) not found."
            )
        csv_text = benchmarks_path.read_text(encoding="utf-8")
        return run_audit_from_csv_text(csv_text, config=config)

    raise CSVValidationError(
        f"Unsupported preset '{preset_name}'. Synthetic presets have been permanently removed; "
        "only 'verified_benchmarks' (100% real observed data) is supported."
    )


def _json_response(
    start_response: Callable[..., Any],
    status_code: int,
    payload: dict[str, Any],
) -> list[bytes]:
    """Serialize payload as UTF-8 JSON and invoke WSGI start_response with CORS headers."""
    status_map = {
        200: "200 OK",
        204: "204 No Content",
        400: "400 Bad Request",
        404: "404 Not Found",
        405: "405 Method Not Allowed",
        500: "500 Internal Server Error",
    }
    body = json.dumps(payload).encode("utf-8")
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("Access-Control-Allow-Origin", "*"),
        ("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS"),
        ("Access-Control-Allow-Headers", "Content-Type"),
    ]
    start_response(status_map.get(status_code, "200 OK"), headers)
    return [body]


def _sync_ground_truth_safe() -> None:
    """Thread-safe disk synchronization callback for background Worker 2."""
    with _STORAGE_LOCK:
        sync_ground_truth_lists()
    with _BENCHMARK_SYNC_LOCK:
        sync_sample_benchmarks()


def create_wsgi_app(config_path: Path = DEFAULT_CONFIG_PATH) -> Callable[..., list[bytes]]:
    """Create the WSGI application serving index.html and /api/* endpoints."""
    # Ensure ground truth lists and empirical sample benchmarks are synchronized on startup
    try:
        _sync_ground_truth_safe()
    except (OSError, json.JSONDecodeError, csv.Error, LabelError) as err:
        logger.warning("Initial ground truth sync skipped: %s", err)

    _WORKER_ENGINE.configure_callbacks(
        build_memory_bank_fn=build_memory_bank_payload,
        sync_disk_fn=_sync_ground_truth_safe,
        build_history_fn=lambda: get_checkpoints_history(active_artist=_ACTIVE_EXTENSION_ARTIST),
        precompute_artist_fn=load_checkpoint_capture,
    )

    def app(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        global _LATEST_EXTENSION_RESULT, _ACTIVE_EXTENSION_ARTIST
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")

        # Handle CORS preflight for local browser extension
        if method == "OPTIONS":
            return _json_response(start_response, 200, {"ok": True})

        # Serve index.html on root
        if path in ("/", "/index.html"):
            if method != "GET":
                return _json_response(start_response, 405, {"error": "Method not allowed"})
            if not INDEX_HTML_PATH.is_file():
                return _json_response(start_response, 404, {"error": "index.html not found"})
            html_bytes = INDEX_HTML_PATH.read_bytes()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Length", str(len(html_bytes))),
                    ("Cache-Control", "no-store"),
                ],
            )
            return [html_bytes]

        # GET /api/memory-bank (Step-0 Unified Confirmed Bots + Humans + Observed Profiles)
        if path == "/api/memory-bank":
            if method != "GET":
                return _json_response(start_response, 405, {"error": "Method not allowed"})
            snap = _WORKER_ENGINE.get_status_snapshot(active_artist=_ACTIVE_EXTENSION_ARTIST)
            if snap["workers_running"]:
                cached_mb = _WORKER_ENGINE.get_cached_memory_bank()
                if cached_mb is None:
                    rev_before = snap["revision"]
                    cached_mb = build_memory_bank_payload()
                    _WORKER_ENGINE.set_cached_memory_bank(cached_mb, revision=rev_before)
                payload = dict(cached_mb)
            else:
                payload = build_memory_bank_payload()
            payload["revision"] = snap["revision"]
            return _json_response(start_response, 200, payload)

        # GET /api/live-status (Real-Time 4-Worker Pulse & Revision Feed for index.html)
        if path == "/api/live-status":
            if method != "GET":
                return _json_response(start_response, 405, {"error": "Method not allowed"})
            active_name = _ACTIVE_EXTENSION_ARTIST
            if not active_name and _LATEST_EXTENSION_RESULT and _LATEST_EXTENSION_RESULT.get("artists"):
                active_name = _LATEST_EXTENSION_RESULT["artists"][0].get("artist")
            snap = _WORKER_ENGINE.get_status_snapshot(active_artist=active_name)
            if not snap.get("memory_bank_totals"):
                mb = build_memory_bank_payload()
                if snap.get("workers_running"):
                    _WORKER_ENGINE.set_cached_memory_bank(mb)
                snap["memory_bank_totals"] = mb["totals"]
            recent_evs = snap.get("recent_events", [])
            snap["latest_event"] = recent_evs[0].get("summary_en", "") if recent_evs else ""
            return _json_response(start_response, 200, snap)

        # GET / POST /api/config
        if path == "/api/config":
            if method == "GET":
                try:
                    cfg = validate_config_dict(load_config(config_path))
                    return _json_response(start_response, 200, {"config": cfg})
                except ConfigError as err:
                    return _json_response(start_response, 400, {"error": str(err)})

            if method == "POST":
                try:
                    body = _read_request_json(environ)
                    new_cfg = validate_config_dict(body.get("config", body))
                    return _json_response(
                        start_response,
                        200,
                        {"config": new_cfg, "valid": True},
                    )
                except (ValueError, TypeError, ConfigError) as err:
                    return _json_response(start_response, 400, {"error": str(err)})

            return _json_response(start_response, 405, {"error": "Method not allowed"})

        # GET / POST /api/checkpoint (Multi-Day Resume Checkpoint per Artist)
        if path == "/api/checkpoint":
            if method == "GET":
                query = parse_qs(environ.get("QUERY_STRING", ""))
                artist_q = (query.get("artist") or ["Audited_Account"])[0]
                cp = load_artist_checkpoint(artist_q)
                return _json_response(start_response, 200, cp)

            if method == "POST":
                try:
                    body = _read_request_json(environ)
                    artist_name = str(body.get("artist") or "Audited_Account").strip()
                    if body.get("clear") is True:
                        with _STORAGE_LOCK:
                            cp_file = _checkpoint_path(artist_name)
                            if cp_file.is_file():
                                cp_file.unlink()
                        _LATEST_EXTENSION_RESULT = None
                        if (
                            _ACTIVE_EXTENSION_ARTIST
                            and _ACTIVE_EXTENSION_ARTIST.lower() == artist_name.lower()
                        ):
                            _ACTIVE_EXTENSION_ARTIST = None
                        _WORKER_ENGINE.record_event(
                            kind="checkpoint_clear",
                            artist=artist_name,
                            username="",
                            summary_en=f"Cleared saved followers for @{artist_name}",
                            summary_ro=f"Au fost șterse profilurile salvate pentru @{artist_name}",
                        )
                        _WORKER_ENGINE.notify_data_changed(
                            artist=artist_name, precompute_artist=False
                        )
                        return _json_response(
                            start_response,
                            200,
                            {"artist": artist_name, "cleared": True, "total_saved": 0},
                        )
                    incoming_profiles = body.get("profiles") or []
                    skip_live_counts = bool(body.get("skip_live_counts"))
                    defer_workers = bool(body.get("defer_workers"))
                    saved = save_artist_checkpoint(
                        artist=artist_name,
                        new_profiles=incoming_profiles,
                        completed_slices=body.get("completed_slices") or [],
                        last_username=str(body.get("last_username") or ""),
                        next_max_id=str(body.get("next_max_id") or ""),
                    )
                    if not defer_workers:
                        reconcile_checkpoint_manual_labels(artist_name, incoming_profiles)
                    # Invalidate cached audit so next UI fetch reflects newly saved tab numbers immediately
                    _LATEST_EXTENSION_RESULT = None
                    _ACTIVE_EXTENSION_ARTIST = artist_name
                    if len(incoming_profiles) == 1 and isinstance(incoming_profiles[0], dict):
                        p0 = incoming_profiles[0]
                        u0 = str(p0.get("username") or "")
                        pts0 = p0.get("score_pts") if isinstance(p0.get("score_pts"), int) else None
                        _WORKER_ENGINE.record_event(
                            kind="profile_checked",
                            artist=artist_name,
                            username=u0,
                            score_pts=pts0,
                            summary_en=f"Checked @{u0} on @{artist_name}",
                            summary_ro=f"Verificat @{u0} în @{artist_name}",
                        )
                    elif incoming_profiles:
                        _WORKER_ENGINE.record_event(
                            kind="batch_sync",
                            artist=artist_name,
                            username=str(saved.get("last_username") or ""),
                            summary_en=f"Saved {saved['total_saved']} followers for @{artist_name}",
                            summary_ro=f"Salvate {saved['total_saved']} profiluri pentru @{artist_name}",
                        )
                    if defer_workers:
                        _WORKER_ENGINE.invalidate_caches(artist=artist_name)
                    else:
                        _WORKER_ENGINE.notify_data_changed(artist=artist_name)
                    # The extension syncs every few seconds and ignores the reply, so send a summary,
                    # not the whole multi-megabyte profile list back.
                    summary = {
                        k: v for k, v in saved.items() if k not in ("profiles", "seen_usernames")
                    }
                    if not skip_live_counts:
                        summary["live_counts"] = _compute_artist_live_counts(artist_name)
                    return _json_response(start_response, 200, summary)
                except (ValueError, TypeError) as err:
                    return _json_response(start_response, 400, {"error": str(err)})

            return _json_response(start_response, 405, {"error": "Method not allowed"})

        # GET / POST / OPTIONS /api/labels (Ground-Truth Sidecar Store)
        if path == "/api/labels":
            if method == "OPTIONS":
                return _json_response(start_response, 200, {"ok": True})
            if method == "GET":
                notes = load_label_notes()
                return _json_response(
                    start_response,
                    200,
                    {
                        "count": len(notes),
                        "notes": [
                            {
                                "username": n.username,
                                "label": n.label,
                                "bot_type": n.bot_type,
                                "labeler": n.labeler,
                                "labeled_on": n.labeled_on,
                                "evidence_note": n.evidence_note,
                                "status": n.status,
                            }
                            for n in notes.values()
                        ],
                    },
                )
            if method == "POST":
                try:
                    body = _read_request_json(environ)
                    raw_u = str(body.get("username", "")).strip()
                    username = extract_username(raw_u).lower()
                    if not username:
                        return _json_response(
                            start_response, 400, {"error": "Missing or empty 'username'"}
                        )

                    raw_label = body.get("label")
                    label = str(raw_label).strip().lower() if raw_label is not None else None
                    if label is not None and label not in VALID_LABELS:
                        return _json_response(
                            start_response,
                            400,
                            {"error": f"Invalid label '{label}'. Must be one of: {', '.join(sorted(VALID_LABELS))}"},
                        )

                    raw_status = body.get("status")
                    status = str(raw_status).strip().lower() if raw_status is not None else "available"
                    if status not in VALID_STATUSES:
                        return _json_response(
                            start_response,
                            400,
                            {"error": f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}"},
                        )

                    raw_bot_type = body.get("bot_type")
                    bot_type = str(raw_bot_type).strip() if raw_bot_type is not None else None
                    raw_ev = body.get("evidence_note") if body.get("evidence_note") is not None else body.get("notes", "")
                    evidence_note = str(raw_ev).strip() or None
                    labeler = str(body.get("labeler", "owner")).strip() or "owner"
                    labeled_on = str(body.get("labeled_on", "")).strip() or datetime.date.today().isoformat()

                    inline_profile = body.get("profile")
                    inline_artist = str(body.get("artist") or "").strip()
                    if isinstance(inline_profile, dict) and inline_profile.get("username") and inline_artist:
                        inline_copy = dict(inline_profile)
                        if label:
                            inline_copy["manual_label"] = label
                        save_artist_checkpoint(
                            artist=inline_artist,
                            new_profiles=[inline_copy],
                            last_username=username,
                        )

                    note = LabelNote(
                        username=username,
                        label=label,
                        bot_type=bot_type,
                        labeler=labeler,
                        labeled_on=labeled_on,
                        evidence_note=evidence_note,
                        status=status,
                    )
                    workers_running = _WORKER_ENGINE.get_status_snapshot()["workers_running"]
                    with _STORAGE_LOCK:
                        if workers_running:
                            save_label_note(note, auto_sync=False)
                            sync_ground_truth_lists()
                        else:
                            save_label_note(note, auto_sync=True)
                        fresh_labels = load_labels()
                    # Invalidate cached audit so manual labels appear live on next dashboard refresh
                    _LATEST_EXTENSION_RESULT = None
                    target_ctx = inline_artist or (_ACTIVE_EXTENSION_ARTIST or "")
                    if target_ctx:
                        _ACTIVE_EXTENSION_ARTIST = target_ctx
                    friendly_en = (
                        "BOT"
                        if label == "bot"
                        else "REAL PERSON"
                        if label == "human"
                        else "UNSURE"
                    )
                    friendly_ro = (
                        "BOT"
                        if label == "bot"
                        else "OM REAL"
                        if label == "human"
                        else "NESIGUR"
                    )
                    _WORKER_ENGINE.record_event(
                        kind="label_confirmed",
                        artist=target_ctx,
                        username=username,
                        label=label,
                        summary_en=f"Confirmed @{username} as {friendly_en}",
                        summary_ro=f"Confirmat @{username} ca {friendly_ro}",
                    )
                    _WORKER_ENGINE.notify_data_changed(
                        artist=target_ctx, sync_disk=workers_running
                    )
                    return _json_response(
                        start_response,
                        200,
                        {
                            "ok": True,
                            "username": username,
                            "label": label,
                            "status": status,
                            "labeled_on": labeled_on,
                            "verified_bots_count": len(fresh_labels.bots),
                            "verified_users_count": len(fresh_labels.humans),
                            "live_counts": (
                                _compute_artist_live_counts(target_ctx, preloaded_labels=fresh_labels)
                                if target_ctx
                                else {}
                            ),
                        },
                    )
                except (ValueError, TypeError, KeyError, LabelError) as err:
                    return _json_response(start_response, 400, {"error": str(err)})

            return _json_response(start_response, 405, {"error": "Method not allowed"})

        # GET / DELETE / POST /api/history (Scanned Accounts History)
        if path == "/api/history":
            if method == "DELETE":
                query_string = environ.get("QUERY_STRING", "")
                query_params = parse_qs(query_string)
                req_artists = query_params.get("artist")
                artist_q = req_artists[0].strip() if req_artists else ""
                if not artist_q:
                    return _json_response(start_response, 400, {"error": "Missing 'artist' parameter"})
                clean_artist = re.sub(r"[^\w\-.]", "_", artist_q)
                with _STORAGE_LOCK:
                    cp_file = CHECKPOINTS_DIR / f"{clean_artist}.json"
                    if cp_file.is_file():
                        cp_file.unlink()
                if (
                    _LATEST_EXTENSION_RESULT
                    and _LATEST_EXTENSION_RESULT.get("checkpoint_meta", {}).get("artist")
                    == clean_artist
                ):
                    _LATEST_EXTENSION_RESULT = None
                if (
                    _ACTIVE_EXTENSION_ARTIST
                    and _ACTIVE_EXTENSION_ARTIST.lower() == clean_artist.lower()
                ):
                    _ACTIVE_EXTENSION_ARTIST = None
                _WORKER_ENGINE.record_event(
                    kind="history_removed",
                    artist=clean_artist,
                    username="",
                    summary_en=f"Removed @{clean_artist} from History",
                    summary_ro=f"Eliminat @{clean_artist} din Istoric",
                )
                _WORKER_ENGINE.notify_data_changed(
                    artist=clean_artist, precompute_artist=False
                )
                return _json_response(start_response, 200, {"ok": True, "removed": clean_artist})

            if method == "POST":
                try:
                    body = _read_request_json(environ)
                    if body.get("action") == "delete" or body.get("clear") is True:
                        artist_q = str(body.get("artist") or "").strip()
                        if not artist_q:
                            return _json_response(
                                start_response, 400, {"error": "Missing 'artist' parameter"}
                            )
                        clean_artist = re.sub(r"[^\w\-.]", "_", artist_q)
                        with _STORAGE_LOCK:
                            cp_file = CHECKPOINTS_DIR / f"{clean_artist}.json"
                            if cp_file.is_file():
                                cp_file.unlink()
                        if (
                            _LATEST_EXTENSION_RESULT
                            and _LATEST_EXTENSION_RESULT.get("checkpoint_meta", {}).get("artist")
                            == clean_artist
                        ):
                            _LATEST_EXTENSION_RESULT = None
                        if (
                            _ACTIVE_EXTENSION_ARTIST
                            and _ACTIVE_EXTENSION_ARTIST.lower() == clean_artist.lower()
                        ):
                            _ACTIVE_EXTENSION_ARTIST = None
                        _WORKER_ENGINE.record_event(
                            kind="history_removed",
                            artist=clean_artist,
                            username="",
                            summary_en=f"Removed @{clean_artist} from History",
                            summary_ro=f"Eliminat @{clean_artist} din Istoric",
                        )
                        _WORKER_ENGINE.notify_data_changed(
                            artist=clean_artist, precompute_artist=False
                        )
                        return _json_response(
                            start_response, 200, {"ok": True, "removed": clean_artist}
                        )
                    return _json_response(start_response, 400, {"error": "Unsupported history action"})
                except (ValueError, TypeError) as err:
                    return _json_response(start_response, 400, {"error": str(err)})

            if method == "GET":
                query_params = parse_qs(environ.get("QUERY_STRING", ""))
                force_recompute = bool(query_params.get("recompute"))
                active_name = _ACTIVE_EXTENSION_ARTIST
                if not active_name and _LATEST_EXTENSION_RESULT and _LATEST_EXTENSION_RESULT.get("artists"):
                    active_name = _LATEST_EXTENSION_RESULT["artists"][0].get("artist")
                snap = _WORKER_ENGINE.get_status_snapshot()
                history = None
                if not force_recompute and snap["workers_running"]:
                    cached_hist = _WORKER_ENGINE.get_cached_history()
                    if cached_hist is not None:
                        active_low = active_name.lower() if active_name else ""
                        for h in cached_hist:
                            h["is_active"] = bool(
                                active_low and str(h.get("artist") or "").lower() == active_low
                            )
                        history = cached_hist
                if history is None:
                    rev_before = snap["revision"]
                    history = get_checkpoints_history(active_artist=active_name)
                    if snap["workers_running"]:
                        _WORKER_ENGINE.set_cached_history(history, revision=rev_before)
                return _json_response(
                    start_response, 200, {"history": history, "active_artist": active_name}
                )

            return _json_response(start_response, 405, {"error": "Method not allowed"})

        # GET /api/bot-network (Cross-Account Bot Connection Mindmap Graph)
        if path == "/api/bot-network":
            if method == "GET":
                return _json_response(start_response, 200, build_bot_network_payload())
            return _json_response(start_response, 405, {"error": "Method not allowed"})

        # GET / POST /api/extension-import
        if path == "/api/extension-import":
            if method == "GET":
                query_string = environ.get("QUERY_STRING", "")
                query_params = parse_qs(query_string)
                req_artists = query_params.get("artist")
                requested_artist = req_artists[0].strip() if req_artists else None
                force_recompute = bool(query_params.get("recompute"))
                force_fresh = bool(
                    query_params.get("fresh")
                    or query_params.get("sync")
                    or query_params.get("_t")
                )

                if requested_artist:
                    snap = _WORKER_ENGINE.get_status_snapshot()
                    loaded = None
                    if not force_recompute and snap["workers_running"]:
                        loaded = _WORKER_ENGINE.get_prewarmed_audit(requested_artist)
                    if loaded is None:
                        rev_before = snap["revision"]
                        loaded = load_checkpoint_capture(requested_artist)
                        if loaded is not None and snap["workers_running"]:
                            _WORKER_ENGINE.set_prewarmed_audit(
                                requested_artist, loaded, revision=rev_before
                            )
                    if loaded is not None:
                        _ACTIVE_EXTENSION_ARTIST = requested_artist
                        _LATEST_EXTENSION_RESULT = loaded
                        return _json_response(start_response, 200, loaded)
                    return _json_response(
                        start_response,
                        404,
                        {"error": f"No checkpoint found for '{requested_artist}'"},
                    )

                if force_recompute or force_fresh or _LATEST_EXTENSION_RESULT is None:
                    target_to_load = _ACTIVE_EXTENSION_ARTIST
                    if not target_to_load and CHECKPOINTS_DIR.is_dir():
                        cp_files = sorted(
                            CHECKPOINTS_DIR.glob("*.json"),
                            key=lambda f: f.stat().st_mtime,
                            reverse=True,
                        )
                        if cp_files:
                            target_to_load = cp_files[0].stem
                    if target_to_load:
                        snap = _WORKER_ENGINE.get_status_snapshot()
                        loaded = None
                        if not force_recompute and snap["workers_running"]:
                            loaded = _WORKER_ENGINE.get_prewarmed_audit(target_to_load)
                        if loaded is None:
                            rev_before = snap["revision"]
                            loaded = load_checkpoint_capture(target_to_load)
                            if loaded is not None and snap["workers_running"]:
                                _WORKER_ENGINE.set_prewarmed_audit(
                                    target_to_load, loaded, revision=rev_before
                                )
                        if loaded is not None:
                            _ACTIVE_EXTENSION_ARTIST = target_to_load
                            _LATEST_EXTENSION_RESULT = loaded

                if _LATEST_EXTENSION_RESULT is None:
                    return _json_response(
                        start_response,
                        404,
                        {"error": "No browser extension batch has been imported yet."},
                    )
                return _json_response(start_response, 200, _LATEST_EXTENSION_RESULT)

            if method == "POST":
                try:
                    body = _read_request_json(environ)
                    artist_name = str(body.get("artist") or "Audited_Account").strip()
                    profiles_in = body.get("profiles")
                    merge_cp = bool(body.get("merge_checkpoint", False))
                    custom_cfg = body.get("config")

                    if not isinstance(profiles_in, list) or not profiles_in:
                        raise CSVValidationError("Extension payload must include a non-empty 'profiles' list.")

                    saved_total_count = len(profiles_in)
                    if merge_cp:
                        cp_saved = save_artist_checkpoint(
                            artist=artist_name,
                            new_profiles=profiles_in,
                            completed_slices=body.get("completed_slices") or [],
                            last_username=str(body.get("last_username") or ""),
                            next_max_id=str(body.get("next_max_id") or ""),
                        )
                        raw_cp_profiles = cp_saved["profiles"]
                        saved_total_count = len(raw_cp_profiles)
                        global_obs = _load_global_observed_profiles()
                        try:
                            notes_map = load_label_notes()
                        except (OSError, LabelError):
                            notes_map = {}
                        profiles_in = _collect_enriched_artist_profiles(
                            artist_name,
                            raw_cp_profiles,
                            global_observed=global_obs,
                            label_notes=notes_map,
                        )

                    # "include_control" is accepted but ignored: the old control rows used invented numbers.
                    result = _audit_capture(
                        parse_har_or_json_to_rows(json.dumps(profiles_in), default_artist=artist_name),
                        config=custom_cfg,
                    )
                    result["source"] = "extension"
                    result["rate_limit_status"] = body.get("rate_limit_status") or {}
                    result["checkpoint_meta"] = {
                        "artist": artist_name,
                        "total_scanned": len(profiles_in),
                        "checkpoint_saved": saved_total_count,
                        "deep_checked": result.get("total_rows_loaded", 0),
                    }
                    for a_entry in result.get("artists") or []:
                        if str(a_entry.get("artist") or "").lower() == artist_name.lower():
                            a_entry["checkpoint_saved"] = saved_total_count
                    _ACTIVE_EXTENSION_ARTIST = artist_name
                    _LATEST_EXTENSION_RESULT = result
                    checked_n = int(result.get("total_rows_loaded") or 0)
                    _WORKER_ENGINE.record_event(
                        kind="send_to_audit_app",
                        artist=artist_name,
                        username="",
                        summary_en=f"Live Audit ready for @{artist_name} ({checked_n} checked)",
                        summary_ro=f"Audit live pregătit pentru @{artist_name} ({checked_n} verificate)",
                    )
                    _WORKER_ENGINE.notify_data_changed(
                        artist=artist_name, precompute_artist=False
                    )
                    _WORKER_ENGINE.set_prewarmed_audit(artist_name, result)
                    return _json_response(start_response, 200, result)
                except (CSVValidationError, ConfigError, ValueError, TypeError) as err:
                    return _json_response(start_response, 400, {"error": str(err)})

            return _json_response(start_response, 405, {"error": "Method not allowed"})

        # GET /api/presets
        if path == "/api/presets":
            if method != "GET":
                return _json_response(start_response, 405, {"error": "Method not allowed"})
            query = parse_qs(environ.get("QUERY_STRING", ""))
            name = (query.get("name") or [""])[0]
            if not name:
                return _json_response(
                    start_response,
                    200,
                    {
                        "presets": [
                            {
                                "id": "verified_benchmarks",
                                "label": "Verified Benchmark (100% real observed numbers from manual audits)",
                                "available": DEFAULT_BENCHMARK_SAMPLE_PATH.is_file(),
                            },
                        ]
                    },
                )
            try:
                result = load_preset_Audit(name)
                return _json_response(start_response, 200, result)
            except (CSVValidationError, ConfigError, ValueError) as err:
                return _json_response(start_response, 400, {"error": str(err)})

        # POST /api/audit
        if path == "/api/audit":
            if method != "POST":
                return _json_response(start_response, 405, {"error": "Method not allowed"})
            try:
                body = _read_request_json(environ)
                mode = str(body.get("mode", "csv"))
                custom_cfg = body.get("config")

                if mode == "preset":
                    preset_name = str(body.get("preset", "verified_benchmarks"))
                    result = load_preset_Audit(preset_name, config=custom_cfg)
                    return _json_response(start_response, 200, result)

                if mode == "urls":
                    raise CSVValidationError(
                        "URL-only assumed-number mode has been removed. "
                        "Only real observed profile metrics (Extension capture, .HAR/.JSON, or 9-column CSV) are allowed."
                    )

                if mode == "har_json":
                    raw_text = str(body.get("raw_text", ""))
                    artist_name = str(body.get("artist", "Audited_Account")).strip() or "Audited_Account"
                    result = _audit_capture(
                        parse_har_or_json_to_rows(raw_text, default_artist=artist_name),
                        config=custom_cfg,
                    )
                    return _json_response(start_response, 200, result)

                # Default mode == "csv"
                csv_text = str(body.get("csv_text", ""))
                result = run_audit_from_csv_text(csv_text, config=custom_cfg)
                if result.get("artists"):
                    for a_stat in result["artists"]:
                        a_name = a_stat.get("artist")
                        if a_name and a_name not in ("Target_Bots", "Control_Users"):
                            a_profiles = [
                                p for p in result.get("profiles", []) if p.get("artist") == a_name
                            ]
                            if len(a_profiles) >= 3:
                                save_artist_checkpoint(
                                    artist=a_name,
                                    new_profiles=a_profiles,
                                )
                                _WORKER_ENGINE.notify_data_changed(artist=a_name)
                return _json_response(start_response, 200, result)

            except (CSVValidationError, ConfigError, ValueError, TypeError) as err:
                return _json_response(start_response, 400, {"error": str(err)})

        return _json_response(start_response, 404, {"error": f"Unknown endpoint: {path}"})

    return app


def _read_request_json(environ: dict[str, Any]) -> dict[str, Any]:
    """Read and parse a JSON request body from a WSGI environ dict."""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    stream = environ.get("wsgi.input")
    raw = stream.read(length) if stream and length > 0 else b"{}"
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"Invalid JSON payload: {err}") from err
    if not isinstance(data, dict):
        raise TypeError("JSON payload must be an object.")
    return data


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the multi-threaded local WSGI HTTP server + 4 background workers."""
    app = create_wsgi_app()
    _WORKER_ENGINE.start()
    with make_server(host, port, app, server_class=ThreadedWSGIServer) as httpd:
        print("=" * 64)
        print("BOT MICROSCOPE v3.0.0 - MULTI-WORKER LIVE ENGINE ACTIVE")
        print(f"Open in your browser: http://{host}:{port}")
        print("4 Background Workers warming Memory Bank, Ground-Truth & History")
        print("Press Ctrl+C to stop the server.")
        print("=" * 64)
        httpd.serve_forever()
