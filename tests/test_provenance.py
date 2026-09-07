"""Tests for Agent Provenance Tracking in SQLite Schema and Display."""
import json
import sqlite3
import pytest

from engrim.cli import connect, main, add_memory
from engrim.mcp_server import serve


def test_provenance_storage_and_defaults(tmp_path):
    db = str(tmp_path / "m.db")
    conn = connect(db)

    id1 = add_memory(
        conn,
        project="/p",
        type="decision",
        summary="Antigravity decision",
        origin_agent="antigravity",
    )
    id2 = add_memory(
        conn,
        project="/p",
        type="fact",
        summary="Claude fact",
        origin_agent="claude-code",
    )
    id3 = add_memory(
        conn,
        project="/p",
        type="decision",
        summary="Cursor decision",
        origin_agent="cursor",
    )
    id4 = add_memory(
        conn,
        project="/p",
        type="fact",
        summary="Legacy or untagged fact",
        origin_agent=None,
    )
    conn.close()

    c = sqlite3.connect(db)
    rows = dict(c.execute("SELECT id, origin_agent FROM memories").fetchall())
    assert rows[id1] == "antigravity"
    assert rows[id2] == "claude-code"
    assert rows[id3] == "cursor"
    assert rows[id4] is None
    c.close()


def test_provenance_cli_add(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    main(["--db", db, "add", "-p", "/p", "-t", "decision", "-s", "cli decision"])
    c = sqlite3.connect(db)
    assert c.execute("SELECT origin_agent FROM memories WHERE id=1").fetchone()[0] == "cli"

    main(["--db", db, "add", "-p", "/p", "-t", "decision", "-s", "agy decision", "--origin-agent", "antigravity"])
    assert c.execute("SELECT origin_agent FROM memories WHERE id=2").fetchone()[0] == "antigravity"
    c.close()


def test_provenance_display_in_context_and_list(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    conn = connect(db)
    add_memory(
        conn,
        project="/p",
        type="decision",
        summary="Inverted stop loss matrix for extreme tails",
        origin_agent="antigravity",
    )
    add_memory(
        conn,
        project="/p",
        type="fact",
        summary="Untagged plain fact",
        origin_agent=None,
    )
    conn.close()

    # Verify engrim context output format
    main(["--db", db, "context", "-p", "/p"])
    out = capsys.readouterr().out
    assert "[DECISION] (via Antigravity): Inverted stop loss matrix for extreme tails" in out
    assert "- #2 Untagged plain fact" in out

    # Verify engrim list output format
    main(["--db", db, "list", "-p", "/p"])
    out_list = capsys.readouterr().out
    assert "(via Antigravity)" in out_list
    assert "#1 [decision/active] (via Antigravity)" in out_list


def test_schema_migration_adds_origin_agent(tmp_path):
    # Simulate a pre-1.3.0 database without origin_agent column
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, project TEXT NOT NULL,
            type TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT,
            status TEXT NOT NULL DEFAULT 'active', tags TEXT, links TEXT, source TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO memories(id, ts, project, type, summary) VALUES(1, '2026-08-01', '/p', 'fact', 'legacy item')"
    )
    conn.commit()
    conn.close()

    # Opening with connect() must auto-migrate the table
    c2 = connect(str(db_file))
    cols = {r[1] for r in c2.execute("PRAGMA table_info(memories)")}
    assert "origin_agent" in cols
    # Preserves existing rows
    r = c2.execute("SELECT summary, origin_agent FROM memories WHERE id=1").fetchone()
    assert r[0] == "legacy item"
    assert r[1] is None
    c2.close()
