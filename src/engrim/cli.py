"""
engrim — project-scoped, cross-session memory for AI coding agents.

Write cheaply, recall a relevant slice on demand. Context comes from retrieval, not from
stuffing everything into the window. One SQLite file, project-tagged, hybrid keyword + semantic
recall. Works with Claude Code via a SessionStart hook (unofficial; not
affiliated with or endorsed by Anthropic).

CLI:
  add       insert a memory          engrim add -t decision -s "..." [-d "..."] [--tags a,b] [--global]
  recall    ranked relevant slice    engrim recall -q "rl reward" [-k 8] [--detail] [--tag auth]
  context   session-boot pack        engrim context [-b 4000]
  hook      SessionStart JSON         engrim hook            (used by the hook; self-scopes to cwd)
  setup     wire the hook + notes     engrim setup           (white-glove one-shot install)
  list      recent for a project     engrim list [-k 20] [--tag auth]
  supersede mark status by id        engrim supersede --id 12 --status superseded
  retire    close resume-pointers    engrim retire [-p P | --all] [--dry-run] [--json]
  project   tag + counts             engrim project [-p P | --global | --all] [--json]
  projects  list tags + counts       engrim projects [--json]   (= project --all)
  stats     row/health summary       engrim stats
  prune     purge logs + vacuum      engrim prune [--keep-days <days> | --all | --vacuum]
  backup    consistent copy          engrim backup COPY.db [--force] [--json]   (safe while agents hold the store)
  review    coverage check           engrim review [--strict]

Every record is tagged by `project` (a folder path). `--project auto` (the default) derives it
from the current directory, so one store serves many projects cleanly.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys

try:
    from engrim import __version__
except ImportError:
    __version__ = "1.4.2"

DEFAULT_DB = os.path.expanduser("~/.engrim/memory.db")
TYPES = ("decision", "fact", "feedback", "state", "reference", "user")
STATUSES = ("active", "superseded", "done")
ORIGIN_AGENTS = ("antigravity", "claude-code", "cursor", "cli", "user")
# priority for the session-boot pack: how-to-work-with-user first, then state, then the rest
_PRIO = {"user": 0, "feedback": 1, "state": 2, "decision": 3, "fact": 4, "reference": 5}


def _norm_agent(agent: str | None) -> str | None:
    if not agent:
        return None
    a = agent.strip().lower()
    if a in ("agy", "antigravity", "gemini"):
        return "antigravity"
    if a in ("claude", "claude_code", "claude-code"):
        return "claude-code"
    if a == "cursor":
        return "cursor"
    if a == "cli":
        return "cli"
    if a == "user":
        return "user"
    return a


def _agent_display(agent: str | None) -> str:
    if not agent:
        return ""
    m = {
        "antigravity": "Antigravity",
        "claude-code": "Claude Code",
        "cursor": "Cursor",
        "cli": "CLI",
        "user": "User",
    }
    return m.get(agent.lower(), agent.title())


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _is_strict(a=None) -> bool:
    """Return True if strict / gate mode is active via CLI flag or environment."""
    if a and (getattr(a, "strict", False) or getattr(a, "gate", False)):
        return True
    env_strict = os.environ.get("ENGRIM_STRICT", "").strip().lower()
    env_gate = os.environ.get("ENGRIM_GATE", "").strip().lower()
    return env_strict in ("1", "true", "yes", "on") or env_gate in ("1", "true", "yes", "on")


# A project root is a dir holding a VCS dir OR a `.claude` project dir. `.claude` matters because
# not every project is a git repo (e.g. a data/ops workspace) — without a marker the tag would fall
# back to the raw cwd, so launching from a subdir silently files records under a SIBLING scope the
# status line and boot pack never read (records land, counter never moves: "is it even logging?").
_PROJECT_MARKERS = (".git", ".hg", ".svn", ".claude")


def _git_root(start: str):
    """Walk up from `start` to the nearest project root (a dir with a _PROJECT_MARKER). None if none.

    The walk STOPS AT $HOME and never climbs past it. `~/.claude` (and a stray `~/.git`) exist for
    almost everyone, so anchoring AT home would collapse every non-repo project under it into ONE
    bucket — worse than the no-marker fallback. Refusing to climb PAST home is what makes the tag
    deterministic: it used to keep walking, so the answer depended on whatever happened to sit in
    /home or C:\\Users on that particular machine, and a dev box with its own marker up there tagged
    differently than a bare CI runner. A real repo above home no longer resolves and falls back to
    the raw cwd tag — coarse, but never wrong, and that case needs a marker at /home or C:\\Users."""
    # normcase, or the home guard fails open on Windows: `C:\Users\Tim` vs `c:/users/tim` are the same
    # directory spelled two ways, and a missed match here is the exact collapse this guard prevents —
    # every loose project under home in ONE bucket. No-op on POSIX, where case is significant.
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    cur = os.path.abspath(start)
    while True:
        if os.path.normcase(os.path.realpath(cur)) == home:
            return None                 # home is not a root, and nothing above it is either
        if any(os.path.exists(os.path.join(cur, m)) for m in _PROJECT_MARKERS):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _resolve_project(p, cwd=None):
    """Project tag precedence: explicit -p  >  $ENGRIM_PROJECT  >  git root of cwd  >  cwd.

    $ENGRIM_PROJECT gives a stable tag across machines/containers (host path != container path);
    project-root (a .git/.hg/.svn repo OR a .claude dir) makes the tag the same no matter which
    subdirectory you launch from — non-repo workspaces anchor on .claude. `cwd` overrides the
    base directory (default: the process's): a hook should resolve against the workspace Claude Code
    reports, not whatever cwd the hook process happens to inherit — see cmd_log's --hook path.
    """
    if p and p != "auto":
        return p
    env = os.environ.get("ENGRIM_PROJECT") or os.environ.get("CLAUDE_PROJECT_TAG")
    if env:
        return env
    base = cwd or os.getcwd()
    return _norm_project(_git_root(base) or base)


def _norm_project(path: str) -> str:
    """Stabilise a path-derived project tag so one project can't split into two memory buckets.

    Windows-only, and deliberately so: the tag is a dict key, and there the SAME directory arrives
    spelled differently depending on who reports it — `C:\\p` from os.getcwd() vs `c:/p` from a hook
    payload written by Claude Code. Separators and drive-letter case are the two ways that happens;
    both normalise here, the rest of the path keeps its casing so output still reads naturally.
    POSIX paths are returned untouched — they're already the one true spelling."""
    if os.name != "nt" or not path:
        return path
    import ntpath          # == os.path on Windows; named explicitly so this is testable anywhere
    drive, rest = ntpath.splitdrive(ntpath.normpath(path))
    return drive.upper() + rest


def _payload_project(payload, explicit=None):
    """Resolve the project for a hook / status line from its stdin payload, preferring the MOST STABLE
    directory Claude Code offers so a session stays pinned to ONE project even if its cwd drifts during
    the session (e.g. a tool subprocess chdir'd elsewhere):

        -p  >  $ENGRIM_PROJECT  >  workspace.project_dir (the launch root — stable)  >
        workspace.current_dir  >  cwd  >  the hook process's own cwd

    project_dir is the directory Claude Code was started in and does not move; current_dir/cwd can.
    Anchoring to it keeps the log's bucket, its byte cursor, and the status line's count all in
    agreement for the whole session. Every entry point (status line, Stop hook, minder) routes through
    here so they can never disagree on which project this session is."""
    if explicit and explicit != "auto":
        return explicit
    env = os.environ.get("ENGRIM_PROJECT") or os.environ.get("CLAUDE_PROJECT_TAG")
    if env:
        return env
    ws = (payload or {}).get("workspace") or {}
    base = ws.get("project_dir") or ws.get("current_dir") or (payload or {}).get("cwd")
    return _resolve_project(None, base)   # base may be None -> falls back to the process cwd; git-root applied


def _csv(val):
    return [x.strip() for x in val.split(",") if x.strip()] if val else []


# The global user-layer: a reserved project tag whose records ride along with EVERY project's reads
# (who you are, how you like to work — truths that aren't about any one repo). It's an additive layer,
# not a new mode: a store with no global records behaves exactly as before, and ENGRIM_NO_GLOBAL turns
# it off entirely. Write to it with `engrim add --global`; every read (boot pack, minder, recall) then
# co-loads it alongside the current project. The sentinel is never produced by _resolve_project (which
# only ever returns an absolute path or an explicit/env tag), so it can't collide with a real project.
GLOBAL_PROJECT = "__global__"


def _global_on():
    return os.environ.get("ENGRIM_NO_GLOBAL", "").strip().lower() not in ("1", "on", "true", "yes")


def _scopes(project):
    """Project tags to READ for `project`: the project itself, plus the global user-layer. Collapses to
    just [project] when reading the global layer itself or when ENGRIM_NO_GLOBAL is set — so the feature
    is fully opt-out and a store with no global records is indistinguishable from before."""
    if project == GLOBAL_PROJECT or not _global_on():
        return [project]
    return [project, GLOBAL_PROJECT]


def _in_clause(scopes, col):
    """Build ('<col> IN (?,?)', [tags...]) to scope a read across the project + global layers."""
    return f"{col} IN ({','.join('?' * len(scopes))})", list(scopes)


def connect(db_path: str) -> sqlite3.Connection:
    d = os.path.dirname(db_path) or "."  # bare "memory.db" -> cwd
    os.makedirs(d, exist_ok=True)
    if db_path == DEFAULT_DB:  # lock the private default dir; leave custom/shared paths alone
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")  # concurrent/Docker writers wait, don't error
    _init(conn)
    # Your memory can be private: keep the store owner-only (best-effort; no-op on Windows).
    for _ext in ("", "-wal", "-shm"):
        try:
            os.chmod(db_path + _ext, 0o600)
        except OSError:
            pass
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, project TEXT NOT NULL,
            type TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT,
            status TEXT NOT NULL DEFAULT 'active', tags TEXT, links TEXT, source TEXT,
            origin_agent TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_mem_project ON memories(project, status, ts);
        CREATE TABLE IF NOT EXISTS engrim_meta (
            project TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
            PRIMARY KEY (project, key)
        );
        CREATE TABLE IF NOT EXISTS log (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, project TEXT NOT NULL,
            session TEXT, role TEXT NOT NULL, content TEXT, raw TEXT, msg_uuid TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_log_project ON log(project, ts);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_log_uuid ON log(project, msg_uuid);
        CREATE TABLE IF NOT EXISTS embedding (
            memory_id INTEGER PRIMARY KEY REFERENCES memories(id),
            model TEXT, dim INTEGER, vec BLOB
        );
        """
    )
    # Migrate older `memories` tables without origin_agent column
    cols_mem = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    if "origin_agent" not in cols_mem:
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN origin_agent TEXT")
        except sqlite3.OperationalError:
            pass
    # Migrate older `log` tables (pre-raw column / global-unique msg_uuid) without losing rows.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(log)")}
    if "raw" not in cols:
        try:
            conn.execute("ALTER TABLE log ADD COLUMN raw TEXT")
        except sqlite3.OperationalError:
            pass
    # FTS5 ships with most Python builds, but not all. If it's missing, recall transparently
    # falls back to LIKE — the tool still works, just without bm25 ranking.
    try:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                summary, detail, tags,
                content='memories', content_rowid='id', tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, summary, detail, tags)
                VALUES (new.id, new.summary, new.detail, new.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, summary, detail, tags)
                VALUES ('delete', old.id, old.summary, old.detail, old.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, summary, detail, tags)
                VALUES ('delete', old.id, old.summary, old.detail, old.tags);
                INSERT INTO memories_fts(rowid, summary, detail, tags)
                VALUES (new.id, new.summary, new.detail, new.tags);
            END;
            """
        )
    except sqlite3.OperationalError:
        pass  # no FTS5 in this SQLite build — LIKE fallback handles recall
    conn.commit()


def _fts_available(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone() is not None


# Seeding markers. The md->store mirror is a ONE-TIME, install-moment "context builder": it pulls a
# project's pre-install history (file-memory) into the store once, then steps aside. After that the
# store is canonical — sessions read from it and `engrim add` logs to it; the historical md is never
# re-applied over the accumulating db (so a /clear of the chat still leaves the db carrying memory).
SEED_KEY = "md_seeded"


def _meta_get(conn, project, key):
    r = conn.execute(
        "SELECT value FROM engrim_meta WHERE project=? AND key=?", (project, key)).fetchone()
    return r[0] if r else None


def _meta_set(conn, project, key, value):
    # INSERT OR REPLACE works on every SQLite (no 3.24+ upsert dependency); the PK is (project,key).
    conn.execute(
        "INSERT OR REPLACE INTO engrim_meta(project,key,value) VALUES(?,?,?)",
        (project, key, value))
    conn.commit()


def add_memory(conn, *, project, type, summary, detail=None, status="active",
               tags=None, links=None, source=None, origin_agent=None) -> int:
    """Insert one memory record and best-effort auto-embed it; return the new id.

    The shared write core behind both the `add` CLI command and the MCP server, so a
    record created either way is identical and immediately searchable by meaning.
    `tags`/`links` are lists (already normalized by the caller)."""
    origin_agent = _norm_agent(origin_agent or os.environ.get("ENGRIM_ORIGIN_AGENT"))
    cur = conn.execute(
        "INSERT INTO memories(ts,project,type,summary,detail,status,tags,links,source,origin_agent) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (_now(), project, type, summary, detail, status,
         json.dumps(tags or []), json.dumps(links or []), source, origin_agent),
    )
    conn.commit()
    # Auto-embed so the record is searchable by meaning immediately — no manual `engrim embed` step.
    # Best-effort: a missing/slow/broken backend must never fail or slow down a write.
    fn, name = _resolve_embedder()
    if fn:
        try:
            _embed_row(conn, cur.lastrowid, summary, detail, fn, name)
            conn.commit()
        except Exception:
            pass
    return cur.lastrowid


def cmd_add(conn, a) -> None:
    if a.type not in TYPES:
        sys.exit(f"--type must be one of {TYPES}")
    if not (a.summary or "").strip():
        sys.exit("--summary cannot be empty")
    # --global writes to the user-layer that loads in every project; otherwise scope to the cwd's project.
    project = GLOBAL_PROJECT if getattr(a, "globl", False) else _resolve_project(a.project)
    origin_agent = getattr(a, "origin_agent", None) or os.environ.get("ENGRIM_ORIGIN_AGENT") or "cli"
    new_id = add_memory(conn, project=project, type=a.type, summary=a.summary,
                        detail=a.detail, status=a.status,
                        tags=_csv(a.tags), links=_csv(a.links), source=a.source,
                        origin_agent=origin_agent)
    shown = "global · loads in every project" if project == GLOBAL_PROJECT else project
    print(f"+ #{new_id} [{a.type}] {shown}\n  {a.summary}")


def _row_line(r, detail: bool) -> str:
    tags = ", ".join(json.loads(r["tags"] or "[]"))
    via = f" (via {_agent_display(r['origin_agent'])})" if ("origin_agent" in r.keys() and r["origin_agent"]) else ""
    head = f"#{r['id']} [{r['type']}/{r['status']}]{via} {r['ts'][:16]}  {r['summary']}"
    if tags:
        head += f"   ({tags})"
    if detail and r["detail"]:
        head += "\n    " + r["detail"].replace("\n", "\n    ")
    return head


def _tag_filter_clause(col: str, tag: str | list[str] | None) -> tuple[str, list[str]]:
    if not tag:
        return "", []
    tags = _csv(tag) if isinstance(tag, str) else list(tag)
    if not tags:
        return "", []
    placeholders = ",".join(["?"] * len(tags))
    clause = (f"AND (CASE WHEN json_valid({col}) "
              f"THEN EXISTS (SELECT 1 FROM json_each({col}) WHERE LOWER(value) IN ({placeholders})) "
              f"ELSE 0 END) ")
    params = [t.lower() for t in tags]
    return clause, params


def _recall_rows(conn, project, query, k, type_=None, include_stale=False, tag=None):
    """Ranked relevant records for `query` (bm25 if FTS5 is present, else LIKE-by-recency).

    Query is tokenized to bare words first — that's what stops a stray "C++"/"useState()"/quote from
    hitting an FTS5 syntax error. No query -> most-recent records. Shared by `recall` and the minder."""
    # Reads span the project + the global user-layer (additive; collapses to project-only when global
    # is empty/off), so user-level truths surface in recall and the minder for every project.
    terms = re.findall(r"\w+", query, flags=re.UNICODE) if query else []
    if query and terms and _fts_available(conn):
        match = " OR ".join('"%s"' % t for t in terms)  # quoted terms = no operator injection
        pclause, pparams = _in_clause(_scopes(project), "m.project")
        sql = ("SELECT m.*, bm25(memories_fts) AS rank FROM memories_fts "
               "JOIN memories m ON m.id = memories_fts.rowid "
               "WHERE memories_fts MATCH ? AND " + pclause + " ")
        params = [match] + pparams
        if type_:
            sql += "AND m.type = ? "
            params.append(type_)
        if not include_stale:
            sql += "AND m.status = 'active' "
        tclause, tparams = _tag_filter_clause("m.tags", tag)
        sql += tclause
        params.extend(tparams)
        sql += "ORDER BY rank LIMIT ?"
        params.append(k)
        return conn.execute(sql, params).fetchall()
    if query and terms:
        # LIKE fallback: no FTS5 in this SQLite build. Still works, ranked by recency.
        clause = " OR ".join(["(summary LIKE ? OR detail LIKE ? OR tags LIKE ?)"] * len(terms))
        pclause, pparams = _in_clause(_scopes(project), "project")
        params = list(pparams)
        for t in terms:
            params += ["%" + t + "%"] * 3
        sql = "SELECT * FROM memories WHERE " + pclause + " AND (" + clause + ") "
        if type_:
            sql += "AND type = ? "
            params.append(type_)
        if not include_stale:
            sql += "AND status = 'active' "
        tclause, tparams = _tag_filter_clause("tags", tag)
        sql += tclause
        params.extend(tparams)
        sql += "ORDER BY ts DESC LIMIT ?"
        params.append(k)
        return conn.execute(sql, params).fetchall()
    if query:
        return []  # query was all punctuation -> no tokens -> graceful empty
    pclause, pparams = _in_clause(_scopes(project), "project")
    sql = "SELECT * FROM memories WHERE " + pclause + " "
    params = list(pparams)
    if type_:
        sql += "AND type = ? "
        params.append(type_)
    if not include_stale:
        sql += "AND status = 'active' "
    tclause, tparams = _tag_filter_clause("tags", tag)
    sql += tclause
    params.extend(tparams)
    sql += "ORDER BY ts DESC LIMIT ?"
    params.append(k)
    return conn.execute(sql, params).fetchall()


def _log_search(conn, project, query, k):
    """Search the transcript log — the 128 MB of history that `recall` never touches.

    Deliberately OPT-IN (`recall --log`). The two-tier split (#98) is that the log never AUTO-loads
    into context; asking for it explicitly doesn't violate that, it's the payoff for having kept it.
    Plain scan, no FTS table: log.content is ~5 MB against 128 MB of raw, and a full scan measures at
    ~21 ms over 44k rows — not worth an index, a migration, or the write amplification."""
    terms = [t for t in _content_terms(query or "")] or [(query or "").strip().lower()]
    terms = [t for t in terms if t]
    if not terms:
        return []
    like = " AND ".join(["LOWER(content) LIKE ?"] * len(terms))
    rows = conn.execute(
        "SELECT ts, role, content FROM log WHERE project = ? AND content IS NOT NULL AND content != '' "
        "AND " + like + " ORDER BY ts DESC LIMIT ?",
        (project, *[f"%{t}%" for t in terms], k)).fetchall()
    return rows


def _log_hit_line(row, query):
    """One result line: the matching slice of the turn, not the whole turn."""
    content = " ".join((row["content"] or "").split())
    terms = [t for t in _content_terms(query or "") if t]
    low = content.lower()
    at = min([low.find(t) for t in terms if low.find(t) >= 0] or [0])
    start = max(0, at - 60)
    snip = ("…" if start else "") + content[start:start + 180] + ("…" if len(content) > start + 180 else "")
    return f"  · [{row['ts'][:16]}] {row['role']:<9} {snip}"


def cmd_recall(conn, a) -> None:
    project = _resolve_project(a.project)
    tag = getattr(a, "tag", None)
    # Hybrid (bm25 + semantic) for a real free-text query — same fusion the minder uses, so a manual
    # `recall` understands meaning too. It degrades to pure lexical when semantic is off, so behavior
    # is unchanged without a backend. Type/stale/tag filters use the precise lexical path (the fusion path
    # is active-only and unfiltered by design).
    if a.query and not a.type and not a.include_stale and not tag:
        rows = _minder_rows(conn, project, a.query, a.query, a.k)
    else:
        rows = _recall_rows(conn, project, a.query, a.k, a.type, a.include_stale, tag=tag)

    if a.json:
        clean = [{k: v for k, v in dict(r).items() if k not in ("rank", "_vec")} for r in rows]
        print(json.dumps(clean, default=str))
        return
    if not rows and not getattr(a, "log", False):
        print(f"(no memories for project={project}"
              + (f" matching {a.query!r}" if a.query else "") + ")")
        return
    if rows:
        print(f"== {len(rows)} memr(s) · project={project}"
              + (f" · q={a.query!r}" if a.query else "") + " ==")
        for r in rows:
            print(_row_line(r, a.detail))
    if getattr(a, "log", False):
        hits = _log_search(conn, project, a.query, a.k)
        print(f"\n== log · {len(hits)} turn(s)"
              + (f" matching {a.query!r}" if a.query else "") + " ==")
        if not hits:
            print("  (nothing in the transcript log — it holds prose plus one line per "
                  "state-changing action)")
        for r in hits:
            print(_log_hit_line(r, a.query))


def cmd_list(conn, a) -> None:
    a.query = None
    cmd_recall(conn, a)


_STOPWORDS = frozenset(
    "the a an and or but if then this that these those is are was were be been being do does did "
    "have has had will would can could should may might must of to in on at by for with from as it "
    "its i you we they he she me my your our their what when where why how which who whom not no yes "
    "ok okay sure thanks please just like get got go now here there about into out up down so very "
    "really again still also too more most some any all let lets make made want need know think see "
    "say said tell told give me him her them us".split())


def _content_terms(text):
    """Substantive query terms from a prompt: lowercase word tokens, minus stopwords and <3-char
    noise, de-duped. This is the efficiency gate — trivial prompts ('ok', 'do it') yield too few
    terms and the minder stays silent rather than inject noise."""
    seen, out = set(), []
    for t in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", text.lower()):
        if t in _STOPWORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


# --------------------------------------------------------------------------- semantic tier (default)
# Embeddings are computed at WRITE time (`add` auto-embeds; `embed` backfills) and stored; the minder
# does cheap cosine at READ time, fused with bm25 (reciprocal-rank fusion). ON by default — model2vec
# (a fast *static* embedder: ~tens of ms to load, no per-query neural pass) ships as a core dependency.
# Set ENGRIM_EMBED=off to force pure lexical (zero third-party deps); a missing/broken backend also
# degrades to lexical, so retrieval never hard-fails. [ENGRIM_EMBED_MODEL=<hf-id> overrides the model.]
_EMBEDDER = None              # process cache: (encode_fn|None, model_name|None)
_EMBEDDER_OVERRIDE = None     # tests/embedders inject (encode_fn, model_name)


def _resolve_embedder():
    """Return (encode_fn, model_name) or (None, None). encode_fn maps str -> list[float].
    Semantic recall is ON by default — model2vec ships as a core dependency, so the minder ranks by
    meaning out of the box. Set ENGRIM_EMBED=off (or 0/none/false/lexical) to force pure-lexical.
    Any load failure degrades to (None, None) — a missing/broken model is never a hard error."""
    global _EMBEDDER
    if _EMBEDDER_OVERRIDE is not None:
        return _EMBEDDER_OVERRIDE
    if _EMBEDDER is not None:
        return _EMBEDDER
    if os.environ.get("ENGRIM_EMBED", "").strip().lower() in ("0", "off", "none", "false", "no", "lexical"):
        _EMBEDDER = (None, None)
        return _EMBEDDER
    try:
        # huggingface_hub's snapshot_download prints a "Fetching N files" tqdm bar to STDERR on
        # first model fetch. Any caller capturing 2>&1 (the SessionStart hook, agent tooling) gets
        # that noise injected into context. Disable HF progress bars before model2vec imports the
        # hub. setdefault so an explicit override from the environment still wins.
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        from model2vec import StaticModel
        model_id = os.environ.get("ENGRIM_EMBED_MODEL", "minishlab/potion-base-8M")
        model = StaticModel.from_pretrained(model_id)
        _EMBEDDER = (lambda t: [float(x) for x in model.encode([t or ""])[0]], "model2vec:" + model_id)
    except Exception:
        _EMBEDDER = (None, None)
    return _EMBEDDER


def _vec_blob(v):
    import array
    return array.array("f", v).tobytes()


def _blob_vec(b):
    import array
    a = array.array("f")
    a.frombytes(b)
    return a


def _embed_row(conn, mid, summary, detail, fn, name):
    """Compute + store one record's embedding — the semantic write step, shared by `add` (auto) and
    `embed` (backfill). Caller passes a resolved (fn, name) so this stays a tight loop. INSERT OR
    REPLACE keeps it idempotent per (record, model)."""
    vec = fn((summary or "") + "\n" + (detail or ""))
    conn.execute("INSERT OR REPLACE INTO embedding(memory_id, model, dim, vec) VALUES(?,?,?,?)",
                 (mid, name, len(vec), _vec_blob(vec)))


def _cosine(a, b):
    import math
    s = da = db = 0.0
    for x, y in zip(a, b):
        s += x * y
        da += x * x
        db += y * y
    return s / math.sqrt(da * db) if da and db else 0.0


# Minimum cosine for a semantic match to count. Below this the static embedder is at noise level
# (empirically with potion-8M: unrelated text ~0.00-0.12; strong matches ~0.50-0.73; short natural-
# language paraphrases of a record — e.g. "what database did we pick" against a SQLite decision —
# land ~0.30-0.46). The floor sits at 0.30 to admit those genuine paraphrases while staying well clear
# of the <0.12 noise band. Lowering it only ever adds matches above 0.30; nothing that cleared the old
# value drops out. Recall and the minder use this; the stricter capture-check in `review`
# (_CAPTURED_SIM) is separate, because a false "safe to clear" costs more than a missed hit.
_SEM_FLOOR = 0.30


def _semantic_rows(conn, project, query, k):
    """Top-k active records whose stored-embedding cosine to the query clears _SEM_FLOOR. None if there
    are no stored vectors, no embedder, or nothing clears the floor (caller then uses lexical only).
    Vectors are checked BEFORE the model is resolved, so a project with nothing embedded never pays the
    model-load cost."""
    pclause, pparams = _in_clause(_scopes(project), "m.project")
    rows = conn.execute(
        "SELECT m.*, e.vec AS _vec FROM embedding e JOIN memories m ON m.id = e.memory_id "
        "WHERE " + pclause + " AND m.status = 'active'", pparams).fetchall()
    if not rows:
        return None
    fn, _name = _resolve_embedder()
    if not fn:
        return None
    qv = fn(query)
    hits = sorted(((r, _cosine(qv, _blob_vec(r["_vec"]))) for r in rows),
                  key=lambda rs: rs[1], reverse=True)
    return [r for r, s in hits[:k] if s >= _SEM_FLOOR] or None


def _minder_rows(conn, project, lexical_query, semantic_query, k):
    """Records for the minder: hybrid bm25 + cosine via reciprocal-rank fusion when a semantic backend
    is available, else pure lexical. The semantic path is fully guarded — it never breaks retrieval."""
    lex = _recall_rows(conn, project, lexical_query, k * 2)
    try:
        sem = _semantic_rows(conn, project, semantic_query, k * 2)
    except Exception:
        sem = None
    if not sem:
        return lex[:k]
    C = 60  # reciprocal-rank-fusion constant
    score, rowmap = {}, {}
    for i, r in enumerate(lex):
        score[r["id"]] = score.get(r["id"], 0.0) + 1.0 / (C + i)
        rowmap[r["id"]] = r
    for i, r in enumerate(sem):
        score[r["id"]] = score.get(r["id"], 0.0) + 1.0 / (C + i)
        rowmap.setdefault(r["id"], r)
    best = sorted(score, key=lambda mid: score[mid], reverse=True)[:k]
    return [rowmap[mid] for mid in best]


def cmd_assist(conn, a) -> None:
    """The minder: a `UserPromptSubmit` hook that auto-glides the relevant db slice into context.

    Reads the prompt from stdin, ranks the store against it, and injects only the top few records,
    budget-capped — so the user never has to say "go fetch X from memory", and the cost is a small
    *relevant* slice per turn instead of carrying the whole history in-window. Hits-only and
    gated on substantive terms: trivial prompts inject nothing (no wasted tokens). Emits the
    UserPromptSubmit hook JSON."""
    def emit(block):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": block}}))
    payload = {}
    try:
        payload = json.load(sys.stdin) or {}
    except Exception:
        return emit("")
    prompt = payload.get("prompt") or ""
    terms = _content_terms(prompt)
    if len(terms) < 2:               # too little signal to be worth any tokens
        return emit("")
    # Same stable resolution as the status line + log hook, so the minder writes its "in play" marker
    # under the project the bar actually reads — they never disagree about which project this session is.
    project = _payload_project(payload, a.project)
    try:
        rows = _minder_rows(conn, project, " ".join(terms), prompt, a.k)
    except Exception:
        return emit("")
    out, used = [], 0
    for r in rows:
        line = f"- [{r['type']}] {r['summary']}"
        detail = (r["detail"] or "").strip().replace("\n", " ")
        if detail and used + len(line) + 2 < a.budget:        # a short detail snippet for context
            line += ": " + detail[:160]
        if used + len(line) > a.budget:
            break
        out.append(line)
        used += len(line) + 1
    # Mid-session auto-curate backstop: if a real backlog of uncaptured decisions has built up inside a
    # long session, nudge the AGENT — not the user — to promote the durable ones now (the boot directive
    # only fires at the NEXT session). Conservative: gated on a backlog floor so ordinary work doesn't
    # trip it; decision-only + dedup-safe, so it self-clears the moment they're captured. Cheap, model-free.
    directive = ""
    try:
        unc = _uncaptured_count(conn, project)
        if unc >= _ASSIST_CURATE_FLOOR:
            directive = (f"\n[engrim · auto-curate] {unc} decisions are logged but not yet in curated "
                         "memory. Before your next reply, SILENTLY `engrim add` the durable ones "
                         "(skip chatter, reversed calls, and false-positives); don't announce it. "
                         "Record what was MEASURED, not what was concluded — 'momentum score is the "
                         "constant 92.0' ages well; 'momentum looks broken' doesn't.")
    except Exception:
        directive = ""
    if not out and not directive:
        return emit("")                                       # nothing relevant -> spend nothing
    if out:
        try:  # record the pull so the ambient status line can show "N in play" — out-of-band, never in chat
            _meta_set(conn, project, "minder_n", str(len(out)))
            _meta_set(conn, project, "minder_ts", _now())
        except Exception:
            pass
    head = ("Possibly-relevant project memory (engrim), pulled for this message — use if helpful:\n"
            + "\n".join(out)) if out else ""
    emit((head + directive).strip())


def cmd_statusline(conn, a) -> None:
    """Print one compact engrim status line for hosts with a command status-line slot.

    Claude Code invokes this from `settings.json.statusLine`; Codex-shaped hook/session payloads are
    also accepted so callers can use the same status command when a host exposes a command slot.
    """
    data, sess = {}, None
    try:
        data = json.load(sys.stdin) or {}
        sess = data.get("session_id") or data.get("sessionId")
    except Exception:
        pass
    # Resolve from the session's STABLE launch dir (project_dir), not whatever cwd the status-line
    # process inherited — a drifted cwd would point the bar at a different project's memory mid-session.
    project = _payload_project(data, a.project)
    try:
        n = conn.execute("SELECT COUNT(*) FROM memories WHERE project=? AND status='active'",
                         (project,)).fetchone()[0]
    except Exception:
        n = 0
    # Turns logged for THIS session — the live, ticking-up signal. Curated count stays put until you
    # `add`; the transcript log grows every turn on its own, so this is what shows engrim is *working*
    # as the conversation deepens (the answer to "why isn't the number moving?").
    turns = 0
    try:
        if sess:
            turns = conn.execute("SELECT COUNT(*) FROM log WHERE project=? AND session=?",
                                 (project, sess)).fetchone()[0]
    except Exception:
        pass
    if not n and not turns:
        print("🧠 engrim · ready")        # installed + watching this project; nothing yet
        return
    parts = [f"🧠 engrim · {n} curated" if n else "🧠 engrim · capturing"]
    if turns:
        parts.append(f"+{turns} logged")           # ticks up every turn — engrim is recording live
    try:    # the live "minder" pull from the last prompt, if recent — proof it's helping NOW
        mn, mts = _meta_get(conn, project, "minder_n"), _meta_get(conn, project, "minder_ts")
        if mn and mts and int(mn) > 0:
            age = (_dt.datetime.now().astimezone() - _dt.datetime.fromisoformat(mts)).total_seconds()
            if 0 <= age < 600:
                parts.append(f"{mn} in play")
    except Exception:
        pass
    # Clear-readiness, live and model-free: recent decisions not yet curated. Ticks up as you decide
    # things, drops back to ✓ as you capture them — the ambient "is it safe to clear?" answer (#143).
    unc = _uncaptured_count(conn, project)
    if unc:
        parts.append(f"✎ {unc} to capture")   # pencil, not a warning: capturing is normal mid-work
    elif turns:
        parts.append("✓ clear-safe")
    print(" · ".join(parts))


_BOOT_SUMMARY_CAP = 200   # keep the pack lean: essay-length summaries are truncated in the boot pack

# The designated session-resume cursor: the newest active record carrying this tag is pinned to the
# TOP of the boot pack and shown UNTRUNCATED, so a fresh session after /clear opens on exactly where
# we left off — continue-as-clear (#191 continuity, taken the last mile). It rides OUTSIDE the per-type
# round-robin and isn't summary-capped; everything else fills the budget around it.
_RESUME_TAG = "resume-pointer"


def _is_resume(row):
    """True if a record is the session-resume cursor (tagged _RESUME_TAG). Tolerant of malformed tags."""
    try:
        return _RESUME_TAG in json.loads(row["tags"] or "[]")
    except Exception:
        return False


def _boot_pack(rows, budget):
    """Build the session-boot slice under a char budget, FAIRLY across types so a flood of one type
    (e.g. dozens of feedback records) can't starve recent decisions/facts. Returns [(row, summary)]
    with long summaries truncated, plus the total char cost. The resume cursor (if any) is pinned
    first and untruncated. Shared by `context` (display) and `stats` (economics) so the reported cost
    is exactly the pack that loads."""
    picked, used = [], 0
    # Pin the resume cursor first, untruncated — the one record we never clip, since it IS the place to
    # resume. Newest wins if several are tagged. It still counts against the budget; the rest fills around.
    resume = [r for r in rows if _is_resume(r)]
    cursor = max(resume, key=lambda r: r["ts"]) if resume else None
    if cursor is not None:
        csum = cursor["summary"] or ""
        picked.append((cursor, csum))
        used += len(csum) + 40
    by_type = {}
    for r in rows:
        if cursor is not None and r["id"] == cursor["id"]:
            continue                                           # already pinned; don't round-robin it again
        by_type.setdefault(r["type"], []).append(r)
    for t in by_type:
        by_type[t].sort(key=lambda r: r["ts"], reverse=True)   # recent-first within a type
    order = sorted(by_type, key=lambda t: _PRIO.get(t, 9))      # priority order across types
    idx = {t: 0 for t in order}
    progressed = True
    while progressed:
        progressed = False
        for t in order:                                        # round-robin: one per type per round
            if idx[t] >= len(by_type[t]):
                continue
            r = by_type[t][idx[t]]
            idx[t] += 1
            progressed = True
            summ = r["summary"] or ""
            if len(summ) > _BOOT_SUMMARY_CAP:
                summ = summ[:_BOOT_SUMMARY_CAP - 1].rstrip() + "…"
            cost = len(summ) + 40
            if used + cost <= budget:                          # skip what won't fit, keep trying others
                picked.append((r, summ))
                used += cost
    return picked, used


# The boot pack's recent-activity tail (#191): the freshest decision-signal turns from the LOG that
# aren't yet in curated memory, so a cold boot (e.g. right after /clear) still sees what was just
# decided — before anyone has promoted it to a record. This is the fix for the recency hole: capture
# already records every turn, but the curated boot pack never read the log, so the last stretch of work
# vanished on clear. Deliberately tiny and SEPARATE from the curated budget — a hard item cap plus its
# own char sub-budget — so it can never crowd out curated records or bloat context. Dedup uses the
# SHARED captured-check (`_is_captured`), same as `review` and the status bar, so the three surfaces
# can never disagree about what's already curated. Biases toward showing recent work over hiding it.
_TAIL_MAX_ITEMS = 3        # hard cap on tail lines — recency hint, not a transcript dump
_TAIL_BUDGET = 600         # own char sub-budget, independent of the curated boot budget
_TAIL_SNIPPET_CAP = 180    # per-line truncation

# Capture-floor: how far back the AUTOMATIC recency net looks. A bare last-N-turns window silently
# loses a decision when a long tail of verification/chatter — or a hard session kill mid-capture —
# pushes it past N before the next boot. So rather than a fixed turn count, scan back to the
# project's newest curated record: every decision-signal turn logged SINCE the last `add` stays
# eligible to resurface until it is itself captured. A hard ceiling keeps the scan cheap and
# model-free on every status refresh; before anything is curated, callers fall back to their count
# window so a fresh project stays lean.
_UNCAPTURED_MAX_SCAN = 200

# ONE lean recent window for every clear-readiness surface — the status bar, the minder's auto-curate
# nudge, the boot tail, and `review`. They used to carry their own numbers (25 vs 40 vs a flat -k),
# so the bar could count a turn `review` never scanned and the two would report different backlogs
# on the same db (#747). Same window + same captured-check = they agree by construction.
_CAPTURE_SCAN = 40
_TAIL_SCAN = _CAPTURE_SCAN

# Mid-session auto-curate backstop: the boot directive only fires at the NEXT session, so a single long
# session can accumulate uncaptured decisions. Once the backlog crosses this floor the minder nudges the
# AGENT (not the user) to promote them.
#
# Floor of 2, lowered from 4 (Tim, 2026-08-11): a wrap-time-only capture habit loses exactly the
# sessions that ran long — the ones worth keeping — because a window-close or limit-expiry can't
# curate itself. Capture wants to happen at the MOMENT of the decision. The old floor of 4 was priced
# for a counter that cried wolf; now that the captured-check can't false-positive on a paraphrase
# (#747), an earlier nudge is cheap and the backlog rarely gets deep enough to lose.
_ASSIST_CURATE_FLOOR = 2


def _parse_ts(s):
    """ISO-8601 timestamp -> aware datetime (None if unparseable). Log turns are stamped 'Z' (UTC)
    while curated records carry a local offset; normalizing 'Z' lets the net compare a turn against
    the last capture as instants, not mismatched strings."""
    try:
        d = _dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None
    return d if d.tzinfo else d.astimezone()   # a naive stamp -> assume local, so comparisons stay aware-safe


def _capture_floor(conn, project):
    """Instant of the project's newest curated record — the 'caught up to here' mark the recency net
    scans back to. None if nothing is curated yet. Project-scoped: a global record isn't a capture of
    THIS project's decisions."""
    try:
        row = conn.execute(
            "SELECT MAX(ts) FROM memories WHERE project = ? AND status = 'active'", (project,)).fetchone()
    except Exception:
        return None
    return _parse_ts(row[0]) if row and row[0] else None


def _past_floor(r, floor, i, scan):
    """Stop condition for a recency scan iterating log rows newest-first. Hybrid window: always scan
    the lean recent `scan` turns, AND extend back to the capture floor — stop only once a turn is
    BOTH outside the recent window and older than the last capture. The recent window protects a
    just-made decision even when a later unrelated `add` moved the floor past it; the floor extends
    reach for a decision buried under a long tail of chatter (or a mid-capture session kill)."""
    if i < scan:
        return False                       # always scan the lean recent window
    if floor is None:
        return True                        # past the window, nothing curated to extend reach
    rts = _parse_ts(r["ts"])
    # strict `<`: a turn in the floor's own second (record ts is second-precision) is still scanned,
    # biasing toward surfacing; the captured-check dedups any that were genuinely captured.
    return rts is None or rts < floor      # past window AND before the last capture -> stop


def _recent_tail(conn, project, scan=_TAIL_SCAN, max_items=_TAIL_MAX_ITEMS, budget=_TAIL_BUDGET):
    """Recent uncaptured decision-signal turns from the log, newest-first, under a hard item + char cap.
    Reuses review's detector; dedups against curated memory lexically so the boot stays fast."""
    floor = _capture_floor(conn, project)
    rows = conn.execute(
        "SELECT ts, content FROM log WHERE project = ? ORDER BY ts DESC LIMIT ?",
        (project, _UNCAPTURED_MAX_SCAN)).fetchall()
    tail_cues = _DECISION_CUES + _OPENTASK_CUES   # continuity: surface open loops, not just decisions
    seen, out, used = set(), [], 0
    for i, r in enumerate(rows):
        if _past_floor(r, floor, i, scan):       # reached the last capture point / lean window
            break
        content = r["content"] or ""
        if not any(cue in content.lower() for cue in tail_cues):
            continue
        snip = _decision_snippet(content, tail_cues)
        key = snip.lower()[:80]
        if not snip or key in seen or _looks_like_narration(snip):
            continue
        seen.add(key)
        if _is_captured(conn, project, snip):     # already covered by a curated record
            continue
        if len(snip) > _TAIL_SNIPPET_CAP:
            snip = snip[:_TAIL_SNIPPET_CAP - 1].rstrip() + "…"
        cost = len(snip) + 20
        if used + cost > budget:
            break
        out.append((r["ts"], snip))
        used += cost
        if len(out) >= max_items:
            break
    return out


def _uncaptured_state_key(conn, project):
    """Fingerprint of everything `_uncaptured_count` reads: the newest logged turn, the newest curated
    record, and how many records are active (so a `supersede` invalidates too). Plus the embed mode,
    since that changes which tier of the captured-check can run."""
    row = conn.execute(
        "SELECT (SELECT MAX(ts) FROM log WHERE project=?),"
        "       (SELECT MAX(ts) FROM memories WHERE project=? AND status='active'),"
        "       (SELECT COUNT(*) FROM memories WHERE project=? AND status='active')",
        (project, project, project)).fetchone()
    mode = os.environ.get("ENGRIM_EMBED", "").strip().lower()
    return f"{_CAPTURE_CHECK_VERSION}|{row[0]}|{row[1]}|{row[2]}|{mode}"


def _uncaptured_count(conn, project, scan=_CAPTURE_SCAN, cap=9):
    """Count of recent decision-signal log turns not yet covered by a curated record — the live
    "safe to clear?" signal behind the status bar's `✎ N to capture` and the minder's auto-curate
    nudge. Shares BOTH the scan window (`_CAPTURE_SCAN`) and the captured-check (`_is_captured`) with
    `review`, so the ambient number and the explicit command can't contradict each other (#747).

    Kept cheap two ways: the lexical tier resolves most snippets with no model at all, and the result
    is memoized against a fingerprint of the rows it read — so the status bar, which re-runs on every
    refresh, recomputes only when the log or the store actually changed."""
    try:
        state = _uncaptured_state_key(conn, project)
        cached = _meta_get(conn, project, "unc_cache")
        if cached:
            ckey, _, cval = cached.partition("=")
            if ckey == state and cval.isdigit():
                return int(cval)
    except Exception:
        state = None
    try:
        floor = _capture_floor(conn, project)
        rows = conn.execute("SELECT ts, content FROM log WHERE project=? ORDER BY ts DESC LIMIT ?",
                            (project, _UNCAPTURED_MAX_SCAN)).fetchall()
    except Exception:
        return 0
    seen, n = set(), 0
    for i, r in enumerate(rows):
        if _past_floor(r, floor, i, scan):       # reached the last capture point / lean window
            break
        content = r["content"] or ""
        if not any(cue in content.lower() for cue in _DECISION_CUES):
            continue
        snip = _decision_snippet(content)
        key = snip.lower()[:80]
        if not snip or key in seen or _looks_like_narration(snip):
            continue
        seen.add(key)
        if _is_captured(conn, project, snip):
            continue
        n += 1
        if n >= cap:
            break
    if state:
        try:
            _meta_set(conn, project, "unc_cache", f"{state}={n}")
        except Exception:
            pass                                 # a read-only db must never break the status bar
    return n


def cmd_context(conn, a) -> None:
    project = _resolve_project(a.project)
    pclause, pparams = _in_clause(_scopes(project), "project")
    rows = conn.execute(
        "SELECT * FROM memories WHERE " + pclause + " AND status = 'active'", pparams
    ).fetchall()
    picked, used = _boot_pack(rows, a.budget)
    if getattr(a, "json", False):
        print(json.dumps([dict(r) for r, _s in picked], default=str))
        return
    tail = _recent_tail(conn, project)
    if not picked and not tail:
        print(f"(no memory for project={project})")
        return
    if picked:
        print(f"🧠 engrim · memory restored for this project — you don't have to re-explain · {project}")
        print(f"  {len(picked)} of {len(rows)} curated records loaded (~{used} chars) · the rest one `recall` away")

        def _line(r, summ):
            tags = ", ".join(json.loads(r["tags"] or "[]"))
            gtag = "  · global" if r["project"] == GLOBAL_PROJECT else ""   # rides along in every project
            via = f" (via {_agent_display(r['origin_agent'])})" if ("origin_agent" in r.keys() and r["origin_agent"]) else ""
            type_tag = f" [{r['type'].upper()}]" if via else ""
            colon = ":" if via else ""
            print(f"- #{r['id']}{type_tag}{via}{colon} {summ}" + (f"  ({tags})" if tags else "") + gtag)

        # The resume cursor leads, in its own section, so a fresh session reads "where we left off" first.
        cursor = next(((r, s) for r, s in picked if _is_resume(r)), None)
        rest = [(r, s) for r, s in picked if not _is_resume(r)]
        if cursor is not None:
            print("\n[▶ RESUME HERE]")
            _line(*cursor)
        rest.sort(key=lambda rs: _PRIO.get(rs[0]["type"], 9))      # group by type for display (recent-first kept)
        cur = None
        for r, summ in rest:
            if r["type"] != cur:
                cur = r["type"]
                print(f"\n[{cur.upper()}]")
            _line(r, summ)
        if len(picked) < len(rows):
            print(f"\n(+{len(rows) - len(picked)} more · `engrim recall -q ...` to pull on demand)")
    # Recency tail: what was just decided but isn't a curated record yet — so a cold boot doesn't lose
    # the last stretch of work (#191). Tiny by construction; promote the durable ones to make them stick.
    if tail:
        print("\n[RECENT — logged this project, not yet curated]")
        for ts, snip in tail:
            print(f"- [{ts[:16]}] {snip}")
    # Clear-readiness verdict — the same gentle signal as the status bar, so the summary answers
    # "safe to /clear?" too. Capturing is a normal part of working, so this is an invitation (✎),
    # never a warning. Only shown once there's session history to reason about.
    #
    # At BOOT (agent_directive, set by `engrim hook`) this flips from a passive nudge-to-the-human into
    # an active directive-to-the-AGENT: a window-close / session-limit expiry can't curate itself (no
    # hook fires reliably at a hard kill), so the *next* session's agent is the recovery path. The raw
    # log already survived the close (Stop hook every turn + boot transcript reconciliation, #101); this
    # directive tells the fresh agent to promote the durable survivors into curated memory before it does
    # anything else — zero user interaction, judgment applied (so #219-type false-positives are skipped),
    # dedup-safe (the detector drops anything already curated). Self-healing: the capture-floor keeps a
    # survivor eligible across boots until it is actually captured.
    if conn.execute("SELECT 1 FROM log WHERE project = ? LIMIT 1", (project,)).fetchone():
        unc = _uncaptured_count(conn, project)
        if getattr(a, "agent_directive", False):
            if unc:   # cheap decision-only gate; the agent's `engrim review` does the thorough semantic pass
                print("\n[⟳ AUTO-CURATE — a prior session left decisions in the raw log but NOT in curated "
                      "memory (a window-close/limit-expiry can't curate itself). You are the recovery path.]")
                print("Before you address the user's message, SILENTLY curate the durable survivors:")
                print("  1. `engrim review`  → the full uncaptured-decision list (heuristic candidates)")
                print("  2. for each GENUINELY durable one:  "
                      "engrim add -t <decision|fact|state|feedback> -s \"<one line>\" [-d \"<why>\"]")
                print("  Skip chatter, reversed calls, and product copy (known false-positives) — keep the "
                      "store high-signal. Don't announce this; just curate, then proceed.")
                print("  Write what was MEASURED, not what was concluded: 'momentum score is the constant "
                      "92.0' stays true and useful; 'momentum looks broken' rots into a dead end.")
        elif unc:
            print(f"\n✎ {unc} recent decision(s) not yet curated — worth capturing before your next "
                  "/clear:  engrim add -t decision -s \"…\"")
        else:
            print("\n✓ recent decisions look captured — safe to /clear.")


def cmd_hook(conn, a) -> None:
    agent = getattr(a, "agent", "claude") or "claude"
    if agent in ("agy", "antigravity"):
        from engrim.adapters.agy import handle_boot, handle_stop
        event = getattr(a, "event", None) or "boot"
        if event == "boot":
            handle_boot(db_path=getattr(a, "db", None))
        elif event == "stop":
            handle_stop(db_path=getattr(a, "db", None), strict=_is_strict(a))
        else:
            sys.exit(f"Unknown event {event} for agent {agent}")
        return

    if agent == "codex":
        # Codex supplies the stable workspace path as `cwd` and expects the same JSON hook output
        # shape as Claude Code. Do not run Claude's file-memory seed or transcript-directory sweep:
        # those paths are Claude-specific and would silently point Codex at the wrong store.
        payload = {}
        try:
            payload = json.load(sys.stdin) or {}
        except Exception:
            pass
        a.project = _payload_project(payload, a.project)
        event = getattr(a, "event", None) or "sessionstart"
        if event not in ("boot", "sessionstart"):
            sys.exit(f"Unknown event {event} for agent {agent}")
        a.no_sync = True

    import contextlib
    import io
    # ONE-TIME context build: the very first session for a project seeds the store from Claude
    # Code's file-memory (its pre-install history), then we mark it seeded and never re-mirror.
    # Afterwards the store is canonical — the hook just injects from it; new knowledge arrives via
    # `engrim add`. A sync hiccup must NEVER break the hook (it has to emit valid JSON), so guard all.
    if not getattr(a, "no_sync", False):
        project = _resolve_project(a.project)
        try:
            if _meta_get(conn, project, SEED_KEY) is None:
                md = _claude_memory_dir()
                if md:
                    _do_sync(conn, project, md, no_prune=True)  # additive seed, never reduces
                _meta_set(conn, project, SEED_KEY, _now())      # seeded (even if no md) -> done
        except Exception:
            pass
        # Resilience: reconcile recent transcripts so a crash / hard window-close that skipped the
        # Stop or SessionEnd hook can't lose the tail of the last session. Idempotent (shared offset
        # cursor + uuid dedup) and bounded to recent files, so it's cheap and safe to run every boot.
        try:
            for tr in _claude_transcripts():
                _ingest_transcript(conn, project, tr)
        except Exception:
            pass
    a.json = False
    a.agent_directive = True   # boot path: emit the auto-curate directive (recovery for a hard window-close)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_context(conn, a)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": buf.getvalue().strip(),
    }}))


