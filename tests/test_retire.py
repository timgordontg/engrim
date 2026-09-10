"""Tests for `engrim retire` — marking the active resume-pointer(s) done."""
import json
import sqlite3

from engrim.cli import main


def _add(db, summary, project="/p", tags=None, type_="state"):
    args = ["--db", str(db), "add", "-p", project, "-t", type_, "-s", summary]
    if tags:
        args += ["--tags", tags]
    main(args)


def _status(db):
    c = sqlite3.connect(db)
    rows = c.execute("SELECT summary, status FROM memories ORDER BY id").fetchall()
    c.close()
    return dict(rows)


def test_retire_marks_every_active_pointer_done_and_nothing_else(tmp_path, capsys):
    db = tmp_path / "m.db"
    _add(db, "old pointer", tags="resume-pointer")
    _add(db, "we chose postgres for billing", type_="decision")
    _add(db, "new pointer", tags="wip,resume-pointer")
    capsys.readouterr()
    main(["--db", str(db), "retire", "-p", "/p"])
    out = capsys.readouterr().out
    assert out.startswith("retired 2 resume-pointer(s) · project=/p\n")
    assert "#1 " in out and "old pointer" in out and "#3 " in out and "new pointer" in out
    assert _status(db) == {"old pointer": "done", "we chose postgres for billing": "active",
                           "new pointer": "done"}


def test_retire_clears_the_resume_section_of_the_boot_pack(tmp_path, capsys):
    db = tmp_path / "m.db"
    _add(db, "pick up at discovery pass three", tags="resume-pointer")
    main(["--db", str(db), "context", "-p", "/p"])
    assert "[▶ RESUME HERE]" in capsys.readouterr().out
    main(["--db", str(db), "retire", "-p", "/p"])
    capsys.readouterr()
    main(["--db", str(db), "context", "-p", "/p"])
    assert "[▶ RESUME HERE]" not in capsys.readouterr().out


def test_retire_is_idempotent(tmp_path, capsys):
    db = tmp_path / "m.db"
    _add(db, "pointer", tags="resume-pointer")
    main(["--db", str(db), "retire", "-p", "/p"])
    capsys.readouterr()
    main(["--db", str(db), "retire", "-p", "/p"])
    assert capsys.readouterr().out == "retired 0 resume-pointer(s) · project=/p\n"


def test_retire_is_scoped_to_the_project_unless_all(tmp_path, capsys):
    db = tmp_path / "m.db"
    _add(db, "p pointer", project="/p", tags="resume-pointer")
    _add(db, "q pointer", project="/q", tags="resume-pointer")
    main(["--db", str(db), "retire", "-p", "/p"])
    assert _status(db) == {"p pointer": "done", "q pointer": "active"}
    capsys.readouterr()
    main(["--db", str(db), "retire", "--all"])
    assert capsys.readouterr().out.startswith("retired 1 resume-pointer(s) · all projects\n")
    assert _status(db) == {"p pointer": "done", "q pointer": "done"}


def test_retire_dry_run_lists_but_writes_nothing(tmp_path, capsys):
    db = tmp_path / "m.db"
    _add(db, "pointer", tags="resume-pointer")
    capsys.readouterr()
    main(["--db", str(db), "retire", "-p", "/p", "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "would retire 1 resume-pointer(s)" in out and "pointer" in out
    assert _status(db) == {"pointer": "active"}


def test_retire_matches_the_tag_not_the_words(tmp_path):
    """Only a record TAGGED resume-pointer is a pointer — the same test the boot pack applies. A
    record that merely mentions the phrase, or carries a tag that contains it, is not."""
    db = tmp_path / "m.db"
    _add(db, "decided to add a resume-pointer before every clear", type_="decision")
    _add(db, "tagged with a superstring", tags="resume-pointers")
    _add(db, "real pointer", tags="resume-pointer")
    main(["--db", str(db), "retire", "-p", "/p"])
    assert _status(db) == {"decided to add a resume-pointer before every clear": "active",
                           "tagged with a superstring": "active", "real pointer": "done"}


def test_retired_pointer_stays_readable_and_ready_to_merge(tmp_path, capsys):
    """Retirement is the same monotonic move as supersede: nothing erased, visible with
    --include-stale, and `merge` carries it to another copy of the store."""
    db, other = tmp_path / "m.db", tmp_path / "other.db"
    _add(db, "pointer", tags="resume-pointer")
    _add(other, "pointer", tags="resume-pointer")
    c = sqlite3.connect(db)
    ts = c.execute("SELECT ts FROM memories").fetchone()[0]
    c.close()
    c = sqlite3.connect(other)
    c.execute("UPDATE memories SET ts = ?", (ts,))     # the same record in two copies of the store
    c.commit()
    c.close()
    main(["--db", str(db), "retire", "-p", "/p"])
    capsys.readouterr()
    main(["--db", str(db), "list", "-p", "/p", "--include-stale"])
    assert "[state/done]" in capsys.readouterr().out
    main(["--db", str(other), "merge", str(db)])
    assert _status(other) == {"pointer": "done"}


def test_retire_leaves_the_global_layer_to_all_or_an_explicit_tag(tmp_path, capsys):
    """Writes are project-exact, like prune and embed: a pointer in the global layer (pinned in every
    project's pack) is not touched by a project-scoped retire, and is by --all or -p __global__."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "--global", "-t", "state", "-s", "global pointer", "--tags", "resume-pointer"])
    _add(db, "p pointer", tags="resume-pointer")
    main(["--db", str(db), "retire", "-p", "/p"])
    assert _status(db) == {"global pointer": "active", "p pointer": "done"}
    main(["--db", str(db), "retire", "-p", "__global__"])
    assert _status(db) == {"global pointer": "done", "p pointer": "done"}


def test_retire_json_reports_what_it_did_or_would_do(tmp_path, capsys):
    """--json follows recall/list/logs: opt-in, one line, the pointers as full rows."""
    db = tmp_path / "m.db"
    _add(db, "pointer", tags="resume-pointer")
    _add(db, "we chose postgres for billing", type_="decision")
    capsys.readouterr()
    main(["--db", str(db), "retire", "-p", "/p", "--dry-run", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert (out["retired"], out["dry_run"], out["project"]) == (1, True, "/p")
    assert [(p["id"], p["summary"], p["status"]) for p in out["pointers"]] == [(1, "pointer", "active")]
    assert _status(db) == {"pointer": "active", "we chose postgres for billing": "active"}
    main(["--db", str(db), "retire", "--all", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert (out["retired"], out["dry_run"], out["project"]) == (1, False, None)
    assert out["pointers"][0]["status"] == "done"
    assert _status(db) == {"pointer": "done", "we chose postgres for billing": "active"}
