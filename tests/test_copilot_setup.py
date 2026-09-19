"""Regression tests for the GitHub Copilot CLI setup and hook integration."""
import io
import json
import sqlite3

import engrim.cli as cli
from engrim.cli import main
from engrim.hosts.copilot import wiring as copilot_host


def _copilot_home(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRIM_PROJECT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_TAG", raising=False)
    home = tmp_path / "home"
    copilot = home / ".copilot"
    copilot.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("COPILOT_HOME", str(copilot))
    return copilot


def _project_dir(tmp_path, name="workspace"):
    project = tmp_path / name
    project.mkdir()
    (project / ".git").mkdir()
    return project


def _run_copilot_setup(tmp_path, monkeypatch, which="/opt/my tools/bin/engrim"):
    copilot = _copilot_home(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: which)
    monkeypatch.setattr(cli, "_verify_hook_bin", lambda _bin: None)
    db = tmp_path / "memory.db"
    main(["--db", str(db), "setup", "--copilot"])
    return db, copilot


def test_setup_copilot_writes_hooks_mcp_status_line_and_instructions(
        tmp_path, monkeypatch, capsys):
    db, copilot = _run_copilot_setup(tmp_path, monkeypatch)
    out = capsys.readouterr().out

    hooks = json.loads((copilot / "hooks" / "engrim.json").read_text(encoding="utf-8"))
    assert hooks["version"] == 1
    assert set(hooks["hooks"]) == {"sessionStart", "userPromptSubmitted", "agentStop"}
    boot = hooks["hooks"]["sessionStart"][0]
    # exec + args, so a path with spaces needs no shell quoting at all.
    assert boot["exec"] == "/opt/my tools/bin/engrim"
    assert boot["args"] == ["hook", "--agent", "copilot", "--event", "sessionstart"]
    assert "bash" not in boot and "command" not in boot
    prompt = hooks["hooks"]["userPromptSubmitted"][0]
    assert prompt["args"] == ["log", "--hook", "--agent", "copilot"]
    stop = hooks["hooks"]["agentStop"][0]
    assert stop["args"] == ["log", "--hook", "--agent", "copilot"]

    mcp = json.loads((copilot / "mcp-config.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["engrim"] == {
        "type": "local",
        "command": "/opt/my tools/bin/engrim",
        "args": ["serve", "--mcp"],
        "tools": ["*"],
    }

    settings = json.loads((copilot / "settings.json").read_text(encoding="utf-8"))
    assert settings["statusLine"] == {
        "type": "command",
        "command": '"/opt/my tools/bin/engrim" statusline',
    }

    instructions = (copilot / "copilot-instructions.md").read_text(encoding="utf-8")
    assert "engrim_recall" in instructions
    assert copilot_host.INSTRUCTIONS_BEGIN in instructions
    assert copilot_host.INSTRUCTIONS_END in instructions
    assert "✓ wired Copilot hooks" in out
    assert db.exists()


def test_setup_copilot_is_idempotent_and_preserves_other_config(tmp_path, monkeypatch, capsys):
    copilot = _copilot_home(tmp_path, monkeypatch)
    mcp_path = copilot / "mcp-config.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"keep": {"command": "keep-me"}}}), encoding="utf-8")
    (copilot / "hooks").mkdir()
    other_hooks = copilot / "hooks" / "mine.json"
    other_hooks.write_text(json.dumps({"version": 1, "hooks": {}}), encoding="utf-8")
    instructions = copilot / "copilot-instructions.md"
    instructions.write_text("# my notes\n", encoding="utf-8")
    settings_path = copilot / "settings.json"
    settings_path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/local/bin/engrim")
    monkeypatch.setattr(cli, "_verify_hook_bin", lambda _bin: None)
    db = tmp_path / "memory.db"

    main(["--db", str(db), "setup", "--copilot"])
    first_hooks = (copilot / "hooks" / "engrim.json").read_text(encoding="utf-8")
    first_instructions = instructions.read_text(encoding="utf-8")
    main(["--db", str(db), "setup", "--copilot"])
    capsys.readouterr()

    assert (copilot / "hooks" / "engrim.json").read_text(encoding="utf-8") == first_hooks
    assert instructions.read_text(encoding="utf-8") == first_instructions
    assert first_instructions.startswith("# my notes\n")
    assert other_hooks.read_text(encoding="utf-8") == json.dumps({"version": 1, "hooks": {}})
    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["keep"] == {"command": "keep-me"}
    assert "engrim" in mcp["mcpServers"]
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["theme"] == "dark"
    assert settings["statusLine"]["command"].endswith("engrim\" statusline")


def test_setup_copilot_preserves_custom_status_line(
        tmp_path, monkeypatch, capsys):
    copilot = _copilot_home(tmp_path, monkeypatch)
    settings_path = copilot / "settings.json"
    custom = {"type": "command", "command": "~/bin/my-status"}
    settings_path.write_text(json.dumps({"statusLine": custom}), encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/local/bin/engrim")
    monkeypatch.setattr(cli, "_verify_hook_bin", lambda _bin: None)

    main(["--db", str(tmp_path / "memory.db"), "setup", "--copilot"])
    out = capsys.readouterr().out

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["statusLine"] == custom
    assert "already configured - leaving it" in out


def test_setup_copilot_updates_stale_engrim_status_line(
        tmp_path, monkeypatch, capsys):
    copilot = _copilot_home(tmp_path, monkeypatch)
    settings_path = copilot / "settings.json"
    settings_path.write_text(
        json.dumps({
            "statusLine": {
                "type": "command",
                "command": '"/old/path/engrim" statusline',
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/new/path/engrim")
    monkeypatch.setattr(cli, "_verify_hook_bin", lambda _bin: None)

    main(["--db", str(tmp_path / "memory.db"), "setup", "--copilot"])
    out = capsys.readouterr().out

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["statusLine"]["command"] == '"/new/path/engrim" statusline'
    assert "updated Engrim status line" in out


def test_setup_copilot_updates_legacy_instructions_and_preserves_neighbors(
        tmp_path, monkeypatch, capsys):
    copilot = _copilot_home(tmp_path, monkeypatch)
    instructions = copilot / "copilot-instructions.md"
    instructions.write_text(
        "# My instructions\n\n"
        "## Project memory (engrim) - shared with every agent on this repo\n\n"
        "The flight recorder only contains prompts.\n\n"
        "## Unrelated instructions\n\n"
        "Keep this section unchanged.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/local/bin/engrim")
    monkeypatch.setattr(cli, "_verify_hook_bin", lambda _bin: None)

    main(["--db", str(tmp_path / "memory.db"), "setup", "--copilot"])
    updated = instructions.read_text(encoding="utf-8")
    capsys.readouterr()

    assert "only contains prompts" not in updated
    assert "prompts and top-level assistant messages" in updated
    assert "run `engrim doctor`" in updated
    assert updated.count(copilot_host.INSTRUCTIONS_BEGIN) == 1
    assert updated.count("## Project memory (engrim)") == 1
    assert updated.endswith("## Unrelated instructions\n\nKeep this section unchanged.\n")


def test_setup_copilot_dry_run_touches_nothing(tmp_path, monkeypatch, capsys):
    copilot = _copilot_home(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/local/bin/engrim")
    monkeypatch.setattr(cli, "_verify_hook_bin", lambda _bin: None)

    main(["--db", str(tmp_path / "memory.db"), "setup", "--copilot", "--dry-run"])
    out = capsys.readouterr().out

    assert "[dry-run]" in out
    assert not (copilot / "hooks" / "engrim.json").exists()
    assert not (copilot / "mcp-config.json").exists()
    assert not (copilot / "settings.json").exists()
    assert not (copilot / "copilot-instructions.md").exists()


def test_copilot_session_start_emits_flat_additional_context(tmp_path, monkeypatch, capsys):
    _copilot_home(tmp_path, monkeypatch)
    project = _project_dir(tmp_path)
    main(["--db", str(tmp_path / "memory.db"), "add", "-p", str(project), "-t", "fact",
          "-s", "Copilot project memory is available"])
    capsys.readouterr()

    payload = {"cwd": str(project), "sessionId": "session-1", "source": "startup"}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
    main(["--db", str(tmp_path / "memory.db"), "hook", "--agent", "copilot",
          "--event", "sessionstart", "--no-sync"])
    result = json.loads(capsys.readouterr().out)

    # Copilot CLI reads a flat object; a nested hookSpecificOutput would be ignored.
    assert set(result) == {"additionalContext"}
    assert "Copilot project memory is available" in result["additionalContext"]


def test_copilot_prompt_hook_logs_once_per_prompt(tmp_path, monkeypatch, capsys):
    _copilot_home(tmp_path, monkeypatch)
    project = _project_dir(tmp_path)
    payload = {
        "cwd": str(project),
        "sessionId": "session-1",
        "timestamp": 1789841102465,
        "prompt": "We decided to keep the SQLite memory store.",
    }
    for _ in range(2):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
        main(["--db", str(tmp_path / "memory.db"), "log", "--hook", "--agent", "copilot"])
        capsys.readouterr()

    conn = sqlite3.connect(tmp_path / "memory.db")
    rows = conn.execute("SELECT project, session, role, content FROM log ORDER BY id").fetchall()
    assert rows == [(str(project), "session-1", "user",
                     "We decided to keep the SQLite memory store.")]


def test_copilot_repeated_prompt_text_is_kept(tmp_path, monkeypatch, capsys):
    """A repeated "continue" is a new turn: only a replayed timestamp is a duplicate."""
    _copilot_home(tmp_path, monkeypatch)
    project = _project_dir(tmp_path)
    for stamp in (1789841102465, 1789841102999):
        payload = {"cwd": str(project), "sessionId": "session-1",
                   "timestamp": stamp, "prompt": "continue"}
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
        main(["--db", str(tmp_path / "memory.db"), "log", "--hook", "--agent", "copilot"])
        capsys.readouterr()

    conn = sqlite3.connect(tmp_path / "memory.db")
    rows = conn.execute("SELECT content FROM log ORDER BY id").fetchall()
    assert rows == [("continue",), ("continue",)]


def test_copilot_zero_timestamp_is_a_timestamp(tmp_path, monkeypatch, capsys):
    """Epoch 0 is a value, not a missing field: two distinct prompts must both land."""
    _copilot_home(tmp_path, monkeypatch)
    project = _project_dir(tmp_path)
    for prompt in ("first", "second"):
        payload = {"cwd": str(project), "sessionId": "session-1",
                   "timestamp": 0, "prompt": prompt}
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
        main(["--db", str(tmp_path / "memory.db"), "log", "--hook", "--agent", "copilot"])
        capsys.readouterr()

    conn = sqlite3.connect(tmp_path / "memory.db")
    rows = conn.execute("SELECT content FROM log ORDER BY id").fetchall()
    assert rows == [("first",), ("second",)]


def test_copilot_prompt_without_a_timestamp_is_never_dropped(tmp_path, monkeypatch, capsys):
    """With no timestamp a repeat is indistinguishable from a replay: keep both, lose neither."""
    _copilot_home(tmp_path, monkeypatch)
    project = _project_dir(tmp_path)
    payload = {"cwd": str(project), "sessionId": "session-1", "prompt": "continue"}
    for _ in range(2):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
        main(["--db", str(tmp_path / "memory.db"), "log", "--hook", "--agent", "copilot"])
        capsys.readouterr()

    conn = sqlite3.connect(tmp_path / "memory.db")
    rows = conn.execute("SELECT content FROM log ORDER BY id").fetchall()
    assert rows == [("continue",), ("continue",)]


def test_copilot_origin_agent_round_trips(tmp_path, monkeypatch, capsys):
    _copilot_home(tmp_path, monkeypatch)
    project = _project_dir(tmp_path)
    db = tmp_path / "memory.db"
    main(["--db", str(db), "add", "-p", str(project), "-t", "decision",
          "-s", "Wired engrim into Copilot CLI", "--origin-agent", "copilot"])
    capsys.readouterr()

    main(["--db", str(db), "list", "-p", str(project)])
    out = capsys.readouterr().out
    assert "via Copilot CLI" in out


def test_uninstall_copilot_removes_only_engrim(tmp_path, monkeypatch, capsys):
    db, copilot = _run_copilot_setup(tmp_path, monkeypatch)
    mcp_path = copilot / "mcp-config.json"
    cfg = json.loads(mcp_path.read_text(encoding="utf-8"))
    cfg["mcpServers"]["keep"] = {"command": "keep-me"}
    mcp_path.write_text(json.dumps(cfg), encoding="utf-8")
    other_hooks = copilot / "hooks" / "mine.json"
    other_hooks.write_text("{}", encoding="utf-8")
    permissions_path = copilot / "permissions-config.json"
    permissions_path.write_text("{}", encoding="utf-8")
    capsys.readouterr()

    main(["--db", str(db), "uninstall", "--copilot"])
    out = capsys.readouterr().out

    assert not (copilot / "hooks" / "engrim.json").exists()
    assert other_hooks.exists()
    remaining = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert remaining["mcpServers"] == {"keep": {"command": "keep-me"}}
    settings = json.loads(
        (copilot / "settings.json").read_text(encoding="utf-8")
    )
    assert "statusLine" not in settings
    instructions = (copilot / "copilot-instructions.md").read_text(encoding="utf-8")
    assert "engrim_recall" not in instructions
    assert "Left unchanged:" in out
    assert "Path" in out
    assert "Description" in out
    assert str(db) in out
    assert "Shared memory database; host uninstall does not delete memories." in out
    assert "Installed Engrim Python package" in out
    assert "permissions-config.json" in out
    assert "Tool and MCP approvals; may retain Engrim entries." in out


def test_uninstall_copilot_reports_preserved_state_without_managed_note(
        tmp_path, monkeypatch, capsys):
    copilot = _copilot_home(tmp_path, monkeypatch)
    instructions = copilot / "copilot-instructions.md"
    instructions.write_text("# My instructions\n", encoding="utf-8")

    main(["--db", str(tmp_path / "memory.db"), "uninstall", "--copilot"])
    out = capsys.readouterr().out

    assert "Left unchanged:" in out
    assert str(tmp_path / "memory.db") in out
    assert instructions.read_text(encoding="utf-8") == "# My instructions\n"


def test_uninstall_copilot_removes_legacy_note_and_preserves_neighbors(
        tmp_path, monkeypatch, capsys):
    copilot = _copilot_home(tmp_path, monkeypatch)
    instructions = copilot / "copilot-instructions.md"
    instructions.write_text(
        "# My instructions\n\n"
        "## Project memory (engrim) - shared with every agent on this repo\n\n"
        "Legacy Engrim guidance.\n\n"
        "## Unrelated instructions\n\n"
        "Keep this section unchanged.\n",
        encoding="utf-8",
    )

    main(["--db", str(tmp_path / "memory.db"), "uninstall", "--copilot"])
    capsys.readouterr()

    assert instructions.read_text(encoding="utf-8") == (
        "# My instructions\n\n"
        "## Unrelated instructions\n\n"
        "Keep this section unchanged.\n"
    )


def test_copilot_home_env_var_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "elsewhere"))
    assert copilot_host.home() == str(tmp_path / "elsewhere")
    monkeypatch.delenv("COPILOT_HOME")
    assert copilot_host.home(pretty=True) == "~/.copilot"


def test_doctor_reports_copilot_environment(tmp_path, monkeypatch, capsys):
    db, copilot = _run_copilot_setup(tmp_path, monkeypatch, which=str(tmp_path / "engrim"))
    (tmp_path / "engrim").write_text("#!/bin/sh\n", encoding="utf-8")
    capsys.readouterr()

    main(["--db", str(db), "doctor", "--json"])
    report = json.loads(capsys.readouterr().out)
    env = report["environments"]["copilot"]
    assert env["detected"] is True
    assert env["mcp"] is True
    assert env["status_line"]["engrim"] is True
    assert env["status_line"]["valid"] is True
    assert set(env["hooks"]) == {"sessionStart", "userPromptSubmitted", "agentStop"}
    assert all(h["valid"] for h in env["hooks"].values())
    assert report["issues"] == []


def test_doctor_flags_a_broken_copilot_mcp_binary(tmp_path, monkeypatch, capsys):
    """A stale MCP path is as broken as a stale hook path, and `--fix` only runs on issues."""
    db, copilot = _run_copilot_setup(tmp_path, monkeypatch, which=str(tmp_path / "engrim"))
    capsys.readouterr()  # the binary was never created, so the recorded path is dead

    main(["--db", str(db), "doctor", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["environments"]["copilot"]["mcp"] is False
    assert any("Copilot CLI MCP server broken" in i for i in report["issues"])


def test_doctor_flags_a_broken_copilot_status_line_binary(
        tmp_path, monkeypatch, capsys):
    db, _copilot = _run_copilot_setup(
        tmp_path, monkeypatch, which=str(tmp_path / "engrim")
    )
    capsys.readouterr()

    main(["--db", str(db), "doctor", "--json"])
    report = json.loads(capsys.readouterr().out)

    status_line = report["environments"]["copilot"]["status_line"]
    assert status_line["engrim"] is True
    assert status_line["valid"] is False
    assert any(
        "Copilot CLI status line broken" in issue
        for issue in report["issues"]
    )


def test_mcp_server_attributes_writes_to_copilot(tmp_path):
    """Copilot CLI identifies itself as `copilot-cli` in the MCP handshake (verified against
    1.0.87-0), so records it writes are attributed without the model having to say so."""
    from engrim.mcp_server import serve

    conn = cli.connect(str(tmp_path / "m.db"))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "copilot-cli", "version": "1.0.87-0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "engrim_add",
                    "arguments": {"type": "decision", "summary": "wired via Copilot CLI",
                                  "project": str(tmp_path)}}},
    ]
    inp = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    out = io.StringIO()
    serve(conn, inp=inp, out=out)

    row = conn.execute("SELECT origin_agent FROM memories WHERE summary = ?",
                       ("wired via Copilot CLI",)).fetchone()
    assert row[0] == "copilot"
