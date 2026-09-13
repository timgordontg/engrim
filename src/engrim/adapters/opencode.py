#!/usr/bin/env python3
"""OpenCode lifecycle adapter for engrim.

OpenCode has no shell-hook system; its extension point is a JavaScript plugin loaded from
`~/.config/opencode/plugins/*.js`. `engrim setup --opencode` writes that plugin (PLUGIN_JS below),
and the plugin shells out to this adapter at each lifecycle moment, piping a small JSON payload on
stdin and reading plain text back on stdout:

  boot    {cwd, session_id, budget?}   -> the session-boot memory pack (injected into the system
                                          prompt once per session, and again after compaction);
                                          budget caps its size in chars (default 4000)
  prompt  {cwd, session_id, prompt}    -> the minder slice: a few records relevant to THIS prompt
  stop    {cwd, session_id, turns:[…]} -> ingests the session's new user/assistant turns into the
                                          flight-recorder log (idempotent: keyed on message id)

Together these replace OpenCode's own session-summary memory (compaction) with engrim's curated,
cross-agent store: the durable decisions live in ~/.engrim/memory.db, not in the summary, so they
survive `/new`, compaction, and switching to another agent on the same repo.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys

from engrim.cli import (
    DEFAULT_DB,
    _assist_block,
    _now,
    _resolve_project,
    _uncaptured_count,
    build_parser,
    cmd_context,
    connect,
)


# One source of truth for the agent-facing instructions: appended to ~/.config/opencode/AGENTS.md by
# setup, and baked into the plugin's system-prompt block by render_plugin, so the two can't drift.
OPENCODE_AGENTS_BLOCK = """\
## Project memory (engrim) — shared with every agent on this repo

engrim is the durable memory for this project: a local SQLite store that Claude Code, Cursor,
Antigravity, and OpenCode all read and write, so decisions survive `/new`, compaction, and switching
tools. The engrim plugin injects the session-boot pack; the `engrim_*` MCP tools are how you use it:
- `engrim_recall(query)` before non-trivial work.
- `engrim_add(type, summary, detail?, tags?)` at every decision, correction, or durable fact
  (types: decision | fact | feedback | state | user | reference).
- `engrim_review()` before `/new` or `/compact`: save anything durable that is still only in the transcript.
Keep it high-signal — curation and retrieval precision are the point, not volume.
"""


def _read_stdin_payload() -> dict:
    if not sys.stdin.isatty():
        try:
            return json.load(sys.stdin) or {}
        except Exception:
            return {}
    return {}


def _project(payload: dict) -> str:
    """Anchor on the directory OpenCode reports (git-rooted), not the adapter process's cwd."""
    return _resolve_project(None, payload.get("cwd") or None)


def _ts(value) -> str:
    """OpenCode timestamps are epoch milliseconds; the log stores ISO like every other source."""
    if isinstance(value, (int, float)) and value > 0:
        import datetime as _dt
        secs = value / 1000.0 if value > 1e11 else value
        try:
            return _dt.datetime.fromtimestamp(secs).astimezone().isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(value, str) and value:
        return value
    return _now()


def handle_boot(payload: dict | None = None, *, db_path: str | None = None,
                print_output: bool = True) -> str:
    """Session boot: the priority-ordered memory pack, with the agent-facing auto-curate directive."""
    if payload is None:
        payload = _read_stdin_payload()
    project = _project(payload)
    db = db_path or os.environ.get("ENGRIM_DB", DEFAULT_DB)
    try:
        budget = int(payload.get("budget") or 0)
    except (TypeError, ValueError):
        budget = 0
    try:
        conn = connect(db)
        args = build_parser().parse_args(
            ["context", "-p", project] + (["-b", str(budget)] if budget > 0 else []))
        args.agent_directive = True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_context(conn, args)
        conn.close()
        text = buf.getvalue().strip()
    except Exception as e:
        text = f"engrim context error: {e}"
    if print_output:
        print(text)
    return text


def handle_prompt(payload: dict | None = None, *, db_path: str | None = None,
                  print_output: bool = True) -> str:
    """Per-prompt minder: the few records relevant to this message, or nothing (spend no tokens)."""
    if payload is None:
        payload = _read_stdin_payload()
    project = _project(payload)
    prompt = payload.get("prompt") or ""
    db = db_path or os.environ.get("ENGRIM_DB", DEFAULT_DB)
    text = ""
    try:
        conn = connect(db)
        text = _assist_block(conn, project, prompt, k=5, budget=600)
        conn.close()
    except Exception:
        text = ""
    if print_output and text:
        print(text)
    return text


