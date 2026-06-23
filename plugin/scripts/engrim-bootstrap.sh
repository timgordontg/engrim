#!/usr/bin/env bash
# engrim plugin bootstrap — runs on SessionStart.
#
# Installs the engrim CLI into the plugin's persistent data directory the first
# time the plugin runs (and again whenever the pinned source changes), so the
# four lifecycle hooks have a stable `engrim` binary to call. This makes the
# plugin a true one-click install: no `pip install` step for the user.
#
# It is idempotent and fast on every run after the first. It never fails the
# session — if python3 is missing or the install errors, it exits 0 and the
# hooks no-op until the next session.
set -euo pipefail

DATA="${CLAUDE_PLUGIN_DATA:?CLAUDE_PLUGIN_DATA is required}"
VENV="$DATA/venv"
BIN="$VENV/bin/engrim"
STAMP="$DATA/.installed-source"

# Pin the install source. Override with ENGRIM_PLUGIN_SOURCE to track a fork,
# a branch, a local checkout (-e /path), or a future PyPI release ("engrim==X").
SRC="${ENGRIM_PLUGIN_SOURCE:-git+https://github.com/timgordontg/engrim@v0.7.0}"
# Split into argv so a multi-token override like "-e /path" reaches pip as two args, not one
# (a single quoted "$SRC" would collapse them and fail). The default git URL is one token.
read -r -a SRC_ARGS <<< "$SRC"

# Fast path: already installed at the desired source -> nothing to do.
if [ -x "$BIN" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$SRC" ]; then
  exit 0
fi

mkdir -p "$DATA"
echo "engrim plugin: installing the memory CLI (one-time, ~30-90s)…" >&2

installed=""

# Preferred path: uv handles venv creation + pip in one fast, self-contained tool.
if command -v uv >/dev/null 2>&1; then
  if uv venv "$VENV" >/dev/null 2>&1 \
     && uv pip install --quiet --python "$VENV/bin/python" "${SRC_ARGS[@]}" >/dev/null 2>&1; then
    installed=1
  fi
fi

# Fallback: stdlib venv. On Debian/Ubuntu a venv can come up *without* pip
# (the python3-venv / ensurepip split), so repair pip via ensurepip first.
if [ -z "$installed" ]; then
  PY="$(command -v python3 || true)"
  if [ -z "$PY" ]; then
    echo "engrim plugin: no uv and no python3 on PATH — skipping; hooks will no-op." >&2
    exit 0
  fi
  "$PY" -m venv "$VENV" >/dev/null 2>&1 || "$PY" -m venv --without-pip "$VENV" >/dev/null 2>&1 || true
  VPY="$VENV/bin/python"
  if ! "$VPY" -m pip --version >/dev/null 2>&1; then
    "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 || true
  fi
  "$VPY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  if "$VPY" -m pip --version >/dev/null 2>&1 && "$VPY" -m pip install --quiet "${SRC_ARGS[@]}" >/dev/null 2>&1; then
    installed=1
  fi
fi

if [ -n "$installed" ] && [ -x "$BIN" ]; then
  printf '%s' "$SRC" >"$STAMP"
  # Warm the static embedder once so the first prompt isn't slowed by a cold load.
  "$BIN" stats >/dev/null 2>&1 || true
  echo "engrim plugin: ready — memory will load at session start and on every prompt." >&2
else
  echo "engrim plugin: install failed (need uv, or python3 with pip/ensurepip) — hooks will no-op until the next session." >&2
fi

exit 0
