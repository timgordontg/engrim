"""Tests for `engrim merge` — folding one store's records into another."""
import shutil
import sqlite3

import pytest

from engrim.cli import main


def _add(db, summary, project="/p", type_="fact"):
    main(["--db", str(db), "add", "-p", project, "-t", type_, "-s", summary])


def _rows(db):
    c = sqlite3.connect(db)
    rows = c.execute("SELECT summary, status FROM memories ORDER BY id").fetchall()
    c.close()
    return rows


def _two_stores(tmp_path):
    """A and B share two records (B is a copy of A), then each gains one of its own."""
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _add(a, "shared one")
    _add(a, "shared two")
    shutil.copyfile(a, b)
    _add(a, "only in a")
    _add(b, "only in b")
    return a, b


def test_merge_adds_missing_and_skips_shared(tmp_path, capsys):
    a, b = _two_stores(tmp_path)
    main(["--db", str(a), "merge", str(b)])
    assert "+1 add, ~0 status, 2 skip" in capsys.readouterr().out
    assert [s for s, _ in _rows(a)] == ["shared one", "shared two", "only in a", "only in b"]


def test_merge_is_idempotent(tmp_path, capsys):
    a, b = _two_stores(tmp_path)
    main(["--db", str(a), "merge", str(b)])
    capsys.readouterr()
    main(["--db", str(a), "merge", str(b)])
    assert "+0 add, ~0 status, 3 skip" in capsys.readouterr().out
    assert len(_rows(a)) == 4


def test_merge_carries_retirement_and_never_reactivates(tmp_path, capsys):
    a, b = _two_stores(tmp_path)
    main(["--db", str(b), "supersede", "--id", "1", "--status", "done"])
    main(["--db", str(a), "merge", str(b)])
    assert "~1 status" in capsys.readouterr().out
    assert _rows(a)[0] == ("shared one", "done")
    # the other direction: a's still-active copy must not undo b's retirement
    main(["--db", str(b), "merge", str(a)])
    assert "~0 status" in capsys.readouterr().out
    assert _rows(b)[0] == ("shared one", "done")


def test_merge_is_a_no_op_in_either_direction_once_converged(tmp_path, capsys):
    a, b = _two_stores(tmp_path)
    main(["--db", str(a), "merge", str(b)])
    main(["--db", str(b), "merge", str(a)])
    capsys.readouterr()
    main(["--db", str(a), "merge", str(b)])
    assert "+0 add, ~0 status" in capsys.readouterr().out
    assert sorted(_rows(a)) == sorted(_rows(b))


def test_merge_dry_run_writes_nothing(tmp_path, capsys):
    a, b = _two_stores(tmp_path)
    main(["--db", str(a), "merge", str(b), "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "+1 add" in out and "only in b" in out
    assert len(_rows(a)) == 3


def test_merge_project_filter(tmp_path, capsys):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _add(b, "for p", project="/p")
    _add(b, "for q", project="/q")
    main(["--db", str(a), "merge", str(b), "-p", "/p"])
    assert "+1 add" in capsys.readouterr().out
    assert [s for s, _ in _rows(a)] == ["for p"]


def test_merged_records_are_searchable(tmp_path, capsys):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _add(a, "seed so the store exists")
    _add(b, "retrieval beats stuffing the context window")
    main(["--db", str(a), "merge", str(b)])
    capsys.readouterr()
    main(["--db", str(a), "recall", "-p", "/p", "-q", "retrieval context"])
    assert "retrieval beats stuffing" in capsys.readouterr().out


def test_merge_log_rows_dedup_with_and_without_uuid(tmp_path, capsys):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _add(a, "x")
    _add(b, "y")
    c = sqlite3.connect(b)
    c.execute("INSERT INTO log(ts,project,session,role,content,msg_uuid) VALUES(?,?,?,?,?,?)",
              ("2026-01-01T00:00:00+00:00", "/p", "s1", "user", "hello", "u-1"))
    c.commit(); c.close()
    c = sqlite3.connect(b)
    c.execute("INSERT INTO log(ts,project,session,role,content,msg_uuid) VALUES(?,?,?,?,?,NULL)",
              ("2026-01-01T00:00:01+00:00", "/p", "s1", "assistant", "[ran] just check"))
    c.commit(); c.close()
    main(["--db", str(a), "merge", str(b)])
    assert "+2 log" in capsys.readouterr().out
    main(["--db", str(a), "merge", str(b)])
    assert "+0 log" in capsys.readouterr().out
    assert sqlite3.connect(a).execute("SELECT count(*) FROM log").fetchone()[0] == 2


def test_merge_leaves_the_source_contents_untouched(tmp_path):
    a, b = _two_stores(tmp_path)
    before = b.read_bytes()
    main(["--db", str(a), "merge", str(b)])
    assert b.read_bytes() == before
    assert _rows(b) == [("shared one", "active"), ("shared two", "active"), ("only in b", "active")]


def test_merge_rejects_missing_and_non_store_sources(tmp_path):
    a = tmp_path / "a.db"
    _add(a, "x")
    with pytest.raises(SystemExit):
        main(["--db", str(a), "merge", str(tmp_path / "nope.db")])
    junk = tmp_path / "junk.db"
    junk.write_text("not a database")
    with pytest.raises(SystemExit):
        main(["--db", str(a), "merge", str(junk)])
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).execute("CREATE TABLE t(x)")
    with pytest.raises(SystemExit):
        main(["--db", str(a), "merge", str(empty)])


def test_merge_rejects_self(tmp_path):
    a = tmp_path / "a.db"
    _add(a, "x")
    with pytest.raises(SystemExit):
        main(["--db", str(a), "merge", str(a)])
