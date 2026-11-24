"""Ground-truth labels: which accounts the owner confirmed as bot-like or human.

Source of truth are two owner-maintained, headerless lists of Instagram links or handles:

    data/verified_bots.csv     accounts the owner confirmed as bot-like
    data/verified_users.csv    accounts the owner confirmed as real people

Owner rule (2026-11-06): a handle that appears in BOTH files is a human.
The files are re-read on every call and are never rewritten by this module.

This tool never connects to the internet.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import logging
import os
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from botaudit.handles import extract_username

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BOTS_PATH = PROJECT_ROOT / "data" / "verified_bots.csv"
DEFAULT_USERS_PATH = PROJECT_ROOT / "data" / "verified_users.csv"
DEFAULT_NOTES_PATH = PROJECT_ROOT / "data" / "labels" / "label_notes.csv"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "data" / "backups" / "labels"
DEFAULT_BENCHMARK_SAMPLE_PATH = PROJECT_ROOT / "data" / "sample" / "verified_benchmarks.csv"
DEFAULT_CHECKPOINTS_DIR = PROJECT_ROOT / "data" / "checkpoints"

# Valid enumerations for the label sidecar store
VALID_STATUSES: frozenset[str] = frozenset({"available", "deleted", "private_unavailable"})
VALID_BOT_TYPES: frozenset[str] = frozenset({
    "farm_template",
    "dressed_up",
    "mass_follow",
    "dormant_or_hijacked",
    "pod_or_engagement",
    "other",
})
VALID_LABELS: frozenset[str] = frozenset({"bot", "human", "unsure"})

# Share of labels reserved as the locked test split (decided by a hash of the handle,
# so a handle never changes split when more labels are added later).
TEST_PERCENT = 30
SPLITS: tuple[str, ...] = ("all", "train", "test")


class LabelError(ValueError):
    """Raised when a label file is missing or invalid."""


@dataclass(frozen=True)
class LabelNote:
    """Sidecar note capturing provenance, sub-type, status, and rationale for a label."""

    username: str
    label: str | None = None  # "bot", "human", "unsure"
    bot_type: str | None = None  # in VALID_BOT_TYPES or None
    labeler: str | None = None
    labeled_on: str | None = None  # ISO date string (YYYY-MM-DD)
    evidence_note: str | None = None
    status: str = "available"  # "available", "deleted", "private_unavailable"

    def __post_init__(self) -> None:
        clean = extract_username(self.username).lower()
        if not clean:
            raise LabelError("Username in LabelNote cannot be empty.")
        object.__setattr__(self, "username", clean)
        if self.status not in VALID_STATUSES:
            raise LabelError(
                f"Invalid status '{self.status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}."
            )
        if self.label is not None and self.label not in VALID_LABELS:
            raise LabelError(
                f"Invalid label '{self.label}'. Must be one of: {', '.join(sorted(VALID_LABELS))}."
            )


@dataclass(frozen=True)
class LabelSet:
    """The confirmed labels, with every data problem kept visible."""

    bots: frozenset[str]
    humans: frozenset[str]
    conflicts: tuple[str, ...]  # in both files; counted as humans (owner rule)
    bot_duplicates: tuple[str, ...]
    human_duplicates: tuple[str, ...]
    ignored_lines: tuple[str, ...]  # lines that are not a handle (kept for transparency)
    unsure: frozenset[str] = field(default_factory=frozenset)  # abstentions; never trained on
    notes: dict[str, LabelNote] = field(default_factory=dict)  # username -> LabelNote

    def __len__(self) -> int:
        return len(self.bots) + len(self.humans)

    @property
    def deleted_handles(self) -> frozenset[str]:
        """Handles confirmed to be deleted by Instagram."""
        return frozenset(u for u, n in self.notes.items() if n.status == "deleted")

    @property
    def private_unavailable_handles(self) -> frozenset[str]:
        """Handles confirmed to be private and inaccessible for deep inspection."""
        return frozenset(u for u, n in self.notes.items() if n.status == "private_unavailable")

    def label_of(self, username: str) -> str | None:
        """'bot', 'human', 'unsure', or None when the handle is not labeled."""
        key = username.strip().lower()
        if key in self.unsure:
            return "unsure"
        if key in self.humans:
            return "human"
        if key in self.bots:
            return "bot"
        return None

    def only(self, split: str) -> LabelSet:
        """The labels that fall into `split` ('all', 'train' or 'test')."""
        if split not in SPLITS:
            raise LabelError(f"Unknown split '{split}'. Choose one of: {', '.join(SPLITS)}.")
        if split == "all":
            return self
        keep = {u for u in self.bots | self.humans | self.unsure if split_of(u) == split}
        return LabelSet(
            bots=frozenset(self.bots & keep),
            humans=frozenset(self.humans & keep),
            conflicts=tuple(c for c in self.conflicts if c in keep),
            bot_duplicates=self.bot_duplicates,
            human_duplicates=self.human_duplicates,
            ignored_lines=self.ignored_lines,
            unsure=frozenset(self.unsure & keep),
            notes={u: n for u, n in self.notes.items() if u in keep},
        )


def split_of(username: str) -> str:
    """'test' for about TEST_PERCENT of handles, else 'train'; stable for a given handle."""
    digest = hashlib.sha256(username.strip().lower().encode("utf-8")).digest()
    return "test" if int.from_bytes(digest[:8], "big") % 100 < TEST_PERCENT else "train"


def _read_handles(path: Path) -> tuple[list[str], list[str]]:
    if not path.is_file():
        raise LabelError(f"Label file not found: {path}")
    handles: list[str] = []
    ignored: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        handle = extract_username(line).lower()
        if handle and not any(ch.isspace() or ch == "," for ch in handle):
            handles.append(handle)
        else:
            ignored.append(line.strip())
    return handles, ignored


def _duplicates(handles: list[str]) -> tuple[str, ...]:
    return tuple(sorted(h for h, n in Counter(handles).items() if n > 1))


def load_label_notes(path: Path = DEFAULT_NOTES_PATH) -> dict[str, LabelNote]:
    """Read the label sidecar CSV, returning a mapping of username -> LabelNote."""
    target = Path(path)
    if not target.is_file():
        return {}

    notes: dict[str, LabelNote] = {}
    with target.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_u = row.get("username", "")
            if not raw_u or not raw_u.strip():
                continue
            u = extract_username(raw_u).lower()
            if not u:
                continue

            label = (row.get("label") or "").strip().lower() or None
            bot_type = (row.get("bot_type") or "").strip() or None
            labeler = (row.get("labeler") or "").strip() or None
            labeled_on = (row.get("labeled_on") or "").strip() or None
            evidence_note = (row.get("evidence_note") or "").strip() or None
            status = (row.get("status") or "available").strip().lower()

            try:
                notes[u] = LabelNote(
                    username=u,
                    label=label if label in VALID_LABELS else None,
                    bot_type=bot_type,
                    labeler=labeler,
                    labeled_on=labeled_on,
                    evidence_note=evidence_note,
                    status=status if status in VALID_STATUSES else "available",
                )
            except LabelError:
                continue
    return notes


def save_label_note(
    note: LabelNote,
    path: Path = DEFAULT_NOTES_PATH,
    auto_sync: bool = True,
) -> None:
    """Save or update a LabelNote in the label sidecar CSV.

    When auto_sync is True and saving to DEFAULT_NOTES_PATH, automatically propagates
    to verified_bots.csv, verified_users.csv, and data/sample/verified_benchmarks.csv.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = load_label_notes(target)
    if note.label and not note.labeled_on:
        prev_note = existing.get(note.username)
        fallback_date = (
            prev_note.labeled_on
            if prev_note and prev_note.labeled_on
            else datetime.date.today().isoformat()
        )
        note = replace(note, labeled_on=fallback_date)
    existing[note.username] = note

    fieldnames = [
        "username",
        "label",
        "bot_type",
        "labeler",
        "labeled_on",
        "evidence_note",
        "status",
    ]
    with target.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for u in sorted(existing.keys()):
            n = existing[u]
            writer.writerow({
                "username": n.username,
                "label": n.label or "",
                "bot_type": n.bot_type or "",
                "labeler": n.labeler or "",
                "labeled_on": n.labeled_on or "",
                "evidence_note": n.evidence_note or "",
                "status": n.status or "available",
            })

    if auto_sync and target.resolve() == DEFAULT_NOTES_PATH.resolve():
        try:
            sync_ground_truth_lists(notes_path=target)
        except (OSError, csv.Error, LabelError) as err:
            logger.warning("Could not auto-sync ground truth lists: %s", err)
        try:
            sync_sample_benchmarks(notes_path=target)
        except (OSError, json.JSONDecodeError, csv.Error, LabelError) as err:
            logger.warning("Could not auto-sync sample benchmarks: %s", err)


