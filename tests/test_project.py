"""Tests for `engrim project` — one project tag's counts, scoped like every other read — and
`engrim projects`, the every-project alias."""
import json

import pytest

from engrim.cli import main


def _add(db, summary, project="/p", type_="fact"):
    main(["--db", str(db), "add", "-p", project, "-t", type_, "-s", summary])


@pytest.fixture
def store(tmp_path):
    """/p: 2 records (1 active); /q: 1; global: 1."""
    db = tmp_path / "m.db"
    _add(db, "p one")
    _add(db, "p two")
    main(["--db", str(db), "supersede", "--id", "2", "--status", "done"])
    _add(db, "q one", project="/q")
    main(["--db", str(db), "add", "--global", "-t", "user", "-s", "me"])
    return db


def _lines(out):
    return [" ".join(l.split()) for l in out.strip().splitlines()]


def test_project_all_lists_every_project(store, capsys):
    main(["--db", str(store), "project", "--all"])
    lines = _lines(capsys.readouterr().out)
    assert len(lines) == 3
    assert any(l.startswith("2 (1 active)") and l.endswith("/p") for l in lines)
    assert any(l.startswith("1 (1 active)") and l.endswith("/q") for l in lines)
    assert any(l.endswith("__global__") for l in lines)


def test_project_defaults_to_the_current_project(store, capsys, monkeypatch):
    """Same precedence as every other read: explicit -p, then $ENGRIM_PROJECT, then the cwd."""
    monkeypatch.setenv("ENGRIM_PROJECT", "/q")
    main(["--db", str(store), "project"])
    lines = _lines(capsys.readouterr().out)
    assert len(lines) == 1 and lines[0].startswith("1 (1 active) last 20") and lines[0].endswith(" /q")
    main(["--db", str(store), "project", "-p", "/p"])
    lines = _lines(capsys.readouterr().out)
    assert len(lines) == 1 and lines[0].startswith("2 (1 active)") and lines[0].endswith(" /p")


def test_project_global_is_the_user_layer_only(store, capsys):
    main(["--db", str(store), "project", "--global"])
    lines = _lines(capsys.readouterr().out)
    assert len(lines) == 1 and lines[0].endswith("__global__")


def test_project_scopes_are_mutually_exclusive(store):
    with pytest.raises(SystemExit):
        main(["--db", str(store), "project", "-p", "/p", "--all"])
    with pytest.raises(SystemExit):
        main(["--db", str(store), "project", "--global", "--all"])


def test_project_json_is_a_list_of_rows(store, capsys):
    main(["--db", str(store), "project", "-p", "/p", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert {k: rows[0][k] for k in ("project", "records", "active")} == {"project": "/p", "records": 2, "active": 1}
    assert rows[0]["last"].startswith("20")
    main(["--db", str(store), "project", "--all", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert sorted(r["project"] for r in rows) == ["/p", "/q", "__global__"]


def test_project_empty_scopes_say_so(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "project", "--all"])
    assert capsys.readouterr().out.strip() == "(empty store)"
    main(["--db", str(db), "project", "-p", "/nowhere"])
    assert capsys.readouterr().out.strip() == "(no records for project=/nowhere)"
    main(["--db", str(db), "project", "-p", "/nowhere", "--json"])
    assert json.loads(capsys.readouterr().out) == []


def test_projects_is_project_all(store, capsys):
    """The plural keeps listing the whole store, text and JSON alike, and takes no scope."""
    main(["--db", str(store), "projects"])
    plural = capsys.readouterr().out
    main(["--db", str(store), "project", "--all"])
    assert plural == capsys.readouterr().out and len(_lines(plural)) == 3
    main(["--db", str(store), "projects", "--json"])
    plural = capsys.readouterr().out
    main(["--db", str(store), "project", "--all", "--json"])
    assert plural == capsys.readouterr().out and len(json.loads(plural)) == 3
    with pytest.raises(SystemExit):
        main(["--db", str(store), "projects", "-p", "/p"])
