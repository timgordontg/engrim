"""Regression tests for the Codex CLI setup and hook integration."""
import io
import json
import sqlite3

import engrim.cli as cli
from engrim.cli import main


def _codex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return codex


def _run_codex_setup(tmp_path, monkeypatch, which="/opt/my tools/bin/engrim"):
    codex = _codex_home(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: which)
    monkeypatch.setattr(cli, "_verify_hook_bin", lambda _bin: None)
    db = tmp_path / "memory.db"
    main(["--db", str(db), "setup", "--codex"])
    return db, codex


def test_setup_codex_writes_codex_native_hooks_without_requiring_mcp(tmp_path, monkeypatch, capsys):
    db, codex = _run_codex_setup(tmp_path, monkeypatch)
    out = capsys.readouterr().out

    hooks_path = codex / "hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert set(hooks["hooks"]) == {"SessionStart", "SessionEnd", "Stop", "UserPromptSubmit"}
    commands = {
        event: group["hooks"][0]["command"]
        for event, groups in hooks["hooks"].items()
        for group in groups
    }
    assert '"/opt/my tools/bin/engrim" hook --agent codex --event sessionstart' in commands["SessionStart"]
    assert '"/opt/my tools/bin/engrim" assist' in commands["UserPromptSubmit"]
    assert '"/opt/my tools/bin/engrim" log --hook --agent codex' in commands["Stop"]
    assert '"/opt/my tools/bin/engrim" log --hook --agent codex' in commands["SessionEnd"]
    assert "✓ wired Codex hooks" in out
    assert "review and trust" in out
    assert not (codex / "config.toml").exists()
    assert db.exists()


def test_setup_codex_preserves_existing_config_and_is_idempotent(tmp_path, monkeypatch, capsys):
    codex = _codex_home(tmp_path, monkeypatch)
    config_path = codex / "config.toml"
    config_path.write_text(
        'model = "gpt-5.6"\n\n[mcp_servers.keep]\ncommand = "keep-me"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/local/bin/engrim")
    monkeypatch.setattr(cli, "_verify_hook_bin", lambda _bin: None)
    db = tmp_path / "memory.db"

    main(["--db", str(db), "setup", "--codex"])
    first_hooks = (codex / "hooks.json").read_text(encoding="utf-8")
    first_config = config_path.read_text(encoding="utf-8")
    main(["--db", str(db), "setup", "--codex"])
    capsys.readouterr()

    assert (codex / "hooks.json").read_text(encoding="utf-8") == first_hooks
    assert config_path.read_text(encoding="utf-8") == first_config
    assert first_config == 'model = "gpt-5.6"\n\n[mcp_servers.keep]\ncommand = "keep-me"\n'


def test_codex_session_start_uses_payload_cwd_and_emits_context(tmp_path, monkeypatch, capsys):
    _codex_home(tmp_path, monkeypatch)
    project = tmp_path / "workspace"
    project.mkdir()
    main(["--db", str(tmp_path / "memory.db"), "add", "-p", str(project), "-t", "fact",
          "-s", "Codex project memory is available"])
    capsys.readouterr()

    payload = {"cwd": str(project), "hook_event_name": "SessionStart", "source": "startup"}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
    main(["--db", str(tmp_path / "memory.db"), "hook", "--agent", "codex",
          "--event", "sessionstart", "--no-sync"])
    result = json.loads(capsys.readouterr().out)
    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Codex project memory is available" in result["hookSpecificOutput"]["additionalContext"]


def test_codex_prompt_and_stop_hooks_log_once(tmp_path, monkeypatch, capsys):
    _codex_home(tmp_path, monkeypatch)
    project = tmp_path / "workspace"
    project.mkdir()
    prompt = {
        "cwd": str(project),
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "prompt": "We decided to keep the SQLite memory store.",
    }
    stop = {
        "cwd": str(project),
        "hook_event_name": "Stop",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "last_assistant_message": "The SQLite memory store remains the selected design.",
    }
    for payload in (prompt, prompt, stop, stop):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
        args = ["--db", str(tmp_path / "memory.db"), "log", "--hook", "--agent", "codex"]
        main(args)
        capsys.readouterr()

    conn = sqlite3.connect(tmp_path / "memory.db")
    rows = conn.execute("SELECT role, content FROM log ORDER BY id").fetchall()
    assert rows == [
        ("user", "We decided to keep the SQLite memory store."),
        ("assistant", "The SQLite memory store remains the selected design."),
    ]


def test_statusline_reads_codex_payload(tmp_path, monkeypatch, capsys):
    _codex_home(tmp_path, monkeypatch)
    project = tmp_path / "workspace"
    project.mkdir()
    db = tmp_path / "memory.db"
    main(["--db", str(db), "add", "-p", str(project), "-t", "fact", "-s", "status is live"])
    capsys.readouterr()

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({
        "cwd": str(project),
        "session_id": "session-1",
    })))
    main(["--db", str(db), "statusline"])
    out = capsys.readouterr().out
    assert "engrim" in out
    assert "curated" in out


def test_uninstall_codex_preserves_unrelated_hooks_and_config(tmp_path, monkeypatch, capsys):
    db, codex = _run_codex_setup(tmp_path, monkeypatch)
    hooks_path = codex / "hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "keep-me"}]})
    hooks_path.write_text(json.dumps(hooks), encoding="utf-8")
    config_path = codex / "config.toml"
    config_path.write_text('[mcp_servers.keep]\ncommand = "keep-me"\n', encoding="utf-8")
    original_config = config_path.read_text(encoding="utf-8")

    main(["--db", str(db), "uninstall", "--codex"])
    capsys.readouterr()
    remaining_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert all(
        h.get("command") == "keep-me"
        for groups in remaining_hooks["hooks"].values()
        for group in groups
        for h in group.get("hooks", [])
    )
    assert config_path.read_text(encoding="utf-8") == original_config
