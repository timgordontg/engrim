"""Zero-dependency MCP (Model Context Protocol) server for engrim.

Exposes engrim's project memory to any MCP client (Antigravity, Cursor, Claude Code, etc.)
over the MCP stdio transport — newline-delimited JSON-RPC 2.0 — with no dependency beyond
engrim's core. Launched via `engrim serve --mcp` (or `engrim mcp`). Each tool reuses the
exact recall / boot-pack / write logic the CLI uses, so memory behaves identically
however it's reached.

Tools:
  engrim_recall   hybrid keyword+semantic search over the project's records
  engrim_context  the session-boot memory pack (curated, budgeted)
  engrim_add      write a durable record (decision/fact/feedback/state/user/reference)
  engrim_review   check uncaptured decisions before clearing context

stdout carries only JSON-RPC frames (the protocol channel); anything diagnostic must
go to stderr, which MCP clients treat as logging.
"""
from __future__ import annotations

import json
import sys

from engrim import __version__
from engrim.cli import (
    TYPES, GLOBAL_PROJECT, ORIGIN_AGENTS, add_memory, _resolve_project,
    _minder_rows, _recall_rows, _boot_pack, _scopes, _in_clause,
    _capture_floor, _past_floor, _decision_snippet, _looks_like_narration,
    _semantic_decision_snippet, _is_captured, _resolve_embedder,
    _DECISION_CUES, _DECISION_EXEMPLARS, _UNCAPTURED_MAX_SCAN,
)

# Echoed back to the client when it doesn't pin one; clients negotiate their own.
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "engrim_recall",
        "description": ("Read-only query: Search this project's engrim memory for records relevant to a query "
                        "using hybrid BM25 keyword and semantic vector ranking. Use before starting non-trivial work to "
                        "recall prior architectural decisions, technical facts, user feedback, and project state. "
                        "Safe to invoke repeatedly with zero mutations or side effects."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text topic or search keywords."},
                "project": {"type": "string", "default": "auto",
                            "description": "Project identifier tag; 'auto' infers from current working directory repository root."},
                "k": {"type": "integer", "default": 5, "description": "Maximum number of records to return (default 5)."},
                "type": {"type": "string", "enum": list(TYPES),
                         "description": "Optional filter by memory type ('decision', 'fact', 'feedback', 'state', 'reference', 'user')."},
                "tag": {"type": "string",
                        "description": "Optional filter by tag name (e.g. 'auth', 'database')."},
                "include_stale": {"type": "boolean", "default": False,
                                  "description": "Include superseded or retired records alongside active records."},
            },
            "required": ["query"],
        },
        "outputSchema": {
            "type": "object",
            "description": "Ranked memory records matching the query.",
            "properties": {
                "project": {"type": "string"},
                "count": {"type": "integer"},
                "records": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["project", "count", "records"],
        },
    },
    {
        "name": "engrim_context",
        "description": ("Read-only query: Return this project's curated session-boot memory pack (high-signal "
                        "architectural decisions, active constraints, and conventions) to orient an agent at the "
                        "start of a session or after clearing context. Safe to call repeatedly with zero mutations or "
                        "external side effects. Records are prioritized and truncated to strictly fit within the specified "
                        "character budget."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "auto",
                            "description": "Project identifier tag; 'auto' infers from current working directory repository root."},
                "budget": {"type": "integer", "default": 4000,
                           "description": "Maximum character budget for the returned memory pack (default 4000). Prioritizes active decisions and constraints."},
            },
            "required": [],
        },
        "outputSchema": {
            "type": "object",
            "description": "Curated session-boot pack within the character budget.",
            "properties": {
                "project": {"type": "string"},
                "loaded": {"type": "integer"},
                "total_active": {"type": "integer"},
                "chars": {"type": "integer"},
                "records": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["project", "loaded", "total_active", "chars", "records"],
        },
    },
    {
        "name": "engrim_add",
        "description": ("Write operation: Save a durable memory record to local SQLite storage so it persists across "
                        "sessions and agent restarts. Call this whenever a non-trivial architectural choice, durable fact, "
                        "user constraint, or state milestone is established. Appends a new record to the project memory database."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(TYPES),
                         "description": "Category of memory: 'decision' (architectural choices), 'fact' (project truths), 'feedback' (user preferences), 'state' (progress milestones), 'reference' (external links), or 'user' (durable user constraints)."},
                "summary": {"type": "string", "description": "Concise one-line headline summarizing the record."},
                "detail": {"type": "string", "description": "Optional detailed explanation, context, trade-offs, or rationale."},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "Optional list of categorical string tags (e.g. ['auth', 'api'])."},
                "project": {"type": "string", "default": "auto",
                            "description": "Project identifier tag; 'auto' infers from current working directory repository root."},
                "global": {"type": "boolean", "default": False,
                           "description": "If true, stores in the global user-layer (~/.engrim/memory.db) accessible across all projects."},
                "origin_agent": {
                    "type": "string",
                    "enum": list(ORIGIN_AGENTS),
                    "description": "Origin agent identifier for provenance tracking ('antigravity', 'claude-code', 'cursor', 'cli', 'user').",
                },
            },
            "required": ["type", "summary"],
        },
        "outputSchema": {
            "type": "object",
            "description": "Confirmation identifying the stored record.",
            "properties": {
                "id": {"type": "integer"},
                "type": {"type": "string"},
                "project": {"type": "string"},
                "summary": {"type": "string"},
                "origin_agent": {"type": "string"},
            },
            "required": ["id", "type", "project", "summary", "origin_agent"],
        },
    },
    {
        "name": "engrim_review",
        "description": ("Read-only check: Inspect conversation and flight recorder history before clearing context to "
                        "surface recent architectural decisions not yet saved to curated memory. Safe to call with no side "
                        "effects. Returns heuristic verdict (safe_to_clear: true/false/null) and uncaptured candidate records."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "auto",
                            "description": "Project identifier tag; 'auto' infers from current working directory repository root."},
            },
            "required": [],
        },
        "outputSchema": {
            "type": "object",
            "description": "Coverage verdict plus uncaptured decision candidates (safe_to_clear is null when no transcript log exists).",
            "properties": {
                "project": {"type": "string"},
                "total_log_turns": {"type": "integer"},
                "scanned_turns": {"type": "integer"},
                "active_curated": {"type": "integer"},
                "safe_to_clear": {"type": ["boolean", "null"]},
                "uncaptured_count": {"type": "integer"},
                "uncaptured": {"type": "array", "items": {"type": "object"}},
                "message": {"type": "string"},
            },
            "required": ["project", "total_log_turns", "scanned_turns", "active_curated", "safe_to_clear", "uncaptured_count", "uncaptured", "message"],
        },
    },
]