def cmd_supersede(conn, a) -> None:
    if a.status not in STATUSES:
        sys.exit(f"--status must be one of {STATUSES}")
    n = conn.execute("UPDATE memories SET status=? WHERE id=?", (a.status, a.id)).rowcount
    conn.commit()
    print(f"updated {n} row(s): #{a.id} -> {a.status}")


def cmd_retire(conn, a) -> None:
    """Mark every active resume-pointer `done` — the records the boot pack would pin under
    [▶ RESUME HERE], selected by the same predicate (`_is_resume`) so the two can't disagree.

    A pointer says where one session left off. Once that work is finished, or the store moves
    somewhere that working tree no longer exists (a CI runner folding a run's store into the
    shared one), an active pointer would lead the next session's pack as if it were its own.
    engrim never retires one on its own, since it can't know the work is done; this is the hand
    that does. `done` is the same monotonic move `supersede` makes: nothing is erased, the record
    stays readable with --include-stale, and `merge` carries the retirement to other copies.
    Scoped to exactly one project like every other write (`prune`, `embed`), so a pointer written
    to the global layer — which the pack pins in every project — takes `-p __global__` or `--all`."""
    if a.all:
        project = None
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = 'active' ORDER BY id").fetchall()
        scope = "all projects"
    else:
        project = _resolve_project(a.project)
        rows = conn.execute(
            "SELECT * FROM memories WHERE project = ? AND status = 'active' ORDER BY id",
            (project,)).fetchall()
        scope = f"project={project}"
    pointers = [r for r in rows if _is_resume(r)]
    if pointers and not a.dry_run:
        clause, ids = _in_clause([r["id"] for r in pointers], "id")
        conn.execute(f"UPDATE memories SET status = 'done' WHERE {clause}", ids)
        conn.commit()
    if a.json:
        status = "active" if a.dry_run else "done"     # the rows were read before the update
        print(json.dumps({"retired": len(pointers), "dry_run": a.dry_run, "project": project,
                          "pointers": [{**dict(r), "status": status} for r in pointers]},
                         default=str))
        return
    head = "DRY-RUN — no changes written; would retire" if a.dry_run else "retired"
    print(f"{head} {len(pointers)} resume-pointer(s) · {scope}")
    for r in pointers:
        print(f"  #{r['id']} {r['ts'][:16]}  {(r['summary'] or '')[:80]}")


