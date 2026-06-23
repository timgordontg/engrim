"""Tests for the v0.4 surface: seed-once sync, the transcript log, and the minder."""
import io
import json
import sqlite3

from engrim.cli import main


def _write(p, text):
    p.write_text(text, encoding="utf-8")


def _md_dir(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    _write(d / "rule.md", "---\ntype: feedback\ndescription: ship only tested code\n---\nbody\n")
    _write(d / "dead.md", "Merged into MEMORY.md. content lives there\n")  # tombstone
    _write(d / "MEMORY.md",
           "# Hub\n## User\nAlice is staff eng, terse comms only — and this inline section is well "
           "over the one hundred forty character threshold so it becomes a real record.\n"
           "## Pointers\n- [x](rule.md) — pointer-only section, should be skipped\n")
    return d


def test_sync_creates_records_and_skips_tombstones(tmp_path, capsys):
    db = tmp_path / "m.db"
    d = _md_dir(tmp_path)
    main(["--db", str(db), "sync", str(d), "-p", "/p"])
    out = capsys.readouterr().out
    assert "1 skip" in out  # the tombstone
    c = sqlite3.connect(db)
    sources = {r[0] for r in c.execute("SELECT source FROM memories WHERE project='/p'")}
    assert "md:file:rule.md" in sources
    assert "md:section:user" in sources
    assert "md:file:dead.md" not in sources


def test_sync_is_idempotent(tmp_path, capsys):
    db = tmp_path / "m.db"
    d = _md_dir(tmp_path)
    main(["--db", str(db), "sync", str(d), "-p", "/p"])
    capsys.readouterr()
    main(["--db", str(db), "sync", str(d), "-p", "/p"])
    assert "+0 add, ~0 update" in capsys.readouterr().out


def test_sync_prune_supersedes_vanished_source(tmp_path):
    db = tmp_path / "m.db"
    d = _md_dir(tmp_path)
    main(["--db", str(db), "sync", str(d), "-p", "/p"])
    (d / "rule.md").unlink()
    main(["--db", str(db), "sync", str(d), "-p", "/p"])
    c = sqlite3.connect(db)
    st = c.execute("SELECT status FROM memories WHERE source='md:file:rule.md'").fetchone()[0]
    assert st == "superseded"  # retired, not deleted


def test_sync_claude_is_seed_once(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    d = _md_dir(tmp_path)
    monkeypatch.setenv("ENGRIM_MD_DIR", str(d))
    main(["--db", str(db), "sync", "--claude", "-p", "/p"])
    capsys.readouterr()
    main(["--db", str(db), "sync", "--claude", "-p", "/p"])
    assert "already seeded" in capsys.readouterr().out
    # --force overrides
    main(["--db", str(db), "sync", "--claude", "--force", "-p", "/p"])
    assert "already seeded" not in capsys.readouterr().out


def test_add_records_are_never_pruned(tmp_path):
    db = tmp_path / "m.db"
    d = _md_dir(tmp_path)
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "hand written, must survive"])
    main(["--db", str(db), "sync", str(d), "-p", "/p"])  # prune runs; must not touch source=NULL
    c = sqlite3.connect(db)
    n = c.execute("SELECT COUNT(*) FROM memories WHERE source IS NULL AND status='active'").fetchone()[0]
    assert n == 1


def _transcript(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "uuid": "u1", "sessionId": "s", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "how do we deploy the billing service"}},
        {"type": "assistant", "uuid": "a1", "sessionId": "s", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"},
                                                       {"type": "text", "text": "via the postgres pipeline"}]}},
    ]
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    return p


def test_log_ingest_idempotent_and_per_project(tmp_path):
    db = tmp_path / "m.db"
    t = _transcript(tmp_path)
    main(["--db", str(db), "log", "--from-transcript", str(t), "-p", "/p"])
    main(["--db", str(db), "log", "--from-transcript", str(t), "-p", "/p"])  # re-run: no dupes
    main(["--db", str(db), "log", "--from-transcript", str(t), "-p", "/other"])  # 2nd project ok
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p'").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/other'").fetchone()[0] == 2
    # raw fidelity + extracted text (thinking excluded by default)
    row = c.execute("SELECT content, raw FROM log WHERE project='/p' AND msg_uuid='a1'").fetchone()
    assert row[0] == "via the postgres pipeline"
    assert "thinking" in row[1]  # raw keeps everything


def test_log_hook_reads_stdin(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    t = _transcript(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(t), "session_id": "s"})))
    main(["--db", str(db), "log", "--hook", "-p", "/p"])
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p'").fetchone()[0] == 2


