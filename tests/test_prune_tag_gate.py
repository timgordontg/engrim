"""Tests for engrim prune, --tag recall filtering, and exit 2 strict gate mode."""
import datetime as dt
import io
import json
import os
import sqlite3
import pytest

import engrim.cli as cli
from engrim.cli import main
import engrim.adapters.agy as agy_adapter
from engrim.mcp_server import _tool_recall


def _insert_log_with_ts(db_path: str, project: str, ts: str, content: str, role: str = "assistant"):
    conn = cli.connect(db_path)
    conn.execute(
        "INSERT INTO log(ts, project, session, role, content) VALUES(?,?,?,?,?)",
        (ts, project, "s1", role, content),
    )
    conn.commit()
    conn.close()


# ==============================================================================
# 1. engrim prune tests
# ==============================================================================

def test_prune_purges_older_logs_and_vacuums(tmp_path, capsys):
    db = tmp_path / "m.db"
    # Seed 1 old log (40 days ago) and 1 recent log (2 days ago)
    now = dt.datetime.now(dt.timezone.utc)
    old_ts = (now - dt.timedelta(days=40)).isoformat()
    recent_ts = (now - dt.timedelta(days=2)).isoformat()

    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "seed memory"])
    _insert_log_with_ts(str(db), "/p", old_ts, "decided to use sqlite 40 days ago")
    _insert_log_with_ts(str(db), "/p", recent_ts, "decided to test prune 2 days ago")

    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p'").fetchone()[0] == 2
    c.close()

    # Dry-run first
    capsys.readouterr()
    main(["--db", str(db), "prune", "-p", "/p", "--keep-days", "30", "--dry-run"])
    out = capsys.readouterr().out
    assert "1 log row(s) older than 30 day(s) would be purged (dry run: no changes written)" in out

    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p'").fetchone()[0] == 2
    c.close()

    # Real prune
    main(["--db", str(db), "prune", "-p", "/p", "--keep-days", "30"])
    out = capsys.readouterr().out
    assert "pruned 1 log row(s) older than 30 day(s) · database vacuumed" in out

    c = sqlite3.connect(db)
    rows = c.execute("SELECT ts, content FROM log WHERE project='/p'").fetchall()
    assert len(rows) == 1
    assert "2 days ago" in rows[0][1]
    c.close()


def test_prune_all_projects_vs_scoped_project(tmp_path, capsys):
    db = tmp_path / "m.db"
    now = dt.datetime.now(dt.timezone.utc)
    old_ts = (now - dt.timedelta(days=60)).isoformat()

    main(["--db", str(db), "add", "-p", "/p1", "-t", "fact", "-s", "p1 memory"])
    _insert_log_with_ts(str(db), "/p1", old_ts, "p1 old log")
    _insert_log_with_ts(str(db), "/p2", old_ts, "p2 old log")

    # Prune only /p1
    main(["--db", str(db), "prune", "-p", "/p1", "--keep-days", "30"])
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p1'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p2'").fetchone()[0] == 1
    c.close()

    # Prune --all
    main(["--db", str(db), "prune", "--all", "--keep-days", "30"])
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log").fetchone()[0] == 0
    c.close()


def test_prune_keep_days_zero_purges_all(tmp_path, capsys):
    db = tmp_path / "m.db"
    now = dt.datetime.now(dt.timezone.utc)
    yesterday = (now - dt.timedelta(days=1)).isoformat()

    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "p memory"])
    _insert_log_with_ts(str(db), "/p", yesterday, "yesterday log")

    main(["--db", str(db), "prune", "-p", "/p", "--keep-days", "0"])
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p'").fetchone()[0] == 0
    c.close()


def test_prune_negative_keep_days_errors(tmp_path):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "p memory"])
    with pytest.raises(SystemExit):
        main(["--db", str(db), "prune", "-p", "/p", "--keep-days", "-5"])


