# engrim — Claude Code plugin

Local-first memory for Claude Code, packaged as a one-click plugin — **no API
key, nothing leaves your machine.** It wires a four-hook loop into your sessions
so your project's decisions and the *why* behind them reload automatically, and
it **self-installs its engine (the `engrim` CLI) on first run**, so there's no
separate `pip install` step.

> Clearing your agent's context without engrim feels like closing a doc
> without hitting save. engrim is the save button: externalize your decisions
> as you go, then `/clear` freely and watch them reload intact.

## Install

From this repo as a marketplace:

```bash
/plugin marketplace add timgordontg/engrim
/plugin install engrim@engrim
```

On the first session after install, the plugin builds a small private
environment and installs the engrim CLI into it (one-time, ~30–90s). After that
it's instant. Requires `python3` with `pip`/`ensurepip`, or [`uv`](https://github.com/astral-sh/uv)
on your PATH; if neither is present the hooks no-op rather than erroring.

## What it wires

| Event | Hook | What it does |
|---|---|---|
| `SessionStart` | bootstrap + `engrim hook` | self-install on first run, then inject the budget-capped boot pack |
| `UserPromptSubmit` | `engrim assist` | the minder — inject only the few records relevant to your prompt |
| `Stop` | `engrim log --hook` | tail the transcript into an append-only log (never enters context) |
| `SessionEnd` | `engrim sync --claude` | final seed-gated mirror of file-memory |

## Configuration

The plugin respects all of engrim's [environment variables](https://github.com/timgordontg/engrim#configuration)
(`ENGRIM_DB`, `ENGRIM_PROJECT`, `ENGRIM_EMBED`, `ENGRIM_NO_GLOBAL`, …). Two extras:

| Var | Default | Purpose |
|---|---|---|
| `ENGRIM_PLUGIN_SOURCE` | `engrim==0.7.2` | What the bootstrap installs (the published PyPI wheel). Point it at a fork, a branch, `-e /local/path`, or a git ref. |

The full project store, the status line, and all commands work exactly as in the
[main README](https://github.com/timgordontg/engrim). This plugin is just the
auto-load adapter — the brain is the same local SQLite store.

MIT © 2026 Tim Gordon. Not affiliated with Anthropic.