def cmd_project(conn, a) -> None:
    """One line per project tag: records, active records, last write. Scoped like every other
    read — the cwd's project by default, `-p` for another, `--global` for the user-layer — with
    `--all` for every project in the store. `engrim projects` is that last form by name (its
    subparser sets all=True), so the plural keeps listing the whole store as it always has.
    `--json` gives the same rows as a list of objects."""
    if a.all:
        where, params, scope = "", (), "all projects"
    else:
        project = GLOBAL_PROJECT if a.globl else _resolve_project(a.project)
        where, params, scope = "WHERE project = ? ", (project,), f"project={project}"
    rows = conn.execute(
        "SELECT project, COUNT(*) n, SUM(status='active') active, MAX(ts) last "
        f"FROM memories {where}GROUP BY project ORDER BY last DESC", params
    ).fetchall()
    if a.json:
        print(json.dumps([{"project": r["project"], "records": r["n"], "active": r["active"],
                           "last": r["last"]} for r in rows]))
        return
    for r in rows:
        print(f"{r['n']:4d} ({r['active']} active)  last {r['last'][:16]}  {r['project']}")
    if not rows:
        print("(empty store)" if a.all else f"(no records for {scope})")


def _est_tokens(chars: int) -> int:
    return max(1, round(chars / 4))  # ~4 chars/token, the common rough heuristic


