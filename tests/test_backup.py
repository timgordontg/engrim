"""Tests for `engrim backup` — a consistent copy of the store through SQLite's online backup API."""
import json
import os
import shutil
import sqlite3
import stat
import sys

import pytest

from engrim.cli import main


def _add(db, summary, project="/p"):
    main(["--db", str(db), "add", "-p", project, "-t", "fact", "-s", summary])


def _rows(db):
    c = sqlite3.connect(db)
    rows = c.execute("SELECT project, summary, status FROM memories ORDER BY id").fetchall()
    c.close()
    return rows


def test_backup_copies_every_record_and_reports_counts(tmp_path, capsys):
    db, copy = tmp_path / "m.db", tmp_path / "copy.db"
    _add(db, "one")
    _add(db, "two", project="/q")
    main(["--db", str(db), "add", "--global", "-t", "user", "-s", "three"])
    main(["--db", str(db), "supersede", "--id", "1", "--status", "done"])
    capsys.readouterr()
    main(["--db", str(db), "backup", str(copy)])
    assert capsys.readouterr().out.strip() == f"backed up 3 records, 2 active -> {copy}"
    assert _rows(copy) == _rows(db)


def test_backup_is_a_working_store(tmp_path, capsys):
    """The copy carries the FTS index, meta and log with it, so it opens and searches as a store."""
    db, copy = tmp_path / "m.db", tmp_path / "copy.db"
    _add(db, "retrieval beats stuffing the context window")
    main(["--db", str(db), "backup", str(copy)])
    capsys.readouterr()
    main(["--db", str(copy), "recall", "-p", "/p", "-q", "retrieval context"])
    assert "retrieval beats" in capsys.readouterr().out


def test_backup_is_independent_of_the_source(tmp_path):
    db, copy = tmp_path / "m.db", tmp_path / "copy.db"
    _add(db, "seed")
    main(["--db", str(db), "backup", str(copy)])
    _add(copy, "only in copy")
    _add(db, "only in source")
    assert [s for _, s, _ in _rows(db)] == ["seed", "only in source"]
    assert [s for _, s, _ in _rows(copy)] == ["seed", "only in copy"]


def test_backup_carries_what_a_plain_file_copy_leaves_in_the_wal(tmp_path, capsys):
    """A committed write from a connection that stays open (the MCP server, mid-session) lives in
    the -wal sidecar until a checkpoint. The backup folds it in; copying the .db file does not."""
    db, copy, naive = tmp_path / "m.db", tmp_path / "copy.db", tmp_path / "naive.db"
    _add(db, "checkpointed")
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO memories(ts,project,type,summary) "
                   "VALUES('2026-01-01T00:00:00+00:00','/p','fact','still in the wal')")
    writer.commit()
    try:
        shutil.copyfile(db, naive)
        main(["--db", str(db), "backup", str(copy)])
    finally:
        writer.close()
    assert "backed up 2 records, 2 active" in capsys.readouterr().out
    assert [s for _, s, _ in _rows(copy)] == ["checkpointed", "still in the wal"]
    assert [s for _, s, _ in _rows(naive)] == ["checkpointed"]


def test_backup_while_another_writer_is_mid_transaction(tmp_path, capsys):
    """An uncommitted write in flight elsewhere neither blocks the copy nor leaks into it."""
    db, copy = tmp_path / "m.db", tmp_path / "copy.db"
    _add(db, "committed")
    writer = sqlite3.connect(db)
    writer.execute(
        "INSERT INTO memories(ts,project,type,summary) VALUES('2026-01-01T00:00:00+00:00','/p','fact','uncommitted')")
    try:
        main(["--db", str(db), "backup", str(copy)])
    finally:
        writer.rollback()
        writer.close()
    assert "backed up 1 records, 1 active" in capsys.readouterr().out
    assert [s for _, s, _ in _rows(copy)] == ["committed"]


def test_backup_refuses_to_overwrite_without_force(tmp_path, capsys):
    db, copy = tmp_path / "m.db", tmp_path / "copy.db"
    _add(db, "one")
    copy.write_text("precious")
    with pytest.raises(SystemExit):
        main(["--db", str(db), "backup", str(copy)])
    assert copy.read_text() == "precious"


def test_backup_force_overwrites_a_stale_copy(tmp_path, capsys):
    db, copy = tmp_path / "m.db", tmp_path / "copy.db"
    _add(db, "one")
    main(["--db", str(db), "backup", str(copy)])
    _add(db, "two")
    capsys.readouterr()
    main(["--db", str(db), "backup", str(copy), "--force"])
    assert "backed up 2 records, 2 active" in capsys.readouterr().out
    assert [s for _, s, _ in _rows(copy)] == ["one", "two"]


def test_backup_force_still_rejects_a_non_sqlite_file(tmp_path):
    """A plain message, not a traceback, when --force points at something SQLite can't open."""
    db, junk = tmp_path / "m.db", tmp_path / "junk.db"
    _add(db, "one")
    junk.write_text("not a database")
    with pytest.raises(SystemExit) as e:
        main(["--db", str(db), "backup", str(junk), "--force"])
    assert "backup:" in str(e.value)


def test_backup_rejects_the_store_itself_and_its_sidecars(tmp_path):
    db = tmp_path / "m.db"
    _add(db, "one")
    for target in (db, tmp_path / "m.db-wal", tmp_path / "m.db-shm"):
        with pytest.raises(SystemExit) as e:
            main(["--db", str(db), "backup", str(target), "--force"])
        assert "the store itself" in str(e.value)
    assert [s for _, s, _ in _rows(db)] == ["one"]


def test_backup_rejects_a_directory_destination_with_a_message(tmp_path):
    db, out = tmp_path / "m.db", tmp_path / "out"
    _add(db, "one")
    out.mkdir()
    for extra in ([], ["--force"]):
        with pytest.raises(SystemExit) as e:
            main(["--db", str(db), "backup", str(out)] + extra)
        assert str(e.value).startswith("backup:")
    assert out.is_dir() and not any(out.iterdir())


def test_backup_creates_the_destination_directory(tmp_path):
    db, copy = tmp_path / "m.db", tmp_path / "out" / "nested" / "copy.db"
    _add(db, "one")
    main(["--db", str(db), "backup", str(copy)])
    assert [s for _, s, _ in _rows(copy)] == ["one"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_backup_is_owner_only_like_the_store(tmp_path):
    db, copy = tmp_path / "m.db", tmp_path / "copy.db"
    _add(db, "one")
    main(["--db", str(db), "backup", str(copy)])
    assert stat.S_IMODE(os.stat(copy).st_mode) == 0o600


def test_backup_json_reports_the_copy(tmp_path, capsys):
    """--json follows recall/list/logs: opt-in, one line, machine-shaped."""
    db, copy = tmp_path / "m.db", tmp_path / "copy.db"
    _add(db, "kept")
    _add(db, "done")
    main(["--db", str(db), "supersede", "--id", "2", "--status", "done"])
    capsys.readouterr()
    main(["--db", str(db), "backup", str(copy), "--json"])
    assert json.loads(capsys.readouterr().out) == {"records": 2, "active": 1, "dest": str(copy)}
    assert [s for _, s, _ in _rows(copy)] == ["kept", "done"]