def ingest_turns(conn, project: str, session: str | None, turns: list) -> int:
    """Append OpenCode messages to the log. Idempotent on (project, message id) via the UNIQUE
    msg_uuid index + INSERT OR IGNORE, so the plugin can safely resend a whole session. That only
    holds for turns that carry an id: SQLite treats NULLs as distinct, so id-less turns (never sent
    by the plugin; possible from hand-rolled callers) are appended on every resend."""
    added = 0
    for t in turns or []:
        if not isinstance(t, dict):
            continue
        role = t.get("role")
        if role not in ("user", "assistant"):
            continue
        text = (t.get("text") or "").strip()
        if not text:
            continue
        uuid = t.get("id") or None
        raw = json.dumps(t, ensure_ascii=False)
        cur = conn.execute(
            "INSERT OR IGNORE INTO log(ts,project,session,role,content,raw,msg_uuid) "
            "VALUES(?,?,?,?,?,?,?)",
            (_ts(t.get("ts")), project, t.get("session_id") or session, role, text, raw, uuid))
        added += cur.rowcount
    conn.commit()
    return added


def handle_stop(payload: dict | None = None, *, db_path: str | None = None,
                print_output: bool = True) -> int:
    """Session idle: ingest the new turns; warn on stderr if decisions are piling up uncurated."""
    if payload is None:
        payload = _read_stdin_payload()
    project = _project(payload)
    session = payload.get("session_id")
    db = db_path or os.environ.get("ENGRIM_DB", DEFAULT_DB)
    added = 0
    failed = False
    try:
        conn = connect(db)
        added = ingest_turns(conn, project, session, payload.get("turns") or [])
        unc = _uncaptured_count(conn, project)
        if unc > 0:   # visible on direct CLI use only; the plugin spawns with stderr ignored
            sys.stderr.write(f"[engrim] {unc} recent decision(s) not yet curated in {project}\n")
        conn.close()
    except Exception as e:
        failed = True
        sys.stderr.write(f"[engrim] opencode stop hook error: {e}\n")
    if print_output:
        print(json.dumps({"logged": added, "ok": not failed}))
        if failed:
            # Non-zero so the plugin keeps these message ids pending and resends them next idle.
            sys.exit(1)
    return added


