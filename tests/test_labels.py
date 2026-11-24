"""Tests for the ground-truth label loader (owner rule: a handle in both files is a human)."""

from __future__ import annotations

from pathlib import Path

import pytest

from botaudit.labels import (
    DEFAULT_BOTS_PATH,
    DEFAULT_USERS_PATH,
    TEST_PERCENT,
    LabelError,
    LabelNote,
    load_label_notes,
    load_labels,
    save_label_note,
    split_of,
    sync_ground_truth_lists,
    sync_sample_benchmarks,
)


def _write(path: Path, lines: list[str], encoding: str = "utf-8") -> Path:
    path.write_text("\n".join(lines) + "\n", encoding=encoding)
    return path


def test_links_handles_case_and_trailing_slashes_are_normalised(tmp_path: Path) -> None:
    bots = _write(
        tmp_path / "bots.csv",
        [
            "https://www.instagram.com/Zebra.9605848/",
            "@quail.87407895",
            "  _.little_qf.y  ",
            "https://instagram.com/mamamatii_?igsh=abc",
        ],
        encoding="utf-8-sig",
    )
    users = _write(tmp_path / "users.csv", ["karinushul"])
    labels = load_labels(bots, users)
    assert labels.bots == {"zebra.9605848", "quail.87407895", "_.little_qf.y", "mamamatii_"}
    assert labels.humans == {"karinushul"}
    assert len(labels) == 5
    assert labels.label_of("ZEBRA.9605848 ") == "bot"
    assert labels.label_of("KarinUshul") == "human"
    assert labels.label_of("never.heard.of") is None


def test_handle_in_both_files_is_a_human(tmp_path: Path) -> None:
    bots = _write(tmp_path / "bots.csv", ["zebra.9605848", "tekk1177"])
    users = _write(tmp_path / "users.csv", ["TEKK1177", "karinushul"])
    labels = load_labels(bots, users)
    assert labels.conflicts == ("tekk1177",)
    assert "tekk1177" not in labels.bots
    assert labels.label_of("tekk1177") == "human"
    assert labels.bots == {"zebra.9605848"}
    assert labels.humans == {"tekk1177", "karinushul"}


def test_repeated_lines_are_reported_but_counted_once(tmp_path: Path) -> None:
    bots = _write(tmp_path / "bots.csv", ["a.b", "a.b", "c_d"])
    users = _write(tmp_path / "users.csv", ["x.y", "x.y", "x.y"])
    labels = load_labels(bots, users)
    assert labels.bot_duplicates == ("a.b",)
    assert labels.human_duplicates == ("x.y",)
    assert len(labels.bots) == 2
    assert len(labels.humans) == 1


def test_lines_that_are_not_handles_are_ignored_and_listed(tmp_path: Path) -> None:
    bots = _write(tmp_path / "bots.csv", ["artist,username", "real.bot", "two words"])
    users = _write(tmp_path / "users.csv", ["real.user"])
    labels = load_labels(bots, users)
    assert labels.bots == {"real.bot"}
    assert labels.ignored_lines == ("artist,username", "two words")


def test_missing_label_file_is_a_clear_error(tmp_path: Path) -> None:
    users = _write(tmp_path / "users.csv", ["real.user"])
    with pytest.raises(LabelError, match="not found"):
        load_labels(tmp_path / "missing.csv", users)


def test_split_is_stable_and_close_to_the_requested_share() -> None:
    handles = [f"account_{i}.{i * 7}" for i in range(3000)]
    first = [split_of(h) for h in handles]
    assert first == [split_of(h) for h in handles]  # same answer every time
    assert split_of("Account_5.35") == split_of("account_5.35 ")  # case/space do not matter
    share = first.count("test") / len(first)
    assert abs(share - TEST_PERCENT / 100) < 0.03


def test_adding_labels_never_moves_an_existing_handle(tmp_path: Path) -> None:
    users = _write(tmp_path / "users.csv", ["human.one"])
    small = load_labels(_write(tmp_path / "b1.csv", [f"bot{i}.x" for i in range(20)]), users)
    large = load_labels(_write(tmp_path / "b2.csv", [f"bot{i}.x" for i in range(200)]), users)
    for handle in small.bots:
        assert (handle in small.only("test").bots) == (handle in large.only("test").bots)


