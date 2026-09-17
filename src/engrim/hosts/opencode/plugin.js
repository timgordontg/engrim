// engrim - cross-agent project memory for OpenCode. Written by `engrim setup --opencode`.
// Re-run `engrim setup --opencode` after upgrading engrim; `engrim uninstall --opencode` removes it.
import { spawn } from "node:child_process"

const ENGRIM = __ENGRIM_BIN__
const HOOK = ["hook", "--agent", "opencode", "--event"]
// The minder runs on every message, so it gets a short leash: a slow spawn costs at most this.
const PROMPT_TIMEOUT_MS = 4000
// How long a model call will wait for the newest prompt's in-flight minder slice before going
// without it (the slice then rides the next request of the same turn instead).
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
// non-zero - callers must not treat that as "engrim saw this" (see flush).
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
  const minder = new Map()  // user messageID -> { sid, slice, pending } (see chat.message)
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
    // System-prompt content is the head of every request, so the provider's prefix cache (vLLM,
    // Anthropic prompt caching) survives only while it is byte-identical across calls. Only the
    // per-session boot pack belongs here. Anything that changes per prompt -- the minder slice, a
    // curate nudge, anything keyed on the message -- must ride the user message instead (see
    // experimental.chat.messages.transform), or every new prompt re-prefills the whole context.
    "experimental.chat.system.transform": async (input, output) => {
      const sid = input.sessionID
      if (!sid) return
      const p = await pack(sid)
      if (p) output.system.push(p + "\n\n" + USAGE)
    },
    "chat.message": async (input, output) => {
      // Fire-and-forget: never hold the request path on a cold Python spawn. The slice is keyed on
      // the user message it was pulled for; messages.transform attaches it to that message on every
      // request from the moment it resolves (waiting at most MINDER_WAIT_MS on the first one).
      const sid = input.sessionID
      const mid = input.messageID || (output.message && output.message.id)
      const prompt = (output.parts || []).filter((p) => p.type === "text" && !p.synthetic && p.text).map((p) => p.text).join("\n")
      if (!sid || !mid || !prompt) return
      const entry = { sid, slice: "", pending: null }
      entry.pending = text("prompt", { cwd: directory, session_id: sid, prompt }, PROMPT_TIMEOUT_MS)
        .then((m) => { entry.slice = m; entry.pending = null })
      minder.set(mid, entry)
    },
    // Runs before every model call on a message list rebuilt from storage each time, so appending
    // here is per-request, not cumulative. The slice goes on the user message it was pulled for --
    // the tail of the prompt on its own turn, and unchanged history on every later one -- so the
    // cached prefix ahead of it stays valid and nothing is ever persisted (flush never sees it).
    "experimental.chat.messages.transform": async (input, output) => {
      const msgs = output.messages || []
      let last = null
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].info && msgs[i].info.role === "user") { last = msgs[i]; break }
      }
      const latest = last && minder.get(last.info.id)
      if (latest && latest.pending) {
        await Promise.race([latest.pending, new Promise((r) => setTimeout(r, MINDER_WAIT_MS))])
      }
      for (const m of msgs) {
        const info = m.info || {}
        const e = info.role === "user" && minder.get(info.id)
        if (!e || !e.slice) continue
        m.parts = m.parts || []
        if (m.parts.some((p) => p.synthetic && p.text === e.slice)) continue
        m.parts.push({ type: "text", text: e.slice, synthetic: true, messageID: info.id, sessionID: e.sid })
      }
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
        if (sid) {
          boot.delete(sid); seen.delete(sid)
          for (const [mid, e] of minder) if (e.sid === sid) minder.delete(mid)
        }
      }
    },
  }
}

export default EngrimPlugin
