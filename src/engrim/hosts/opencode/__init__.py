"""Everything engrim needs for OpenCode, in one place:

  hooks.py    runtime — the boot / prompt / stop handlers `engrim hook --agent opencode` runs
  wiring.py   install time — `engrim setup|uninstall --opencode`: plugin file, MCP entry, AGENTS.md
  plugin.js   the plugin setup writes into ~/.config/opencode/plugins/ (placeholders filled by setup)
  AGENTS.md   the agent-facing usage note, appended to ~/.config/opencode/AGENTS.md and baked into the plugin

Kept empty so importing `.wiring` never pulls in `.hooks` (which imports the CLI).
"""
