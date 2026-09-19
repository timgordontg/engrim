"""Runtime hook adapter for GitHub Copilot CLI."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from engrim.hosts.copilot.transcript import (
    COMPATIBILITY_KEY,
    TurnState,
    cursor_key,
    newest_event_files,
    read_events,
    read_metadata,
    state_key,
)

_POLL_QUIET_SECONDS = 0.2


@dataclass(frozen=True)
class IngestionResult:
    ok: bool
    added: int = 0
    error_code: str | None = None
    fingerprint: str | None = None


def _meta_get(conn, project: str, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM engrim_meta WHERE project=? AND key=?", (project, key)
    ).fetchone()
    return row[0] if row else None


def _meta_set(conn, project: str, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO engrim_meta(project,key,value) VALUES(?,?,?)",
        (project, key, value),
    )


def compatibility_marker(conn, project: str) -> dict | None:
    value = _meta_get(conn, project, COMPATIBILITY_KEY)
    if not value:
        return None
    try:
        marker = json.loads(value)
    except ValueError:
        return None
    return marker if isinstance(marker, dict) else None


def _store_marker(conn, project: str, error) -> None:
    _meta_set(
        conn,
        project,
        COMPATIBILITY_KEY,
        json.dumps(error.marker(), sort_keys=True),
    )


def _clear_marker(conn, project: str) -> None:
    conn.execute(
        "DELETE FROM engrim_meta WHERE project=? AND key=?",
        (project, COMPATIBILITY_KEY),
    )


def _load_offset(conn, project: str, session_id: str, path: Path) -> int:
    try:
        offset = int(_meta_get(conn, project, cursor_key(session_id)) or 0)
    except (TypeError, ValueError):
        offset = 0
    try:
        if offset > path.stat().st_size:
            conn.execute(
                "DELETE FROM engrim_meta WHERE project=? AND key IN (?,?)",
                (project, cursor_key(session_id), state_key(session_id)),
            )
            conn.commit()
            return 0
    except OSError:
        return 0
    return offset


def _persist_progress(
    conn, project: str, session_id: str, offset: int, state: TurnState
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO engrim_meta(project,key,value) VALUES(?,?,?)",
        (project, cursor_key(session_id), str(offset)),
    )
    cursor = conn.execute(
        "UPDATE engrim_meta SET value=? WHERE project=? AND key=? "
        "AND CAST(value AS INTEGER) <= ?",
        (str(offset), project, cursor_key(session_id), offset),
    )
    if cursor.rowcount:
        _meta_set(conn, project, state_key(session_id), state.to_json())
    conn.commit()


def ingest_transcript(
    conn,
    project: str,
    path: str | Path,
    session_id: str,
    *,
    poll_timeout: float = 1.0,
    poll_interval: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> IngestionResult:
    """Ingest a Copilot event tail, polling briefly for the current completed turn."""
    transcript = Path(path)
    if not transcript.is_file():
        return IngestionResult(ok=False, error_code="transcript_unavailable")
    offset = _load_offset(conn, project, session_id, transcript)
    state = (
        TurnState.from_json(_meta_get(conn, project, state_key(session_id)))
        if offset
        else TurnState()
    )
    added = 0
    started_at = monotonic()
    deadline = started_at + max(0.0, poll_timeout)
    last_progress_at = started_at
    saw_compatible_assistant = False

    while True:
        previous_offset = offset
        try:
            batch = read_events(transcript, offset, state)
        except OSError:
            return IngestionResult(ok=False, added=added, error_code="transcript_read_error")
        for message in batch.messages:
            timestamp = message.timestamp if message.timestamp is not None else ""
            cur = conn.execute(
                "INSERT OR IGNORE INTO log(ts,project,session,role,content,raw,msg_uuid) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    timestamp,
                    project,
                    session_id,
                    "assistant",
                    message.content,
                    message.raw,
                    f"copilot:{session_id}:{message.message_id}",
                ),
            )
            added += cur.rowcount
        offset = batch.offset
        state = batch.state
        now = monotonic()
        if offset > previous_offset:
            last_progress_at = now
        saw_compatible_assistant = (
            saw_compatible_assistant or batch.compatible_assistant
        )
        _persist_progress(conn, project, session_id, offset, state)

        if batch.error is not None:
            _store_marker(conn, project, batch.error)
            conn.commit()
            return IngestionResult(
                ok=False,
                added=added,
                error_code=batch.error.code,
                fingerprint=batch.error.fingerprint,
            )
        if (
            saw_compatible_assistant
            and now - last_progress_at >= _POLL_QUIET_SECONDS
        ):
            _clear_marker(conn, project)
            conn.commit()
            return IngestionResult(ok=True, added=added)
        if now >= deadline:
            if saw_compatible_assistant:
                _clear_marker(conn, project)
            conn.commit()
            return IngestionResult(ok=True, added=added)
        sleep(poll_interval)


def _log_prompt(conn, payload: dict, explicit_project: str = "auto") -> None:
    from engrim.cli import _now, _payload_project

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return
    project = _payload_project(payload, explicit_project)
    session = payload.get("sessionId") or payload.get("session_id")
    stamp = payload.get("timestamp")
    if stamp is None:
        stamp = uuid.uuid4().hex
    identity = hashlib.sha256(f"{stamp}|{prompt}".encode("utf-8")).hexdigest()[:16]
    msg_uuid = f"copilot:{session or 'session'}:UserPromptSubmit:{identity}"
    conn.execute(
        "INSERT OR IGNORE INTO log(ts,project,session,role,content,raw,msg_uuid) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            _now(),
            project,
            session,
            "user",
            prompt,
            json.dumps(payload, ensure_ascii=False),
            msg_uuid,
        ),
    )
    conn.commit()


def _claim_warning(conn, project: str, fingerprint: str) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO engrim_meta(project,key,value) VALUES(?,?,?)",
        (project, f"copilot:transcript_warned:{fingerprint}", "1"),
    )
    conn.commit()
    return cursor.rowcount == 1


def handle_log(conn, payload: dict, explicit_project: str = "auto") -> dict | None:
    """Handle either ``userPromptSubmitted`` or ``agentStop`` hook input."""
    from engrim.cli import _payload_project

    if "transcriptPath" not in payload and "transcript_path" not in payload:
        _log_prompt(conn, payload, explicit_project)
        return None

    project = _payload_project(payload, explicit_project)
    session_id = payload.get("sessionId") or payload.get("session_id")
    path = payload.get("transcriptPath") or payload.get("transcript_path")
    if not isinstance(session_id, str) or not session_id or not isinstance(path, str):
        sys.stderr.write("[engrim] Copilot agentStop payload is missing transcript identity\n")
        return {"decision": "allow"}
    result = ingest_transcript(conn, project, path, session_id)
    if result.fingerprint:
        first_warning = _claim_warning(conn, project, result.fingerprint)
        if first_warning and not payload.get("stop_hook_active"):
            return {
                "decision": "block",
                "reason": (
                    "Engrim detected an unsupported Copilot transcript format and stopped "
                    "assistant capture. Tell the user to run `engrim doctor`; do not continue "
                    "the original task."
                ),
            }
    elif not result.ok:
        sys.stderr.write(
            f"[engrim] Copilot assistant capture deferred: {result.error_code}\n"
        )
    return {"decision": "allow"}


def catch_up(conn, project: str, workspace: str | None = None) -> int:
    """Recover eligible tails from at most the six newest Copilot sessions."""
    from engrim.cli import _git_root, _norm_project

    active_path = workspace or project
    if not os.path.isabs(active_path):
        return 0
    active_root = _norm_project(_git_root(active_path) or active_path)

    added = 0
    try:
        paths = newest_event_files(6)
    except OSError as exc:
        sys.stderr.write(f"[engrim] Copilot catch-up could not list event files: {exc}\n")
        return 0
    for path in paths:
        try:
            metadata = read_metadata(path)
        except OSError as exc:
            sys.stderr.write(f"[engrim] Copilot catch-up could not read event metadata: {exc}\n")
            continue
        roots = [value for value in (metadata.git_root, metadata.cwd) if value]
        owner_roots = {
            _norm_project(_git_root(root) or root)
            for root in roots
            if os.path.isabs(root)
        }
        if active_root not in owner_roots:
            continue
        result = ingest_transcript(
            conn, project, path, path.parent.name, poll_timeout=0
        )
        added += result.added
        if not result.ok and not result.fingerprint:
            sys.stderr.write(
                f"[engrim] Copilot catch-up deferred: {result.error_code}\n"
            )
    return added


def handle_session_start(conn, args, payload: dict) -> None:
    """Recover recent tails, then emit Copilot's flat session-start response."""
    from engrim.cli import _payload_project, cmd_context

    project = _payload_project(payload, args.project)
    workspace = payload.get("cwd")
    try:
        catch_up(conn, project, workspace if isinstance(workspace, str) else None)
    except Exception as exc:
        sys.stderr.write(f"[engrim] Copilot session catch-up failed: {exc}\n")
    args.project = project
    args.json = False
    args.agent_directive = True
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_context(conn, args)
    print(json.dumps({"additionalContext": buf.getvalue().strip()}))
