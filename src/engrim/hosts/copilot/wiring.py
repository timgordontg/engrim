"""Copilot CLI install-time wiring: the hook file, the MCP entry, and the instructions note.

`engrim setup --copilot` calls `setup()`; `engrim uninstall --copilot` calls `uninstall()`.
This module needs nothing from the rest of engrim, so the CLI can import it freely.

Copilot CLI loads every `*.json` in `<copilot home>/hooks/`, so engrim owns one file of its own
(`engrim.json`) instead of merging into a shared one: setup rewrites it, uninstall deletes it, and
a user's other hook files are never parsed, let alone touched.

The hook entries use `exec` + `args` rather than a `bash` string. Copilot CLI runs `exec` entries
directly, with no shell in between, which removes the quoting that broke engrim's Windows hooks
before (a path with spaces or backslashes needs no escaping when it is argv[0]). The price is that
there is no `|| engrim ...` PATH fallback if the recorded path later moves; `engrim doctor` checks
the recorded binary, and re-running setup re-resolves it.
"""
from __future__ import annotations

import json
import os
import sys
from importlib import resources

_FILES = resources.files(__package__)

# Appended to <copilot home>/copilot-instructions.md: how the agent is meant to use the MCP tools.
INSTRUCTIONS_MD = (_FILES / "INSTRUCTIONS.md").read_text(encoding="utf-8")
INSTRUCTIONS_BEGIN = "<!-- engrim:begin -->"
INSTRUCTIONS_END = "<!-- engrim:end -->"
INSTRUCTIONS_BLOCK = (
    f"{INSTRUCTIONS_BEGIN}\n{INSTRUCTIONS_MD.rstrip()}\n{INSTRUCTIONS_END}"
)
LEGACY_INSTRUCTIONS_HEADING = INSTRUCTIONS_MD.splitlines()[0]

HOOK_FILE = "engrim.json"
MCP_FILE = "mcp-config.json"
INSTRUCTIONS_FILE = "copilot-instructions.md"
SETTINGS_FILE = "settings.json"


