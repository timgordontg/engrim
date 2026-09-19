## Project memory (engrim) - shared with every agent on this repo

engrim is the durable memory for this project: a local SQLite store that Copilot CLI, Claude Code, Cursor, Antigravity, Codex, and OpenCode all read and write, so decisions survive `/clear`, compaction, and switching tools. The engrim hooks inject the session-boot pack and record the conversation; the `engrim_*` MCP tools are how you use it:

- `engrim_recall(query)` before non-trivial work.
- `engrim_add(type, summary, detail?, tags?)` at every decision, correction, or durable fact (types: decision | fact | feedback | state | user | reference).
- `engrim_review()` before `/clear` or `/compact`: save anything durable that is still only in the transcript.

The flight recorder includes prompts and top-level assistant messages. Copilot may flush a final event after the stop hook begins, so Engrim also recovers delayed tails at the next session start. If assistant capture reports an incompatible Copilot event schema, tell the user to run `engrim doctor`; durable decisions should still be recorded with `engrim_add`.

Keep it high-signal - curation and retrieval precision are the point, not volume.