def cmd_stats(conn, a) -> None:
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    by_type = conn.execute("SELECT type, COUNT(*) n FROM memories GROUP BY type ORDER BY n DESC").fetchall()
    by_status = conn.execute("SELECT status, COUNT(*) n FROM memories GROUP BY status").fetchall()
    print(f"total: {total}")
    print("by type:   " + ", ".join(f"{r['type']}={r['n']}" for r in by_type))
    print("by status: " + ", ".join(f"{r['status']}={r['n']}" for r in by_status))

    # context economics for the current project: what it costs to stay oriented
    proj = _resolve_project(a.project)
    pclause, pparams = _in_clause(_scopes(proj), "project")   # economics reflect the real pack: project + global
    active = conn.execute(
        "SELECT * FROM memories WHERE " + pclause + " AND status = 'active'", pparams
    ).fetchall()
    if not active:
        return
    # Economics computed with the SAME builder the boot pack uses, so the number reflects reality.
    full = sum(12 + len(r["summary"] or "") + len(r["detail"] or "") for r in active)
    picked, pack = _boot_pack(active, a.budget)
    n = len(picked)
    pct = (pack / full * 100) if full else 0
    print(f"\ncontext economics · project={proj}")
    print(f"  full project memory:  {len(active)} records  ~{_est_tokens(full)} tokens (every record, full detail)")
    print(f"  session-boot pack:    {n} records  ~{_est_tokens(pack)} tokens (truncated summaries, auto-loaded each session)")
    print(f"  → orient for ~{_est_tokens(pack)} tokens/session = {pct:.0f}% of full memory; "
          f"the other {100 - pct:.0f}% is one `recall` away")


# --------------------------------------------------------------------------- setup (white glove)
HOOK_EVENT = "SessionStart"

CLAUDE_MD_BLOCK = """\
## Project Memory (engrim) — use it every session, scoped by project path

A project-tagged SQLite memory store persists decisions, facts, feedback, and state across
sessions. A SessionStart hook mirrors your file-memory in and auto-injects the current project's
memory pack (you start oriented); a SessionEnd hook mirrors the session's writes back out. Use it
proactively:
- Recall before non-trivial work: `engrim recall -q "<topic>"` (or `engrim context` for the pack).
- Write at every decision/correction/durable fact: `engrim add -t <decision|fact|feedback|state|user|reference> -s "<one line>" [--tags a,b]`.
- Cross-project truths about you (authorship, conventions, how you like to work): add `--global` so they load in every project.
- Supersede stale records: `engrim supersede --id N --status superseded`.
Keep it high-signal — curation and retrieval precision are the point, not volume.
"""


def _hook_bin(path: str) -> str:
    """Quote the resolved engrim path for the shell Claude Code runs hooks in.

    On Windows `which` hands back `C:\\Users\\...\\Scripts\\engrim.EXE`; interpolated raw, bash reads
    the backslashes as escapes and the command collapses to `C:UserstimgoAppData...` — not found.
    The `2>/dev/null || true` then swallows the error, so every hook is a silent no-op while setup
    still prints its green checkmarks. Forward slashes + quotes survive; quoting also fixes paths
    with spaces on POSIX, which were equally broken and just rarer."""
    return '"' + path.replace("\\", "/") + '"'


def _cmd_has(command: str, marker: str) -> bool:
    """Does an already-wired hook command carry this marker? Normalised so Windows spellings match.

    `"C:/Users/.../engrim.EXE" hook` has to read as `engrim hook`, or re-running setup won't
    recognise its own hooks and appends a duplicate group every time (setup is documented idempotent)."""
    norm = command.replace("\\", "/").replace('"', "").replace("'", "").lower()
    return marker in norm.replace(".exe", "")


def _verify_hook_bin(engrim_bin: str):
    """Actually run the wired binary the way Claude Code will. Returns None if it works, else why not.

    This is the missing feedback loop behind every silent Windows failure: the hook commands end in
    `2>/dev/null || true` so a session is never broken by a bad hook, which also means a completely
    non-functional install still prints a full column of green checkmarks. Setup is the one moment the
    user is watching, so the checkmark gets earned here instead of assumed. Hooks run through bash on
    every platform (Git Bash on Windows), so the check has to go through bash to be worth anything —
    it is the shell quoting, not the binary, that broke."""
    import subprocess              # local: `setup` runs once, the status line runs every refresh
    shell = shutil.which("bash")
    cmd = [shell, "-c", f"{engrim_bin} --help"] if shell else f"{engrim_bin} --help"
    try:
        p = subprocess.run(cmd, shell=not shell, capture_output=True, text=True,
                           errors="replace", timeout=30)
    except Exception as e:                       # no shell at all, or it couldn't be spawned
        return f"couldn't run the command ({type(e).__name__}: {e})"
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip().splitlines()
        return (detail[-1] if detail else f"exit status {p.returncode}")
    return None


CANONICAL_AGY_SKILL = """---
name: engrim
description: Cross-session memory and context continuity for heavy, long-horizon work. Use to recall prior decisions, facts, feedback, and architecture rationale, or to save new durable project decisions across session clears.
---

# Engrim: Cross-Session Memory & Context Continuity

Engrim provides persistent, project-scoped memory across agent sessions. It allows you to externalize architectural decisions, facts, and milestones so you can clear context freely without losing the "why" behind past decisions.

---

## Direct MCP Tools (Recommended)

When Engrim MCP server is active, use these tools directly:

- `engrim_recall(query, project="auto", k=5, type=None, tag=None)`:
  Run hybrid (keyword + semantic) search over project memory. Optionally filter by record type or tag.
- `engrim_context(project="auto", budget=4000)`:
  Fetch the session-boot memory pack (high-signal active records).
- `engrim_add(type, summary, detail=None, tags=[])`:
  Save a durable record into Engrim memory.
  `type` must be one of: `decision`, `fact`, `feedback`, `state`, `user`, `reference`.
- `engrim_review(project="auto")`:
  Check coverage before clearing context: surface recent decisions from the transcript log.

---

## CLI Commands

You can also run Engrim via `run_command` in bash:

```bash
# Add a durable decision
engrim add -t decision -s "Chose Postgres over Mongo" --tags db

# Query memory for relevant records (supports --tag)
engrim recall -q "database architecture" --tag db

# View active boot pack context
engrim context

# Check if recent decisions are safe before clearing (--strict gates with exit 2)
engrim review --strict

# Purge old transcript logs and VACUUM the database (opt-in retention; off by default)
engrim prune --keep-days 90

# Perform doctor health check
engrim doctor
```

---

## Session Continuity Best Practices

1. **Capture as you work**: Whenever a major decision, architecture choice, or milestone is established, call `engrim_add` (or `engrim add`).
2. **Use `resume-pointer`**: Before clearing context or wrapping up a session, add a record tagged `resume-pointer` summarizing the immediate next step. The newest `resume-pointer` will be pinned to the top of the next session's boot pack!
3. **Clear freely**: Once decisions are in Engrim, context can be safely cleared (`/clear`), as Engrim will inject the active memory pack at the start of the next session.
"""


def _setup_agy(engrim_bin: str, dry_run: bool = False, strict: bool = False) -> None:
    print("Wiring Google Antigravity environment…")
    hooks_path = os.path.expanduser("~/.gemini/config/hooks.json")
    boot_cmd = f"{engrim_bin} hook --agent agy --event boot 2>/dev/null || true"
    stop_cmd = f"{engrim_bin} hook --agent agy --event stop --strict" if strict else f"{engrim_bin} hook --agent agy --event stop >/dev/null 2>&1 || true"
    if dry_run:
        print(f"[dry-run] Would wire Antigravity hooks in {hooks_path}:")
        print(f"    PreInvocation: {boot_cmd}")
        print(f"    Stop:          {stop_cmd}")
    else:
        os.makedirs(os.path.dirname(hooks_path), exist_ok=True)
        hooks_data = {}
        if os.path.exists(hooks_path):
            try:
                with open(hooks_path, "r", encoding="utf-8") as f:
                    hooks_data = json.load(f)
            except Exception:
                hooks_data = {}
        engrim_entry = hooks_data.setdefault("engrim", {})
        engrim_entry["PreInvocation"] = [{"type": "command", "command": boot_cmd, "timeout": 30}]
        engrim_entry["Stop"] = [{"type": "command", "command": stop_cmd, "timeout": 30}]
        tmp = hooks_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hooks_data, f, indent=2)
        os.replace(tmp, hooks_path)
        print(f"✓ wired PreInvocation & Stop hooks in {hooks_path}")

    skill_path = os.path.expanduser("~/.gemini/config/skills/engrim/SKILL.md")
    if dry_run:
        print(f"[dry-run] Would deploy canonical Antigravity skill to {skill_path}")
    else:
        os.makedirs(os.path.dirname(skill_path), exist_ok=True)
        tmp = skill_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(CANONICAL_AGY_SKILL)
        os.replace(tmp, skill_path)
        print(f"✓ deployed canonical Antigravity skill to {skill_path}")

    mcp_paths = [
        os.path.expanduser("~/.gemini/antigravity-cli/mcp_config.json"),
        os.path.expanduser("~/.gemini/config/mcp_config.json"),
    ]
    raw_bin = engrim_bin.strip('"')
    mcp_entry = {
        "command": raw_bin,
        "args": ["serve", "--mcp"],
    }
    for mp in mcp_paths:
        if dry_run:
            print(f"[dry-run] Would register MCP server in {mp}")
        else:
            os.makedirs(os.path.dirname(mp), exist_ok=True)
            cfg = {}
            if os.path.exists(mp):
                try:
                    with open(mp, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {}
            servers = cfg.setdefault("mcpServers", {})
            servers["engrim"] = mcp_entry
            tmp = mp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, mp)
            print(f"✓ registered MCP server in {mp}")


def _setup_cursor(engrim_bin: str, dry_run: bool = False) -> None:
    print("Wiring Cursor MCP environment…")
    cursor_mcp = os.path.expanduser("~/.cursor/mcp.json")
    raw_bin = engrim_bin.strip('"')
    mcp_entry = {
        "command": raw_bin,
        "args": ["serve", "--mcp"],
    }
    if dry_run:
        print(f"[dry-run] Would register engrim in {cursor_mcp}")
    else:
        os.makedirs(os.path.dirname(cursor_mcp), exist_ok=True)
        cfg = {}
        if os.path.exists(cursor_mcp):
            try:
                with open(cursor_mcp, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        servers = cfg.setdefault("mcpServers", {})
        servers["engrim"] = mcp_entry
        tmp = cursor_mcp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, cursor_mcp)
        print(f"✓ registered Cursor MCP entry in {cursor_mcp}")


def _codex_home() -> str:
    """Return the Codex home directory, honoring the same override Codex uses."""
    return os.path.abspath(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))


def _codex_hook_commands(engrim_bin: str):
    """Commands for the Codex-native hook events.

    Codex sends one JSON object on stdin for every command hook. The command hooks deliberately
    swallow helper failures so a local memory integration can never interrupt the coding session.
    The hook itself still emits Codex-compatible JSON on the two context-producing events.
    """
    return {
        "SessionStart": (
            f"{engrim_bin} hook --agent codex --event sessionstart 2>/dev/null || true",
            20,
        ),
        "SessionEnd": (
            f"{engrim_bin} log --hook --agent codex 2>/dev/null || true",
            3,
        ),
        "Stop": (
            f"{engrim_bin} log --hook --agent codex 2>/dev/null || true",
            30,
        ),
        "UserPromptSubmit": (
            f"{engrim_bin} assist 2>/dev/null || true",
            20,
        ),
    }


def _setup_codex(engrim_bin: str, dry_run: bool = False) -> None:
    """Wire Codex to engrim through command hooks.

    MCP is intentionally not part of this path. Codex can run the same local CLI commands as Claude
    Code, while MCP remains an optional manual integration for users who want model-invoked tools.
    """
    print("Wiring Codex CLI environment…")
    hooks_path = os.path.join(_codex_home(), "hooks.json")
    commands = _codex_hook_commands(engrim_bin)
    if dry_run:
        print(f"[dry-run] Would wire Codex hooks in {hooks_path}")
        for event, (command, _timeout) in commands.items():
            print(f"    {event}: {command}")
        print("[dry-run] Codex hooks must be reviewed and trusted with /hooks before they run")
        return

    os.makedirs(os.path.dirname(hooks_path), exist_ok=True)
    hooks_data = {}
    if os.path.exists(hooks_path):
        try:
            with open(hooks_path, "r", encoding="utf-8") as f:
                hooks_data = json.load(f)
        except json.JSONDecodeError as e:
            sys.exit(f"Codex hooks file exists but is not valid JSON ({e}). Fix it, then re-run.")
        except OSError as e:
            sys.exit(f"can't read {hooks_path} ({e}). Fix the permissions, then re-run.")
    if not isinstance(hooks_data, dict):
        sys.exit(f"Codex hooks file must contain a JSON object: {hooks_path}")
    hooks = hooks_data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        sys.exit(f"Codex hooks field must be a JSON object: {hooks_path}")

    changed = False
    for event, (command, timeout) in commands.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            sys.exit(f"Codex hook event {event} must contain an array: {hooks_path}")
        managed = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks", [])
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if isinstance(handler, dict) and _cmd_has(handler.get("command", ""), "engrim"):
                    managed.append(handler)
        if managed:
            for handler in managed:
                desired = {"type": "command", "command": command, "timeout": timeout}
                if handler != desired:
                    handler.clear()
                    handler.update(desired)
                    changed = True
            print(f"✓ {event} Codex hook already present in {hooks_path}")
        else:
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": timeout}]})
            changed = True
            print(f"✓ wired {event} Codex hook\n    {command}")

    if changed:
        tmp = hooks_path + ".engrim-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hooks_data, f, indent=2)
            f.write("\n")
        os.replace(tmp, hooks_path)
        print(f"✓ wired Codex hooks in {hooks_path}")
    else:
        print(f"✓ Codex hooks already current in {hooks_path}")
    print("! review and trust these hooks in Codex with /hooks before they run")