def home(pretty: bool = False) -> str:
    """Copilot CLI's home: $COPILOT_HOME when set (it relocates hooks, MCP, and config), else ~/.copilot."""
    env = os.environ.get("COPILOT_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return "~/.copilot" if pretty else os.path.abspath(os.path.expanduser("~/.copilot"))


def _display_path(path: str) -> str:
    user_home = os.path.expanduser("~")
    if path == user_home:
        return "~"
    if path.startswith(user_home + os.sep):
        return "~" + path[len(user_home):]
    return path


def _print_preserved_state(
    cp_home: str,
    db_path: str | None,
    *,
    dry_run: bool,
) -> None:
    resolved_db = os.path.abspath(os.path.expanduser(
        db_path or os.environ.get("ENGRIM_DB") or "~/.engrim/memory.db"
    ))
    rows = [
        (
            str(resources.files("engrim")),
            "Installed Engrim Python package; host uninstall does not uninstall the software.",
        ),
        (
            resolved_db,
            "Shared memory database; host uninstall does not delete memories.",
        ),
    ]
    copilot_state = [
        ("config.json", "Trusted folders; may retain the Engrim repository path."),
        (
            "permissions-config.json",
            "Tool and MCP approvals; may retain Engrim entries.",
        ),
        ("command-history-state.json", "Command history; may mention Engrim."),
        ("logs", "Copilot logs; may mention Engrim."),
        (
            "session-state",
            "Session transcripts and rewind snapshots; may mention Engrim.",
        ),
        ("sidebar-sessions-state", "Sidebar session metadata; may mention Engrim."),
    ]
    rows.extend(
        (path, description)
        for name, description in copilot_state
        if os.path.exists(path := os.path.join(cp_home, name))
    )
    display_rows = [(_display_path(path), description) for path, description in rows]
    path_width = max(len("Path"), *(len(path) for path, _description in display_rows))

    print("[dry-run] Would leave unchanged:" if dry_run else "• Left unchanged:")
    print(f"  {'Path':<{path_width}}  Description")
    print(f"  {'-' * path_width}  {'-' * len('Description')}")
    for path, description in display_rows:
        print(f"  {path:<{path_width}}  {description}")


def hook_config(engrim_bin: str) -> dict:
    """The hook file engrim owns.

    `sessionStart` is the whole point: its stdout `additionalContext` is injected into the new
    session, so the boot pack arrives before the first prompt. `userPromptSubmitted` logs the
    prompt to the flight recorder - Copilot CLI drops the output of command hooks on that event, so
    it deliberately injects nothing. `agentStop` reads assistant messages from the supplied
    transcript path and returns a protocol-safe completion decision.
    """
    raw_bin = engrim_bin.strip('"')
    return {
        "version": 1,
        "hooks": {
            "sessionStart": [{
                "type": "command",
                "exec": raw_bin,
                "args": ["hook", "--agent", "copilot", "--event", "sessionstart"],
                "timeoutSec": 20,
            }],
            "userPromptSubmitted": [{
                "type": "command",
                "exec": raw_bin,
                "args": ["log", "--hook", "--agent", "copilot"],
                "timeoutSec": 10,
            }],
            "agentStop": [{
                "type": "command",
                "exec": raw_bin,
                "args": ["log", "--hook", "--agent", "copilot"],
                "timeoutSec": 10,
            }],
        },
    }


def mcp_entry(engrim_bin: str) -> dict:
    """The stdio MCP server entry, which is how the model writes memory (`engrim_add`)."""
    return {
        "type": "local",
        "command": engrim_bin.strip('"'),
        "args": ["serve", "--mcp"],
        "tools": ["*"],
    }


def status_line_entry(engrim_bin: str) -> dict:
    """Return Copilot CLI's command-backed status-line configuration."""
    return {"type": "command", "command": f"{engrim_bin} statusline"}


def is_engrim_status_line(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    command = value.get("command")
    if not isinstance(command, str):
        return False
    normalized = command.replace("\\", "/").replace('"', "").replace("'", "").lower()
    return "engrim statusline" in normalized.replace(".exe", "")


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".engrim-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _instruction_span(content: str) -> tuple[int, int] | None:
    start = content.find(INSTRUCTIONS_BEGIN)
    if start >= 0:
        end = content.find(INSTRUCTIONS_END, start + len(INSTRUCTIONS_BEGIN))
        if end < 0:
            raise ValueError(
                f"found {INSTRUCTIONS_BEGIN!r} without matching {INSTRUCTIONS_END!r}"
            )
        return start, end + len(INSTRUCTIONS_END)

    start = content.find(LEGACY_INSTRUCTIONS_HEADING)
    while start >= 0:
        line_start = start == 0 or content[start - 1] == "\n"
        heading_end = start + len(LEGACY_INSTRUCTIONS_HEADING)
        line_end = heading_end == len(content) or content[heading_end] == "\n"
        if line_start and line_end:
            next_heading = content.find("\n## ", heading_end)
            end = len(content) if next_heading < 0 else next_heading + 1
            return start, end
        start = content.find(LEGACY_INSTRUCTIONS_HEADING, start + 1)
    return None


def _replace_instructions(content: str, replacement: str | None) -> str:
    span = _instruction_span(content)
    if span is None:
        if replacement is None:
            return content
        parts = [content.rstrip("\n"), replacement]
    else:
        start, end = span
        parts = [content[:start].rstrip("\n")]
        if replacement is not None:
            parts.append(replacement)
        parts.append(content[end:].lstrip("\n"))
    normalized = [part.strip("\n") for part in parts if part.strip("\n")]
    return "\n\n".join(normalized) + ("\n" if normalized else "")


def setup(engrim_bin: str, dry_run: bool = False) -> None:
    print("Wiring GitHub Copilot CLI environment…")
    cp_home = home()
    hooks_path = os.path.join(cp_home, "hooks", HOOK_FILE)
    mcp_path = os.path.join(cp_home, MCP_FILE)
    instructions_path = os.path.join(cp_home, INSTRUCTIONS_FILE)
    settings_path = os.path.join(cp_home, SETTINGS_FILE)
    desired_hooks = hook_config(engrim_bin)
    desired_mcp = mcp_entry(engrim_bin)
    desired_status_line = status_line_entry(engrim_bin)
    if dry_run:
        print(f"[dry-run] Would write Copilot hooks to {hooks_path}:")
        for event, entries in desired_hooks["hooks"].items():
            print(f"    {event}: {entries[0]['exec']} {' '.join(entries[0]['args'])}")
        print(f"[dry-run] Would register engrim MCP server in {mcp_path}")
        print(f"[dry-run] Would configure the Engrim status line in {settings_path}")
        print(f"[dry-run] Would add or update usage note in {instructions_path}")
        return

    # Validate shared config before writing anything, so a bad file can't leave a half-wired setup.
    cfg = {}
    if os.path.exists(mcp_path):
        try:
            with open(mcp_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except json.JSONDecodeError as e:
            sys.exit(f"{mcp_path} exists but is not valid JSON ({e}). Fix it, then re-run.")
        except OSError as e:
            sys.exit(f"can't read {mcp_path} ({e}). Fix the permissions, then re-run.")
    if not isinstance(cfg, dict):
        sys.exit(f"{mcp_path} is not a JSON object. Fix it, then re-run.")
    if "mcpServers" in cfg and not isinstance(cfg["mcpServers"], dict):
        sys.exit(f"{mcp_path}: the \"mcpServers\" key must be a JSON object. Fix it, then re-run.")

    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        except json.JSONDecodeError as e:
            sys.exit(f"{settings_path} exists but is not valid JSON ({e}). Fix it, then re-run.")
        except OSError as e:
            sys.exit(f"can't read {settings_path} ({e}). Fix the permissions, then re-run.")
    if not isinstance(settings, dict):
        sys.exit(f"{settings_path} is not a JSON object. Fix it, then re-run.")

    os.makedirs(os.path.dirname(hooks_path), exist_ok=True)
    existing_hooks = None
    if os.path.exists(hooks_path):
        try:
            with open(hooks_path, encoding="utf-8") as f:
                existing_hooks = json.load(f)
        except Exception:
            existing_hooks = None
    if existing_hooks == desired_hooks:
        print(f"✓ Copilot hooks already current in {hooks_path}")
    else:
        _write_json(hooks_path, desired_hooks)
        print(f"✓ wired Copilot hooks in {hooks_path}")
        for event, entries in desired_hooks["hooks"].items():
            print(f"    {event}: {entries[0]['exec']} {' '.join(entries[0]['args'])}")

    servers = cfg.setdefault("mcpServers", {})
    if servers.get("engrim") == desired_mcp:
        print(f"✓ MCP server already registered in {mcp_path}")
    else:
        servers["engrim"] = desired_mcp
        _write_json(mcp_path, cfg)
        print(f"✓ registered MCP server in {mcp_path}")

    status_line = settings.get("statusLine")
    if is_engrim_status_line(status_line):
        if status_line == desired_status_line:
            print(f"✓ status line already shows engrim ({settings_path})")
        else:
            settings["statusLine"] = desired_status_line
            _write_json(settings_path, settings)
            print(f"✓ updated Engrim status line in {settings_path}")
    elif status_line:
        print(
            "• a Copilot status line is already configured - leaving it. "
            f"To show engrim, set its command to: {desired_status_line['command']}"
        )
    else:
        settings["statusLine"] = desired_status_line
        _write_json(settings_path, settings)
        print(f"✓ wired status line in {settings_path}")

    existing = ""
    if os.path.exists(instructions_path):
        with open(instructions_path, encoding="utf-8", errors="replace") as f:
            existing = f.read()
    try:
        updated = _replace_instructions(existing, INSTRUCTIONS_BLOCK)
    except ValueError as exc:
        sys.exit(f"{instructions_path}: {exc}. Fix the managed markers, then re-run.")
    if updated == existing:
        print(f"✓ instructions already mention engrim ({instructions_path})")
    else:
        with open(instructions_path, "w", encoding="utf-8") as f:
            f.write(updated)
        print(f"✓ added or updated usage note in {instructions_path}")
    print("  Hook changes load at startup: open a NEW Copilot CLI session to pick them up.")


def uninstall(dry_run: bool = False, db_path: str | None = None) -> None:
    print("Unwiring GitHub Copilot CLI environment…")
    cp_home = home()
    hooks_path = os.path.join(cp_home, "hooks", HOOK_FILE)
    mcp_path = os.path.join(cp_home, MCP_FILE)
    instructions_path = os.path.join(cp_home, INSTRUCTIONS_FILE)
    settings_path = os.path.join(cp_home, SETTINGS_FILE)
    if dry_run:
        print(f"[dry-run] Would remove {hooks_path}, the engrim MCP entry in {mcp_path}, "
              f"the Engrim status line in {settings_path}, and the note in {instructions_path}")
        _print_preserved_state(cp_home, db_path, dry_run=True)
        return

    if os.path.exists(hooks_path):
        os.remove(hooks_path)
        print(f"✓ removed {hooks_path}")
    else:
        print(f"✓ Copilot hooks already unwired ({hooks_path})")

    if os.path.exists(mcp_path):
        try:
            with open(mcp_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = None
            print(f"• {mcp_path} isn't valid JSON - remove the mcpServers.engrim entry by hand")
        servers = cfg.get("mcpServers") if isinstance(cfg, dict) else None
        if isinstance(servers, dict) and "engrim" in servers:
            del servers["engrim"]
            if not servers:
                del cfg["mcpServers"]
            _write_json(mcp_path, cfg)
            print(f"✓ removed MCP entry from {mcp_path}")
        elif cfg is not None:
            print(f"✓ MCP entry already removed from {mcp_path}")

    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        except Exception:
            settings = None
            print(f"• {settings_path} isn't valid JSON - remove the Engrim status line by hand")
        if isinstance(settings, dict) and is_engrim_status_line(
            settings.get("statusLine")
        ):
            del settings["statusLine"]
            _write_json(settings_path, settings)
            print(f"✓ removed Engrim status line from {settings_path}")
        elif settings is not None:
            print(f"✓ Engrim status line already removed from {settings_path}")

    if os.path.exists(instructions_path):
        with open(instructions_path, encoding="utf-8", errors="replace") as f:
            existing = f.read()
        try:
            new = _replace_instructions(existing, None)
        except ValueError as exc:
            print(f"• {instructions_path}: {exc} - remove the engrim section by hand")
        else:
            if new != existing:
                with open(instructions_path, "w", encoding="utf-8") as f:
                    f.write(new)
                print(f"✓ removed usage note from {instructions_path}")

    _print_preserved_state(cp_home, db_path, dry_run=False)
