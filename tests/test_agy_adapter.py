"""Tests for the Antigravity (AGY) Lifecycle Hook Adapter."""
import io
import json
import sqlite3
import pytest

from engrim.adapters.agy import handle_boot, handle_stop, get_project_path
from engrim.cli import connect, main, add_memory


def test_get_project_path_resolution():
    # Priority: workspacePaths[0] > cwd > os.getcwd()
    assert get_project_path({"workspacePaths": ["/custom/workspace"], "cwd": "/fallback"}) == "/custom/workspace"
    assert get_project_path({"workspacePaths": [], "cwd": "/fallback"}) == "/fallback"
    assert get_project_path({}) != ""


def test_handle_boot_inject_steps_structure(tmp_path):
    db = str(tmp_path / "m.db")
    conn = connect(db)
    add_memory(
        conn,
        project="/test/proj",
        type="decision",
        summary="Use inverted stop loss matrix for high volatility",
        origin_agent="antigravity",
    )
    conn.close()

    mock_payload = {
        "workspacePaths": ["/test/proj"],
        "cwd": "/test/proj",
    }

    res = handle_boot(mock_payload, db_path=db, print_output=False)
    assert "injectSteps" in res
    assert isinstance(res["injectSteps"], list)
    assert len(res["injectSteps"]) == 1
    assert "ephemeralMessage" in res["injectSteps"][0]
    msg = res["injectSteps"][0]["ephemeralMessage"]
    assert "inverted stop loss matrix" in msg.lower()
    assert "(via Antigravity)" in msg


def test_handle_stop_ingests_transcript(tmp_path):
    db = str(tmp_path / "m.db")
    transcript_file = tmp_path / "transcript.jsonl"
    transcript_lines = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Let's change risk params"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "I have updated the parameters to 2.5%"},
    ]
    with open(transcript_file, "w", encoding="utf-8") as f:
        for line in transcript_lines:
            f.write(json.dumps(line) + "\n")

    mock_payload = {
        "workspacePaths": ["/test/proj"],
        "transcriptPath": str(transcript_file),
        "conversationId": "conv-1234",
    }

    res = handle_stop(mock_payload, db_path=db, print_output=False)
    assert res == {}

    conn = connect(db)
    rows = conn.execute("SELECT role, content FROM log WHERE project='/test/proj'").fetchall()
    assert len(rows) == 2
    assert rows[0]["role"] == "user"
    assert "risk params" in rows[0]["content"]
    assert rows[1]["role"] == "assistant"
    assert "2.5%" in rows[1]["content"]
    conn.close()


def test_cli_hook_agy_boot_and_stop(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "m.db")
    conn = connect(db)
    add_memory(conn, project="/test/proj", type="fact", summary="Host is Linux x86_64", origin_agent="antigravity")
    conn.close()

    # Test boot via CLI
    stdin_data = json.dumps({"workspacePaths": ["/test/proj"]})
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_data))
    main(["--db", db, "hook", "--agent", "agy", "--event", "boot"])
    out = capsys.readouterr().out
    boot_json = json.loads(out)
    assert "injectSteps" in boot_json
    assert "Host is Linux x86_64" in boot_json["injectSteps"][0]["ephemeralMessage"]

    # Test stop via CLI
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"workspacePaths": ["/test/proj"]})))
    main(["--db", db, "hook", "--agent", "agy", "--event", "stop"])
    out = capsys.readouterr().out
    stop_json = json.loads(out)
    assert stop_json == {}