def test_prune_default_is_off_leaves_data_untouched(tmp_path, capsys):
    db = tmp_path / "m.db"
    now = dt.datetime.now(dt.timezone.utc)
    old_ts = (now - dt.timedelta(days=120)).isoformat()
    _insert_log_with_ts(str(db), "/p", old_ts, "valuable historical turn from 120 days ago")

    # Calling prune without retention flag fails safe
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(db), "prune", "-p", "/p"])
    assert exc.value.code != 0
    assert "pruning is off by default" in str(exc.value)

    # Log table is completely untouched
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p'").fetchone()[0] == 1
    c.close()


def test_prune_vacuum_leaves_logs_intact(tmp_path, capsys):
    db = tmp_path / "m.db"
    now = dt.datetime.now(dt.timezone.utc)
    old_ts = (now - dt.timedelta(days=120)).isoformat()
    _insert_log_with_ts(str(db), "/p", old_ts, "valuable log")

    capsys.readouterr()
    main(["--db", str(db), "prune", "--vacuum"])
    out = capsys.readouterr().out
    assert "database vacuumed (no logs purged)" in out

    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log").fetchone()[0] == 1
    c.close()


def test_prune_all_flag_without_keep_days(tmp_path, capsys):
    db = tmp_path / "m.db"
    now = dt.datetime.now(dt.timezone.utc)
    _insert_log_with_ts(str(db), "/p", now.isoformat(), "current log")

    capsys.readouterr()
    main(["--db", str(db), "prune", "--all"])
    out = capsys.readouterr().out
    assert "pruned 1 log row(s) all · database vacuumed" in out

    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log").fetchone()[0] == 0
    c.close()


def test_prune_env_variable_keep_days(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    now = dt.datetime.now(dt.timezone.utc)
    old_ts = (now - dt.timedelta(days=45)).isoformat()
    _insert_log_with_ts(str(db), "/p", old_ts, "45-day old log")

    monkeypatch.setenv("ENGRIM_PRUNE_KEEP_DAYS", "30")
    capsys.readouterr()
    main(["--db", str(db), "prune", "-p", "/p"])
    out = capsys.readouterr().out
    assert "pruned 1 log row(s) older than 30 day(s)" in out

    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p'").fetchone()[0] == 0
    c.close()



# ==============================================================================
# 2. --tag filter on recall tests
# ==============================================================================

def test_recall_filter_by_tag(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "JWT tokens for auth", "--tags", "auth,security"])
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "Stripe for billing", "--tags", "billing,finance"])
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "Postgres for persistence", "--tags", "db"])

    capsys.readouterr()
    # Filter by single tag
    main(["--db", str(db), "recall", "-p", "/p", "--tag", "auth"])
    out = capsys.readouterr().out
    assert "JWT tokens for auth" in out
    assert "Stripe for billing" not in out
    assert "Postgres for persistence" not in out

    # Filter by tag case-insensitively
    main(["--db", str(db), "recall", "-p", "/p", "--tag", "AUTH"])
    out = capsys.readouterr().out
    assert "JWT tokens for auth" in out

    # Filter with query and tag
    main(["--db", str(db), "recall", "-p", "/p", "-q", "tokens", "--tag", "auth"])
    out = capsys.readouterr().out
    assert "JWT tokens for auth" in out

    # Query matching something in a different tag should return no match
    main(["--db", str(db), "recall", "-p", "/p", "-q", "Stripe", "--tag", "auth"])
    out = capsys.readouterr().out
    assert "no memories" in out

    # Multi-tag comma separated matches either tag
    main(["--db", str(db), "recall", "-p", "/p", "--tags", "auth,billing"])
    out = capsys.readouterr().out
    assert "JWT tokens for auth" in out
    assert "Stripe for billing" in out
    assert "Postgres for persistence" not in out


def test_list_filter_by_tag(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "fact tagged auth", "--tags", "auth"])
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "fact tagged db", "--tags", "db"])

    capsys.readouterr()
    main(["--db", str(db), "list", "-p", "/p", "--tag", "db"])
    out = capsys.readouterr().out
    assert "fact tagged db" in out
    assert "fact tagged auth" not in out