def _setup_claude(conn, a, engrim_bin: str, dry_run: bool = False) -> None:
    print("Wiring Claude Code environment…")
    settings_path = os.path.expanduser(getattr(a, "settings", None) or "~/.claude/settings.json")
    bin_error = _verify_hook_bin(engrim_bin)
    stop_cmd = f"{engrim_bin} log --hook --strict" if _is_strict(a) else f"{engrim_bin} log --hook >/dev/null 2>&1 || true"
    wired = {
        "SessionStart": (f"{engrim_bin} hook 2>/dev/null || true", "engrim hook"),
        "SessionEnd":   (f"{engrim_bin} sync --claude >/dev/null 2>&1 || true", "engrim sync"),
        "Stop":         (stop_cmd, "engrim log"),
        "UserPromptSubmit": (f"{engrim_bin} assist 2>/dev/null || true", "engrim assist"),
    }

    if dry_run:
        print(f"[dry-run] Would wire Claude Code hooks in {settings_path}")
        return

    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        except json.JSONDecodeError as e:
            sys.exit(f"settings.json exists but is not valid JSON ({e}). Fix it, then re-run.")
        except OSError as e:
            sys.exit(f"can't read {settings_path} ({e}). Fix the permissions, then re-run.")
        except Exception as e:
            sys.exit(f"couldn't load {settings_path} ({type(e).__name__}: {e}). "
                     f"The file itself may be fine — please report this with the message above.")

    if bin_error:
        print(f"! the engrim command isn't runnable from a shell — {bin_error}\n"
              f"    tried: {engrim_bin} --help\n"
              f"  Wiring the hooks anyway, but they will do NOTHING until this resolves.\n")

    hooks = settings.setdefault("hooks", {})
    changed = False
    for event, (cmd, marker) in wired.items():
        groups = hooks.setdefault(event, [])
        present = any(_cmd_has(h.get("command", ""), marker)
                      for grp in groups for h in grp.get("hooks", []))
        if present:
            print(f"✓ {event} hook already present in {settings_path}")
        else:
            groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 20}]})
            changed = True
            print(f"✓ wired {event} hook\n    {cmd}")

    sl, sl_cmd = settings.get("statusLine"), f"{engrim_bin} statusline"
    if isinstance(sl, dict) and _cmd_has(sl.get("command") or "", "engrim"):
        print("✓ status line already shows engrim")
    elif sl:
        print(f"• a status line is already configured — leaving it. To show engrim, set its command to: {sl_cmd}")
    else:
        settings["statusLine"] = {"type": "command", "command": sl_cmd}
        changed = True
        print(f"✓ wired status line\n    {sl_cmd}")

    if changed:
        tmp = settings_path + ".engrim-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        os.replace(tmp, settings_path)

    if not getattr(a, "no_claude_md", False):
        md_path = os.path.expanduser("~/.claude/CLAUDE.md")
        existing = ""
        if os.path.exists(md_path):
            with open(md_path, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        if "engrim" in existing and "Project Memory" in existing:
            print(f"✓ CLAUDE.md already mentions engrim ({md_path})")
        else:
            os.makedirs(os.path.dirname(md_path), exist_ok=True)
            with open(md_path, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n" + CLAUDE_MD_BLOCK)
            print(f"✓ added usage note to {md_path}")


def cmd_setup(conn, a) -> None:
    """Universal multi-agent setup: Antigravity, Claude Code, Cursor, and Codex."""
    engrim_bin = _hook_bin(shutil.which("engrim") or "engrim")
    dry_run = getattr(a, "dry_run", False)
    bin_error = _verify_hook_bin(engrim_bin)

    explicit = bool(
        getattr(a, "agy", False) or
        getattr(a, "claude", False) or
        getattr(a, "cursor", False) or
        getattr(a, "codex", False) or
        getattr(a, "all", False) or
        getattr(a, "settings", None)
    )

    wire_agy = getattr(a, "agy", False) or getattr(a, "all", False)
    wire_claude = getattr(a, "claude", False) or getattr(a, "all", False) or bool(getattr(a, "settings", None))
    wire_cursor = getattr(a, "cursor", False) or getattr(a, "all", False)
    wire_codex = getattr(a, "codex", False) or getattr(a, "all", False)

    if not explicit:
        gemini_dir = os.path.expanduser("~/.gemini")
        claude_dir = os.path.expanduser("~/.claude")
        cursor_dir = os.path.expanduser("~/.cursor")
        codex_dir = _codex_home()
        detected = []
        if os.path.isdir(gemini_dir):
            wire_agy = True
            detected.append("Antigravity (~/.gemini)")
        if os.path.isdir(claude_dir):
            wire_claude = True
            detected.append("Claude Code (~/.claude)")
        if os.path.isdir(cursor_dir):
            wire_cursor = True
            detected.append("Cursor (~/.cursor)")
        if os.path.isdir(codex_dir):
            wire_codex = True
            detected.append("Codex CLI (~/.codex)")

        if detected:
            print(f"Auto-detected environments: {', '.join(detected)}")
        else:
            print("No specific environment directories detected (~/.gemini, ~/.claude, ~/.cursor, ~/.codex).")
            print("Defaulting to Claude Code setup. (Use --agy, --cursor, --codex, or --all to wire others).")
            wire_claude = True

    if wire_agy:
        _setup_agy(engrim_bin, dry_run=dry_run, strict=_is_strict(a))
    if wire_claude:
        _setup_claude(conn, a, engrim_bin, dry_run=dry_run)
    if wire_cursor:
        _setup_cursor(engrim_bin, dry_run=dry_run)
    if wire_codex:
        _setup_codex(engrim_bin, dry_run=dry_run)

    if not dry_run and os.environ.get("ENGRIM_EMBED", "").strip().lower() not in ("0", "off", "none", "false", "no", "lexical"):
        print("\nPreparing semantic recall (first run downloads a small embedding model)…")
        fn, name = _resolve_embedder()
        if fn:
            print(f"✓ semantic recall ready ({name})")
            try:
                proj = _resolve_project(None)
                rows = conn.execute("SELECT id, summary, detail FROM memories "
                                    "WHERE project = ? AND status = 'active'", (proj,)).fetchall()
                n = 0
                for r in rows:
                    ex = conn.execute("SELECT model FROM embedding WHERE memory_id = ?",
                                      (r["id"],)).fetchone()
                    if ex and ex[0] == name:
                        continue
                    _embed_row(conn, r["id"], r["summary"], r["detail"], fn, name)
                    n += 1
                conn.commit()
                if n:
                    print(f"  embedded {n} existing record(s) for {proj}")
            except Exception:
                pass
        else:
            print("• semantic recall unavailable (model2vec didn't load) — running pure-lexical for now")

    if (wire_claude or wire_codex) and bin_error and not dry_run:
        sys.stdout.flush()
        sys.exit(f"\nNOT done — the hooks are written, but `{engrim_bin} --help` fails in a shell "
                 f"({bin_error}),\nso every one of them will silently do nothing. Fix that and "
                 f"re-run `engrim setup`.")

    if wire_claude and not dry_run:
        print("\nDone. Open a NEW Claude Code session (or run /hooks to reload) and your project "
              "memory will auto-load. Try: engrim add -t fact -s \"hello world\" ; engrim context")
    if wire_codex and not dry_run:
        print("\nCodex hooks are installed. Open Codex and use /hooks to review and trust them; "
              "memory will load on the next session.")

    print("\nUniversal memory setup complete.")



def cmd_uninstall(conn, a) -> None:
    """Universal multi-agent uninstall: Antigravity, Claude Code, Cursor, and Codex."""
    dry_run = getattr(a, "dry_run", False)
    explicit = bool(
        getattr(a, "agy", False) or
        getattr(a, "claude", False) or
        getattr(a, "cursor", False) or
        getattr(a, "codex", False) or
        getattr(a, "all", False) or
        getattr(a, "settings", None)
    )

    wire_agy = getattr(a, "agy", False) or getattr(a, "all", False)
    wire_claude = getattr(a, "claude", False) or getattr(a, "all", False) or bool(getattr(a, "settings", None))
    wire_cursor = getattr(a, "cursor", False) or getattr(a, "all", False)
    wire_codex = getattr(a, "codex", False) or getattr(a, "all", False)

    if not explicit:
        gemini_dir = os.path.expanduser("~/.gemini")
        claude_dir = os.path.expanduser("~/.claude")
        cursor_dir = os.path.expanduser("~/.cursor")
        codex_dir = _codex_home()
        detected = []
        if os.path.isdir(gemini_dir):
            wire_agy = True
            detected.append("Antigravity (~/.gemini)")
        if os.path.isdir(claude_dir):
            wire_claude = True
            detected.append("Claude Code (~/.claude)")
        if os.path.isdir(cursor_dir):
            wire_cursor = True
            detected.append("Cursor (~/.cursor)")
        if os.path.isdir(codex_dir):
            wire_codex = True
            detected.append("Codex CLI (~/.codex)")

        if detected:
            print(f"Auto-detected environments: {', '.join(detected)}")
        else:
            print("No specific environment directories detected (~/.gemini, ~/.claude, ~/.cursor, ~/.codex).")
            print("Defaulting to Claude Code uninstall. (Use --agy, --cursor, --codex, or --all to specify others).")
            wire_claude = True

    if wire_agy:
        _uninstall_agy(dry_run=dry_run)
    if wire_claude:
        _uninstall_claude(a, dry_run=dry_run)
    if wire_cursor:
        _uninstall_cursor(dry_run=dry_run)
    if wire_codex:
        _uninstall_codex(dry_run=dry_run)
        
    print("\nUniversal memory uninstall complete.")

def _uninstall_agy(dry_run: bool = False) -> None:
    print("Unwiring Google Antigravity environment…")
    hooks_path = os.path.expanduser("~/.gemini/config/hooks.json")
    if dry_run:
        print(f"[dry-run] Would unwire Antigravity hooks in {hooks_path}")
    else:
        if os.path.exists(hooks_path):
            with open(hooks_path, "r", encoding="utf-8") as f:
                hooks_data = json.load(f)
            if "engrim" in hooks_data:
                del hooks_data["engrim"]
                tmp = hooks_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(hooks_data, f, indent=2)
                os.replace(tmp, hooks_path)
                print(f"✓ unwired PreInvocation & Stop hooks in {hooks_path}")
            else:
                print(f"✓ hooks already unwired in {hooks_path}")

    skill_dir = os.path.expanduser("~/.gemini/config/skills/engrim")
    if dry_run:
        print(f"[dry-run] Would remove Antigravity skill directory {skill_dir}")
    else:
        if os.path.exists(skill_dir):
            shutil.rmtree(skill_dir, ignore_errors=True)
            print(f"✓ removed Antigravity skill directory {skill_dir}")
        else:
            print(f"✓ skill already removed {skill_dir}")

    mcp_paths = [
        os.path.expanduser("~/.gemini/antigravity-cli/mcp_config.json"),
        os.path.expanduser("~/.gemini/config/mcp_config.json"),
    ]
    for mp in mcp_paths:
        if dry_run:
            print(f"[dry-run] Would remove MCP server from {mp}")
        else:
            if os.path.exists(mp):
                with open(mp, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                servers = cfg.get("mcpServers", {})
                if "engrim" in servers:
                    del servers["engrim"]
                    tmp = mp + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2)
                    os.replace(tmp, mp)
                    print(f"✓ removed MCP server from {mp}")
                else:
                    print(f"✓ MCP server already removed from {mp}")

def _uninstall_cursor(dry_run: bool = False) -> None:
    print("Unwiring Cursor MCP environment…")
    cursor_mcp = os.path.expanduser("~/.cursor/mcp.json")
    if dry_run:
        print(f"[dry-run] Would remove engrim from {cursor_mcp}")
    else:
        if os.path.exists(cursor_mcp):
            with open(cursor_mcp, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            servers = cfg.get("mcpServers", {})
            if "engrim" in servers:
                del servers["engrim"]
                tmp = cursor_mcp + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
                os.replace(tmp, cursor_mcp)
                print(f"✓ removed Cursor MCP entry from {cursor_mcp}")
            else:
                print(f"✓ Cursor MCP entry already removed from {cursor_mcp}")

def _uninstall_claude(a, dry_run: bool = False) -> None:
    print("Unwiring Claude Code environment…")
    settings_path = os.path.expanduser(getattr(a, "settings", None) or "~/.claude/settings.json")
    if dry_run:
        print(f"[dry-run] Would unwire Claude Code hooks in {settings_path}")
        return

    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
        hooks = settings.get("hooks", {})
        changed = False
        
        events = ["SessionStart", "SessionEnd", "Stop", "UserPromptSubmit"]
        for event in events:
            if event in hooks:
                groups = hooks[event]
                new_groups = []
                for grp in groups:
                    new_hooks = [h for h in grp.get("hooks", []) if not _cmd_has(h.get("command", ""), "engrim")]
                    if len(new_hooks) != len(grp.get("hooks", [])):
                        changed = True
                    if new_hooks:
                        grp["hooks"] = new_hooks
                        new_groups.append(grp)
                if len(new_groups) != len(groups):
                    changed = True
                hooks[event] = new_groups
                if not hooks[event]:
                    del hooks[event]
                    
        sl = settings.get("statusLine")
        if isinstance(sl, dict) and _cmd_has(sl.get("command", ""), "engrim"):
            del settings["statusLine"]
            changed = True
            
        if changed:
            tmp = settings_path + ".engrim-tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
            os.replace(tmp, settings_path)
            print(f"✓ unwired hooks and status line from {settings_path}")
        else:
            print(f"✓ hooks and status line already unwired from {settings_path}")


def _uninstall_codex(dry_run: bool = False) -> None:
    """Remove only the command hooks that setup owns; leave Codex config and MCP untouched."""
    print("Unwiring Codex CLI environment…")
    hooks_path = os.path.join(_codex_home(), "hooks.json")
    if dry_run:
        print(f"[dry-run] Would unwire Codex hooks in {hooks_path}")
        return
    if not os.path.exists(hooks_path):
        print(f"✓ Codex hooks already unwired from {hooks_path}")
        return

    try:
        with open(hooks_path, "r", encoding="utf-8") as f:
            hooks_data = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"Codex hooks file exists but is not valid JSON ({e}). Fix it, then re-run.")
    if not isinstance(hooks_data, dict):
        sys.exit(f"Codex hooks file must contain a JSON object: {hooks_path}")
    hooks = hooks_data.get("hooks", {})
    if not isinstance(hooks, dict):
        sys.exit(f"Codex hooks field must be a JSON object: {hooks_path}")
    changed = False
    for event in ("SessionStart", "SessionEnd", "Stop", "UserPromptSubmit"):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            handlers = group.get("hooks", [])
            if not isinstance(handlers, list):
                new_groups.append(group)
                continue
            new_handlers = [
                handler for handler in handlers
                if not (isinstance(handler, dict) and _cmd_has(handler.get("command", ""), "engrim"))
            ]
            if len(new_handlers) != len(handlers):
                changed = True
            if new_handlers:
                group["hooks"] = new_handlers
                new_groups.append(group)
            else:
                changed = True
        if new_groups:
            hooks[event] = new_groups
        elif event in hooks:
            del hooks[event]
            changed = True

    if changed:
        tmp = hooks_path + ".engrim-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hooks_data, f, indent=2)
            f.write("\n")
        os.replace(tmp, hooks_path)
        print(f"✓ unwired Codex hooks from {hooks_path}")
    else:
        print(f"✓ Codex hooks already unwired from {hooks_path}")


_IMPORT_TYPE_MAP = {
    "user": "user", "feedback": "feedback", "reference": "reference",
    "project": "state", "state": "state", "decision": "decision", "fact": "fact",
}


def _parse_md(path: str):
    """Parse a markdown note (optional YAML-ish frontmatter) into (summary, type, body)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    name = os.path.splitext(os.path.basename(path))[0]
    summary = None
    ftype = None
    body = text
    if text.lstrip().startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            front, body = parts[1], parts[2]
            for line in front.splitlines():
                m = re.match(r"\s*description:\s*(.+)", line)
                if m and not summary:
                    summary = m.group(1).strip().strip("\"'")
                m = re.match(r"\s*type:\s*([A-Za-z_]+)\s*$", line)
                if m and not ftype:
                    ftype = m.group(1).strip().lower()
    body = body.strip()
    if not summary:  # fall back to first heading / first non-empty line
        for line in body.splitlines():
            s = line.strip().lstrip("#").strip()
            if s:
                summary = s
                break
    return (summary or name)[:400], _IMPORT_TYPE_MAP.get(ftype or "", "fact"), (body or None), name


def cmd_import(conn, a) -> None:
    """Import markdown notes (a file or a directory tree) as records — one record per file.
    Frontmatter `description`/`type` are honored; otherwise the first heading becomes the summary."""
    project = _resolve_project(a.project)
    paths = []
    if os.path.isdir(a.path):
        for root, _, files in os.walk(a.path):
            for fn in sorted(files):
                if fn.lower().endswith((".md", ".markdown")):
                    paths.append(os.path.join(root, fn))
    elif os.path.isfile(a.path):
        paths = [a.path]
    else:
        sys.exit(f"not found: {a.path}")

    existing = {r[0] for r in conn.execute(
        "SELECT summary FROM memories WHERE project = ?", (project,))}
    added = skipped = 0
    for p in paths:
        base = os.path.basename(p)
        if a.exclude and re.search(a.exclude, base):
            skipped += 1
            continue
        summary, typ, body, name = _parse_md(p)
        if not summary or summary in existing:
            skipped += 1
            continue
        tags = [t for t in re.split(r"[_\-.]", name) if len(t) > 1][:6]
        conn.execute(
            "INSERT INTO memories(ts,project,type,summary,detail,status,tags,links,source,origin_agent) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (_now(), project, typ, summary, body, "active",
             json.dumps(tags), json.dumps([]), "import:" + base, "cli"),
        )
        existing.add(summary)
        added += 1
    conn.commit()
    print(f"imported {added} record(s), skipped {skipped} (dupe/excluded/empty) -> project={project}")


_TOMBSTONE_RE = re.compile(r"^\s*merged into\b", re.IGNORECASE)
_INDEX_LINE_RE = re.compile(r"^\s*[-*]\s*\[[^\]]+\]\([^)]+\.(?:md|markdown)\)")


def _is_tombstone(body: str) -> bool:
    """A redirect stub like 'Merged into MEMORY.md ...' — content lives elsewhere, not a record."""
    for line in (body or "").splitlines():
        s = line.strip()
        if s:
            return bool(_TOMBSTONE_RE.match(s))
    return True  # empty body == nothing to store


def _section_type(title: str) -> str:
    t = title.lower()
    if "feedback" in t or "rule" in t:
        return "feedback"
    if t.startswith("user"):
        return "user"
    return "state"


def _parse_index_sections(path: str):
    """Split a hub/index markdown (e.g. MEMORY.md) into '## ' sections, yielding only those
    that carry inline CONTENT (not pure pointer lists). Pointer lines '- [x](y.md)' are stripped
    when deciding; a section that is nothing but pointers is skipped (its files sync on their own)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    sections, title, buf = [], None, []
    for line in lines:
        if line.startswith("## "):
            if title is not None:
                sections.append((title, "\n".join(buf).strip()))
            title, buf = line[3:].strip(), []
        elif title is not None:
            buf.append(line)
    if title is not None:
        sections.append((title, "\n".join(buf).strip()))
    out = []
    for title, body in sections:
        content = "\n".join(l for l in body.splitlines() if not _INDEX_LINE_RE.match(l)).strip()
        if len(content) < 140:        # essentially a pointer-only section
            continue
        stable = re.sub(r"\s*\(.*\)\s*$", "", title).strip()  # drop "(session 58, ...)" churn
        out.append((stable, _section_type(title), body[:4000]))
    return out


def _claude_memory_dir(cwd: str = None):
    """Best-effort path to Claude Code's per-project file-memory dir for `cwd`.

    Claude Code stores it at ~/.claude/projects/<slug>/memory where <slug> is the abs cwd with
    every non-alphanumeric char turned into '-'. `$ENGRIM_MD_DIR` overrides for non-standard
    setups. Returns the path only if it exists on disk, else None — so callers no-op cleanly for
    users who don't use file-memory at all."""
    env = os.environ.get("ENGRIM_MD_DIR")
    if env:
        return env if os.path.isdir(env) else None
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd or os.getcwd()))
    path = os.path.expanduser(os.path.join("~/.claude/projects", slug, "memory"))
    return path if os.path.isdir(path) else None


def _claude_transcripts(cwd=None, limit=6):
    """The most-recent Claude Code transcript JSONLs for this project (they live beside the memory
    dir, in ~/.claude/projects/<slug>/). Bounded to the newest `limit` so SessionStart catch-up
    stays cheap — those cover any session that just crashed/closed without a clean SessionEnd."""
    env = os.environ.get("ENGRIM_MD_DIR")
    if env:
        base = os.path.dirname(env.rstrip("/\\"))      # transcripts sit next to the memory dir
    else:
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd or os.getcwd()))
        base = os.path.expanduser(os.path.join("~/.claude/projects", slug))
    try:
        files = [os.path.join(base, f) for f in os.listdir(base) if f.endswith(".jsonl")]
    except OSError:
        return []
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files[:limit]


def _do_sync(conn, project, path, hub="MEMORY.md", exclude=None, dry_run=False, no_prune=False):
    """Core mirror: active markdown dir -> engrim, idempotent and keyed on a stable `source`.

    One record per topic file (`md:file:<base>`), tombstone redirects skipped; the hub file's
    inline-content sections become records (`md:section:<slug>`); legacy `import:<base>` rows are
    adopted; rows whose md source has vanished get superseded. Returns (added, updated, pruned,
    skipped, plan). Pure data movement — no printing — so `sync`, `hook`, and setup can all reuse it."""
    hub = os.path.basename(hub) if hub else "MEMORY.md"
    by_source = {}
    for r in conn.execute(
        "SELECT id,source,summary,detail,type,status FROM memories WHERE project=?", (project,)):
        if r["source"]:
            by_source[r["source"]] = r

    added = updated = skipped = 0
    seen = set()      # sources present in the active md this run (incl. adopted legacy keys)
    plan = []         # (action, source, summary)

    def upsert(source, legacy, summary, typ, detail, tags):
        nonlocal added, updated
        seen.add(source)
        if legacy:
            seen.add(legacy)
        row = by_source.get(source) or (by_source.get(legacy) if legacy else None)
        if row is None:
            plan.append(("ADD", source, summary))
            if not dry_run:
                conn.execute(
                    "INSERT INTO memories(ts,project,type,summary,detail,status,tags,links,source,origin_agent)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (_now(), project, typ, summary, detail, "active",
                     json.dumps(tags), json.dumps([]), source, "claude-code"))
            added += 1
        elif (row["summary"], row["detail"], row["type"], row["source"]) != (summary, detail, typ, source):
            plan.append(("UPD", source, summary))
            if not dry_run:
                conn.execute(
                    "UPDATE memories SET ts=?,type=?,summary=?,detail=?,tags=?,source=? WHERE id=?",
                    (_now(), typ, summary, detail, json.dumps(tags), source, row["id"]))
            updated += 1
        # unchanged -> no-op

    # 1) topic files (one record each), skip the hub and tombstones
    for root, _, files in os.walk(path):
        for fn in sorted(files):
            if not fn.lower().endswith((".md", ".markdown")):
                continue
            if fn == hub or (exclude and re.search(exclude, fn)):
                continue
            summary, typ, body, name = _parse_md(os.path.join(root, fn))
            if not body or _is_tombstone(body):
                skipped += 1
                plan.append(("SKIP", "md:file:" + fn, "(tombstone/empty)"))
                continue
            tags = [t for t in re.split(r"[_\-.]", name) if len(t) > 1][:6]
            upsert("md:file:" + fn, "import:" + fn, summary[:400], typ, body, tags)

    # 2) hub inline-content sections
    hub_path = os.path.join(path, hub)
    if os.path.isfile(hub_path):
        for stable, typ, detail in _parse_index_sections(hub_path):
            slug = re.sub(r"[^a-z0-9]+", "-", stable.lower()).strip("-")[:60]
            tags = [t for t in slug.split("-") if len(t) > 1][:6]
            upsert("md:section:" + slug, None, stable[:400], typ, detail, tags)

    # 3) reconcile: retire sync-managed rows whose md source has vanished (file deleted /
    # tombstoned, section removed). Never touches hand-written rows (source NULL/other).
    # Guarded: only prune when this run actually saw content, so a misfire can't wipe the store.
    pruned = 0
    if seen and not no_prune:
        for src, row in by_source.items():
            if src in seen or row["status"] != "active":
                continue
            if not (src.startswith("md:file:") or src.startswith("md:section:")
                    or src.startswith("import:")):
                continue
            plan.append(("PRUNE", src, row["summary"]))
            if not dry_run:
                conn.execute("UPDATE memories SET status='superseded' WHERE id=?", (row["id"],))
            pruned += 1

    if not dry_run:
        conn.commit()
    return added, updated, pruned, skipped, plan