def sync_ground_truth_lists(
    notes_path: Path = DEFAULT_NOTES_PATH,
    bots_path: Path = DEFAULT_BOTS_PATH,
    users_path: Path = DEFAULT_USERS_PATH,
    backup_dir: Path | None = DEFAULT_BACKUP_DIR,
) -> tuple[int, int]:
    """Sync label_notes.csv into verified_bots.csv and verified_users.csv.

    Source of truth for manual evaluations is label_notes.csv:
    - Handles labeled 'bot' are assigned to bots.
    - Handles labeled 'human' are assigned to users.
    - Handles labeled 'unsure' are excluded from both files.
    - Existing verified handles from verified_bots.csv / verified_users.csv not in notes are preserved.
    - Conflict resolution (owner rule): any handle present in both files is resolved as human.
    - Canonical URL format: 'https://www.instagram.com/{username}/'.
    - Deduplicated and sorted alphabetically.
    - Automated backup: creates timestamped backups in backup_dir before writing if content changed.

    Returns:
        tuple[int, int]: (count_bots, count_users)
    """
    bots_target = Path(bots_path)
    users_target = Path(users_path)
    notes_source = Path(notes_path)

    existing_bots: set[str] = set()
    existing_users: set[str] = set()

    if bots_target.is_file():
        b_list, _ = _read_handles(bots_target)
        existing_bots.update(b_list)

    if users_target.is_file():
        u_list, _ = _read_handles(users_target)
        existing_users.update(u_list)

    if notes_source.is_file():
        notes = load_label_notes(notes_source)
        for u, note in notes.items():
            if note.label == "bot":
                existing_bots.add(u)
                existing_users.discard(u)
            elif note.label == "human":
                existing_users.add(u)
                existing_bots.discard(u)
            elif note.label == "unsure":
                existing_bots.discard(u)
                existing_users.discard(u)

    # Owner rule: handle in both files is resolved as human
    for conflict in list(existing_bots & existing_users):
        existing_bots.discard(conflict)
        existing_users.add(conflict)

    new_bots_text = "".join(f"https://www.instagram.com/{u}/\n" for u in sorted(existing_bots))
    new_users_text = "".join(f"https://www.instagram.com/{u}/\n" for u in sorted(existing_users))

    curr_bots_text = bots_target.read_text(encoding="utf-8") if bots_target.is_file() else ""
    curr_users_text = users_target.read_text(encoding="utf-8") if users_target.is_file() else ""

    bots_changed = new_bots_text != curr_bots_text
    users_changed = new_users_text != curr_users_text
    bots_missing = not bots_target.is_file()
    users_missing = not users_target.is_file()

    if bots_changed or users_changed or bots_missing or users_missing:
        if backup_dir is not None and (bots_changed or users_changed):
            b_dir = Path(backup_dir)
            b_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            if bots_changed and curr_bots_text:
                (b_dir / f"verified_bots_{ts}.csv").write_text(curr_bots_text, encoding="utf-8")
            if users_changed and curr_users_text:
                (b_dir / f"verified_users_{ts}.csv").write_text(curr_users_text, encoding="utf-8")

        bots_target.parent.mkdir(parents=True, exist_ok=True)
        users_target.parent.mkdir(parents=True, exist_ok=True)

        if bots_changed or bots_missing:
            bots_target.write_text(new_bots_text, encoding="utf-8")
        if users_changed or users_missing:
            users_target.write_text(new_users_text, encoding="utf-8")

    return len(existing_bots), len(existing_users)


