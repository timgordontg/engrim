"""
engrim — project-scoped, cross-session memory for AI coding agents.

Write cheaply, recall a relevant slice on demand. Context comes from retrieval, not from
stuffing everything into the window. One SQLite file, project-tagged, hybrid keyword + semantic
recall. Works with Claude Code via a SessionStart hook (unofficial; not
affiliated with or endorsed by Anthropic).

CLI:
  add       insert a memory          engrim add -t decision -s "..." [-d "..."] [--tags a,b] [--global]
  recall    ranked relevant slice    engrim recall -q "rl reward" [-k 8] [--detail]
  context   session-boot pack        engrim context [-b 4000]
  hook      SessionStart JSON         engrim hook            (used by the hook; self-scopes to cwd)
  setup     wire the hook + notes     engrim setup           (white-glove one-shot install)
  list      recent for a project     engrim list [-k 20]
  supersede mark status by id        engrim supersede --id 12 --status superseded
  projects  list tags + counts       engrim projects
  stats     row/health summary       engrim stats

Every record is tagged by `project` (a folder path). `--project auto` (the default) derives it
from the current directory, so one store serves many projects cleanly.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import sqlite3
import sys

DEFAULT_DB = os.path.expanduser("~/.engrim/memory.db")
TYPES = ("decision", "fact", "feedback", "state", "reference", "user")
STATUSES = ("active", "superseded", "done")
# priority for the session-boot pack: how-to-work-with-user first, then state, then the rest
_PRIO = {"user": 0, "feedback": 1, "state": 2, "decision": 3, "fact": 4, "reference": 5}


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


_PROJECT_MARKERS = (".git", ".hg", ".svn")


def _git_root(start: str):
    """Walk up from `start` to the nearest repo root (.git/.hg/.svn). None if not in a repo."""
    cur = os.path.abspath(start)
    while True:
        if any(os.path.exists(os.path.join(cur, m)) for m in _PROJECT_MARKERS):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _resolve_project(p):
    """Project tag precedence: explicit -p  >  $ENGRIM_PROJECT  >  git root of cwd  >  cwd.

    $ENGRIM_PROJECT gives a stable tag across machines/containers (host path != container path);
    git-root makes the tag the same no matter which subdirectory you launch from.
    """
    if p and p != "auto":
        return p
    env = os.environ.get("ENGRIM_PROJECT") or os.environ.get("CLAUDE_PROJECT_TAG")
    if env:
        return env
    return _git_root(os.getcwd()) or os.getcwd()


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
            status TEXT NOT NULL DEFAULT 'active', tags TEXT, links TEXT, source TEXT
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
               tags=None, links=None, source=None) -> int:
    """Insert one memory record and best-effort auto-embed it; return the new id.

    The shared write core behind both the `add` CLI command and the MCP server, so a
    record created either way is identical and immediately searchable by meaning.
    `tags`/`links` are lists (already normalized by the caller)."""
    cur = conn.execute(
        "INSERT INTO memories(ts,project,type,summary,detail,status,tags,links,source) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (_now(), project, type, summary, detail, status,
         json.dumps(tags or []), json.dumps(links or []), source),
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
    new_id = add_memory(conn, project=project, type=a.type, summary=a.summary,
                        detail=a.detail, status=a.status,
                        tags=_csv(a.tags), links=_csv(a.links), source=a.source)
    shown = "global · loads in every project" if project == GLOBAL_PROJECT else project
    print(f"+ #{new_id} [{a.type}] {shown}\n  {a.summary}")


def _row_line(r, detail: bool) -> str:
    tags = ", ".join(json.loads(r["tags"] or "[]"))
    head = f"#{r['id']} [{r['type']}/{r['status']}] {r['ts'][:16]}  {r['summary']}"
    if tags:
        head += f"   ({tags})"
    if detail and r["detail"]:
        head += "\n    " + r["detail"].replace("\n", "\n    ")
    return head


def _recall_rows(conn, project, query, k, type_=None, include_stale=False):
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
    sql += "ORDER BY ts DESC LIMIT ?"
    params.append(k)
    return conn.execute(sql, params).fetchall()


def cmd_recall(conn, a) -> None:
    project = _resolve_project(a.project)
    # Hybrid (bm25 + semantic) for a real free-text query — same fusion the minder uses, so a manual
    # `recall` understands meaning too. It degrades to pure lexical when semantic is off, so behavior
    # is unchanged without a backend. Type/stale filters use the precise lexical path (the fusion path
    # is active-only and unfiltered by design).
    if a.query and not a.type and not a.include_stale:
        rows = _minder_rows(conn, project, a.query, a.query, a.k)
    else:
        rows = _recall_rows(conn, project, a.query, a.k, a.type, a.include_stale)

    if a.json:
        clean = [{k: v for k, v in dict(r).items() if k not in ("rank", "_vec")} for r in rows]
        print(json.dumps(clean, default=str))
        return
    if not rows:
        print(f"(no memories for project={project}"
              + (f" matching {a.query!r}" if a.query else "") + ")")
        return
    print(f"== {len(rows)} memr(s) · project={project}"
          + (f" · q={a.query!r}" if a.query else "") + " ==")
    for r in rows:
        print(_row_line(r, a.detail))


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
    try:
        prompt = (json.load(sys.stdin) or {}).get("prompt") or ""
    except Exception:
        return emit("")
    terms = _content_terms(prompt)
    if len(terms) < 2:               # too little signal to be worth any tokens
        return emit("")
    project = _resolve_project(a.project)
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
    if not out:
        return emit("")                                       # nothing relevant -> spend nothing
    try:    # record the pull so the ambient status line can show "N in play" — out-of-band, never in chat
        _meta_set(conn, project, "minder_n", str(len(out)))
        _meta_set(conn, project, "minder_ts", _now())
    except Exception:
        pass
    emit("Possibly-relevant project memory (engrim), pulled for this message — use if helpful:\n"
         + "\n".join(out))


def cmd_statusline(conn, a) -> None:
    """Ambient status line for Claude Code (settings.json `statusLine`). Prints ONE compact line so the
    user can SEE engrim is live and working for this project — in the status bar, never in the chat. That
    answers "what did I just install / is this thing even doing anything?" without muddying the exchange
    or nagging a security-aware user. Reads Claude Code's session JSON on stdin (for the workspace dir),
    falls back to cwd. Fast + model-free: it only counts rows + reads meta, so it's safe to run on every
    status refresh."""
    cwd = sess = None
    try:
        data = json.load(sys.stdin) or {}
        cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd")
        sess = data.get("session_id") or data.get("sessionId")
    except Exception:
        pass
    if cwd:
        try:
            os.chdir(cwd)
        except Exception:
            pass
    project = _resolve_project(a.project)
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


def _boot_pack(rows, budget):
    """Build the session-boot slice under a char budget, FAIRLY across types so a flood of one type
    (e.g. dozens of feedback records) can't starve recent decisions/facts. Returns [(row, summary)]
    with long summaries truncated, plus the total char cost. Shared by `context` (display) and `stats`
    (economics) so the reported cost is exactly the pack that loads."""
    by_type = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    for t in by_type:
        by_type[t].sort(key=lambda r: r["ts"], reverse=True)   # recent-first within a type
    order = sorted(by_type, key=lambda t: _PRIO.get(t, 9))      # priority order across types
    idx = {t: 0 for t in order}
    picked, used, progressed = [], 0, True
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
# LEXICAL captured-check (no embedder) to keep every boot fast and model-load-free; the explicitly-run
# `review` stays embedder-precise. Biases toward showing recent work (continuity) over hiding it.
_TAIL_SCAN = 40            # recent log turns to scan for decision signal
_TAIL_MAX_ITEMS = 3        # hard cap on tail lines — recency hint, not a transcript dump
_TAIL_BUDGET = 600         # own char sub-budget, independent of the curated boot budget
_TAIL_SNIPPET_CAP = 180    # per-line truncation


def _recent_tail(conn, project, scan=_TAIL_SCAN, max_items=_TAIL_MAX_ITEMS, budget=_TAIL_BUDGET):
    """Recent uncaptured decision-signal turns from the log, newest-first, under a hard item + char cap.
    Reuses review's detector; dedups against curated memory lexically so the boot stays fast."""
    rows = conn.execute(
        "SELECT ts, content FROM log WHERE project = ? ORDER BY ts DESC LIMIT ?",
        (project, scan)).fetchall()
    seen, out, used = set(), [], 0
    for r in rows:
        content = r["content"] or ""
        if not any(cue in content.lower() for cue in _DECISION_CUES):
            continue
        snip = _decision_snippet(content)
        key = snip.lower()[:80]
        if not snip or key in seen or _looks_like_narration(snip):
            continue
        seen.add(key)
        if _lexical_overlap_captured(conn, project, snip):     # already covered by a curated record
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