def cmd_sync(conn, a) -> None:
    """One-time context build: mirror an active markdown memory dir into the store, then step aside.

    `--claude` auto-targets Claude Code's per-project memory dir (so a hook needs no path) and is
    seed-once: after the first build the store is canonical and `--claude` no-ops, unless `--force`.
    Giving an explicit path is always treated as a deliberate (re)build and runs every time."""
    project = _resolve_project(a.project)
    path = a.path
    if a.claude and not path:
        path = _claude_memory_dir()
        if not path:                       # no file-memory for this project — nothing to mirror
            _meta_set(conn, project, SEED_KEY, _now())   # mark done; store is db-native from here
            print(f"sync: no Claude memory dir for project={project} (nothing to seed)")
            return
    if not path:
        sys.exit("sync needs a directory path (or --claude to auto-detect Claude Code's)")
    if not os.path.isdir(path):
        sys.exit(f"sync expects a directory: {path}")

    # Seed-once gate for the automatic (--claude) path: don't let install-time history keep
    # overwriting the live, accumulating store on every session close.
    if a.claude and not a.force and _meta_get(conn, project, SEED_KEY) is not None:
        print(f"sync: project={project} already seeded — store is canonical now, nothing "
              f"re-imported (use --force to rebuild from md).")
        return

    added, updated, pruned, skipped, plan = _do_sync(
        conn, project, path, hub=a.hub, exclude=a.exclude, dry_run=a.dry_run, no_prune=a.no_prune)
    if not a.dry_run:
        _meta_set(conn, project, SEED_KEY, _now())
    head = "DRY-RUN — no changes written" if a.dry_run else "synced"
    print(f"{head}: +{added} add, ~{updated} update, {pruned} retire, {skipped} skip "
          f"-> project={project}")
    if a.dry_run or a.verbose:
        for action, source, summary in plan:
            print(f"  {action:4} {source}\n         {summary[:80]}")


# --------------------------------------------------------------------------- merge
# Two stores of the same project meet whenever a project's agents run in more than one place — a
# laptop and a CI runner, two machines, two agents at once — and each ends up holding decisions the
# other lacks. `merge` folds OTHER's records into this store, idempotently, so both places can keep
# writing and reconcile afterwards.
#
# Identity is content, not id. `memories.id` is assigned per store, so two stores seeded from the
# same base hand the same id to different records; the natural key is
# (project, ts, type, summary, detail) — ts is to the second with a zone, and nothing ever rewrites
# a record's text (`supersede` changes status only). Status is monotonic: active -> superseded/done
# is the only mutation engrim makes, so a non-active status on either side wins, which makes the
# merge safe in either direction and more than once. `memories_fts` follows through its triggers;
# a merged row is embedded the way `add` embeds (best-effort); `log` rows dedup on their own
# (project, msg_uuid) index. The source is opened read-only and its contents are never modified
# (reading a WAL-mode store creates empty -wal/-shm sidecars beside it, as any reader does).
_MERGE_KEY_SQL = "project = ? AND ts = ? AND type = ? AND summary = ? AND coalesce(detail,'') = ?"


def _open_store_readonly(path: str) -> sqlite3.Connection:
    """Open another engrim store for reading only — no schema migration, no WAL switch, and the
    file is never created or written. Exits with a plain message when `path` is not an engrim
    store."""
    if not os.path.isfile(path):
        sys.exit(f"merge: no such file: {path}")
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        has = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone()
    except sqlite3.DatabaseError:
        sys.exit(f"merge: not an engrim store (not a SQLite database): {path}")
    if not has:
        sys.exit(f"merge: not an engrim store (no memories table): {path}")
    return src


def merge_store(conn, other_path, project=None, dry_run=False):
    """Fold OTHER's memories (and transcript log rows) into `conn`.

    Returns (added, restatused, skipped, log_added, plan) where plan is a list of
    (action, summary) for --dry-run / --verbose. Idempotent: a second run adds nothing."""
    src = _open_store_readonly(other_path)
    src_cols = {r[1] for r in src.execute("PRAGMA table_info(memories)")}
    where, params = ("WHERE project = ?", (project,)) if project else ("", ())
    rows = src.execute(f"SELECT * FROM memories {where} ORDER BY id", params).fetchall()
    added = restatused = skipped = 0
    plan = []
    fn, name = _resolve_embedder()
    for r in rows:
        detail = r["detail"]
        key = (r["project"], r["ts"], r["type"], r["summary"], detail or "")
        mine = conn.execute(
            f"SELECT id, status FROM memories WHERE {_MERGE_KEY_SQL}", key).fetchone()
        if mine is None:
            plan.append(("add", r["summary"]))
            added += 1
            if dry_run:
                continue
            cur = conn.execute(
                "INSERT INTO memories(ts,project,type,summary,detail,status,tags,links,source,origin_agent) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (r["ts"], r["project"], r["type"], r["summary"], detail, r["status"],
                 r["tags"], r["links"], r["source"],
                 r["origin_agent"] if "origin_agent" in src_cols else None))
            if fn:
                try:
                    _embed_row(conn, cur.lastrowid, r["summary"], detail, fn, name)
                except Exception:
                    pass
        elif mine["status"] == "active" and r["status"] != "active":
            plan.append((r["status"][:4], r["summary"]))
            restatused += 1
            if not dry_run:
                conn.execute("UPDATE memories SET status=? WHERE id=?", (r["status"], mine["id"]))
        else:
            plan.append(("skip", r["summary"]))
            skipped += 1
    log_added = 0
    if src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='log'").fetchone():
        log_cols = {c[1] for c in src.execute("PRAGMA table_info(log)")}
        for lr in src.execute(f"SELECT * FROM log {where} ORDER BY id", params):
            # Rows with a uuid dedup on the store's own (project, msg_uuid) index; rows without
            # one (NULLs never collide there) dedup on their content instead, so a second merge
            # adds nothing either way.
            if lr["msg_uuid"]:
                exists = conn.execute("SELECT 1 FROM log WHERE project = ? AND msg_uuid = ?",
                                      (lr["project"], lr["msg_uuid"])).fetchone()
            else:
                exists = conn.execute(
                    "SELECT 1 FROM log WHERE project = ? AND ts = ? AND role = ? "
                    "AND coalesce(content,'') = ? AND msg_uuid IS NULL",
                    (lr["project"], lr["ts"], lr["role"], lr["content"] or "")).fetchone()
            if exists:
                continue
            log_added += 1
            if not dry_run:
                conn.execute(
                    "INSERT INTO log(ts,project,session,role,content,raw,msg_uuid) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (lr["ts"], lr["project"], lr["session"], lr["role"], lr["content"],
                     lr["raw"] if "raw" in log_cols else None, lr["msg_uuid"]))
    src.close()
    if not dry_run:
        conn.commit()
    return added, restatused, skipped, log_added, plan


def cmd_merge(conn, a) -> None:
    """Fold another store's records into this one (see merge_store). `--project` narrows to one
    project tag; the default takes every project the other store holds, since merging is about
    two copies of the same memory, not about which directory you happen to be in."""
    if os.path.isfile(a.other) and os.path.realpath(a.other) == os.path.realpath(a.db):
        sys.exit("merge: source and target are the same store")
    added, restatused, skipped, log_added, plan = merge_store(
        conn, a.other, project=a.project, dry_run=a.dry_run)
    head = "DRY-RUN — no changes written" if a.dry_run else "merged"
    print(f"{head}: +{added} add, ~{restatused} status, {skipped} skip, +{log_added} log "
          f"-> from {a.other}")
    if a.dry_run or a.verbose:
        for action, summary in plan:
            print(f"  {action:4} {summary[:80]}")


def cmd_backup(conn, a) -> None:
    """Write a consistent copy of the whole store to `a.dest` through SQLite's online backup API.

    Safe while another process (the MCP server, a session's hooks) still holds the store open: the
    copy is one snapshot, never a read torn by a write in flight, and whatever sits in the -wal
    sidecar is folded in rather than left behind. A plain file copy of a WAL-mode store gives
    neither guarantee. The result is a complete engrim store (FTS, embeddings, log and meta
    included) that `--db` can open directly."""
    dest = a.dest
    fresh = not os.path.exists(dest)
    if not fresh:
        mine = {os.path.realpath(a.db + ext) for ext in ("", "-wal", "-shm")}
        if os.path.realpath(dest) in mine:
            sys.exit("backup: destination is the store itself")
        if not a.force:
            sys.exit(f"backup: {dest} exists (pass --force to overwrite it)")
    d = os.path.dirname(dest)
    if d:
        os.makedirs(d, exist_ok=True)
    copy = err = None
    try:
        copy = sqlite3.connect(dest)
        conn.backup(copy)
        total = copy.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = copy.execute(
            "SELECT COUNT(*) FROM memories WHERE status = 'active'").fetchone()[0]
    except sqlite3.DatabaseError as e:
        err = e
    finally:
        if copy is not None:
            copy.close()                # before any remove: Windows can't unlink an open file
    if err is not None:
        if fresh:                       # don't leave a partial file that blocks the next run
            try:
                os.remove(dest)
            except OSError:
                pass
        sys.exit(f"backup: {err}: {dest}")
    # The store's own owner-only stance (best-effort; no-op on Windows), sidecars included.
    for _ext in ("", "-wal", "-shm"):
        try:
            os.chmod(dest + _ext, 0o600)
        except OSError:
            pass
    if a.json:
        print(json.dumps({"records": total, "active": active, "dest": dest}))
        return
    print(f"backed up {total} records, {active} active -> {dest}")


# --------------------------------------------------------------------------- transcript log
# A SEPARATE, append-only tier from `memories`. It records the raw back-and-forth so engineers
# have a full, replayable record — but it is NEVER injected into the boot pack / context window, so
# it can't bloat a session or drag the system. Curated memory (small, loaded) and the transcript
# log (complete, never loaded) are two tiers that don't compete.

# --- action lines: the WORK, not just the talk ---------------------------------------------------
#
# The log used to keep prose only ("the actual conversation, not machinery"), which left it too
# chat-focused: measured on one real session, prose was 14 KB against 315 KB of tool traffic, so the
# record of what was actually DONE — files changed, releases cut — existed nowhere searchable (#756).
#
# The fix is a snippet of value per action, not the payload. A tool call becomes ONE line naming the
# change; the 93 KB of tool_use in that session compresses to ~10 KB of readable spine. Deliberately
# STATE-CHANGING only: greps, reads and inspection are how you find things, not what you did, and
# including them buried the signal 4:1.
_ACTION_PREFIXES = ("[changed]", "[ran]")
_EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
_STATE_CHANGING_CMD = re.compile(
    r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?("
    r"git\s+(?:commit|push|tag|merge|rebase|reset|revert|cherry-pick)"
    r"|gh\s+(?:release|pr)\s+create"
    r"|(?:pip|pip3|uv|npm|pnpm|yarn|cargo|gem|brew|apt|apt-get)\s+"
    r"(?:install|uninstall|add|remove|publish)"
    r"|twine\s+upload"
    r"|engrim\s+(?:add|supersede|import|sync)"
    r"|mkdir|chmod|chown|ln\s+-s|rm\s|mv\s|cp\s"
    r")", re.I)
_ACTION_LINE_CAP = 160


def _action_lines(blocks):
    """One compact line per STATE-CHANGING tool call. Returns [] for pure investigation."""
    out = []
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        name, inp = b.get("name"), b.get("input") or {}
        if not isinstance(inp, dict):
            continue
        if name in _EDIT_TOOLS:
            path = inp.get("file_path") or inp.get("notebook_path")
            if path:
                out.append(f"[changed] {path}")
        elif name == "Bash":
            cmd = " ".join((inp.get("command") or "").split())
            if not cmd or not _STATE_CHANGING_CMD.search(cmd):
                continue
            desc = " ".join((inp.get("description") or "").split())
            line = f"[ran] {desc} — {cmd}" if desc else f"[ran] {cmd}"
            out.append(line[:_ACTION_LINE_CAP].rstrip())
    return out


def _extract_text(content, include_thinking=False):
    """Pull the searchable text out of a Claude transcript message's `content`.

    `content` is a str (plain user prompt) or a list of typed blocks. We keep `text` (the visible
    exchange), optionally `thinking`, plus a one-line summary of each state-changing tool call
    (see `_action_lines`). Tool RESULTS and images are still skipped — they're bulk, not signal."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "thinking" and include_thinking:
            parts.append("[thinking] " + b.get("thinking", ""))
    parts.extend(_action_lines(content))
    return "\n".join(p for p in parts if p).strip()


def _ingest_transcript(conn, project, path, session=None, include_thinking=False):
    """Append new user/assistant turns from a Claude Code transcript JSONL into the `log` table.

    Idempotent two ways: a per-session byte-offset cursor (in engrim_meta) means we only parse what's
    been appended since last time (cheap, even on multi-MB transcripts), and a UNIQUE msg_uuid with
    INSERT OR IGNORE guarantees no duplicates even if the file is re-read from the top. Sidechain
    (subagent) turns are skipped — this is the human<->assistant back-and-forth."""
    if not path or not os.path.isfile(path):
        return 0
    # Key the cursor by session id. For a Claude transcript the filename stem *is* the session id,
    # so the live Stop hook (passes session_id) and SessionStart catch-up (passes a path) share one
    # cursor instead of double-reading the same file.
    okey = "log_offset:" + (session or os.path.splitext(os.path.basename(path))[0])
    try:
        start = int(_meta_get(conn, project, okey) or 0)
    except (TypeError, ValueError):
        start = 0
    size = os.path.getsize(path)
    if start > size:        # file rotated/truncated -> re-read from the top
        start = 0
    added = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            otype = o.get("type")
            osource = o.get("source")
            if otype in ("user", "assistant"):
                role = otype
                msg = o.get("message") or {}
                text = _extract_text(msg.get("content"), include_thinking)
                uuid = o.get("uuid")
                ts = o.get("timestamp") or _now()
                sess = o.get("sessionId") or session
            elif otype in ("USER_INPUT", "PLANNER_RESPONSE") or osource in ("USER_EXPLICIT", "MODEL"):
                role = "user" if (otype == "USER_INPUT" or osource == "USER_EXPLICIT") else "assistant"
                text = o.get("content") or ""
                if not text and o.get("thinking") and include_thinking:
                    text = "[thinking] " + o.get("thinking")
                uuid = f"{session or 'agy'}-{o.get('step_index', '')}" if o.get("step_index") is not None else None
                ts = o.get("created_at") or _now()
                sess = session
            else:
                continue
            # Full fidelity: keep the complete original JSON line in `raw` (every turn, including
            # tool turns and sidechains), plus an extracted text slice in `content` for readable
            # search. raw never enters context, so completeness costs disk, not tokens.
            cur = conn.execute(
                "INSERT OR IGNORE INTO log(ts,project,session,role,content,raw,msg_uuid) "
                "VALUES(?,?,?,?,?,?,?)",
                (ts, project, sess, role, text, line, uuid))
            added += cur.rowcount
        end = f.tell()
    # Advance the cursor monotonically. When start==0 we deliberately re-read from the top (new
    # session, or a rotated/truncated file detected above), so the freshly-read position is correct.
    # Otherwise never rewind past where a concurrent run may already have advanced: two overlapping
    # Stop hooks must not let a slower one reopen ground the faster one already covered.
    if start:
        try:
            end = max(end, int(_meta_get(conn, project, okey) or 0))
        except (TypeError, ValueError):
            pass
    _meta_set(conn, project, okey, str(end))   # commits
    conn.commit()
    return added


def _log_codex_hook(conn, payload, explicit_project="auto"):
    """Capture the stable prompt/assistant fields Codex exposes to command hooks.

    Codex documents `transcript_path` as a convenience only and does not promise its file format.
    Logging the event payload keeps the integration useful without coupling it to that private file.
    """
    event = payload.get("hook_event_name") or payload.get("event")
    if event == "UserPromptSubmit":
        role = "user"
        content = payload.get("prompt") or ""
    elif event == "Stop":
        role = "assistant"
        content = payload.get("last_assistant_message") or ""
    else:
        return
    if not content:
        return

    project = _payload_project(payload, explicit_project)
    session = payload.get("session_id")
    turn = payload.get("turn_id")
    identity = turn or hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    msg_uuid = f"codex:{session or 'session'}:{event}:{identity}"
    conn.execute(
        "INSERT OR IGNORE INTO log(ts,project,session,role,content,raw,msg_uuid) "
        "VALUES(?,?,?,?,?,?,?)",
        (_now(), project, session, role, content, json.dumps(payload, ensure_ascii=False), msg_uuid),
    )
    conn.commit()


def cmd_log(conn, a) -> None:
    """Append to the raw transcript log. `--hook` reads a Stop-hook JSON from stdin and ingests the
    session's new turns; `--from-transcript PATH` ingests a file; otherwise append one -r/-c turn."""
    if a.hook:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            return
        if getattr(a, "agent", "claude") == "codex":
            _log_codex_hook(conn, payload, a.project)
            return
        # Resolve from the session's STABLE launch dir, not the hook process's os.getcwd(). A Stop
        # hook can be spawned with an incidental cwd (e.g. it inherits one a tool subprocess chdir'd
        # into), and a single Claude session is one transcript file with one session id. Keying
        # ingestion off an unstable cwd splits that session across project buckets — each with its own
        # byte-offset cursor — so turns after the drift land in the wrong project, the original
        # cursor stalls, and the status line's per-(project,session) count freezes. Routing through
        # _payload_project (project_dir-first) keeps the whole session in one bucket and, because the
        # status line resolves the same way, keeps the bucket and the displayed count in agreement.
        project = _payload_project(payload, a.project)
        _ingest_transcript(conn, project, payload.get("transcript_path"),
                           payload.get("session_id"), a.include_thinking)
        if _is_strict(a):
            unc = _uncaptured_count(conn, project)
            if unc > 0:
                sys.stderr.write(
                    f"[engrim] {unc} uncaptured decision(s) detected in {project} — "
                    "capture with `engrim add` before stopping\n"
                )
                sys.exit(2)
        return  # silent: this runs from a hook
    project = _resolve_project(a.project)
    if getattr(a, "reindex", False):
        # Re-derive `content` from the preserved `raw` for turns already on disk. Because raw was
        # always kept in full, action lines can be recovered for the ENTIRE history — the feature
        # doesn't start empty on a store with 44k turns behind it. Only ADDS extracted text; a turn
        # whose re-extraction yields nothing keeps whatever it had.
        n, changed = 0, 0
        for r in conn.execute(
                "SELECT id, content, raw FROM log WHERE project = ? AND raw IS NOT NULL",
                (project,)).fetchall():
            n += 1
            try:
                blocks = json.loads(r["raw"]).get("message", {}).get("content")
            except Exception:
                continue
            fresh = _extract_text(blocks, a.include_thinking)
            if fresh and fresh != (r["content"] or ""):
                conn.execute("UPDATE log SET content = ? WHERE id = ?", (fresh, r["id"]))
                changed += 1
        conn.commit()
        print(f"reindexed {n} turn(s) · {changed} gained text -> project={project}")
        return
    if a.from_transcript:
        n = _ingest_transcript(conn, project, a.from_transcript, a.session, a.include_thinking)
        print(f"logged {n} new turn(s) from transcript -> project={project}")
        return
    if not a.role or a.content is None:
        sys.exit("log needs -r ROLE and -c CONTENT (or --hook / --from-transcript)")
    conn.execute(
        "INSERT INTO log(ts,project,session,role,content,msg_uuid) VALUES(?,?,?,?,?,?)",
        (_now(), project, a.session, a.role, a.content, None))
    conn.commit()
    print(f"+ logged [{a.role}] -> project={project}")


def cmd_logs(conn, a) -> None:
    """Browse/search the raw transcript log (kept out of the boot pack on purpose)."""
    project = _resolve_project(a.project)
    if a.query:
        terms = re.findall(r"\w+", a.query, flags=re.UNICODE)
        clause = " AND (" + " OR ".join(["content LIKE ?"] * len(terms)) + ")" if terms else ""
        params = [project] + ["%" + t + "%" for t in terms]
        rows = conn.execute(
            "SELECT ts,role,content FROM log WHERE project=?" + clause +
            " ORDER BY ts DESC LIMIT ?", params + [a.k]).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts,role,content FROM log WHERE project=? ORDER BY ts DESC LIMIT ?",
            (project, a.k)).fetchall()
    if a.json:
        print(json.dumps([dict(r) for r in rows], default=str))
        return
    total = conn.execute("SELECT COUNT(*) FROM log WHERE project=?", (project,)).fetchone()[0]
    print(f"== {len(rows)}/{total} log line(s) · project={project} ==")
    for r in reversed(rows):
        body = (r["content"] or "").replace("\n", " ")
        print(f"  {r['ts'][:16]} [{r['role']:9}] {body[:100]}")


