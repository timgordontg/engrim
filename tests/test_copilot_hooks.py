"""Copilot CLI transcript parsing and hook behavior."""
import io
import json
import os
import sqlite3

import engrim.cli as cli
import pytest
from engrim.cli import connect, main
from engrim.hosts.copilot import hooks
from engrim.hosts.copilot.transcript import COMPATIBILITY_KEY, cursor_key


@pytest.fixture(autouse=True)
def _clear_project_tags(monkeypatch):
    monkeypatch.delenv("ENGRIM_PROJECT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_TAG", raising=False)


def _event(event_type, data=None, *, timestamp="2026-09-19T20:00:00Z", **extra):
    return {"type": event_type, "timestamp": timestamp, "data": data or {}, **extra}


def _session_start(project, *, version=1, copilot_version="1.0.87-0"):
    return _event(
        "session.start",
        {
            "version": version,
            "copilotVersion": copilot_version,
            "context": {"gitRoot": str(project), "cwd": str(project)},
        },
    )


def _turn(turn_id, messages):
    return [
        _event("assistant.turn_start", {"turnId": turn_id}),
        *messages,
        _event("assistant.turn_end", {"turnId": turn_id}),
    ]


def _assistant(message_id, turn_id, content, **data):
    return _event(
        "assistant.message",
        {"messageId": message_id, "turnId": turn_id, "content": content, **data},
    )


def _write_events(path, events, *, final_newline=True):
    text = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
    path.write_text(text + ("\n" if final_newline else ""), encoding="utf-8")


def _rows(conn):
    return conn.execute(
        "SELECT ts, session, role, content, raw, msg_uuid FROM log ORDER BY id"
    ).fetchall()