def _uncaptured_count(conn, project, scan=25, cap=9):
    """Cheap, model-free count of recent decision-signal log turns not yet covered by a curated record —
    the same 'safe to clear?' signal `review` reports, kept light enough for the ambient status bar (a
    small bounded log scan + lexical capture-check, no embedder). Powers the live 'N to capture' nudge."""
    try:
        rows = conn.execute("SELECT content FROM log WHERE project=? ORDER BY ts DESC LIMIT ?",
                            (project, scan)).fetchall()
    except Exception:
        return 0
    seen, n = set(), 0
    for r in rows:
        content = r["content"] or ""
        if not any(cue in content.lower() for cue in _DECISION_CUES):
            continue
        snip = _decision_snippet(content)
        key = snip.lower()[:80]
        if not snip or key in seen or _looks_like_narration(snip):
            continue
        seen.add(key)
        if _lexical_overlap_captured(conn, project, snip):
            continue
        n += 1
        if n >= cap:
            break
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
        picked.sort(key=lambda rs: _PRIO.get(rs[0]["type"], 9))     # group by type for display (recent-first kept)
        cur = None
        for r, summ in picked:
            if r["type"] != cur:
                cur = r["type"]
                print(f"\n[{cur.upper()}]")
            tags = ", ".join(json.loads(r["tags"] or "[]"))
            gtag = "  · global" if r["project"] == GLOBAL_PROJECT else ""   # rides along in every project
            print(f"- #{r['id']} {summ}" + (f"  ({tags})" if tags else "") + gtag)
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
    if conn.execute("SELECT 1 FROM log WHERE project = ? LIMIT 1", (project,)).fetchone():
        unc = _uncaptured_count(conn, project)
        if unc:
            print(f"\n✎ {unc} recent decision(s) not yet curated — worth capturing before your next "
                  "/clear:  engrim add -t decision -s \"…\"")
        else:
            print("\n✓ recent decisions look captured — safe to /clear.")


