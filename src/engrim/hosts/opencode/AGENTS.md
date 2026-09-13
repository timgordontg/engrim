## Project memory (engrim) — shared with every agent on this repo

engrim is the durable memory for this project: a local SQLite store that Claude Code, Cursor,
Antigravity, and OpenCode all read and write, so decisions survive `/new`, compaction, and switching
tools. The engrim plugin injects the session-boot pack; the `engrim_*` MCP tools are how you use it:
- `engrim_recall(query)` before non-trivial work.
- `engrim_add(type, summary, detail?, tags?)` at every decision, correction, or durable fact
  (types: decision | fact | feedback | state | user | reference).
- `engrim_review()` before `/new` or `/compact`: save anything durable that is still only in the transcript.
Keep it high-signal — curation and retrieval precision are the point, not volume.
