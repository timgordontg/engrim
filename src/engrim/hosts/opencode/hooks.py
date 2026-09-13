#!/usr/bin/env python3
"""OpenCode lifecycle hooks for engrim.

OpenCode has no shell-hook system; its extension point is a JavaScript plugin loaded from
`~/.config/opencode/plugins/*.js`. `engrim setup --opencode` writes that plugin (plugin.js next to
this file; wiring.py installs it), and the plugin shells out to this adapter at each lifecycle
moment, piping a small JSON payload on stdin and reading plain text back on stdout:

  boot    {cwd, session_id, budget?}   -> the session-boot memory pack (injected into the system
                                          prompt once per session, and again after compaction);
                                          budget caps its size in chars (default 4000)
  prompt  {cwd, session_id, prompt}    -> the minder slice: a few records relevant to THIS prompt
  stop    {cwd, session_id, turns:[…]} -> ingests the session's new user/assistant turns into the
                                          flight-recorder log (idempotent: keyed on message id)

Together these replace OpenCode's own session-summary memory (compaction) with engrim's curated,
cross-agent store: the durable decisions live in ~/.engrim/memory.db, not in the summary, so they
survive `/new`, compaction, and switching to another agent on the same repo.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys

from engrim.cli import (
    DEFAULT_DB,
    _assist_block,
    _now,
    _resolve_project,
    _uncaptured_count,
    build_parser,
    cmd_context,
    connect,
)


def _read_stdin_payload() -> dict:
    if not sys.stdin.isatty():
        try:
            return json.load(sys.stdin) or {}
        except Exception:
            return {}
    return {}


def _project(payload: dict) -> str:
    """Anchor on the directory OpenCode reports (git-rooted), not the adapter process's cwd."""
    return _resolve_project(None, payload.get("cwd") or None)


def _ts(value) -> str:
    """OpenCode timestamps are epoch milliseconds; the log stores ISO like every other source."""
    if isinstance(value, (int, float)) and value > 0:
        import datetime as _dt
        secs = value / 1000.0 if value > 1e11 else value
        try:
            return _dt.datetime.fromtimestamp(secs).astimezone().isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(value, str) and value:
        return value
    return _now()


def handle_boot(payload: dict | None = None, *, db_path: str | None = None,
                print_output: bool = True) -> str:
    """Session boot: the priority-ordered memory pack, with the agent-facing auto-curate directive."""
    if payload is None:
        payload = _read_stdin_payload()
    project = _project(payload)
    db = db_path or os.environ.get("ENGRIM_DB", DEFAULT_DB)
    try:
        budget = int(payload.get("budget") or 0)
    except (TypeError, ValueError):
        budget = 0
    try:
        conn = connect(db)
        args = build_parser().parse_args(
            ["context", "-p", project] + (["-b", str(budget)] if budget > 0 else []))
        args.agent_directive = True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_context(conn, args)
        conn.close()
        text = buf.getvalue().strip()
    except Exception as e:
        text = f"engrim context error: {e}"
    if print_output:
        print(text)
    return text


def handle_prompt(payload: dict | None = None, *, db_path: str | None = None,
                  print_output: bool = True) -> str:
    """Per-prompt minder: the few records relevant to this message, or nothing (spend no tokens)."""
    if payload is None:
        payload = _read_stdin_payload()
    project = _project(payload)
    prompt = payload.get("prompt") or ""
    db = db_path or os.environ.get("ENGRIM_DB", DEFAULT_DB)
    text = ""
    try:
        conn = connect(db)
        text = _assist_block(conn, project, prompt, k=5, budget=600)
        conn.close()
    except Exception:
        text = ""
    if print_output and text:
        print(text)
    return text


def ingest_turns(conn, project: str, session: str | None, turns: list) -> int:
    """Append OpenCode messages to the log. Idempotent on (project, message id) via the UNIQUE
    msg_uuid index + INSERT OR IGNORE, so the plugin can safely resend a whole session. That only
    holds for turns that carry an id: SQLite treats NULLs as distinct, so id-less turns (never sent
    by the plugin; possible from hand-rolled callers) are appended on every resend."""
    added = 0
    for t in turns or []:
        if not isinstance(t, dict):
            continue
        role = t.get("role")
        if role not in ("user", "assistant"):
            continue
        text = (t.get("text") or "").strip()
        if not text:
            continue
        uuid = t.get("id") or None
        raw = json.dumps(t, ensure_ascii=False)
        cur = conn.execute(
            "INSERT OR IGNORE INTO log(ts,project,session,role,content,raw,msg_uuid) "
            "VALUES(?,?,?,?,?,?,?)",
            (_ts(t.get("ts")), project, t.get("session_id") or session, role, text, raw, uuid))
        added += cur.rowcount
    conn.commit()
    return added


def handle_stop(payload: dict | None = None, *, db_path: str | None = None,
                print_output: bool = True) -> int:
    """Session idle: ingest the new turns; warn on stderr if decisions are piling up uncurated."""
    if payload is None:
        payload = _read_stdin_payload()
    project = _project(payload)
    session = payload.get("session_id")
    db = db_path or os.environ.get("ENGRIM_DB", DEFAULT_DB)
    added = 0
    failed = False
    try:
        conn = connect(db)
        added = ingest_turns(conn, project, session, payload.get("turns") or [])
        unc = _uncaptured_count(conn, project)
        if unc > 0:   # visible on direct CLI use only; the plugin spawns with stderr ignored
            sys.stderr.write(f"[engrim] {unc} recent decision(s) not yet curated in {project}\n")
        conn.close()
    except Exception as e:
        failed = True
        sys.stderr.write(f"[engrim] opencode stop hook error: {e}\n")
    if print_output:
        print(json.dumps({"logged": added, "ok": not failed}))
        if failed:
            # Non-zero so the plugin keeps these message ids pending and resends them next idle.
            sys.exit(1)
    return added



def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m engrim.hosts.opencode.hooks [boot|prompt|stop]")
    mode = sys.argv[1].lower()
    if mode == "boot":
        handle_boot()
    elif mode == "prompt":
        handle_prompt()
    elif mode == "stop":
        handle_stop()
    else:
        sys.exit(f"Unknown mode: {mode}")


if __name__ == "__main__":
    main()