def test_log_hook_resolves_project_from_payload_not_process_cwd(tmp_path, monkeypatch):
    """One Claude session is one transcript file; it must land in ONE project bucket even if the hook
    process's cwd drifts (e.g. it inherits a cwd a tool subprocess chdir'd into). The Stop hook must
    resolve the project from the workspace Claude Code reports in the payload, not os.getcwd() — else
    the session forks across buckets, each with its own cursor, and the status line's count freezes."""
    db = tmp_path / "m.db"
    t = _transcript(tmp_path)
    workspace = tmp_path / "the_session_project"; workspace.mkdir()
    drifted = tmp_path / "some_other_repo"; drifted.mkdir()
    monkeypatch.delenv("ENGRIM_PROJECT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_TAG", raising=False)
    monkeypatch.setattr("os.getcwd", lambda: str(drifted))          # hook process cwd has drifted away
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"transcript_path": str(t), "session_id": "s", "cwd": str(workspace)})))
    main(["--db", str(db), "log", "--hook"])                        # no -p: must auto-resolve
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log WHERE project=?", (str(workspace),)).fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM log WHERE project=?", (str(drifted),)).fetchone()[0] == 0


def test_log_ingest_cursor_advances_to_eof_and_is_stable(tmp_path):
    """The byte-offset cursor advances to true EOF, and a re-run on the unchanged file holds there —
    no re-reading old ground, no duplicate rows. (The monotonic guard additionally protects a
    concurrent run from rewinding it; that path needs real concurrency and is covered by reasoning.)"""
    db = tmp_path / "m.db"
    t = _transcript(tmp_path)
    okey = "log_offset:" + t.stem
    main(["--db", str(db), "log", "--from-transcript", str(t), "-p", "/p"])
    c = sqlite3.connect(db)
    first = int(c.execute("SELECT value FROM engrim_meta WHERE project='/p' AND key=?", (okey,)).fetchone()[0])
    assert first == t.stat().st_size                                # advanced to true EOF
    c.close()
    main(["--db", str(db), "log", "--from-transcript", str(t), "-p", "/p"])  # re-run, unchanged file
    c = sqlite3.connect(db)
    after = int(c.execute("SELECT value FROM engrim_meta WHERE project='/p' AND key=?", (okey,)).fetchone()[0])
    assert after == first                                           # stable, not rewound
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p'").fetchone()[0] == 2  # no dupes


def test_minder_injects_relevant_slice(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "chose postgres for billing integrity"])
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "office plants need weekly water"])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "remind me about the billing postgres choice"})))
    main(["--db", str(db), "assist", "-p", "/p"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "postgres for billing" in ctx
    assert "office plants" not in ctx


def test_minder_silent_on_trivial_prompt(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "something relevant here exists"])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "ok thanks do it now"})))
    main(["--db", str(db), "assist", "-p", "/p"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert ctx == ""  # gated: too few substantive terms -> spend nothing


def test_sessionstart_recovers_orphaned_transcript(tmp_path, capsys, monkeypatch):
    """A crash/hard-close skips Stop+SessionEnd; the next SessionStart must sweep up the lost tail."""
    db = tmp_path / "m.db"
    md = tmp_path / "memory"
    md.mkdir()
    monkeypatch.setenv("ENGRIM_MD_DIR", str(md))           # transcripts live beside the memory dir
    tr = tmp_path / "sessX.jsonl"
    tr.write_text("\n".join(json.dumps(o) for o in [
        {"type": "user", "uuid": "x1", "sessionId": "sessX", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "a question that never got logged at close"}},
        {"type": "assistant", "uuid": "x2", "sessionId": "sessX", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "an answer lost to a crash"}]}},
    ]) + "\n", encoding="utf-8")
    main(["--db", str(db), "hook", "-p", "/p"])             # fresh session boots
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p'").fetchone()[0] == 2
    main(["--db", str(db), "hook", "-p", "/p"])             # idempotent across boots
    assert c.execute("SELECT COUNT(*) FROM log WHERE project='/p'").fetchone()[0] == 2