_DECISION_CUES = (
    "decided", "decision", "we chose", "i chose", "we picked", "let's go with",
    "lets go with", "going with", "go with", "we'll use", "we will use", "let's use",
    "lets use", "we should use", "switch to", "switching to", "instead of",
    "the plan is", "we settled on", "settled on", "opt for", "we're using",
    "we are using", "let's do", "lets do", "agreed on", "final call",
)

# Open-loop / next-action cues — distinct from decisions. The recency TAIL honors these too, so a
# mid-task /clear still surfaces where we left off ("pick this up later", "the next step is …",
# "still need to …"). The 'safe to clear?' nudge (_uncaptured_count) deliberately does NOT use them:
# it counts real decisions, not every open loop, or it would nag on ordinary work-in-progress. Phrase-
# based (not bare "next"/"resume") to avoid false hits; agent process-chatter is still dropped by the
# narration filter, which catches "next i'll", "then i'll", etc.
_OPENTASK_CUES = (
    "next step", "next action", "next we", "next up", "the next thing",
    "pick up later", "pick this up", "pick it back up", "pick back up", "pick this back up",
    "where we left off", "left off", "still need to", "to do next", "todo", "to-do",
    "blocked on", "blocking on", "open loop", "remaining work", "we'll continue",
    "continue from here", "resume here", "resume pointer", "we'll pick", "pick up where",
)

# The agent's OWN process/meta narration trips the cue list ("let me close the loop by
# capturing…", "next I'll switch to the tests") — these are workflow chatter, not project
# decisions, and they inflate the "to capture" nudge (#197). A snippet dominated by a marker
# below is treated as narration and dropped from the clear-readiness signal. Kept deliberately
# specific to capture-talk and task-sequencing so it can't swallow a real decision an assistant
# happens to narrate ("I'll use Postgres because…" has no marker and survives).
_NARRATION_MARKERS = (
    "close the loop", "capture this", "capture that", "to capture", "capturing",
    "worth capturing", "add a record", "adding a record", "make a record", "log this",
    "logging this", "engrim add", "let me capture", "let me add a", "i'll add a record",
    "next i'll", "next, i'll", "first i'll", "then i'll", "i'll start by",
    "let me run", "let me check", "let me look", "let me read",
    "safe to clear", "safe to /clear", "before you clear", "before your next",
)

# Exemplar decisions for SEMANTIC candidate recall in `review`. A cue-less but real decision
# ("the free tier caps at 500 records and Pro unlocks the reranker") carries no trigger word, so
# the keyword detector misses it and `review` falsely reports "safe to clear" (#200 — the
# trust-critical failure, since the high-value rationale is exactly what's lost on /clear). When an
# embedder is present, `review` also flags any turn whose sentence reads semantically like one of
# these, biasing toward surfacing over silence (#143).
_DECISION_EXEMPLARS = (
    "We decided to go with this approach instead of the alternative.",
    "The plan is to use this design for the system.",
    "We'll structure the pricing as a free tier plus a paid tier.",
    "We're going to bundle this so it works out of the box.",
    "Let's adopt this convention from now on.",
    "We settled on this architecture.",
    "The final call is to ship it this way.",
)
# Cosine floor for a turn to read as decision-ish against an exemplar. Sits in the same band as the
# captured-check (potion: genuine matches ~0.50+, unrelated ~0.18); recall-leaning per bias-to-flag.
_DECISION_SEM_FLOOR = 0.45

# Captured-check threshold (cosine), calibrated empirically for the default static embedder
# (potion-base-8M): a genuine paraphrase scores ~0.50, an unrelated decision ~0.18. Sit just below
# the paraphrase band so real restatements read as captured while unrelated decisions get flagged.
# It stays a heuristic — the output hedges ("appear to", "glance at anything critical") rather than
# promising safety, and precision improves with the stronger embedder on the roadmap. When torn,
# bias toward flagging (a harmless nudge) over a false "captured" (a silently dropped decision; #143).
_CAPTURED_SIM = 0.45

# Both durable capture caches must expire when the evidence policy changes.
_CAPTURE_CHECK_VERSION = 2


def _capture_candidate_allowed(snippet, summary, detail):
    """Similarity cannot establish the scope of a negation. Require the same statement whenever
    either side contains explicit negation; retain every word and its order for that comparison.
    A missed paraphrase prompts review, whereas a false match silently hides a reversed decision.
    This is deliberately conservative, not a general contradiction/entailment classifier."""
    def words(text):
        return re.findall(r"\b\w+(?:'\w+)*\b", (text or "").casefold().replace("’", "'"))

    tokens = words(snippet)
    fields = (summary or "", detail or "")
    negations = {"not", "no", "never", "neither", "nor", "without", "cannot"}
    if not any(t in negations or t.endswith("n't")
               for t in tokens + words("\n".join(fields))):
        return True
    # Check fields independently so surrounding rationale does not prevent an exact capture.
    return bool(tokens) and any(
        tokens == words(statement)
        for field in fields
        for statement in [field, *re.split(r"(?<=[.!?])\s+|\n+", field)]
    )


def _decision_snippet(text, cues=_DECISION_CUES):
    """Tighten a turn down to the sentence that carried the cue (decisions by default; the recency
    tail passes the wider decision+open-task set so it can quote the open loop it matched)."""
    flat = (text or "").replace("\n", " ")
    for s in re.split(r"(?<=[.!?])\s+", flat):
        if any(cue in s.lower() for cue in cues):
            return s.strip()
    return flat.strip()


def _looks_like_narration(snippet):
    """True if the snippet is the agent's own process/meta chatter rather than a project decision
    (#197). Drops it from the clear-readiness signal so 'to capture' counts real decisions only."""
    s = (snippet or "").lstrip()
    # Action lines are a record of WORK, not a decision to capture. They're searchable via
    # `recall --log`, but they must never inflate the "to capture" nudge — the counter's precision is
    # the whole reason it's trusted (#219, #747).
    if s.startswith(_ACTION_PREFIXES):
        return True
    return any(m in s.lower() for m in _NARRATION_MARKERS)


def _semantic_decision_snippet(content, fn, exemplar_vecs):
    """For SEMANTIC candidate recall in `review`: the sentence in `content` that reads most like a
    decision (vs the exemplars), if it clears the floor. Catches real decisions that carry no cue
    word (#200). Returns the snippet or None. Skips narration and tiny fragments."""
    if not fn or not exemplar_vecs:
        return None
    flat = (content or "").replace("\n", " ")
    best_s, best_score = None, 0.0
    for s in re.split(r"(?<=[.!?])\s+", flat):
        s = s.strip()
        if len(s) < 25 or _looks_like_narration(s):     # too short to embed meaningfully, or chatter
            continue
        v = fn(s)
        score = max(_cosine(v, ev) for ev in exemplar_vecs)
        if score > best_score:
            best_s, best_score = s, score
    return best_s if best_score >= _DECISION_SEM_FLOOR else None


def _max_similarity(conn, project, text, fn):
    """Best capture-eligible cosine (0.0 if none / no backend). Search has its own similarity path."""
    if not fn:
        return 0.0
    rows = conn.execute(
        "SELECT e.vec AS vec, m.summary, m.detail FROM embedding e JOIN memories m ON m.id = e.memory_id "
        "WHERE m.project = ? AND m.status = 'active'", (project,)).fetchall()
    rows = [r for r in rows if _capture_candidate_allowed(text, r["summary"], r["detail"])]
    if not rows:
        return 0.0
    qv = fn(text)
    return max(_cosine(qv, _blob_vec(r["vec"])) for r in rows)


_UNSET = object()   # "caller didn't supply an embedder" — distinct from an explicit None ("lexical only")

# Per-SNIPPET semantic verdict cache. The count-level memo alone isn't enough: its fingerprint includes
# the newest log row, and a new row lands every single turn, so on a project with a live nag the first
# status refresh after each turn paid a full ~1.2s model load. A verdict about "is THIS snippet already
# curated?" only goes stale when the CURATED side changes — new log turns are irrelevant to it. So this
# is keyed on curated state alone, which means the embedder loads once per genuinely new decision, not
# once per turn. Bounded and stored as one small meta row; a curation change drops the whole map.
_SEM_VERDICT_CAP = 96


def _curated_state_key(conn, project):
    """Fingerprint of the CURATED side only — what a captured-verdict actually depends on."""
    row = conn.execute(
        "SELECT MAX(ts), COUNT(*) FROM memories WHERE project=? AND status='active'",
        (project,)).fetchone()
    return f"{_CAPTURE_CHECK_VERSION}|{row[0]}|{row[1]}|{os.environ.get('ENGRIM_EMBED', '').strip().lower()}"


def _snippet_key(snippet):
    return hashlib.sha1((snippet or "").lower().encode("utf-8", "replace")).hexdigest()[:16]


def _semantic_verdict_get(conn, project, snippet):
    """(verdict, state) — verdict is None on a miss. `state` is handed back so the caller can store
    under the same fingerprint it read, without recomputing it."""
    try:
        state = _curated_state_key(conn, project)
    except Exception:
        return None, None
    try:
        blob = _meta_get(conn, project, "cap_cache")
        if blob:
            d = json.loads(blob)
            if d.get("k") == state:
                v = d.get("v", {}).get(_snippet_key(snippet))
                if v is not None:
                    return bool(v), state
    except Exception:
        pass
    return None, state


def _semantic_verdict_put(conn, project, snippet, verdict, state):
    if not state:
        return
    try:
        blob = _meta_get(conn, project, "cap_cache")
        d = json.loads(blob) if blob else {}
        v = d.get("v", {}) if d.get("k") == state else {}     # curation moved -> start clean
        if len(v) >= _SEM_VERDICT_CAP:
            v = {}                                            # bounded; cheap to refill
        v[_snippet_key(snippet)] = 1 if verdict else 0
        _meta_set(conn, project, "cap_cache", json.dumps({"k": state, "v": v}))
    except Exception:
        pass                                                  # a read-only db must never break the bar


def _is_captured(conn, project, snippet, fn=_UNSET):
    """THE definition of "this decision is already in curated memory". Every clear-readiness surface
    (status bar, minder nudge, boot tail, `review`) must route through here — they used to each pick
    their own check, and on a host WITH an embedder that split into two different answers: `review`
    scored a paraphrase as captured (cosine >= _CAPTURED_SIM) while the bar, hard-wired to the lexical
    check, kept counting it forever. Curating a decision in your own words then never cleared the
    nudge, so the counter looked stale and cried wolf — the exact trust the clear-safe signal is for
    (#219, #747).

    Evidence is a UNION: strong word overlap OR semantic match, but only among capture-eligible
    records. Explicit negation requires a matching statement in either tier: similarity alone
    cannot distinguish a decision from its opposite. A lexical hit stays a hit when the embedder
    is unavailable on one call and present on the next.

    Tiered on purpose: the lexical pass is free and runs first, so a project that's already clear-safe
    never pays for a model load. Only a snippet that lexical would NAG about escalates to the embedder
    — cost lands exactly where it buys precision. Pass `fn` if you've already resolved an embedder;
    pass None to force pure-lexical."""
    if _lexical_overlap_captured(conn, project, snippet):
        return True
    # Consult the verdict cache BEFORE resolving an embedder — resolving is what costs ~1s, so a
    # lookup after it would save nothing.
    cached, state = _semantic_verdict_get(conn, project, snippet)
    if cached is not None:
        return cached
    if fn is _UNSET:                    # not supplied -> resolve lazily (cached per process)
        fn, _ = _resolve_embedder()
    if not fn:
        return False
    verdict = _max_similarity(conn, project, snippet, fn) >= _CAPTURED_SIM
    _semantic_verdict_put(conn, project, snippet, verdict, state)
    return verdict


def _lexical_overlap_captured(conn, project, snippet):
    """Lexical tier of the captured-check (see `_is_captured`): does any active record share most of
    the snippet's content words? Conservative on purpose."""
    toks = set(re.findall(r"[a-z0-9]{4,}", snippet.lower()))
    if not toks:
        return False
    for r in conn.execute(
            "SELECT summary, detail FROM memories WHERE project = ? AND status = 'active'",
            (project,)).fetchall():
        if not _capture_candidate_allowed(snippet, r["summary"], r["detail"]):
            continue
        rt = set(re.findall(r"[a-z0-9]{4,}", ((r["summary"] or "") + " " + (r["detail"] or "")).lower()))
        if rt and len(toks & rt) / len(toks) >= 0.6:
            return True
    return False


def cmd_review(conn, a) -> None:
    """Coverage check before a /clear: surface recent decisions from the transcript log that don't
    appear to be in curated memory yet, so nothing important is lost when you clear. Heuristic and
    deliberately honest — it flags candidates for you (or your agent) to confirm, and never claims a
    'safe' it cannot verify (a false 'captured' would silently drop a decision; see #143)."""
    project = _resolve_project(a.project)
    total_log = conn.execute("SELECT COUNT(*) FROM log WHERE project = ?", (project,)).fetchone()[0]
    curated = conn.execute("SELECT COUNT(*) FROM memories WHERE project = ? AND status = 'active'",
                           (project,)).fetchone()[0]
    print(f"review · project={project}")
    if not total_log:
        print("  no transcript log yet — nothing to check. "
              "(the Stop hook captures turns as you work, then `review` can vet them.)")
        return
    # Same hybrid window the automatic surfaces use: the lean recent `-k` turns, extended back to the
    # last capture so a decision buried under a long verification tail is still in scope. A flat
    # last-k window here was half of why the status bar and `review` disagreed — the bar could count a
    # turn `review` never looked at (#747).
    floor = _capture_floor(conn, project)
    scanned = conn.execute(
        "SELECT ts, content FROM log WHERE project = ? ORDER BY ts DESC LIMIT ?",
        (project, max(a.k, _UNCAPTURED_MAX_SCAN))).fetchall()
    rows = []
    for i, r in enumerate(scanned):
        if _past_floor(r, floor, i, a.k):
            break
        rows.append(r)
    print(f"  log: {total_log} turns (scanned last {len(rows)}) · curated: {curated} active records")

    # Resolve the embedder up front: it raises candidate RECALL (cue-less real decisions, #200) and
    # powers the captured-check below. Candidate selection is keyword OR semantic, minus narration.
    fn, _name = _resolve_embedder()
    exemplar_vecs = [fn(e) for e in _DECISION_EXEMPLARS] if fn else []

    seen, candidates = set(), []
    for r in rows:
        snip = None
        if any(cue in (r["content"] or "").lower() for cue in _DECISION_CUES):
            snip = _decision_snippet(r["content"])
            if _looks_like_narration(snip):          # agent's own process chatter, not a decision (#197)
                snip = None
        if snip is None and fn:                      # no cue word — does it READ like a decision? (#200)
            snip = _semantic_decision_snippet(r["content"], fn, exemplar_vecs)
        if not snip:
            continue
        key = snip.lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        candidates.append((r["ts"], snip))
    if not candidates:
        print("  no decision-signal language in the scanned turns — nothing obvious to capture. "
              "(heuristic, not proof: eyeball anything you know was important.)")
        return

    # Shared captured-check — `fn` is passed explicitly (possibly None) so review uses exactly the
    # embedder it resolved above, and the bar's lazy resolution can't diverge from it.
    uncaptured = [(ts, snip) for ts, snip in candidates if not _is_captured(conn, project, snip, fn)]
    print(f"  {len(candidates)} decision-signal turn(s) detected; "
          f"{len(candidates) - len(uncaptured)} look captured, {len(uncaptured)} may not be.")
    if not uncaptured:
        print("\n✓ recent decisions appear to be in curated memory — looks safe to clear. "
              "(capture-check is heuristic; glance at anything critical first.)")
        return
    print("\n⚠ these recent decisions don't clearly appear in curated memory — capture before you clear?\n")
    for ts, snip in uncaptured:
        print(f"  · [{ts[:16]}] {snip[:160]}")
    print("\n  capture with:  engrim add -t decision -s \"…\"   (your agent can do this for you)")
    if _is_strict(a):
        sys.stderr.write(
            f"[engrim] {len(uncaptured)} uncaptured decision(s) detected in {project} — "
            "capture with `engrim add` before clearing\n"
        )
        sys.exit(2)