def cmd_hook(conn, a) -> None:
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


def cmd_projects(conn, a) -> None:
    rows = conn.execute(
        "SELECT project, COUNT(*) n, SUM(status='active') active, MAX(ts) last "
        "FROM memories GROUP BY project ORDER BY last DESC"
    ).fetchall()
    for r in rows:
        print(f"{r['n']:4d} ({r['active']} active)  last {r['last'][:16]}  {r['project']}")
    if not rows:
        print("(empty store)")


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


def cmd_setup(conn, a) -> None:
    """White-glove: wire the full SessionStart+SessionEnd loop into Claude Code settings.

    SessionStart `engrim hook`  -> mirror file-memory in, then inject the boot pack (start oriented).
    SessionEnd  `engrim sync --claude` -> mirror the session's file-memory writes into the store.
    Together these keep the SQLite store and Claude Code's md memory in lockstep, session in/out."""
    settings_path = os.path.expanduser(a.settings or "~/.claude/settings.json")
    engrim_bin = shutil.which("engrim") or "engrim"
    wired = {
        "SessionStart": (f"{engrim_bin} hook 2>/dev/null || true", "engrim hook"),
        "SessionEnd":   (f"{engrim_bin} sync --claude >/dev/null 2>&1 || true", "engrim sync"),
        # Tail the transcript into the append-only log after each turn — cheap, never loaded.
        "Stop":         (f"{engrim_bin} log --hook >/dev/null 2>&1 || true", "engrim log"),
        # The minder: auto-inject the relevant db slice for each prompt (top-k, budget-capped).
        "UserPromptSubmit": (f"{engrim_bin} assist 2>/dev/null || true", "engrim assist"),
    }

    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except Exception as e:
            sys.exit(f"settings.json exists but is not valid JSON ({e}). Fix it, then re-run.")

    hooks = settings.setdefault("hooks", {})
    changed = False
    for event, (cmd, marker) in wired.items():
        groups = hooks.setdefault(event, [])
        present = any(marker in h.get("command", "")
                      for grp in groups for h in grp.get("hooks", []))
        if present:
            print(f"✓ {event} hook already present in {settings_path}")
        else:
            groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 20}]})
            changed = True
            print(f"✓ wired {event} hook\n    {cmd}")

    # Ambient status line: shows engrim is live + working in the status bar, never in the chat — so the
    # user sees the benefit without being interrupted (the answer to "is this thing even doing anything?").
    sl, sl_cmd = settings.get("statusLine"), f"{engrim_bin} statusline"
    if isinstance(sl, dict) and "engrim" in (sl.get("command") or ""):
        print("✓ status line already shows engrim")
    elif sl:
        print(f"• a status line is already configured — leaving it. To show engrim, set its command to: {sl_cmd}")
    else:
        settings["statusLine"] = {"type": "command", "command": sl_cmd}
        changed = True
        print(f"✓ wired status line\n    {sl_cmd}")

    if changed:
        tmp = settings_path + ".engrim-tmp"
        with open(tmp, "w") as f:
            json.dump(settings, f, indent=2)
        os.replace(tmp, settings_path)  # atomic: never leave a half-written settings.json

    if not a.no_claude_md:
        md_path = os.path.expanduser("~/.claude/CLAUDE.md")
        existing = ""
        if os.path.exists(md_path):
            with open(md_path) as f:
                existing = f.read()
        if "engrim" in existing and "Project Memory" in existing:
            print(f"✓ CLAUDE.md already mentions engrim ({md_path})")
        else:
            with open(md_path, "a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n" + CLAUDE_MD_BLOCK)
            print(f"✓ added usage note to {md_path}")

    # Warm the semantic backend now — a visible, one-time model fetch in a command the user is watching,
    # so session hooks never cold-download mid-prompt. Then embed this project's existing records so the
    # minder ranks by meaning from the very first session. All best-effort; never blocks setup.
    if os.environ.get("ENGRIM_EMBED", "").strip().lower() not in ("0", "off", "none", "false", "no", "lexical"):
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

    print("\nDone. Open a NEW Claude Code session (or run /hooks to reload) and your project "
          "memory will auto-load. Try: engrim add -t fact -s \"hello world\" ; engrim context")


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
            "INSERT INTO memories(ts,project,type,summary,detail,status,tags,links,source) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (_now(), project, typ, summary, body, "active",
             json.dumps(tags), json.dumps([]), "import:" + base),
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
                    "INSERT INTO memories(ts,project,type,summary,detail,status,tags,links,source)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (_now(), project, typ, summary, detail, "active",
                     json.dumps(tags), json.dumps([]), source))
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