def test_mcp_recall_filter_by_tag(tmp_path):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "use OAuth2 for identity", "--tags", "auth"])
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "use sqlite for storage", "--tags", "db"])

    conn = cli.connect(str(db))
    res = json.loads(_tool_recall(conn, {"project": "/p", "query": "identity", "tag": "auth"}))
    conn.close()

    assert res["count"] == 1
    assert "OAuth2" in res["records"][0]["summary"]


# ==============================================================================
# 3. exit 2 in review / Stop hook tests (strict / gate mode)
# ==============================================================================

def test_review_safe_exits_zero(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "we decided on Redis cache"])
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    _insert_log_with_ts(str(db), "/p", now, "we decided on Redis cache")

    capsys.readouterr()
    # Safe to clear: review should exit 0 with or without --strict
    main(["--db", str(db), "review", "-p", "/p"])
    out = capsys.readouterr().out
    assert "safe to clear" in out

    main(["--db", str(db), "review", "-p", "/p", "--strict"])
    out = capsys.readouterr().out
    assert "safe to clear" in out


def test_review_uncaptured_default_exits_zero_strict_exits_two(tmp_path, capsys):
    db = tmp_path / "m.db"
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    _insert_log_with_ts(str(db), "/p", now, "we decided to switch to Rust backend instead of Node")

    capsys.readouterr()
    # Default non-strict review surfaces the decision but exits 0
    main(["--db", str(db), "review", "-p", "/p"])
    out = capsys.readouterr().out
    assert "these recent decisions don't clearly appear in curated memory" in out
    assert "switch to Rust" in out

    # --strict mode exits with code 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(db), "review", "-p", "/p", "--strict"])
    assert exc.value.code == 2

    # --gate mode also exits with code 2
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(db), "review", "-p", "/p", "--gate"])
    assert exc.value.code == 2


def test_review_strict_via_env_var(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    _insert_log_with_ts(str(db), "/p", now, "we chose PostgreSQL for persistence")

    monkeypatch.setenv("ENGRIM_STRICT", "1")
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(db), "review", "-p", "/p"])
    assert exc.value.code == 2


def test_log_hook_strict_exits_code_2_on_uncaptured(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    # Create transcript file with an uncurated decision
    transcript = tmp_path / "session.jsonl"
    line = json.dumps({
        "type": "assistant",
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "uuid": "u1",
        "message": {"content": [{"type": "text", "text": "we decided on Kafka for streaming events"}]},
    }) + "\n"
    transcript.write_text(line, encoding="utf-8")

    payload = {
        "transcript_path": str(transcript),
        "session_id": "s1",
        "workspace": {"project_dir": "/p"},
    }

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    # Without --strict, log --hook succeeds and exits normally
    main(["--db", str(db), "log", "--hook", "-p", "/p"])

    # Now with --strict, since the uncaptured decision is in log, it must exit 2
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(db), "log", "--hook", "-p", "/p", "--strict"])
    assert exc.value.code == 2

    # Once curated, --strict exits cleanly (0)
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "we decided on Kafka for streaming events"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    main(["--db", str(db), "log", "--hook", "-p", "/p", "--strict"])


def test_agy_stop_hook_strict_exits_code_2(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    _insert_log_with_ts(str(db), "/p", now, "we decided on Docker Swarm over Kubernetes")

    payload = {"cwd": "/p", "workspacePaths": ["/p"]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    # Non-strict stop hook succeeds
    res = agy_adapter.handle_stop(payload, db_path=str(db), strict=False, print_output=False)
    assert res == {}

    # Strict stop hook exits 2
    with pytest.raises(SystemExit) as exc:
        agy_adapter.handle_stop(payload, db_path=str(db), strict=True, print_output=False)
    assert exc.value.code == 2

    # CLI hook command with --agent agy --event stop --strict
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(db), "hook", "--agent", "agy", "--event", "stop", "--strict", "-p", "/p"])
    assert exc.value.code == 2


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert f"engrim {cli.__version__}" in out

