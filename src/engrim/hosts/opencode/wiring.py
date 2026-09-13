"""OpenCode install-time wiring: the plugin file, the MCP entry, and the AGENTS.md note.

`engrim setup --opencode` calls `setup()`; `engrim uninstall --opencode` calls `uninstall()`.
Runtime behaviour (the boot / prompt / stop handlers plugin.js shells out to) is in hooks.py
next to this file. This module needs nothing from the rest of engrim, so the CLI can import it
freely.
"""
from __future__ import annotations

import json
import os
import sys
from importlib import resources

_FILES = resources.files(__package__)

# AGENTS.md: the agent-facing instructions. `setup` appends them to ~/.config/opencode/AGENTS.md and
# render_plugin bakes them into the plugin's system-prompt block, so the two can't drift.
AGENTS_MD = (_FILES / "AGENTS.md").read_text(encoding="utf-8")

# The plugin OpenCode loads (plugin.js next to this file). `__ENGRIM_BIN__` and `__ENGRIM_USAGE__`
# are filled in by render_plugin at setup time, so the plugin depends on neither OpenCode's PATH nor
# a second copy of the usage text. Plain JS + node:child_process — Bun runs it as-is, nothing to build.
PLUGIN_JS = (_FILES / "plugin.js").read_text(encoding="utf-8")


def render_plugin(engrim_bin: str) -> str:
    """Bake the resolved binary into the plugin source (JSON-quoted: safe for Windows paths/spaces)."""
    return (PLUGIN_JS
            .replace("__ENGRIM_BIN__", json.dumps(engrim_bin.strip('"').replace("\\", "/")))
            .replace("__ENGRIM_USAGE__", json.dumps(AGENTS_MD.rstrip(), ensure_ascii=False)))


def config_dir(pretty: bool = False) -> str:
    """OpenCode's global config dir: $XDG_CONFIG_HOME/opencode, i.e. ~/.config/opencode by default."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "opencode")
    return "~/.config/opencode" if pretty else os.path.expanduser("~/.config/opencode")


def config_file(cfg_dir: str) -> str:
    """The config file to merge the MCP entry into. `opencode.json` first; `opencode.jsonc` only if it
    is a strict-JSON file we can round-trip (comments would be lost on rewrite, so we refuse those)."""
    plain = os.path.join(cfg_dir, "opencode.json")
    jsonc = os.path.join(cfg_dir, "opencode.jsonc")
    if os.path.exists(plain) or not os.path.exists(jsonc):
        return plain
    try:
        with open(jsonc, encoding="utf-8") as f:
            json.load(f)
        return jsonc
    except Exception:
        return plain


def setup(engrim_bin: str, dry_run: bool = False) -> None:
    print("Wiring OpenCode environment…")
    cfg_dir = config_dir()
    plugin_path = os.path.join(cfg_dir, "plugins", "engrim.js")
    cfg_path = config_file(cfg_dir)
    agents_md = os.path.join(cfg_dir, "AGENTS.md")
    raw_bin = engrim_bin.strip('"')
    mcp_entry = {"type": "local", "command": [raw_bin, "serve", "--mcp"], "enabled": True}
    if dry_run:
        print(f"[dry-run] Would write OpenCode plugin to {plugin_path}")
        print(f"[dry-run] Would register engrim MCP server in {cfg_path}")
        print(f"[dry-run] Would add usage note to {agents_md}")
        return

    # Validate the config first so a bad file can't leave a half-wired setup (plugin written, MCP not).
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except json.JSONDecodeError as e:
            sys.exit(f"{cfg_path} exists but is not valid JSON ({e}). Fix it, then re-run.")
    if not isinstance(cfg, dict):
        sys.exit(f"{cfg_path} is not a JSON object. Fix it, then re-run.")
    if "mcp" in cfg and not isinstance(cfg["mcp"], dict):
        sys.exit(f"{cfg_path}: the \"mcp\" key must be a JSON object. Fix it, then re-run.")

    os.makedirs(os.path.dirname(plugin_path), exist_ok=True)
    tmp = plugin_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(render_plugin(engrim_bin))
    os.replace(tmp, plugin_path)
    print(f"✓ wrote OpenCode plugin {plugin_path}")

    servers = cfg.setdefault("mcp", {})
    if servers.get("engrim") == mcp_entry:
        print(f"✓ MCP server already registered in {cfg_path}")
    else:
        servers["engrim"] = mcp_entry
        cfg.setdefault("$schema", "https://opencode.ai/config.json")
        tmp = cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, cfg_path)
        print(f"✓ registered MCP server in {cfg_path}")
    jsonc = os.path.join(cfg_dir, "opencode.jsonc")
    if cfg_path.endswith(".json") and os.path.exists(jsonc):
        print(f"• {jsonc} also exists — OpenCode reads both; the MCP entry went into opencode.json")

    existing = ""
    if os.path.exists(agents_md):
        with open(agents_md, encoding="utf-8", errors="replace") as f:
            existing = f.read()
    if "engrim" in existing and "Project memory" in existing:
        print(f"✓ AGENTS.md already mentions engrim ({agents_md})")
    else:
        with open(agents_md, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n" + AGENTS_MD)
        print(f"✓ added usage note to {agents_md}")
    print("  Restart OpenCode to load the plugin and MCP server.")


def uninstall(dry_run: bool = False) -> None:
    print("Unwiring OpenCode environment…")
    cfg_dir = config_dir()
    plugin_path = os.path.join(cfg_dir, "plugins", "engrim.js")
    agents_md = os.path.join(cfg_dir, "AGENTS.md")
    if dry_run:
        print(f"[dry-run] Would remove {plugin_path}, the engrim MCP entry, and the AGENTS.md note")
        return
    if os.path.exists(plugin_path):
        os.remove(plugin_path)
        print(f"✓ removed {plugin_path}")
    else:
        print(f"✓ plugin already removed ({plugin_path})")
    for name in ("opencode.json", "opencode.jsonc"):
        cfg_path = os.path.join(cfg_dir, name)
        if not os.path.exists(cfg_path):
            continue
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            print(f"• {cfg_path} isn't strict JSON — remove the mcp.engrim entry by hand if present")
            continue
        servers = cfg.get("mcp") if isinstance(cfg, dict) else None
        if isinstance(servers, dict) and "engrim" in servers:
            del servers["engrim"]
            if not servers:
                del cfg["mcp"]
            tmp = cfg_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
                f.write("\n")
            os.replace(tmp, cfg_path)
            print(f"✓ removed MCP entry from {cfg_path}")
    if os.path.exists(agents_md):
        with open(agents_md, encoding="utf-8", errors="replace") as f:
            existing = f.read()
        if AGENTS_MD in existing:
            new = existing.replace("\n" + AGENTS_MD, "").replace(AGENTS_MD, "")
            with open(agents_md, "w", encoding="utf-8") as f:
                f.write(new)
            print(f"✓ removed usage note from {agents_md}")