# --------------------------------------------------------------------------- transcript log
# A SEPARATE, append-only tier from `memories`. It records the raw back-and-forth so engineers
# have a full, replayable record — but it is NEVER injected into the boot pack / context window, so
# it can't bloat a session or drag the system. Curated memory (small, loaded) and the transcript
# log (complete, never loaded) are two tiers that don't compete.

def _extract_text(content, include_thinking=False):
    """Pull the human-readable text out of a Claude transcript message's `content`.

    `content` is a str (plain user prompt) or a list of typed blocks. We keep `text` (the visible
    exchange), optionally `thinking`, and skip tool_use/tool_result/images so the log stays the
    actual conversation, not machinery."""
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
            if o.get("type") not in ("user", "assistant"):
                continue
            # Full fidelity: keep the complete original JSON line in `raw` (every turn, including
            # tool turns and sidechains), plus an extracted text slice in `content` for readable
            # search. raw never enters context, so completeness costs disk, not tokens.
            msg = o.get("message") or {}
            text = _extract_text(msg.get("content"), include_thinking)
            cur = conn.execute(
                "INSERT OR IGNORE INTO log(ts,project,session,role,content,raw,msg_uuid) "
                "VALUES(?,?,?,?,?,?,?)",
                (o.get("timestamp") or _now(), project, o.get("sessionId") or session,
                 o.get("type"), text, line, o.get("uuid")))
            added += cur.rowcount
        end = f.tell()
    _meta_set(conn, project, okey, str(end))   # commits
    conn.commit()
    return added


def cmd_log(conn, a) -> None:
    """Append to the raw transcript log. `--hook` reads a Stop-hook JSON from stdin and ingests the
    session's new turns; `--from-transcript PATH` ingests a file; otherwise append one -r/-c turn."""
    project = _resolve_project(a.project)
    if a.hook:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            return
        n = _ingest_transcript(conn, project, payload.get("transcript_path"),
                               payload.get("session_id"), a.include_thinking)
        return  # silent: this runs from a hook
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


def _decision_snippet(text):
    """Tighten a turn down to the sentence that carried the decision cue."""
    flat = (text or "").replace("\n", " ")
    for s in re.split(r"(?<=[.!?])\s+", flat):
        if any(cue in s.lower() for cue in _DECISION_CUES):
            return s.strip()
    return flat.strip()


def _looks_like_narration(snippet):
    """True if the snippet is the agent's own process/meta chatter rather than a project decision
    (#197). Drops it from the clear-readiness signal so 'to capture' counts real decisions only."""
    low = (snippet or "").lower()
    return any(m in low for m in _NARRATION_MARKERS)


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
    """Best cosine of `text` against the project's stored record embeddings (0.0 if none / no backend)."""
    if not fn:
        return 0.0
    rows = conn.execute(
        "SELECT e.vec AS vec FROM embedding e JOIN memories m ON m.id = e.memory_id "
        "WHERE m.project = ? AND m.status = 'active'", (project,)).fetchall()
    if not rows:
        return 0.0
    qv = fn(text)
    return max(_cosine(qv, _blob_vec(r["vec"])) for r in rows)


