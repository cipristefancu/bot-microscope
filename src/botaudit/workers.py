"""Multi-worker background engine for live Instagram auditing, memory bank, and UI sync.

Coordinates 4 lightweight background daemon threads (100% local, zero internet calls):
  1. MemoryBankWorker: Pre-warms unified confirmed Bots + Humans + observed profile metrics
     at server startup so the Chrome Extension recognizes confirmed accounts immediately
     while scrolling (even before index.html is ever opened).
  2. GroundTruthSyncWorker: Synchronizes verified_bots.csv, verified_users.csv, and
     verified_benchmarks.csv off the main HTTP request thread.
  3. AuditPrecomputeWorker: Pre-computes rich History card breakdowns (bot %, real %,
     confirmed counts) and pre-warms active artist audits after tab opens or marks.
  4. LivePulseTracker: Maintains a thread-safe monotonic revision counter and human-readable
     activity feed for live zero-refresh updates in index.html.
"""

from __future__ import annotations

import collections
import datetime
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class AuditWorkerEngine:
    """Thread-safe 4-worker local background coordinator for the WSGI server."""

    def __init__(
        self,
        *,
        debounce_memory_s: float = 0.35,
        debounce_sync_s: float = 0.90,
        debounce_precompute_s: float = 1.40,
    ) -> None:
        self._lock = threading.RLock()
        self._started = False
        self._stop_event = threading.Event()
        self._debounce_memory_s = max(0.0, float(debounce_memory_s))
        self._debounce_sync_s = max(0.0, float(debounce_sync_s))
        self._debounce_precompute_s = max(0.0, float(debounce_precompute_s))

        self._memory_queue: queue.Queue[str] = queue.Queue()
        self._sync_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._precompute_queue: queue.Queue[str] = queue.Queue()

        self._revision: int = 1
        self._last_updated_iso: str = self._now_iso()
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=25)

        self._memory_bank_cache: dict[str, Any] | None = None
        self._memory_bank_cached_at_revision: int = -1
        self._history_rich_cache: list[dict[str, Any]] | None = None
        self._history_cached_at_revision: int = -1
        self._prewarmed_audits: dict[str, dict[str, Any]] = {}

        self._tasks_processed: dict[str, int] = {
            "memory_bank_refreshes": 0,
            "ground_truth_syncs": 0,
            "audit_precomputes": 0,
            "live_events_logged": 0,
        }

        self._build_memory_bank_fn: Callable[[], dict[str, Any]] | None = None
        self._sync_disk_fn: Callable[[], None] | None = None
        self._build_history_fn: Callable[[], list[dict[str, Any]]] | None = None
        self._precompute_artist_fn: Callable[[str], dict[str, Any] | None] | None = None

    @staticmethod
    def _now_iso() -> str:
        return datetime.datetime.now(tz=datetime.UTC).strftime("%H:%M:%S UTC")

    def configure_callbacks(
        self,
        *,
        build_memory_bank_fn: Callable[[], dict[str, Any]],
        sync_disk_fn: Callable[[], None],
        build_history_fn: Callable[[], list[dict[str, Any]]],
        precompute_artist_fn: Callable[[str], dict[str, Any] | None],
    ) -> None:
        """Bind backend builder functions from web.py."""
        with self._lock:
            self._build_memory_bank_fn = build_memory_bank_fn
            self._sync_disk_fn = sync_disk_fn
            self._build_history_fn = build_history_fn
            self._precompute_artist_fn = precompute_artist_fn

    def start(self) -> None:
        """Launch background daemon threads once and trigger Step-0 startup pre-warm."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()

            workers = [
                threading.Thread(
                    target=self._run_memory_worker,
                    name="BotAudit-Worker1-MemoryBank",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._run_sync_worker,
                    name="BotAudit-Worker2-GroundTruthSync",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._run_precompute_worker,
                    name="BotAudit-Worker3-AuditPrecompute",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._run_pulse_watchdog,
                    name="BotAudit-Worker4-LivePulse",
                    daemon=True,
                ),
            ]
            for w in workers:
                w.start()

            self.record_event(
                kind="startup",
                artist="system",
                username="",
                summary_en="Engine started — loading confirmed Bots, Real People & checked profiles",
                summary_ro="Motor pornit — se încarcă boții confirmați, oamenii reali și profilurile",
            )
            self._memory_queue.put("startup_warm")
            self._precompute_queue.put("__HISTORY_ONLY__")

    def stop(self) -> None:
        """Signal all background worker threads to exit."""
        with self._lock:
            self._stop_event.set()
            self._started = False

    def record_event(
        self,
        *,
        kind: str,
        artist: str,
        username: str,
        summary_en: str,
        summary_ro: str,
        label: str | None = None,
        score_pts: int | None = None,
    ) -> int:
        """Append a live human-readable activity event and bump the revision number."""
        with self._lock:
            self._revision += 1
            self._last_updated_iso = self._now_iso()
            self._tasks_processed["live_events_logged"] += 1
            evt = {
                "seq": self._revision,
                "at": self._last_updated_iso,
                "kind": kind,
                "artist": artist,
                "username": username,
                "label": label,
                "score_pts": score_pts,
                "summary_en": summary_en,
                "summary_ro": summary_ro,
            }
            self._events.appendleft(evt)
            return self._revision

    def invalidate_caches(self, *, artist: str = "") -> int:
        """Invalidate hot caches and bump revision without waking background workers."""
        clean_artist = artist.strip().lstrip("@")
        with self._lock:
            self._revision += 1
            self._last_updated_iso = self._now_iso()
            self._memory_bank_cache = None
            self._memory_bank_cached_at_revision = -1
            self._history_rich_cache = None
            self._history_cached_at_revision = -1
            if clean_artist:
                self._prewarmed_audits.pop(clean_artist.lower(), None)
            else:
                self._prewarmed_audits.clear()
            return self._revision

    def notify_data_changed(
        self,
        *,
        artist: str = "",
        sync_disk: bool = False,
        precompute_artist: bool = True,
    ) -> None:
        """Invalidate stale caches and schedule background workers to refresh hot state."""
        clean_artist = artist.strip().lstrip("@")
        self.invalidate_caches(artist=clean_artist)

        if self._started:
            if sync_disk:
                self._sync_queue.put({"artist": clean_artist})
            self._memory_queue.put("refresh")
            if precompute_artist and clean_artist:
                self._precompute_queue.put(clean_artist)
            else:
                self._precompute_queue.put("__HISTORY_ONLY__")

    def get_cached_memory_bank(self) -> dict[str, Any] | None:
        """Return hot pre-warmed memory bank if fresh for the current revision."""
        with self._lock:
            if (
                self._memory_bank_cache is None
                or self._memory_bank_cached_at_revision < self._revision
            ):
                return None
            return self._memory_bank_cache

    def set_cached_memory_bank(
        self, payload: dict[str, Any], revision: int | None = None
    ) -> None:
        """Store freshly computed memory bank in RAM if not superseded by a newer revision."""
        with self._lock:
            target_rev = self._revision if revision is None else revision
            if target_rev < self._revision:
                return
            self._memory_bank_cache = payload
            self._memory_bank_cached_at_revision = target_rev

    def get_cached_history(self) -> list[dict[str, Any]] | None:
        """Return hot pre-warmed rich history list if fresh for the current revision."""
        with self._lock:
            if (
                self._history_rich_cache is None
                or self._history_cached_at_revision < self._revision
            ):
                return None
            return [dict(item) for item in self._history_rich_cache]

    def set_cached_history(
        self, items: list[dict[str, Any]], revision: int | None = None
    ) -> None:
        """Store rich history metadata in RAM if not superseded by a newer revision."""
        with self._lock:
            target_rev = self._revision if revision is None else revision
            if target_rev < self._revision:
                return
            self._history_rich_cache = [dict(item) for item in items]
            self._history_cached_at_revision = target_rev

    def get_prewarmed_audit(self, artist: str) -> dict[str, Any] | None:
        """Return pre-computed artist audit if fresh."""
        key = artist.strip().lstrip("@").lower()
        with self._lock:
            entry = self._prewarmed_audits.get(key)
            if entry is None:
                return None
            if isinstance(entry, dict) and "_cached_at_revision" in entry:
                if entry["_cached_at_revision"] < self._revision:
                    return None
                return dict(entry["data"])
            return dict(entry)

    def set_prewarmed_audit(
        self, artist: str, result: dict[str, Any], revision: int | None = None
    ) -> None:
        """Cache pre-computed artist audit if not superseded by a newer revision."""
        key = artist.strip().lstrip("@").lower()
        if not key:
            return
        with self._lock:
            target_rev = self._revision if revision is None else revision
            if target_rev < self._revision:
                return
            self._prewarmed_audits[key] = {
                "_cached_at_revision": target_rev,
                "data": result,
            }

    def get_status_snapshot(self, active_artist: str | None = None) -> dict[str, Any]:
        """Return live worker telemetry, revision counter, and recent activity feed."""
        with self._lock:
            mb = self._memory_bank_cache or {}
            totals = mb.get("totals") or {}
            return {
                "ok": True,
                "workers_running": self._started,
                "worker_count": 4,
                "revision": self._revision,
                "last_updated": self._last_updated_iso,
                "active_artist": active_artist or "",
                "queues_pending": (
                    self._memory_queue.qsize()
                    + self._sync_queue.qsize()
                    + self._precompute_queue.qsize()
                ),
                "tasks_processed": dict(self._tasks_processed),
                "memory_bank_totals": totals,
                "recent_events": list(self._events)[:12],
            }

    # ------------------------------------------------------------------ workers

    def _coalesce_wait(
        self,
        q: queue.Queue[Any],
        initial_item: Any,
        delay_s: float,
        *,
        collect_unique: bool = False,
        skip_debounce: bool = False,
    ) -> list[Any]:
        """Wait briefly for burst notifications and drain duplicates from `q`."""
        items: list[Any] = [initial_item]

        def _drain() -> None:
            while not q.empty():
                try:
                    nxt = q.get_nowait()
                    if not collect_unique or nxt not in items:
                        items.append(nxt)
                except queue.Empty:
                    break

        _drain()
        if not skip_debounce and delay_s > 0.0:
            if self._stop_event.wait(timeout=delay_s):
                _drain()
                return items
            _drain()
        return items

    def _run_memory_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                first_item = self._memory_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self._coalesce_wait(
                self._memory_queue,
                first_item,
                self._debounce_memory_s,
                skip_debounce=(first_item == "startup_warm"),
            )
            if self._stop_event.is_set():
                break
            fn = self._build_memory_bank_fn
            if fn is not None:
                try:
                    with self._lock:
                        rev_before = self._revision
                    data = fn()
                    self.set_cached_memory_bank(data, revision=rev_before)
                    with self._lock:
                        self._tasks_processed["memory_bank_refreshes"] += 1
                except Exception as err:  # noqa: BLE001
                    logger.debug("MemoryBankWorker refresh skipped: %s", err)

    def _run_sync_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                first_item = self._sync_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self._coalesce_wait(
                self._sync_queue,
                first_item,
                self._debounce_sync_s,
            )
            if self._stop_event.is_set():
                break
            fn = self._sync_disk_fn
            if fn is not None:
                try:
                    fn()
                    with self._lock:
                        self._tasks_processed["ground_truth_syncs"] += 1
                    self._memory_queue.put("post_sync")
                except Exception as err:  # noqa: BLE001
                    logger.debug("GroundTruthSyncWorker skipped: %s", err)

    def _run_precompute_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                target = self._precompute_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            is_startup_hist = (
                target == "__HISTORY_ONLY__"
                and self._tasks_processed.get("audit_precomputes", 0) == 0
                and self._history_rich_cache is None
            )
            targets = self._coalesce_wait(
                self._precompute_queue,
                target,
                self._debounce_precompute_s,
                collect_unique=True,
                skip_debounce=is_startup_hist,
            )
            if self._stop_event.is_set():
                break

            hist_fn = self._build_history_fn
            if hist_fn is not None:
                try:
                    with self._lock:
                        rev_before = self._revision
                    rich_hist = hist_fn()
                    self.set_cached_history(rich_hist, revision=rev_before)
                except Exception as err:  # noqa: BLE001
                    logger.debug("History precompute skipped: %s", err)

            audit_fn = self._precompute_artist_fn
            if audit_fn is not None:
                for t in targets:
                    if t and t != "__HISTORY_ONLY__":
                        try:
                            with self._lock:
                                rev_before = self._revision
                            res = audit_fn(t)
                            if res is not None:
                                self.set_prewarmed_audit(t, res, revision=rev_before)
                                with self._lock:
                                    self._tasks_processed["audit_precomputes"] += 1
                        except Exception as err:  # noqa: BLE001
                            logger.debug("Artist precompute skipped for %s: %s", t, err)

    def _run_pulse_watchdog(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(15.0)
            if self._memory_bank_cache is None and self._build_memory_bank_fn is not None:
                self._memory_queue.put("periodic")