def test_ingest_accepts_top_level_assistant_text_and_rejects_other_events(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "events.jsonl"
    events = [
        _session_start(project),
        _event("user.message", {"messageId": "u1", "content": "private prompt"}),
        *_turn(
            "turn-1",
            [
                _assistant("a1", "turn-1", "commentary", phase="commentary"),
                _assistant("a2", "turn-1", "final", phase="final_answer"),
                _assistant("a3", "turn-1", "unphased"),
                _assistant("a4", "turn-1", ""),
                _assistant("a5", "turn-1", "nested", parentToolCallId="tool-1"),
                _event("tool.execution_start", {"content": "tool output"}),
                _event("future.event", {"content": "unknown"}),
            ],
        ),
    ]
    _write_events(path, events)
    conn = connect(str(tmp_path / "memory.db"))

    result = hooks.ingest_transcript(
        conn, str(project), path, "session-1", poll_timeout=0
    )
    repeated = hooks.ingest_transcript(
        conn, str(project), path, "session-1", poll_timeout=0
    )

    assert result.ok and result.added == 3
    assert repeated.ok and repeated.added == 0
    rows = _rows(conn)
    assert [row["content"] for row in rows] == ["commentary", "final", "unphased"]
    assert [row["msg_uuid"] for row in rows] == [
        "copilot:session-1:a1",
        "copilot:session-1:a2",
        "copilot:session-1:a3",
    ]
    assert rows[0]["ts"] == events[2]["timestamp"]
    assert rows[0]["session"] == "session-1"
    assert rows[0]["role"] == "assistant"
    assert rows[0]["raw"] == json.dumps(events[3], ensure_ascii=False)


def test_incomplete_line_is_retried_without_advancing_cursor(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "events.jsonl"
    start = _session_start(project)
    message = _assistant("a1", "turn-1", "arrived later")
    _write_events(path, [start])
    with path.open("ab") as stream:
        stream.write(json.dumps(message).encode("utf-8")[:-4])
    conn = connect(str(tmp_path / "memory.db"))

    first = hooks.ingest_transcript(conn, str(project), path, "session-1", poll_timeout=0)
    offset = int(
        conn.execute(
            "SELECT value FROM engrim_meta WHERE project=? AND key=?",
            (str(project), cursor_key("session-1")),
        ).fetchone()[0]
    )

    assert first.ok and first.added == 0
    assert offset == len(json.dumps(start).encode("utf-8")) + 1

    _write_events(path, [start, *_turn("turn-1", [message])])
    second = hooks.ingest_transcript(conn, str(project), path, "session-1", poll_timeout=0)
    assert second.ok and second.added == 1
    assert [row["content"] for row in _rows(conn)] == ["arrived later"]


def test_truncated_event_file_restarts_safely_without_duplicates(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "events.jsonl"
    first = _assistant("a1", "turn-1", "x" * 500)
    _write_events(path, [_session_start(project), *_turn("turn-1", [first])])
    conn = connect(str(tmp_path / "memory.db"))
    hooks.ingest_transcript(conn, str(project), path, "session-1", poll_timeout=0)

    second = _assistant("a2", "turn-2", "new file")
    _write_events(path, [_session_start(project), *_turn("turn-2", [second])])
    result = hooks.ingest_transcript(
        conn, str(project), path, "session-1", poll_timeout=0
    )

    assert result.ok and result.added == 1
    assert [row["content"] for row in _rows(conn)] == ["x" * 500, "new file"]
    cursor = int(
        conn.execute(
            "SELECT value FROM engrim_meta WHERE project=? AND key=?",
            (str(project), cursor_key("session-1")),
        ).fetchone()[0]
    )
    assert cursor == path.stat().st_size
    repeated = hooks.ingest_transcript(
        conn, str(project), path, "session-1", poll_timeout=0
    )
    assert repeated.added == 0


def test_malformed_known_event_keeps_cursor_and_marker_content_free(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "events.jsonl"
    private_text = "never include this private assistant text"
    bad = _event(
        "assistant.message",
        {"messageId": "a1", "turnId": "turn-1", "content": [private_text]},
    )
    _write_events(path, [_session_start(project), bad])
    conn = connect(str(tmp_path / "memory.db"))

    result = hooks.ingest_transcript(
        conn, str(project), path, "session-1", poll_timeout=0
    )
    marker = conn.execute(
        "SELECT value FROM engrim_meta WHERE project=? AND key=?",
        (str(project), COMPATIBILITY_KEY),
    ).fetchone()[0]

    assert not result.ok
    assert result.error_code == "assistant_content_type"
    assert private_text not in marker
    assert set(json.loads(marker)) == {
        "code",
        "copilot_version",
        "event_type",
        "fields",
        "fingerprint",
        "stream_version",
    }
    assert int(
        conn.execute(
            "SELECT value FROM engrim_meta WHERE project=? AND key=?",
            (str(project), cursor_key("session-1")),
        ).fetchone()[0]
    ) == len(json.dumps(_session_start(project)).encode("utf-8")) + 1


def test_unsupported_version_is_incompatible_and_empty_model_call_is_not(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    conn = connect(str(tmp_path / "memory.db"))
    unsupported = tmp_path / "unsupported.jsonl"
    _write_events(unsupported, [_session_start(project, version=2)])

    version_result = hooks.ingest_transcript(
        conn, str(project), unsupported, "version-session", poll_timeout=0
    )
    assert version_result.error_code == "unsupported_stream_version"

    empty_turn = tmp_path / "empty.jsonl"
    _write_events(
        empty_turn,
        [
            _session_start(project),
            *_turn("turn-1", [_assistant("a1", "turn-1", "")]),
        ],
    )
    turn_result = hooks.ingest_transcript(
        conn, str(project), empty_turn, "empty-session", poll_timeout=0
    )
    assert turn_result.ok and turn_result.error_code is None


def test_tool_call_cycle_does_not_block_later_assistant_text(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "events.jsonl"
    events = [
        _session_start(project),
        *_turn(
            "model-call-1",
            [_assistant("tool-request", "model-call-1", "", toolRequests=[{"id": "t1"}])],
        ),
        _event("tool.execution_start", {"toolCallId": "t1"}),
        *_turn(
            "model-call-2",
            [_assistant("answer", "model-call-2", "visible answer")],
        ),
    ]
    _write_events(path, events)
    conn = connect(str(tmp_path / "memory.db"))

    result = hooks.ingest_transcript(
        conn, str(project), path, "session-1", poll_timeout=0
    )

    assert result.ok and result.added == 1
    assert [row["content"] for row in _rows(conn)] == ["visible answer"]


def test_delayed_turn_completion_is_polled_without_real_sleep(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "events.jsonl"
    _write_events(path, [_session_start(project), _event("assistant.turn_start", {"turnId": "t"})])
    conn = connect(str(tmp_path / "memory.db"))
    now = [0.0]
    appended = [False]

    def append_during_sleep(_seconds):
        now[0] += _seconds
        if now[0] >= 0.15 and not appended[0]:
            appended[0] = True
            with path.open("a", encoding="utf-8") as stream:
                for event in [
                    _assistant("a1", "t", "delayed"),
                    _event("assistant.turn_end", {"turnId": "t"}),
                ]:
                    stream.write(json.dumps(event) + "\n")

    result = hooks.ingest_transcript(
        conn,
        str(project),
        path,
        "session-1",
        poll_timeout=1,
        poll_interval=0.05,
        sleep=append_during_sleep,
        monotonic=lambda: now[0],
    )

    assert result.ok and result.added == 1
    assert _rows(conn)[0]["content"] == "delayed"


def test_session_start_catch_up_uses_newest_six_and_filters_project(tmp_path, monkeypatch):
    copilot_home = tmp_path / "copilot"
    monkeypatch.setenv("COPILOT_HOME", str(copilot_home))
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    (project / ".git").mkdir()
    (other / ".git").mkdir()
    sessions = copilot_home / "session-state"
    sessions.mkdir(parents=True)
    for index in range(8):
        session = sessions / f"session-{index}"
        session.mkdir()
        owner = other if index == 7 else project
        _write_events(
            session / "events.jsonl",
            [_session_start(owner), *_turn(f"t-{index}", [_assistant(f"a-{index}", f"t-{index}", f"m-{index}")])],
        )
        os.utime(session / "events.jsonl", (index, index))
    project_tag = "stable-project-tag"
    monkeypatch.setenv("ENGRIM_PROJECT", project_tag)
    conn = connect(str(tmp_path / "memory.db"))

    added = hooks.catch_up(conn, project_tag, str(project))

    assert added == 5
    assert [row["content"] for row in _rows(conn)] == ["m-6", "m-5", "m-4", "m-3", "m-2"]


def test_agent_stop_blocks_once_allows_reentry_and_clears_after_success(
        tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "events.jsonl"
    bad = _event("assistant.message", {"messageId": "", "turnId": "t", "content": "secret"})
    _write_events(path, [_session_start(project), bad])
    db = tmp_path / "memory.db"
    payload = {
        "cwd": str(project),
        "sessionId": "session-1",
        "transcriptPath": str(path),
        "stop_hook_active": False,
    }

    for active, expected in ((False, "block"), (False, "allow"), (True, "allow")):
        payload["stop_hook_active"] = active
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
        main(["--db", str(db), "log", "--hook", "--agent", "copilot"])
        assert json.loads(capsys.readouterr().out)["decision"] == expected

    _write_events(path, [_session_start(project), *_turn("t", [_assistant("a1", "t", "fixed")])])
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
    main(["--db", str(db), "log", "--hook", "--agent", "copilot"])
    assert json.loads(capsys.readouterr().out)["decision"] == "allow"
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT value FROM engrim_meta WHERE project=? AND key=?",
        (str(project), COMPATIBILITY_KEY),
    ).fetchone() is None


def test_compatible_session_clears_another_sessions_marker(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    conn = connect(str(tmp_path / "memory.db"))
    bad_path = tmp_path / "bad.jsonl"
    _write_events(
        bad_path,
        [_session_start(project), _event("assistant.message", {"content": "secret"})],
    )
    bad = hooks.ingest_transcript(
        conn, str(project), bad_path, "bad-session", poll_timeout=0
    )
    assert not bad.ok

    good_path = tmp_path / "good.jsonl"
    _write_events(
        good_path,
        [_session_start(project), *_turn("turn-1", [_assistant("a1", "turn-1", "good")])],
    )
    good = hooks.ingest_transcript(
        conn, str(project), good_path, "good-session", poll_timeout=0
    )

    assert good.ok
    assert hooks.compatibility_marker(conn, str(project)) is None


def test_compatible_session_does_not_rearm_warned_fingerprint(
        tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    db = tmp_path / "memory.db"
    bad_path = tmp_path / "bad.jsonl"
    _write_events(
        bad_path,
        [_session_start(project), _event("assistant.message", {"content": "secret"})],
    )
    bad_payload = {
        "cwd": str(project),
        "sessionId": "bad-session",
        "transcriptPath": str(bad_path),
        "stop_hook_active": False,
    }
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(bad_payload)))
    main(["--db", str(db), "log", "--hook", "--agent", "copilot"])
    assert json.loads(capsys.readouterr().out)["decision"] == "block"

    good_path = tmp_path / "good.jsonl"
    _write_events(
        good_path,
        [_session_start(project), *_turn("turn-1", [_assistant("a1", "turn-1", "good")])],
    )
    good_payload = {
        "cwd": str(project),
        "sessionId": "good-session",
        "transcriptPath": str(good_path),
        "stop_hook_active": False,
    }
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(good_payload)))
    main(["--db", str(db), "log", "--hook", "--agent", "copilot"])
    assert json.loads(capsys.readouterr().out)["decision"] == "allow"

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(bad_payload)))
    main(["--db", str(db), "log", "--hook", "--agent", "copilot"])
    assert json.loads(capsys.readouterr().out)["decision"] == "allow"


def test_zero_progress_timeout_does_not_clear_compatibility_marker(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    conn = connect(str(tmp_path / "memory.db"))
    bad_path = tmp_path / "bad.jsonl"
    _write_events(
        bad_path,
        [_session_start(project), _event("assistant.message", {"content": "secret"})],
    )
    hooks.ingest_transcript(
        conn, str(project), bad_path, "bad-session", poll_timeout=0
    )
    marker = hooks.compatibility_marker(conn, str(project))

    idle_path = tmp_path / "idle.jsonl"
    _write_events(idle_path, [_session_start(project)])
    idle = hooks.ingest_transcript(
        conn, str(project), idle_path, "idle-session", poll_timeout=0
    )

    assert idle.ok
    assert hooks.compatibility_marker(conn, str(project)) == marker


def test_active_stop_hook_never_blocks_a_new_fingerprint(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [_session_start(project), _event("assistant.message", {"content": "secret"})],
    )
    payload = {
        "cwd": str(project),
        "sessionId": "session-1",
        "transcriptPath": str(path),
        "stop_hook_active": True,
    }
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))

    main(["--db", str(tmp_path / "memory.db"), "log", "--hook", "--agent", "copilot"])

    assert json.loads(capsys.readouterr().out)["decision"] == "allow"


def test_session_start_catch_up_failure_keeps_json_protocol_safe(
        tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        hooks,
        "catch_up",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        io.StringIO(json.dumps({"cwd": str(project), "sessionId": "session-1"})),
    )

    main(
        [
            "--db",
            str(tmp_path / "memory.db"),
            "hook",
            "--agent",
            "copilot",
            "--event",
            "sessionstart",
            "--no-sync",
        ]
    )

    captured = capsys.readouterr()
    assert set(json.loads(captured.out)) == {"additionalContext"}
    assert "catch-up failed" in captured.err


def test_doctor_reports_transcript_incompatibility_without_content(
        tmp_path, monkeypatch, capsys):
    copilot_home = tmp_path / "copilot"
    copilot_home.mkdir()
    monkeypatch.setenv("COPILOT_HOME", str(copilot_home))
    project = tmp_path / "project"
    project.mkdir()
    db = tmp_path / "memory.db"
    conn = connect(str(db))
    private_text = "private transcript content"
    marker = {
        "code": "assistant_content_type",
        "event_type": "assistant.message",
        "fields": ["data.content", "data.messageId"],
        "stream_version": 1,
        "copilot_version": "1.0.87-0",
        "fingerprint": "abc123",
    }
    conn.execute(
        "INSERT INTO engrim_meta(project,key,value) VALUES(?,?,?)",
        (str(project), COMPATIBILITY_KEY, json.dumps(marker)),
    )
    conn.commit()
    conn.close()

    main(["--db", str(db), "doctor", "-p", str(project), "--json"])
    report = json.loads(capsys.readouterr().out)
    capture = report["environments"]["copilot"]["assistant_capture"]
    assert capture["healthy"] is False
    assert capture["code"] == "assistant_content_type"
    assert private_text not in json.dumps(report)
    assert any("assistant capture incompatible" in issue for issue in report["issues"])

    with pytest.raises(SystemExit) as exc:
        main(["--db", str(db), "doctor", "-p", str(project)])
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "Assistant capture: incompatible" in output
    assert private_text not in output