def _lexical_overlap_captured(conn, project, snippet):
    """Lexical fallback for the captured-check (used only when there's no embedding backend): does any
    active record share most of the snippet's content words? Conservative on purpose."""
    toks = set(re.findall(r"[a-z0-9]{4,}", snippet.lower()))
    if not toks:
        return False
    for r in conn.execute(
            "SELECT summary, detail FROM memories WHERE project = ? AND status = 'active'",
            (project,)).fetchall():
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
    rows = conn.execute(
        "SELECT ts, content FROM log WHERE project = ? ORDER BY ts DESC LIMIT ?",
        (project, a.k)).fetchall()
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

    uncaptured = [
        (ts, snip) for ts, snip in candidates
        if not ((_max_similarity(conn, project, snip, fn) >= _CAPTURED_SIM) if fn
                else _lexical_overlap_captured(conn, project, snip))
    ]
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
    p.add_argument("--db", default=os.environ.get("ENGRIM_DB", DEFAULT_DB),
                   help="SQLite store path (or set $ENGRIM_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

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
    pa.set_defaults(func=cmd_add)

    pr = sub.add_parser("recall")
    pr.add_argument("-p", "--project", default="auto")
    pr.add_argument("-q", "--query")
    pr.add_argument("-t", "--type")
    pr.add_argument("-k", type=int, default=8)
    pr.add_argument("--detail", action="store_true")
    pr.add_argument("--include-stale", action="store_true")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_recall)

    pl = sub.add_parser("list")
    pl.add_argument("-p", "--project", default="auto")
    pl.add_argument("-t", "--type")
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

    ph = sub.add_parser("hook")
    ph.add_argument("-p", "--project", default="auto")
    ph.add_argument("-b", "--budget", type=int, default=4000)
    ph.add_argument("--no-sync", action="store_true",
                    help="don't mirror Claude Code's file-memory before injecting")
    ph.set_defaults(func=cmd_hook)

    ps = sub.add_parser("supersede")
    ps.add_argument("--id", type=int, required=True)
    ps.add_argument("--status", default="superseded")
    ps.set_defaults(func=cmd_supersede)

    pse = sub.add_parser("setup")
    pse.add_argument("--settings", help="path to settings.json (default ~/.claude/settings.json)")
    pse.add_argument("--no-claude-md", action="store_true", help="don't touch ~/.claude/CLAUDE.md")
    pse.set_defaults(func=cmd_setup)

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

    plog = sub.add_parser("log")
    plog.add_argument("-p", "--project", default="auto")
    plog.add_argument("-r", "--role", help="user|assistant (for a manual single-turn append)")
    plog.add_argument("-c", "--content", help="content for a manual single-turn append")
    plog.add_argument("--session", help="session id to tag the turn(s) with")
    plog.add_argument("--from-transcript", help="ingest new turns from a Claude Code transcript JSONL")
    plog.add_argument("--hook", action="store_true",
                      help="read a Stop-hook JSON from stdin and ingest the session's new turns")
    plog.add_argument("--include-thinking", action="store_true",
                      help="also log assistant 'thinking' blocks (off by default; large + internal)")
    plog.set_defaults(func=cmd_log)

    pas = sub.add_parser("assist")
    pas.add_argument("-p", "--project", default="auto")
    pas.add_argument("-k", type=int, default=5, help="max records to inject (default 5)")
    pas.add_argument("-b", "--budget", type=int, default=600,
                     help="char budget for the injected slice (default 600 ≈ ~150 tokens)")
    pas.set_defaults(func=cmd_assist)

    psl = sub.add_parser("statusline", help="one-line ambient status for Claude Code's status bar")
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
    prv.add_argument("-k", type=int, default=40, help="how many recent log turns to scan (default 40)")
    prv.set_defaults(func=cmd_review)

    sub.add_parser("projects").set_defaults(func=cmd_projects)

    pst = sub.add_parser("stats")
    pst.add_argument("-p", "--project", default="auto")
    pst.add_argument("-b", "--budget", type=int, default=4000)
    pst.set_defaults(func=cmd_stats)

    pmcp = sub.add_parser("mcp", help="run engrim as an MCP server (stdio) for clients like Claude Code")
    pmcp.set_defaults(func=cmd_mcp)
    return p


def cmd_mcp(conn, a) -> None:
    """Run engrim as an MCP server over stdio (for MCP clients like Claude Code)."""
    from engrim.mcp_server import serve
    serve(conn)


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
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