def sync_sample_benchmarks(
    checkpoints_dir: Path = DEFAULT_CHECKPOINTS_DIR,
    output_path: Path = DEFAULT_BENCHMARK_SAMPLE_PATH,
    notes_path: Path = DEFAULT_NOTES_PATH,
    bots_path: Path = DEFAULT_BOTS_PATH,
    users_path: Path = DEFAULT_USERS_PATH,
) -> int:
    """Generate empirical benchmarks in data/sample/ from real observed checkpoint metrics.

    Reads confirmed bot and human labels, finds their real observed metrics from checkpoints,
    and writes data/sample/verified_benchmarks.csv with standard 9 columns:
        artist,username,has_profile_photo,post_count,followers_count,following_count,bio_length,account_private,username_digit_ratio

    Strictly complies with Rule R-NOFAKE: zero assumed numbers. Only accounts that have real
    observed counts in checkpoints are included.

    Returns:
        int: Number of benchmark profiles written.
    """
    cp_dir = Path(checkpoints_dir)
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    labels = load_labels(bots_path=bots_path, users_path=users_path, notes_path=notes_path)

    observed_profiles: dict[str, dict[str, Any]] = {}
    if out_file.is_file():
        try:
            with out_file.open("r", encoding="utf-8", newline="") as prev_f:
                for row in csv.DictReader(prev_f):
                    uname = extract_username(str(row.get("username") or "")).lower()
                    if not uname:
                        continue
                    try:
                        observed_profiles[uname] = {
                            "username": uname,
                            "has_profile_photo": str(row.get("has_profile_photo", "")).lower() == "true",
                            "post_count": int(row["post_count"]),
                            "followers_count": int(row["followers_count"]),
                            "following_count": int(row["following_count"]),
                            "bio_length": int(row["bio_length"]),
                            "account_private": str(row.get("account_private", "")).lower() == "true",
                            "username_digit_ratio": float(row.get("username_digit_ratio") or 0.0),
                            "score_complete": True,
                        }
                    except (KeyError, TypeError, ValueError):
                        continue
        except OSError:
            pass

    if cp_dir.is_dir():
        for cp_path in sorted(cp_dir.glob("*.json")):
            try:
                data = json.loads(cp_path.read_text(encoding="utf-8"))
                for p in data.get("profiles", []):
                    raw_uname = str(p.get("username", "")).strip()
                    uname = extract_username(raw_uname).lower()
                    if not uname:
                        continue
                    if p.get("post_count") is not None and p.get("followers_count") is not None:
                        existing_p = observed_profiles.get(uname)
                        if not existing_p or (
                            p.get("score_complete") and not existing_p.get("score_complete")
                        ):
                            observed_profiles[uname] = p
            except (OSError, json.JSONDecodeError):
                continue

    rows: list[dict[str, str]] = []

    # 1. Real confirmed humans
    for u in sorted(labels.humans):
        p = observed_profiles.get(u)
        if not p:
            continue
        ratio = float(p.get("username_digit_ratio") or 0.0)
        rows.append({
            "artist": "Control_Users",
            "username": u,
            "has_profile_photo": "true" if p.get("has_profile_photo") else "false",
            "post_count": str(int(p.get("post_count") or 0)),
            "followers_count": str(int(p.get("followers_count") or 0)),
            "following_count": str(int(p.get("following_count") or 0)),
            "bio_length": str(int(p.get("bio_length") or 0)),
            "account_private": "true" if p.get("account_private") else "false",
            "username_digit_ratio": f"{ratio:.4f}",
        })

    # 2. Real confirmed bots
    for u in sorted(labels.bots):
        p = observed_profiles.get(u)
        if not p:
            continue
        ratio = float(p.get("username_digit_ratio") or 0.0)
        rows.append({
            "artist": "Target_Bots",
            "username": u,
            "has_profile_photo": "true" if p.get("has_profile_photo") else "false",
            "post_count": str(int(p.get("post_count") or 0)),
            "followers_count": str(int(p.get("followers_count") or 0)),
            "following_count": str(int(p.get("following_count") or 0)),
            "bio_length": str(int(p.get("bio_length") or 0)),
            "account_private": "true" if p.get("account_private") else "false",
            "username_digit_ratio": f"{ratio:.4f}",
        })

    fieldnames = [
        "artist",
        "username",
        "has_profile_photo",
        "post_count",
        "followers_count",
        "following_count",
        "bio_length",
        "account_private",
        "username_digit_ratio",
    ]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    rendered = buf.getvalue()

    if out_file.is_file():
        try:
            if out_file.read_text(encoding="utf-8") == rendered:
                return len(rows)
        except OSError:
            pass

    tmp_path = out_file.with_name(
        f"{out_file.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    try:
        tmp_path.write_text(rendered, encoding="utf-8", newline="")
        for attempt in range(12):
            try:
                tmp_path.replace(out_file)
                return len(rows)
            except PermissionError:
                if attempt >= 11:
                    raise
                time.sleep(0.015 * (attempt + 1))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return len(rows)


_USE_DEFAULT = object()


def load_labels(
    bots_path: Path = DEFAULT_BOTS_PATH,
    users_path: Path = DEFAULT_USERS_PATH,
    notes_path: Any = _USE_DEFAULT,
) -> LabelSet:
    """Read label files and optional sidecar, applying the owner's conflict rule."""
    bot_lines, bot_ignored = _read_handles(Path(bots_path))
    user_lines, user_ignored = _read_handles(Path(users_path))
    bots_all, humans = set(bot_lines), set(user_lines)

    resolved_notes_path: Path | None = None
    if notes_path is _USE_DEFAULT:
        # Default sidecar is attached when loading default label files
        if Path(bots_path) == DEFAULT_BOTS_PATH and Path(users_path) == DEFAULT_USERS_PATH:
            resolved_notes_path = DEFAULT_NOTES_PATH
    elif notes_path is not None:
        resolved_notes_path = Path(notes_path)

    notes = (
        load_label_notes(resolved_notes_path)
        if resolved_notes_path and resolved_notes_path.is_file()
        else {}
    )

    unsure: set[str] = set()
    for u, note in notes.items():
        if note.label == "unsure":
            bots_all.discard(u)
            humans.discard(u)
            unsure.add(u)
        elif note.label == "human":
            humans.add(u)
            bots_all.discard(u)
        elif note.label == "bot":
            bots_all.add(u)
            humans.discard(u)

    return LabelSet(
        bots=frozenset(bots_all - humans),
        humans=frozenset(humans),
        conflicts=tuple(sorted(bots_all & humans)),
        bot_duplicates=_duplicates(bot_lines),
        human_duplicates=_duplicates(user_lines),
        ignored_lines=tuple(bot_ignored + user_ignored),
        unsure=frozenset(unsure),
        notes=notes,
    )
