"""Tests for `engrim count` — one line, whole store: `N records, M active`."""
import json

from engrim.cli import main


def _add(db, summary, project="/p", type_="fact"):
    main(["--db", str(db), "add", "-p", project, "-t", type_, "-s", summary])


def test_count_empty_store(tmp_path, capsys):
    main(["--db", str(tmp_path / "m.db"), "count"])
    assert capsys.readouterr().out.strip() == "0 records, 0 active"


def test_count_spans_every_project_and_the_global_layer(tmp_path, capsys):
    db = tmp_path / "m.db"
    _add(db, "one", project="/p")
    _add(db, "two", project="/q")
    main(["--db", str(db), "add", "--global", "-t", "user", "-s", "three"])
    capsys.readouterr()
    main(["--db", str(db), "count"])
    assert capsys.readouterr().out.strip() == "3 records, 3 active"


def test_count_separates_active_from_retired(tmp_path, capsys):
    db = tmp_path / "m.db"
    _add(db, "kept")
    _add(db, "superseded")
    _add(db, "done")
    main(["--db", str(db), "supersede", "--id", "2", "--status", "superseded"])
    main(["--db", str(db), "supersede", "--id", "3", "--status", "done"])
    capsys.readouterr()
    main(["--db", str(db), "count"])
    assert capsys.readouterr().out.strip() == "3 records, 1 active"


def test_count_needs_no_project_and_prints_exactly_one_line(tmp_path, capsys, monkeypatch):
    """No cwd-derived project tag is involved, so the line is the same from any directory."""
    db = tmp_path / "m.db"
    _add(db, "one")
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    main(["--db", str(db), "count"])
    assert capsys.readouterr().out == "1 records, 1 active\n"


def test_count_json_is_one_object(tmp_path, capsys):
    """--json follows recall/list/logs: opt-in, one line, machine-shaped."""
    db = tmp_path / "m.db"
    _add(db, "kept")
    _add(db, "done")
    main(["--db", str(db), "supersede", "--id", "2", "--status", "done"])
    capsys.readouterr()
    main(["--db", str(db), "count", "--json"])
    assert json.loads(capsys.readouterr().out) == {"records": 2, "active": 1}