def _tool_recall(conn, args: dict) -> str:
    project = _resolve_project(args.get("project", "auto"))
    query = args.get("query") or ""
    k = max(0, int(args.get("k", 5)))
    type_ = args.get("type")
    tag = args.get("tag")
    include_stale = bool(args.get("include_stale", False))
    if query and not type_ and not include_stale and not tag:
        rows = _minder_rows(conn, project, query, query, k)
    else:
        rows = _recall_rows(conn, project, query, k, type_, include_stale, tag=tag)
    recs = [{kk: vv for kk, vv in dict(r).items() if kk not in ("rank", "_vec")} for r in rows]
    return json.dumps({"project": project, "count": len(recs), "records": recs},
                      default=str, indent=2)


def _tool_context(conn, args: dict) -> str:
    project = _resolve_project(args.get("project", "auto"))
    budget = max(0, int(args.get("budget", 4000)))
    pclause, pparams = _in_clause(_scopes(project), "project")
    rows = conn.execute("SELECT * FROM memories WHERE " + pclause + " AND status = 'active'",
                        pparams).fetchall()
    picked, used = _boot_pack(rows, budget)
    recs = [dict(r) for r, _s in picked]
    return json.dumps({"project": project, "loaded": len(recs), "total_active": len(rows),
                       "chars": used, "records": recs}, default=str, indent=2)


def _tool_add(conn, args: dict, client_agent: str | None = None) -> str:
    type_ = args.get("type")
    summary = (args.get("summary") or "").strip()
    if type_ not in TYPES:
        raise ValueError(f"type must be one of {list(TYPES)}")
    if not summary:
        raise ValueError("summary cannot be empty")
    project = GLOBAL_PROJECT if args.get("global") else _resolve_project(args.get("project", "auto"))
    tags = args.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    origin_agent = args.get("origin_agent") or client_agent or "cursor"
    new_id = add_memory(conn, project=project, type=type_, summary=summary,
                        detail=args.get("detail"), tags=list(tags),
                        origin_agent=origin_agent)
    return json.dumps({
        "id": new_id,
        "type": type_,
        "project": project,
        "summary": summary,
        "origin_agent": origin_agent,
    })