def test_train_and_test_splits_partition_the_labels(tmp_path: Path) -> None:
    labels = load_labels(
        _write(tmp_path / "b.csv", [f"bot{i}.x" for i in range(100)]),
        _write(tmp_path / "u.csv", [f"human{i}.y" for i in range(100)]),
    )
    train, test = labels.only("train"), labels.only("test")
    assert train.bots | test.bots == labels.bots
    assert train.bots.isdisjoint(test.bots)
    assert train.humans | test.humans == labels.humans
    assert train.humans.isdisjoint(test.humans)
    assert labels.only("all") is labels
    with pytest.raises(LabelError, match="Unknown split"):
        labels.only("validation")


def test_the_real_label_files_follow_the_owner_rule() -> None:
    """Checks invariants only, so it stays valid while the owner keeps adding labels."""
    if not DEFAULT_BOTS_PATH.is_file() or not DEFAULT_USERS_PATH.is_file():
        pytest.skip("label files not present")
    labels = load_labels()
    assert labels.bots.isdisjoint(labels.humans)
    assert set(labels.conflicts) <= labels.humans
    assert len(labels) == len(labels.bots) + len(labels.humans) > 0
    assert not any(any(ch.isspace() for ch in handle) for handle in labels.bots | labels.humans)


def test_label_note_validation() -> None:
    note = LabelNote(username="  @bad_bot.123  ", status="available", label="bot")
    assert note.username == "bad_bot.123"

    with pytest.raises(LabelError, match="cannot be empty"):
        LabelNote(username="")

    with pytest.raises(LabelError, match="Invalid status"):
        LabelNote(username="valid_user", status="non_existent_status")

    with pytest.raises(LabelError, match="Invalid label"):
        LabelNote(username="valid_user", label="maybe_a_robot")


def test_label_notes_save_load_roundtrip(tmp_path: Path) -> None:
    notes_csv = tmp_path / "labels" / "label_notes.csv"
    assert load_label_notes(notes_csv) == {}

    note1 = LabelNote(
        username="bot.farm.001",
        label="bot",
        bot_type="farm_template",
        labeler="researcher",
        labeled_on="2026-11-06",
        evidence_note="Day-1 setup burst 4 posts",
        status="available",
    )
    note2 = LabelNote(
        username="deleted.account",
        label="bot",
        status="deleted",
    )
    save_label_note(note1, notes_csv)
    save_label_note(note2, notes_csv)

    loaded = load_label_notes(notes_csv)
    assert len(loaded) == 2
    assert loaded["bot.farm.001"].bot_type == "farm_template"
    assert loaded["bot.farm.001"].status == "available"
    assert loaded["deleted.account"].status == "deleted"


def test_load_labels_integrates_notes_and_unsure(tmp_path: Path) -> None:
    bots = _write(tmp_path / "bots.csv", ["bot1", "bot2", "bot3"])
    users = _write(tmp_path / "users.csv", ["user1", "user2"])
    notes_csv = tmp_path / "label_notes.csv"

    # bot2 is marked unsure -> should be removed from bots and humans
    save_label_note(LabelNote(username="bot2", label="unsure"), notes_csv)
    # bot3 is marked deleted
    save_label_note(LabelNote(username="bot3", status="deleted"), notes_csv)
    # user2 is marked private_unavailable
    save_label_note(LabelNote(username="user2", status="private_unavailable"), notes_csv)

    labels = load_labels(bots, users, notes_path=notes_csv)
    assert "bot2" not in labels.bots
    assert "bot2" not in labels.humans
    assert labels.label_of("bot2") == "unsure"
    assert "bot2" in labels.unsure
    assert labels.deleted_handles == {"bot3"}
    assert labels.private_unavailable_handles == {"user2"}

    # Split partitioning preserves unsure and notes
    train, test = labels.only("train"), labels.only("test")
    assert train.unsure | test.unsure == labels.unsure
    assert train.notes.keys() | test.notes.keys() == labels.notes.keys()