def test_minder_emits_valid_json_on_garbage_stdin(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    main(["--db", str(db), "assist", "-p", "/p"])
    json.loads(capsys.readouterr().out)  # must still be valid JSON


# --- semantic tier: a deterministic concept embedder (no model/pip needed) proves the plumbing ---
_CONCEPTS = {
    "postgres": (1., 0., 0.), "database": (1., 0., 0.), "sql": (1., 0., 0.),
    "db": (1., 0., 0.), "relational": (1., 0., 0.),
    "coffee": (0., 1., 0.), "brew": (0., 1., 0.),
    "plant": (0., 0., 1.), "plants": (0., 0., 1.), "water": (0., 0., 1.),
}


def _fake_embed(text):
    import re
    v = [0., 0., 0.]
    for tok in re.findall(r"[a-z]+", (text or "").lower()):
        c = _CONCEPTS.get(tok)
        if c:
            v = [v[i] + c[i] for i in range(3)]
    return v if any(v) else [0.001, 0.001, 0.001]


def test_add_auto_embeds_and_embed_is_idempotent(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr("engrim.cli._EMBEDDER_OVERRIDE", (_fake_embed, "test-embed"))
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "chose postgres for billing"])
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "office plants need water"])
    # `add` auto-embeds, so both records already carry vectors — no manual embed step required.
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM embedding").fetchone()[0] == 2
    capsys.readouterr()
    main(["--db", str(db), "embed", "-p", "/p"])               # backfill is idempotent: nothing new
    assert "embedded 0" in capsys.readouterr().out
    main(["--db", str(db), "embed", "-p", "/p", "--force"])    # --force re-embeds everything
    assert "embedded 2" in capsys.readouterr().out


def test_semantic_minder_finds_what_lexical_cannot(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr("engrim.cli._EMBEDDER_OVERRIDE", (_fake_embed, "test-embed"))
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "chose postgres for the billing layer"])
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "office plants need weekly water"])
    main(["--db", str(db), "embed", "-p", "/p"])
    capsys.readouterr()
    # "relational database" shares NO words with the postgres record -> bm25 can't find it; cosine can.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "which relational database did we pick"})))
    main(["--db", str(db), "assist", "-p", "/p", "-k", "1"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "postgres" in ctx
    assert "plants" not in ctx


def test_minder_falls_back_to_lexical_without_backend(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr("engrim.cli._EMBEDDER_OVERRIDE", None)  # no semantic backend
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "chose postgres for billing"])
    capsys.readouterr()
    # synonym-only prompt with no backend -> no lexical match, nothing injected (unchanged behavior)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "which relational database engine"})))
    main(["--db", str(db), "assist", "-p", "/p"])
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"] == ""


# --- review: the honest "safe to clear" coverage signal -----------------------------------------
def _insert_log(db, project, content, role="assistant", ts="2026-06-20T09:00:00"):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO log(ts,project,session,role,content,raw,msg_uuid) VALUES(?,?,?,?,?,?,?)",
                (ts, project, "s1", role, content, "{}", None))
    con.commit()
    con.close()


def test_review_empty_log_is_graceful(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "seed"])  # initializes the schema
    capsys.readouterr()
    main(["--db", str(db), "review", "-p", "/p"])
    assert "no transcript log yet" in capsys.readouterr().out


def test_review_flags_uncaptured_decision(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "office plants need water"])
    _insert_log(str(db), "/p", "We decided to migrate the billing service to Kafka for throughput.")
    capsys.readouterr()
    main(["--db", str(db), "review", "-p", "/p"])
    out = capsys.readouterr().out
    assert "may not be" in out and "Kafka" in out and "⚠" in out


def test_review_sees_captured_decision(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision",
          "-s", "migrate the billing service to Kafka for throughput"])
    _insert_log(str(db), "/p", "We decided to migrate the billing service to Kafka for throughput.")
    capsys.readouterr()
    main(["--db", str(db), "review", "-p", "/p"])
    assert "looks safe to clear" in capsys.readouterr().out


def test_recall_command_is_hybrid(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr("engrim.cli._EMBEDDER_OVERRIDE", (_fake_embed, "test-embed"))
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "we picked postgres for billing"])
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "office plants need weekly water"])
    capsys.readouterr()
    # "relational database" shares no words with the postgres record -> only semantic can find it
    main(["--db", str(db), "recall", "-p", "/p", "-q", "relational database choice", "-k", "1"])
    out = capsys.readouterr().out
    assert "postgres" in out and "plants" not in out


def test_review_semantic_capture_check(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setattr("engrim.cli._EMBEDDER_OVERRIDE", (_fake_embed, "test-embed"))
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "the database choice is settled"])
    # same concept as the curated record but no shared words -> semantic capture-check should catch it
    _insert_log(str(db), "/p", "We decided to go with postgres for the relational layer.")
    # an unrelated decision -> should be flagged
    _insert_log(str(db), "/p", "We chose to water the office plants weekly.", ts="2026-06-20T09:05:00")
    capsys.readouterr()
    main(["--db", str(db), "review", "-p", "/p"])
    out = capsys.readouterr().out
    assert "1 look captured, 1 may not be" in out
    assert "plants" in out and "⚠" in out