def _tool_review(conn, args: dict) -> str:
    project = _resolve_project(args.get("project", "auto"))
    total_log = conn.execute("SELECT COUNT(*) FROM log WHERE project = ?", (project,)).fetchone()[0]
    curated = conn.execute("SELECT COUNT(*) FROM memories WHERE project = ? AND status = 'active'",
                           (project,)).fetchone()[0]
    if not total_log:
        return json.dumps({
            "project": project,
            "total_log_turns": 0,
            "scanned_turns": 0,
            "active_curated": curated,
            "safe_to_clear": None,
            "uncaptured_count": 0,
            "uncaptured": [],
            "message": "no transcript log yet — insufficient evidence to assess whether it is safe to clear.",
        }, indent=2)

    floor = _capture_floor(conn, project)
    scanned = conn.execute(
        "SELECT ts, content FROM log WHERE project = ? ORDER BY ts DESC LIMIT ?",
        (project, _UNCAPTURED_MAX_SCAN)).fetchall()
    rows = []
    for i, r in enumerate(scanned):
        if _past_floor(r, floor, i, 40):
            break
        rows.append(r)

    fn, _name = _resolve_embedder()
    exemplar_vecs = [fn(e) for e in _DECISION_EXEMPLARS] if fn else []

    seen, candidates = set(), []
    for r in rows:
        snip = None
        if any(cue in (r["content"] or "").lower() for cue in _DECISION_CUES):
            snip = _decision_snippet(r["content"])
            if _looks_like_narration(snip):
                snip = None
        if snip is None and fn:
            snip = _semantic_decision_snippet(r["content"], fn, exemplar_vecs)
        if not snip:
            continue
        key = snip.lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        candidates.append((r["ts"], snip))

    uncaptured = [
        {"timestamp": ts, "summary": snip}
        for ts, snip in candidates
        if not _is_captured(conn, project, snip, fn)
    ]

    return json.dumps({
        "project": project,
        "total_log_turns": total_log,
        "scanned_turns": len(rows),
        "active_curated": curated,
        "safe_to_clear": len(uncaptured) == 0,
        "uncaptured_count": len(uncaptured),
        "uncaptured": uncaptured,
        "message": "recent decisions appear captured — safe to clear." if not uncaptured else f"{len(uncaptured)} uncaptured decision(s) detected — capture before clearing",
    }, indent=2)


_DISPATCH = {
    "engrim_recall": _tool_recall,
    "engrim_context": _tool_context,
    "engrim_add": _tool_add,
    "engrim_review": _tool_review,
}


def serve(conn, inp=None, out=None) -> None:
    """Run the JSON-RPC stdio loop until stdin EOF. `inp`/`out` are injectable for tests."""
    inp = inp or sys.stdin
    out = out or sys.stdout
    detected_client: str | None = None

    def _send(msg: dict) -> None:
        out.write(json.dumps(msg) + "\n")
        out.flush()

    def _ok(rid, result) -> None:
        _send({"jsonrpc": "2.0", "id": rid, "result": result})

    def _err(rid, code, message) -> None:
        _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    while True:
        line = inp.readline()
        if not line:                       # EOF — client closed the transport
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue                       # ignore non-JSON noise; can't reply without an id
        method = msg.get("method")
        rid = msg.get("id")                # absent on notifications

        if method == "initialize":
            client_info = (msg.get("params") or {}).get("clientInfo", {})
            cname = client_info.get("name", "").lower()
            if "antigravity" in cname or "gemini" in cname:
                detected_client = "antigravity"
            elif "claude" in cname:
                detected_client = "claude-code"
            elif "cursor" in cname:
                detected_client = "cursor"

            client_ver = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
            _ok(rid, {
                "protocolVersion": client_ver,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "engrim", "version": __version__},
            })
        elif method == "notifications/initialized":
            continue                       # notification: no response
        elif method == "ping":
            _ok(rid, {})
        elif method == "tools/list":
            _ok(rid, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            fn = _DISPATCH.get(name)
            if fn is None:
                _err(rid, -32602, f"unknown tool: {name}")
                continue
            try:
                if name == "engrim_add":
                    text = _tool_add(conn, params.get("arguments") or {}, client_agent=detected_client)
                else:
                    text = fn(conn, params.get("arguments") or {})
                _ok(rid, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:         # tool errors are reported in-band, never crash the server
                _ok(rid, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        elif rid is not None:
            _err(rid, -32601, f"method not found: {method}")
        # else: unknown notification — ignore