def test_sync_ground_truth_lists_syncs_notes_and_creates_backup(tmp_path: Path) -> None:
    bots_csv = _write(tmp_path / "verified_bots.csv", ["old_bot_1", "old_bot_2"])
    users_csv = _write(tmp_path / "verified_users.csv", ["old_user_1"])
    notes_csv = tmp_path / "labels" / "label_notes.csv"
    backup_dir = tmp_path / "backups"

    # Add notes: one bot, one human, one unsure
    save_label_note(LabelNote(username="new_bot_1", label="bot"), notes_csv, auto_sync=False)
    save_label_note(LabelNote(username="new_user_1", label="human"), notes_csv, auto_sync=False)
    save_label_note(LabelNote(username="old_bot_2", label="unsure"), notes_csv, auto_sync=False)

    bots_c, users_c = sync_ground_truth_lists(
        notes_path=notes_csv,
        bots_path=bots_csv,
        users_path=users_csv,
        backup_dir=backup_dir,
    )

    # old_bot_2 was marked unsure, so it is removed
    assert bots_c == 2  # old_bot_1, new_bot_1
    assert users_c == 2  # old_user_1, new_user_1

    labels = load_labels(bots_csv, users_csv, notes_path=notes_csv)
    assert labels.bots == {"old_bot_1", "new_bot_1"}
    assert labels.humans == {"old_user_1", "new_user_1"}
    assert labels.unsure == {"old_bot_2"}

    # Backup files must exist
    backups = list(backup_dir.glob("*.csv"))
    assert len(backups) >= 2


def test_sync_sample_benchmarks_extracts_observed_profiles_only(tmp_path: Path) -> None:
    bots_csv = _write(tmp_path / "verified_bots.csv", ["target_bot"])
    users_csv = _write(tmp_path / "verified_users.csv", ["control_user", "unobserved_user"])
    notes_csv = tmp_path / "label_notes.csv"
    out_csv = tmp_path / "sample" / "verified_benchmarks.csv"

    # Checkpoint with real observed numbers for target_bot and control_user
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    import json

    cp_data = {
        "artist": "audited_target",
        "profiles": [
            {
                "username": "target_bot",
                "has_profile_photo": False,
                "post_count": 0,
                "followers_count": 12,
                "following_count": 1800,
                "bio_length": 0,
                "account_private": False,
                "username_digit_ratio": 0.45,
                "score_complete": True,
            },
            {
                "username": "control_user",
                "has_profile_photo": True,
                "post_count": 55,
                "followers_count": 420,
                "following_count": 310,
                "bio_length": 45,
                "account_private": True,
                "username_digit_ratio": 0.0,
                "score_complete": True,
            },
            {
                # Incomplete / handle-only profile: should be skipped per R-NOFAKE
                "username": "unobserved_user",
                "has_profile_photo": True,
                "post_count": None,
                "followers_count": None,
                "following_count": None,
            },
        ],
    }
    (cp_dir / "target.json").write_text(json.dumps(cp_data), encoding="utf-8")

    count = sync_sample_benchmarks(
        checkpoints_dir=cp_dir,
        output_path=out_csv,
        notes_path=notes_csv,
        bots_path=bots_csv,
        users_path=users_csv,
    )

    assert count == 2  # target_bot and control_user; unobserved_user excluded!
    assert out_csv.is_file()

    from botaudit.loader import load_followers_csv

    res = load_followers_csv(out_csv)
    assert len(res.df) == 2
    assert len(res.rejects) == 0
    assert set(res.df["artist"]) == {"Target_Bots", "Control_Users"}


def test_sync_ground_truth_lists_fresh_clone_creates_missing_csvs(tmp_path: Path) -> None:
    """On a brand-new clone with no data CSVs, sync_ground_truth_lists creates both files safely."""
    bots_csv = tmp_path / "verified_bots.csv"
    users_csv = tmp_path / "verified_users.csv"
    notes_csv = tmp_path / "labels" / "label_notes.csv"

    bots_c, users_c = sync_ground_truth_lists(
        notes_path=notes_csv,
        bots_path=bots_csv,
        users_path=users_csv,
        backup_dir=None,
    )
    assert bots_c == 0
    assert users_c == 0
    assert bots_csv.is_file()
    assert users_csv.is_file()

    labels = load_labels(bots_csv, users_csv, notes_path=notes_csv)
    assert len(labels) == 0