# The plugin OpenCode loads. `__ENGRIM_BIN__` is replaced at setup time with the resolved binary
# path (JSON-escaped), so the plugin doesn't depend on OpenCode's PATH. Plain JS + node:child_process
# — Bun runs it as-is, and there is nothing to build or npm-install.
PLUGIN_JS = r'''// engrim — cross-agent project memory for OpenCode. Written by `engrim setup --opencode`.
// Re-run `engrim setup --opencode` after upgrading engrim; `engrim uninstall --opencode` removes it.
import { spawn } from "node:child_process"

const ENGRIM = __ENGRIM_BIN__
const HOOK = ["hook", "--agent", "opencode", "--event"]
// The minder runs on every message, so it gets a short leash: a slow spawn costs at most this.
const PROMPT_TIMEOUT_MS = 4000
// How long a model call will wait for an in-flight minder slice before going without it.
const MINDER_WAIT_MS = 1500
// Size cap (chars) for the boot pack that rides every model call; override in OpenCode's environment.
const BOOT_BUDGET = Number(process.env.ENGRIM_BOOT_BUDGET) > 0 ? Number(process.env.ENGRIM_BOOT_BUDGET) : 4000

// Node can't spawn a Windows .cmd/.bat shim (pipx/pip put one on PATH) without a shell; route those
// through cmd.exe with the path quoted, everything else runs directly.
const spawnEngrim = (args, opts) => /\.(cmd|bat)$/i.test(ENGRIM)
  ? spawn(process.env.ComSpec || "cmd.exe", ["/d", "/s", "/c", `"${ENGRIM}" ${args.join(" ")}`],
          { ...opts, windowsVerbatimArguments: true })
  : spawn(ENGRIM, args, opts)

// Resolves to { ok, out }: ok is false when the process could not be spawned, timed out, or exited
// non-zero — callers must not treat that as "engrim saw this" (see flush).
function run(event, payload, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let out = ""
    let done = false
    const finish = (ok) => { if (!done) { done = true; resolve({ ok, out: out.trim() }) } }
    let p
    try {
      p = spawnEngrim([...HOOK, event], { stdio: ["pipe", "pipe", "ignore"] })
    } catch { return finish(false) }
    const timer = setTimeout(() => { try { p.kill() } catch {} finish(false) }, timeoutMs)
    p.stdout.on("data", (d) => { out += d })
    p.on("error", () => { clearTimeout(timer); finish(false) })
    p.on("close", (code) => { clearTimeout(timer); finish(code === 0) })
    p.stdin.on("error", () => {})
    p.stdin.end(JSON.stringify(payload))
  })
}
const text = async (event, payload, timeoutMs) => (await run(event, payload, timeoutMs)).out

// Same text `engrim setup --opencode` appends to AGENTS.md (baked in at setup time).
const USAGE = "The block above is this session's boot pack.\n\n" + __ENGRIM_USAGE__

export const EngrimPlugin = async ({ client, directory }) => {
  const boot = new Map()    // sessionID -> boot pack (built once per session, rebuilt after compaction)
  const minder = new Map()  // sessionID -> Promise<slice> for the message being answered (see chat.message)
  const seen = new Map()    // sessionID -> Set of message ids already logged

  const pack = async (sid) => {
    if (!boot.has(sid)) boot.set(sid, await text("boot", { cwd: directory, session_id: sid, budget: BOOT_BUDGET }))
    return boot.get(sid)
  }

  const partText = (parts) => parts
    .filter((p) => p.type === "text" && !p.synthetic && p.text)
    .map((p) => p.text)
    .concat(parts
      .filter((p) => p.type === "tool" && p.tool)
      .map((p) => `[tool] ${p.tool}${p.state && p.state.title ? ": " + p.state.title : ""}`))
    .join("\n")

  const flush = async (sid) => {
    let res
    try { res = await client.session.messages({ path: { id: sid }, query: { directory } }) } catch { return }
    const msgs = (res && res.data) || []
    const logged = seen.get(sid) || new Set()
    const turns = []
    const pending = []   // ids in this batch; only promoted to `logged` once engrim has ingested them
    for (const m of msgs) {
      const info = m.info || {}
      if (!info.id || logged.has(info.id)) continue
      if (info.role !== "user" && info.role !== "assistant") continue
      // Compaction writes its summary as an assistant message; the raw turns it summarises are
      // already in the log, so logging it too would double-count every decision it restates.
      if (info.summary) { logged.add(info.id); continue }
      if (info.role === "assistant" && !(info.time && info.time.completed)) continue
      const body = partText(m.parts || [])
      if (!body) { logged.add(info.id); continue }
      pending.push(info.id)
      turns.push({ id: info.id, role: info.role, text: body, ts: info.time && info.time.created, session_id: sid })
    }
    if (turns.length) {
      // Commit the ids only after a successful ingest: if engrim couldn't be spawned or timed out,
      // the next idle retries the same turns (the log is idempotent on message id, so no dupes).
      const r = await run("stop", { cwd: directory, session_id: sid, turns })
      if (r.ok) for (const id of pending) logged.add(id)
    }
    seen.set(sid, logged)
  }

  return {
    "experimental.chat.system.transform": async (input, output) => {
      const sid = input.sessionID
      if (!sid) return
      const p = await pack(sid)
      if (p) output.system.push(p + "\n\n" + USAGE)
      const pending = minder.get(sid)
      if (pending) {
        const m = await Promise.race([pending, new Promise((r) => setTimeout(() => r(""), MINDER_WAIT_MS))])
        if (m) output.system.push(m)
      }
    },
    "chat.message": async (input, output) => {
      // Fire-and-forget: never hold the request path on a cold Python spawn. The next
      // system.transform picks the slice up if it is ready within MINDER_WAIT_MS, otherwise the
      // message goes out with the boot pack alone and the slice is dropped.
      const sid = input.sessionID
      const prompt = (output.parts || []).filter((p) => p.type === "text" && p.text).map((p) => p.text).join("\n")
      if (prompt) minder.set(sid, text("prompt", { cwd: directory, session_id: sid, prompt }, PROMPT_TIMEOUT_MS))
      else minder.delete(sid)
    },
    "experimental.session.compacting": async (input, output) => {
      await flush(input.sessionID)
      boot.delete(input.sessionID)
      const p = await pack(input.sessionID)
      output.context.push(
        "Durable project memory is kept OUTSIDE this transcript in engrim (the engrim_* MCP tools). " +
        "In the summary, list under 'Uncaptured decisions' every decision, constraint, or finding from this " +
        "session that is not already in the engrim pack below, so the next turn can engrim_add them." +
        (p ? "\n\n" + p : ""))
    },
    event: async ({ event }) => {
      const props = event.properties || {}
      if (event.type === "session.idle" && props.sessionID) await flush(props.sessionID)
      if (event.type === "session.compacted" && props.sessionID) boot.delete(props.sessionID)
      if (event.type === "session.deleted") {
        const sid = props.info && props.info.id
        if (sid) { boot.delete(sid); minder.delete(sid); seen.delete(sid) }
      }
    },
  }
}

export default EngrimPlugin
'''


def render_plugin(engrim_bin: str) -> str:
    """Bake the resolved binary into the plugin source (JSON-quoted: safe for Windows paths/spaces)."""
    return (PLUGIN_JS
            .replace("__ENGRIM_BIN__", json.dumps(engrim_bin.strip('"').replace("\\", "/")))
            .replace("__ENGRIM_USAGE__", json.dumps(OPENCODE_AGENTS_BLOCK.rstrip(), ensure_ascii=False)))


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m engrim.adapters.opencode [boot|prompt|stop]")
    mode = sys.argv[1].lower()
    if mode == "boot":
        handle_boot()
    elif mode == "prompt":
        handle_prompt()
    elif mode == "stop":
        handle_stop()
    else:
        sys.exit(f"Unknown mode: {mode}")


if __name__ == "__main__":
    main()