def test_review_ignores_agent_narration(tmp_path, capsys):
    """#197: the agent's OWN process chatter carries cue words ('close the loop … capture this',
    'next I'll switch to …') but is not a project decision — it must not be flagged to capture."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "seed"])
    _insert_log(str(db), "/p",
                "Let me close the loop here — the decision is to capture this, so I'll go with adding a record now.")
    _insert_log(str(db), "/p", "Next I'll switch to wiring up the tests.", ts="2026-06-20T09:05:00")
    capsys.readouterr()
    main(["--db", str(db), "review", "-p", "/p"])
    assert "nothing obvious to capture" in capsys.readouterr().out


def test_review_semantic_recall_flags_cueless_decision(tmp_path, capsys, monkeypatch):
    """#200: a real decision with NO cue word is still surfaced via semantic recall, so `review` never
    falsely reports 'safe to clear' on exactly the rationale that's otherwise lost on /clear."""
    db = tmp_path / "m.db"
    monkeypatch.setattr("engrim.cli._EMBEDDER_OVERRIDE", (_fake_embed, "test-embed"))
    monkeypatch.setattr("engrim.cli._DECISION_EXEMPLARS", ("postgres relational database",))
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "office plants need weekly water"])
    # cue-less, but semantically a database decision; shares no words with the plants fact
    _insert_log(str(db), "/p",
                "The relational database powers the analytics layer for every customer account.")
    capsys.readouterr()
    main(["--db", str(db), "review", "-p", "/p"])
    out = capsys.readouterr().out
    assert "may not be" in out and "⚠" in out and "relational" in out


# --- boot-pack recent-activity tail: close the /clear recency hole (#191) ---

def test_context_recent_tail_surfaces_uncaptured_decision(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "office plants need weekly water"])
    _insert_log(str(db), "/p", "We decided to migrate the billing service to Kafka for throughput.")
    capsys.readouterr()
    main(["--db", str(db), "context", "-p", "/p"])
    out = capsys.readouterr().out
    # the recent decision lives only in the log, isn't curated, and is unrelated to the fact -> it shows
    assert "[RECENT" in out and "Kafka" in out


def test_context_recent_tail_dedupes_captured_decision(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision",
          "-s", "migrate the billing service to Kafka for throughput"])
    _insert_log(str(db), "/p", "We decided to migrate the billing service to Kafka for throughput.")
    capsys.readouterr()
    main(["--db", str(db), "context", "-p", "/p"])
    out = capsys.readouterr().out
    # the only decision candidate is already curated -> tail is empty -> no RECENT section (no dup noise)
    assert "[RECENT" not in out


# --- ambient status line: the user can SEE engrim working, out-of-band ---

def test_statusline_ready_when_empty(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "seed"])  # init schema, different project
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    main(["--db", str(db), "statusline", "-p", "/empty-proj"])
    assert "🧠 engrim · ready" in capsys.readouterr().out


def test_statusline_shows_count_and_in_play(tmp_path, capsys, monkeypatch):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "chose postgres for billing integrity"])
    # a relevant prompt makes the minder pull -> it records the pull for the status line
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "remind me about the billing postgres choice"})))
    main(["--db", str(db), "assist", "-p", "/p"])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    main(["--db", str(db), "statusline", "-p", "/p"])
    out = capsys.readouterr().out
    assert "curated" in out and "in play" in out


def test_statusline_logged_count_ticks_up_per_session(tmp_path, capsys, monkeypatch):
    """The live signal: turns logged for THIS session show in the bar, so it moves as the chat grows."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "seed record"])
    main(["--db", str(db), "log", "-p", "/p", "--session", "sess-1", "-r", "user", "-c", "first turn"])
    main(["--db", str(db), "log", "-p", "/p", "--session", "sess-1", "-r", "assistant", "-c", "second turn"])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "sess-1"})))
    main(["--db", str(db), "statusline", "-p", "/p"])
    assert "+2 logged" in capsys.readouterr().out


def test_statusline_flags_uncaptured_for_clear(tmp_path, capsys, monkeypatch):
    """An uncaptured decision in the log surfaces as a live 'to capture' clear-readiness nudge."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "totally unrelated seed about weather"])
    main(["--db", str(db), "log", "-p", "/p", "--session", "s9", "-r", "user",
          "-c", "we decided to switch to a columnar store for the analytics rollups"])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s9"})))
    main(["--db", str(db), "statusline", "-p", "/p"])
    assert "to capture" in capsys.readouterr().out


def test_statusline_narration_does_not_trip_capture_nudge(tmp_path, capsys, monkeypatch):
    """#197: agent meta-narration in the log must not inflate the live 'to capture' nudge — with no
    real uncaptured decision the bar reads clear-safe, model-free."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "seed record"])
    main(["--db", str(db), "log", "-p", "/p", "--session", "sx", "-r", "assistant",
          "-c", "Let me close the loop and capture this; next I'll switch to the tests."])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "sx"})))
    main(["--db", str(db), "statusline", "-p", "/p"])
    out = capsys.readouterr().out
    assert "to capture" not in out and "clear-safe" in out