def cmd_prune(conn, a) -> None:
    """Purge old records from the transcript log table and VACUUM the database to reclaim disk space.

    Pruning is off by default to protect transcript history (SQLite files are small and
    transcripts are valuable audit history). Users can explicitly pass `--keep-days <N>`,
    set `$ENGRIM_PRUNE_KEEP_DAYS`, pass `--all`, or pass `--vacuum`.
    """
    keep_days = a.keep_days
    if keep_days is None and not a.all and not getattr(a, "vacuum", False):
        env_val = os.environ.get("ENGRIM_PRUNE_KEEP_DAYS")
        if env_val:
            try:
                keep_days = int(env_val)
            except ValueError:
                pass

    if getattr(a, "vacuum", False) and keep_days is None and not a.all:
        conn.execute("VACUUM")
        print("database vacuumed (no logs purged)")
        return

    if keep_days is None and not a.all:
        sys.exit(
            "engrim prune: pruning is off by default to prevent accidental data loss.\n"
            "Specify --keep-days <N> (e.g. --keep-days 90) or set $ENGRIM_PRUNE_KEEP_DAYS to purge logs,\n"
            "or pass --all to purge all transcript logs.\n"
            "To reclaim fragmented disk space without purging any logs, use: engrim prune --vacuum"
        )

    if keep_days is not None and keep_days < 0:
        sys.exit("--keep-days must be non-negative")

    if a.all and keep_days is None:
        cutoff_clause = "1=1"
        cutoff_params: list[str] = []
        days_label = "all"
    else:
        days = keep_days if keep_days is not None else 0
        cutoff_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
        cutoff_iso = cutoff_dt.isoformat()
        cutoff_clause = "(datetime(ts) < datetime(?) OR (datetime(ts) IS NULL AND ts < ?))"
        cutoff_params = [cutoff_iso, cutoff_iso]
        days_label = f"older than {days} day(s)"

    if a.all or (a.project and a.project.lower() == "all"):
        scope_clause = ""
        scope_params: list[str] = []
        scope_label = "all projects"
    else:
        project = _resolve_project(a.project)
        scope_clause = "project = ? AND "
        scope_params = [project]
        scope_label = f"project={project}"

    where_clause = f"WHERE {scope_clause}{cutoff_clause}"
    params = scope_params + cutoff_params

    count_sql = f"SELECT COUNT(*) FROM log {where_clause}"
    to_delete = conn.execute(count_sql, params).fetchone()[0]

    if getattr(a, "dry_run", False):
        print(f"prune · {to_delete} log row(s) {days_label} would be purged "
              f"(dry run: no changes written) · {scope_label}")
        return

    del_sql = f"DELETE FROM log {where_clause}"
    conn.execute(del_sql, params)
    conn.commit()
    conn.execute("VACUUM")
    print(f"pruned {to_delete} log row(s) {days_label} · database vacuumed · {scope_label}")


def cmd_embed(conn, a) -> None:
    """Backfill embeddings for a project's active records. `add` already auto-embeds new records, so
    this is mainly for re-embedding after a model change (`--force`) or seeding a store that predates
    the semantic tier. No-op (with a hint) if no backend is available; skips records already embedded
    with the current model unless --force."""
    project = _resolve_project(a.project)
    fn, name = _resolve_embedder()
    if not fn:
        print("embed: semantic recall is off (ENGRIM_EMBED=off, or model2vec unavailable) — "
              "the minder stays lexical until a backend is available")
        return
    rows = conn.execute(
        "SELECT id, summary, detail FROM memories WHERE project = ? AND status = 'active'",
        (project,)).fetchall()
    done = 0
    for r in rows:
        if not a.force:
            ex = conn.execute("SELECT model FROM embedding WHERE memory_id = ?", (r["id"],)).fetchone()
            if ex and ex[0] == name:
                continue
        _embed_row(conn, r["id"], r["summary"], r["detail"], fn, name)
        done += 1
    conn.commit()
    print(f"embedded {done} record(s) with {name} -> project={project}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="engrim",
        description="Project-scoped, cross-session memory for AI coding agents.",
        epilog=(
            "environment:\n"
            "  ENGRIM_DB       path to the SQLite store (default ~/.engrim/memory.db)\n"
            "  ENGRIM_PROJECT  stable project tag — set this to share one project's memory\n"
            "                  across host + Docker containers (host path != container path)\n\n"
            "project-tag precedence:  --project  >  $ENGRIM_PROJECT  >  git root of cwd  >  cwd"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--db", default=os.environ.get("ENGRIM_DB", DEFAULT_DB),
                   help="SQLite store path (or set $ENGRIM_DB)")
    sub = p.add_subparsers(dest="cmd")

    pa = sub.add_parser("add")
    pa.add_argument("-p", "--project", default="auto")
    pa.add_argument("-g", "--global", dest="globl", action="store_true",
                    help="write to the global user-layer that co-loads in EVERY project "
                         "(who you are / how you work), instead of this project")
    pa.add_argument("-t", "--type", required=True)
    pa.add_argument("-s", "--summary", required=True)
    pa.add_argument("-d", "--detail")
    pa.add_argument("--status", default="active")
    pa.add_argument("--tags")
    pa.add_argument("--links")
    pa.add_argument("--source")
    pa.add_argument("--origin-agent", "--agent", dest="origin_agent",
                    choices=ORIGIN_AGENTS,
                    help="Origin agent for provenance tracking (default: cli)")
    pa.set_defaults(func=cmd_add)

    pr = sub.add_parser("recall")
    pr.add_argument("-p", "--project", default="auto")
    pr.add_argument("-q", "--query")
    pr.add_argument("-t", "--type")
    pr.add_argument("--tag", "--tags", dest="tag",
                    help="filter memories by tag (e.g. --tag auth)")
    pr.add_argument("-k", type=int, default=8)
    pr.add_argument("--detail", action="store_true")
    pr.add_argument("--include-stale", action="store_true")
    pr.add_argument("--json", action="store_true")
    pr.add_argument("--log", action="store_true",
                    help="also search the transcript log (prose + state-changing actions). Opt-in: "
                         "the log never auto-loads into context, but it's searchable on demand")
    pr.set_defaults(func=cmd_recall)

    pl = sub.add_parser("list")
    pl.add_argument("-p", "--project", default="auto")
    pl.add_argument("-t", "--type")
    pl.add_argument("--tag", "--tags", dest="tag",
                    help="filter memories by tag (e.g. --tag auth)")
    pl.add_argument("-k", type=int, default=20)
    pl.add_argument("--detail", action="store_true")
    pl.add_argument("--include-stale", action="store_true")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)

    pc = sub.add_parser("context")
    pc.add_argument("-p", "--project", default="auto")
    pc.add_argument("-b", "--budget", type=int, default=4000)
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(func=cmd_context)

    ph = sub.add_parser("hook", help="Lifecycle hook JSON for Claude Code / Antigravity / Codex")
    ph.add_argument("-p", "--project", default="auto")
    ph.add_argument("-b", "--budget", type=int, default=4000)
    ph.add_argument("--no-sync", action="store_true",
                    help="don't mirror Claude Code's file-memory before injecting")
    ph.add_argument("--agent", choices=["claude", "agy", "antigravity", "codex"], default="claude",
                     help="Target agent environment (default: claude)")
    ph.add_argument("--event", choices=["boot", "stop", "sessionstart"], default=None,
                    help="Hook lifecycle event (default: boot or sessionstart)")
    ph.add_argument("--strict", "--gate", dest="strict", action="store_true",
                    help="gate mode: exit with code 2 if uncaptured decisions detected on stop")
    ph.set_defaults(func=cmd_hook)

    ps = sub.add_parser("supersede")
    ps.add_argument("--id", type=int, required=True)
    ps.add_argument("--status", default="superseded")
    ps.set_defaults(func=cmd_supersede)

    prt = sub.add_parser("retire", help="mark the active resume-pointer(s) done (this project, or --all)")
    prt.add_argument("-p", "--project", default="auto",
                     help="scope to a project (default: auto; use --all for every project)")
    prt.add_argument("--all", action="store_true", help="retire pointers across all projects")
    prt.add_argument("--dry-run", action="store_true", help="list what would be retired, write nothing")
    prt.add_argument("--json", action="store_true")
    prt.set_defaults(func=cmd_retire)

    pse = sub.add_parser("setup", help="wire engrim into agent environments (Antigravity, Claude, Cursor, Codex)")
    pse.add_argument("--agy", "--antigravity", dest="agy", action="store_true",
                     help="wire Antigravity hooks, deploy skill, and register MCP server")
    pse.add_argument("--claude", dest="claude", action="store_true",
                     help="wire Claude Code SessionStart/Stop hooks and CLAUDE.md")
    pse.add_argument("--strict", "--gate", dest="strict", action="store_true",
                     help="configure Stop hook in strict gate mode (exit 2 on uncommitted decisions)")
    pse.add_argument("--cursor", dest="cursor", action="store_true",
                     help="add engrim MCP entry to Cursor mcp.json")
    pse.add_argument("--codex", dest="codex", action="store_true",
                     help="wire Codex CLI command hooks (MCP is optional and not required)")
    pse.add_argument("--all", dest="all", action="store_true",
                     help="configure all detected agent environments")
    pse.add_argument("--dry-run", action="store_true",
                     help="display planned configurations without modifying disk")
    pse.add_argument("--settings", help="path to settings.json (default ~/.claude/settings.json)")
    
    pse.add_argument("--no-claude-md", action="store_true", help="don't touch ~/.claude/CLAUDE.md")
    pse.set_defaults(func=cmd_setup)

    pun = sub.add_parser("uninstall", help="remove engrim from agent environments (Antigravity, Claude, Cursor, Codex)")
    pun.add_argument("--agy", "--antigravity", dest="agy", action="store_true",
                     help="remove Antigravity hooks, skill, and MCP server")
    pun.add_argument("--claude", dest="claude", action="store_true",
                     help="remove Claude Code hooks and statusLine")
    pun.add_argument("--cursor", dest="cursor", action="store_true",
                     help="remove engrim MCP entry from Cursor mcp.json")
    pun.add_argument("--codex", dest="codex", action="store_true",
                     help="remove Codex CLI hooks and MCP")
    pun.add_argument("--all", dest="all", action="store_true",
                     help="remove from all detected agent environments")
    pun.add_argument("--dry-run", action="store_true",
                     help="display planned configurations without modifying disk")
    pun.add_argument("--settings", help="path to settings.json (default ~/.claude/settings.json)")
    pun.set_defaults(func=cmd_uninstall)


    pi = sub.add_parser("import")
    pi.add_argument("path", help="a markdown file or a directory tree to import (one record per file)")
    pi.add_argument("-p", "--project", default="auto")
    pi.add_argument("--exclude", help="regex; skip files whose basename matches (e.g. 'INDEX|README')")
    pi.set_defaults(func=cmd_import)

    psy = sub.add_parser("sync")
    psy.add_argument("path", nargs="?",
                     help="active markdown memory directory to mirror into engrim")
    psy.add_argument("--claude", action="store_true",
                     help="auto-target Claude Code's per-project memory dir (no path needed); seed-once")
    psy.add_argument("--force", action="store_true",
                     help="re-run the md->store build even if this project was already seeded")
    psy.add_argument("-p", "--project", default="auto")
    psy.add_argument("--hub", help="hub/index file whose inline sections also become records (default MEMORY.md)")
    psy.add_argument("--exclude", help="regex; skip topic files whose basename matches")
    psy.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    psy.add_argument("--no-prune", action="store_true",
                     help="keep records whose md source vanished (default: supersede them)")
    psy.add_argument("--verbose", action="store_true", help="list every add/update/skip/prune")
    psy.set_defaults(func=cmd_sync)

    pmg = sub.add_parser("merge", help="fold another engrim store's records into this one (idempotent)")
    pmg.add_argument("other", help="path to the other engrim store (opened read-only)")
    pmg.add_argument("-p", "--project", default=None,
                     help="only this project tag (default: every project in the other store)")
    pmg.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    pmg.add_argument("--verbose", action="store_true", help="list every add/status/skip")
    pmg.set_defaults(func=cmd_merge)

    pbk = sub.add_parser("backup", help="write a consistent copy of the store (safe while it's in use)")
    pbk.add_argument("dest", help="path for the copy (a complete engrim store)")
    pbk.add_argument("--force", action="store_true", help="overwrite an existing file at DEST")
    pbk.add_argument("--json", action="store_true")
    pbk.set_defaults(func=cmd_backup)

    plog = sub.add_parser("log")
    plog.add_argument("-p", "--project", default="auto")
    plog.add_argument("-r", "--role", help="user|assistant (for a manual single-turn append)")
    plog.add_argument("-c", "--content", help="content for a manual single-turn append")
    plog.add_argument("--session", help="session id to tag the turn(s) with")
    plog.add_argument("--from-transcript", help="ingest new turns from a Claude Code transcript JSONL")
    plog.add_argument("--hook", action="store_true",
                      help="read a Stop-hook JSON from stdin and ingest the session's new turns")
    plog.add_argument("--agent", choices=["claude", "codex"], default="claude",
                      help="hook payload source (default: claude)")
    plog.add_argument("--reindex", action="store_true",
                      help="re-derive searchable text from the raw turns already stored (recovers "
                           "action lines for history logged before they existed)")
    plog.add_argument("--include-thinking", action="store_true",
                      help="also log assistant 'thinking' blocks (off by default; large + internal)")
    plog.add_argument("--strict", "--gate", dest="strict", action="store_true",
                      help="gate mode: exit with code 2 if uncaptured decisions detected on stop")
    plog.set_defaults(func=cmd_log)

    pas = sub.add_parser("assist")
    pas.add_argument("-p", "--project", default="auto")
    pas.add_argument("-k", type=int, default=5, help="max records to inject (default 5)")
    pas.add_argument("-b", "--budget", type=int, default=600,
                     help="char budget for the injected slice (default 600 ≈ ~150 tokens)")
    pas.set_defaults(func=cmd_assist)

    psl = sub.add_parser("statusline", help="one-line ambient engrim status for a host status bar")
    psl.add_argument("-p", "--project", default="auto")
    psl.set_defaults(func=cmd_statusline)

    pe = sub.add_parser("embed")
    pe.add_argument("-p", "--project", default="auto")
    pe.add_argument("--force", action="store_true",
                    help="re-embed records already embedded with the current model")
    pe.set_defaults(func=cmd_embed)

    plogs = sub.add_parser("logs")
    plogs.add_argument("-p", "--project", default="auto")
    plogs.add_argument("-q", "--query", help="substring filter over logged content")
    plogs.add_argument("-k", type=int, default=20)
    plogs.add_argument("--json", action="store_true")
    plogs.set_defaults(func=cmd_logs)

    prv = sub.add_parser("review")
    prv.add_argument("-p", "--project", default="auto")
    prv.add_argument("-k", type=int, default=_CAPTURE_SCAN,
                     help=f"lean recent window of log turns to scan (default {_CAPTURE_SCAN}); the scan "
                          "always extends back to the last capture on top of this")
    prv.add_argument("--strict", "--gate", dest="strict", action="store_true",
                     help="gate mode: exit with code 2 if uncaptured decisions detected (blocks clear/stop)")
    prv.set_defaults(func=cmd_review)

    ppr = sub.add_parser("prune", help="purge old transcript logs and VACUUM the database (opt-in)")
    ppr.add_argument("-p", "--project", default="auto",
                     help="scope pruning to a project (default: auto; use --all for all projects)")
    ppr.add_argument("--all", action="store_true", help="prune logs across all projects (or all logs if no --keep-days)")
    ppr.add_argument("--keep-days", type=int, default=None,
                     help="retention window in days (e.g. --keep-days 30; older logs are purged; off by default)")
    ppr.add_argument("--vacuum", action="store_true",
                     help="reclaim disk space via VACUUM without purging any logs")
    ppr.add_argument("--dry-run", action="store_true",
                     help="display how many rows would be purged without modifying the database")
    ppr.set_defaults(func=cmd_prune)

    ppj = sub.add_parser("project", help="records, active and last write for a project tag")
    scope = ppj.add_mutually_exclusive_group()
    scope.add_argument("-p", "--project", default="auto",
                       help="one project tag (default: auto, the current directory's)")
    scope.add_argument("-g", "--global", dest="globl", action="store_true",
                       help="the global user-layer only")
    scope.add_argument("--all", action="store_true", help="every project in the store")
    ppj.add_argument("--json", action="store_true")
    ppj.set_defaults(func=cmd_project)

    ppjs = sub.add_parser("projects", help="every project's records, active and last write (= project --all)")
    ppjs.add_argument("--json", action="store_true")
    ppjs.set_defaults(func=cmd_project, project=None, globl=False, all=True)

    pst = sub.add_parser("stats")
    pst.add_argument("-p", "--project", default="auto")
    pst.add_argument("-b", "--budget", type=int, default=4000)
    pst.set_defaults(func=cmd_stats)

    pmcp = sub.add_parser("mcp", help="run engrim as an MCP server (stdio) for clients like Claude Code")
    pmcp.set_defaults(func=cmd_mcp)

    psv = sub.add_parser("serve", help="serve engrim over stdio (e.g. --mcp)")
    psv.add_argument("--mcp", action="store_true", default=True, help="run engrim as an MCP server over stdio")
    psv.set_defaults(func=cmd_serve)
    return p


def cmd_mcp(conn, a) -> None:
    """Run engrim as an MCP server over stdio (for MCP clients like Claude Code)."""
    from engrim.mcp_server import serve
    serve(conn)


def cmd_serve(conn, a) -> None:
    """Run engrim as an MCP server over stdio."""
    from engrim.mcp_server import serve
    serve(conn)


def main(argv=None) -> None:
    # Windows defaults the std streams to cp1252 the moment they aren't a console — and Claude Code
    # drives every hook and the status line through pipes. Two live failures came out of that:
    # `statusline`/`context`/`stats` died with UnicodeEncodeError on the bar's leading 🧠, and stdin
    # decoding broke the hooks that read a JSON payload (any emoji or smart quote in a prompt).
    # Both look fine in a terminal, which is what makes them easy to ship. UTF-8 everywhere, and
    # errors="replace" so an odd byte degrades to a glyph instead of taking the command down.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass                       # not a TextIOWrapper (captured/redirected in-process) — fine
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        if not sys.stdin.isatty():
            # Automated stdio launcher (e.g. MCP proxy/runner like Glama, Cursor, Windsurf)
            # running `engrim` without arguments: default directly to stdio MCP server.
            from engrim.mcp_server import serve
            conn = connect(args.db)
            try:
                serve(conn)
            finally:
                conn.close()
            return
        parser.print_help()
        sys.exit(0)
    if getattr(args, "k", None) is not None:
        args.k = max(0, args.k)        # a negative LIMIT would dump the whole store
    if getattr(args, "budget", None) is not None:
        args.budget = max(0, args.budget)
    conn = connect(args.db)
    try:
        args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
