#!/usr/bin/env python3
"""Antigravity (AGY) Lifecycle Hook Adapter for engrim.

Bridges Antigravity lifecycle hooks (PreInvocation, Stop) to the engrim memory engine.

Usage in ~/.gemini/config/hooks.json:
  PreInvocation: engrim hook --agent agy --event boot
  Stop:          engrim hook --agent agy --event stop
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys

from engrim.cli import (
    connect,
    cmd_context,
    build_parser,
    DEFAULT_DB,
    _ingest_transcript,
    _uncaptured_count,
)


def get_project_path(payload: dict | None = None) -> str:
    """Extract project root directory from hook payload or cwd."""
    if not payload:
        return os.getcwd()
    ws_paths = payload.get("workspacePaths") or []
    if ws_paths:
        return ws_paths[0]
    return payload.get("cwd") or os.getcwd()


def _read_stdin_payload() -> dict:
    if not sys.stdin.isatty():
        try:
            return json.load(sys.stdin)
        except Exception:
            return {}
    return {}


def handle_boot(
    payload: dict | None = None,
    *,
    db_path: str | None = None,
    print_output: bool = True,
) -> dict:
    """Handle Antigravity PreInvocation boot hook.

    Extracts project workspace, fetches engrim context, and formats output as injectSteps.
    """
    if payload is None:
        payload = _read_stdin_payload()

    project = get_project_path(payload)

    # Auto-checkpoint baseline in background if checkpoint utility is available
    try:
        subprocess.Popen(
            ["checkpoint", "save", "--auto", "-p", project, "-l", "Auto Session Boot Checkpoint"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    db = db_path or os.environ.get("ENGRIM_DB", DEFAULT_DB)
    try:
        conn = connect(db)
        parser = build_parser()
        args = parser.parse_args(["context", "-p", project])
        args.agent_directive = True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_context(conn, args)
        conn.close()
        context_text = buf.getvalue().strip()
    except Exception as e:
        context_text = f"engrim context error: {e}"

    out = {
        "injectSteps": [
            {
                "ephemeralMessage": context_text
            }
        ]
    }
    if print_output:
        print(json.dumps(out))
    return out


def handle_stop(
    payload: dict | None = None,
    *,
    db_path: str | None = None,
    print_output: bool = True,
) -> dict:
    """Handle Antigravity Stop lifecycle hook.

    Ingests session transcript and verifies recent decisions are captured.
    """
    if payload is None:
        payload = _read_stdin_payload()

    project = get_project_path(payload)
    transcript_path = payload.get("transcriptPath") or payload.get("transcript_path")
    session_id = payload.get("conversationId") or payload.get("session_id")

    db = db_path or os.environ.get("ENGRIM_DB", DEFAULT_DB)
    if transcript_path and os.path.exists(transcript_path):
        try:
            conn = connect(db)
            _ingest_transcript(conn, project, transcript_path, session=session_id)
            # Verify recent decisions are captured
            unc = _uncaptured_count(conn, project)
            if unc > 0:
                sys.stderr.write(f"[engrim] {unc} recent decision(s) not yet curated in {project}\n")
            conn.close()
        except Exception as e:
            sys.stderr.write(f"[engrim] stop hook error: {e}\n")

    out: dict = {}
    if print_output:
        print(json.dumps(out))
    return out


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m engrim.adapters.agy [boot|stop]")
    mode = sys.argv[1].lower()
    if mode == "boot":
        handle_boot()
    elif mode == "stop":
        handle_stop()
    else:
        sys.exit(f"Unknown mode: {mode}")


if __name__ == "__main__":
    main()
