"""Tests for universal multi-agent setup (Antigravity, Claude, Cursor)."""
import json
import os
import pytest

from engrim.cli import main


def test_setup_agy_dry_run(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    main(["--db", str(tmp_path / "m.db"), "setup", "--agy", "--dry-run"])
    out = capsys.readouterr().out
    assert "[dry-run] Would wire Antigravity hooks" in out
    assert "[dry-run] Would deploy canonical Antigravity skill" in out
    assert "[dry-run] Would register MCP server" in out

    # Verify dry run wrote nothing
    assert not (fake_home / ".gemini").exists()


def test_setup_agy_execution(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    main(["--db", str(tmp_path / "m.db"), "setup", "--agy"])
    out = capsys.readouterr().out
    assert "✓ wired PreInvocation & Stop hooks" in out
    assert "✓ deployed canonical Antigravity skill" in out
    assert "✓ registered MCP server" in out

    # Verify hooks.json
    hooks_file = fake_home / ".gemini" / "config" / "hooks.json"
    assert hooks_file.exists()
    hooks_cfg = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert "engrim" in hooks_cfg
    assert any("hook --agent agy --event boot" in h["command"] for h in hooks_cfg["engrim"]["PreInvocation"])
    assert any("hook --agent agy --event stop" in h["command"] for h in hooks_cfg["engrim"]["Stop"])

    # Verify SKILL.md
    skill_file = fake_home / ".gemini" / "config" / "skills" / "engrim" / "SKILL.md"
    assert skill_file.exists()
    skill_content = skill_file.read_text(encoding="utf-8")
    assert "name: engrim" in skill_content
    assert "engrim_recall" in skill_content
    assert "engrim_review" in skill_content

    # Verify MCP configs
    mcp_file = fake_home / ".gemini" / "antigravity-cli" / "mcp_config.json"
    assert mcp_file.exists()
    mcp_cfg = json.loads(mcp_file.read_text(encoding="utf-8"))
    assert "engrim" in mcp_cfg["mcpServers"]
    assert mcp_cfg["mcpServers"]["engrim"]["args"] == ["serve", "--mcp"]


def test_setup_cursor_execution(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    main(["--db", str(tmp_path / "m.db"), "setup", "--cursor"])
    out = capsys.readouterr().out
    assert "✓ registered Cursor MCP entry" in out

    cursor_mcp = fake_home / ".cursor" / "mcp.json"
    assert cursor_mcp.exists()
    cursor_cfg = json.loads(cursor_mcp.read_text(encoding="utf-8"))
    assert "engrim" in cursor_cfg["mcpServers"]
    assert cursor_cfg["mcpServers"]["engrim"]["args"] == ["serve", "--mcp"]


def test_setup_auto_detects_gemini(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".gemini").mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    main(["--db", str(tmp_path / "m.db"), "setup"])
    out = capsys.readouterr().out
    assert "Auto-detected environments: Antigravity (~/.gemini)" in out
    assert (fake_home / ".gemini" / "config" / "hooks.json").exists()
