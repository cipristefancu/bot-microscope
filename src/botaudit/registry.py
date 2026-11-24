"""Local Bot Registry and Cross-Artist Recycling Fingerprinter (Enhancement B).

Maintains a local, privacy-first, 100% offline registry of confirmed bot and human
accounts derived from data/labels/label_notes.csv and data/verified_bots.csv, and detects
cross-artist bot network recycling across audited checkpoints.

This tool never connects to the internet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from botaudit.handles import extract_username
from botaudit.labels import (
    DEFAULT_BOTS_PATH,
    DEFAULT_NOTES_PATH,
    DEFAULT_USERS_PATH,
    LabelError,
    LabelSet,
    load_labels,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHECKPOINTS_DIR = PROJECT_ROOT / "data" / "checkpoints"


@dataclass(frozen=True)
class LocalBotRegistry:
    """Read-only view of confirmed accounts and cross-artist occurrences."""

    known_bots: frozenset[str]
    known_humans: frozenset[str]
    unsure_handles: frozenset[str] = field(default_factory=frozenset)
    # username (lowercase) -> set of artist checkpoint stems where they were observed
    artist_occurrences: dict[str, frozenset[str]] = field(default_factory=dict)

    def is_known_bot(self, username: str) -> bool:
        """True if the handle is in the confirmed bot registry and not a human."""
        clean = extract_username(username).lower()
        return clean in self.known_bots

    def is_known_human(self, username: str) -> bool:
        """True if the handle is confirmed as a real human."""
        clean = extract_username(username).lower()
        return clean in self.known_humans

    def get_artist_occurrences(self, username: str) -> list[str]:
        """List of artist names where this follower was observed across checkpoints."""
        clean = extract_username(username).lower()
        return sorted(self.artist_occurrences.get(clean, frozenset()))

    def is_cross_artist_recycled(self, username: str, min_artists: int = 2) -> bool:
        """True if this handle appears in follower lists of multiple audited creators."""
        clean = extract_username(username).lower()
        return len(self.artist_occurrences.get(clean, frozenset())) >= min_artists

    def find_shared_bots(self, artist_a: str, artist_b: str) -> list[str]:
        """Return handles that appear in both artists' checkpoints AND are confirmed bots."""
        target_a = artist_a.lower().replace("@", "").strip()
        target_b = artist_b.lower().replace("@", "").strip()
        shared: list[str] = []
        for uname, artists in self.artist_occurrences.items():
            if target_a in artists and target_b in artists and uname in self.known_bots:
                shared.append(uname)
        return sorted(shared)

    def summarize(self) -> dict[str, Any]:
        """Aggregate statistics on registry coverage and bot network overlap."""
        multi_artist_users = [
            u for u, arts in self.artist_occurrences.items() if len(arts) >= 2
        ]
        multi_artist_bots = [u for u in multi_artist_users if u in self.known_bots]
        return {
            "total_known_bots": len(self.known_bots),
            "total_known_humans": len(self.known_humans),
            "total_unsure": len(self.unsure_handles),
            "total_indexed_profiles": len(self.artist_occurrences),
            "total_multi_artist_profiles": len(multi_artist_users),
            "total_recycled_bots": len(multi_artist_bots),
        }


def load_local_registry(
    checkpoints_dir: Path | str | None = None,
    bots_path: Path | str | None = None,
    users_path: Path | str | None = None,
    notes_path: Path | str | None = None,
    label_set: LabelSet | None = None,
) -> LocalBotRegistry:
    """Build a LocalBotRegistry from ground-truth label files and checkpoint history."""
    if label_set is None:
        b_path = Path(bots_path) if bots_path is not None else DEFAULT_BOTS_PATH
        u_path = Path(users_path) if users_path is not None else DEFAULT_USERS_PATH
        n_path = Path(notes_path) if notes_path is not None else DEFAULT_NOTES_PATH
        try:
            label_set = load_labels(bots_path=b_path, users_path=u_path, notes_path=n_path)
        except (OSError, LabelError):
            label_set = LabelSet(
                bots=frozenset(),
                humans=frozenset(),
                conflicts=(),
                bot_duplicates=(),
                human_duplicates=(),
                ignored_lines=(),
                unsure=frozenset(),
                notes={},
            )

    ckpt_dir = (
        Path(checkpoints_dir)
        if checkpoints_dir is not None
        else DEFAULT_CHECKPOINTS_DIR
    )
    occurrences: dict[str, set[str]] = {}

    if ckpt_dir.is_dir():
        for json_file in ckpt_dir.glob("*.json"):
            artist_name = json_file.stem.lower()
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                profiles = data.get("profiles") or []
                for p in profiles:
                    if isinstance(p, dict) and p.get("username"):
                        clean = extract_username(str(p["username"])).lower()
                        if clean:
                            occurrences.setdefault(clean, set()).add(artist_name)
            except (OSError, json.JSONDecodeError):
                continue

    frozen_occurrences = {u: frozenset(arts) for u, arts in occurrences.items()}

    return LocalBotRegistry(
        known_bots=label_set.bots,
        known_humans=label_set.humans,
        unsure_handles=label_set.unsure,
        artist_occurrences=frozen_occurrences,
    )
