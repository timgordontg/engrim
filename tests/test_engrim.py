"""Real test suite for engrim — exercises the installed package end to end."""
import json
import sqlite3

from engrim.cli import main


def test_add_persists(tmp_path):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/proj", "-t", "fact", "-s", "the sky is blue", "--tags", "a,b"])
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert c.execute("SELECT summary FROM memories").fetchone()[0] == "the sky is blue"


def test_special_chars_roundtrip(tmp_path):
    db = tmp_path / "m.db"
    s = "O'Brien's \"quoted\" plan & <edge>"
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", s])
    c = sqlite3.connect(db)
    assert c.execute("SELECT summary FROM memories").fetchone()[0] == s


def test_recall_ranks_relevant_first(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "retrieval beats stuffing the context window"])
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "unrelated note about coffee"])
    capsys.readouterr()
    main(["--db", str(db), "recall", "-p", "/p", "-q", "retrieval context"])
    out = capsys.readouterr().out
    assert "retrieval beats" in out
    assert "coffee" not in out


def test_recall_no_match_is_graceful(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "hello"])
    capsys.readouterr()
    main(["--db", str(db), "recall", "-p", "/p", "-q", "zzznope"])
    assert "no memories" in capsys.readouterr().out


def test_supersede_hides_from_active_but_recoverable(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "old truth here"])
    main(["--db", str(db), "supersede", "--id", "1", "--status", "superseded"])
    capsys.readouterr()
    main(["--db", str(db), "recall", "-p", "/p", "-q", "truth"])
    assert "no memories" in capsys.readouterr().out
    main(["--db", str(db), "recall", "-p", "/p", "-q", "truth", "--include-stale"])
    assert "old truth" in capsys.readouterr().out


def test_context_orders_by_priority(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "a plain fact"])
    main(["--db", str(db), "add", "-p", "/p", "-t", "user", "-s", "user prefers concise replies"])
    capsys.readouterr()
    main(["--db", str(db), "context", "-p", "/p"])
    out = capsys.readouterr().out
    assert "[USER]" in out and "[FACT]" in out
    assert out.index("[USER]") < out.index("[FACT]")  # user-context loads first


def test_context_budget_truncates(tmp_path, capsys):
    db = tmp_path / "m.db"
    for i in range(5):
        main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", f"fact number {i} with some length"])
    capsys.readouterr()
    main(["--db", str(db), "context", "-p", "/p", "-b", "90"])
    out = capsys.readouterr().out
    assert "more ·" in out  # paged: not everything fit, pointer shown


def test_context_unknown_project_empty(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "context", "-p", "/nope"])
    assert "no memory" in capsys.readouterr().out


def test_hook_emits_valid_sessionstart_json(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "state", "-s", "current state X is wired"])
    capsys.readouterr()
    main(["--db", str(db), "hook", "-p", "/p"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "current state X" in payload["hookSpecificOutput"]["additionalContext"]


def test_hook_unknown_project_never_breaks(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "hook", "-p", "/nope"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert isinstance(payload["hookSpecificOutput"]["additionalContext"], str)


def test_bad_type_rejected(tmp_path):
    db = tmp_path / "m.db"
    try:
        main(["--db", str(db), "add", "-p", "/p", "-t", "bogus", "-s", "x"])
        assert False, "should have exited on bad type"
    except SystemExit:
        pass


def test_git_root_tagging_from_subdir(tmp_path, monkeypatch):
    """Launching from any subdir of a repo tags to the repo root, not the cwd."""
    import os
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    db = tmp_path / "m.db"
    monkeypatch.delenv("ENGRIM_PROJECT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_TAG", raising=False)
    monkeypatch.chdir(sub)
    main(["--db", str(db), "add", "-t", "fact", "-s", "tagged at repo root"])  # -p auto
    proj = sqlite3.connect(db).execute("SELECT project FROM memories").fetchone()[0]
    assert os.path.realpath(proj) == os.path.realpath(str(repo))


def test_claude_dir_anchors_non_git_workspace(tmp_path, monkeypatch):
    """A non-repo workspace (no .git) marked by a .claude dir anchors from any subdir to that root.

    This is the edge case that bit in the field: launching from `proj/backend` filed records under
    the `proj/backend` SIBLING scope while the status line read `proj` — so the count never moved and
    logging *looked* dead though writes were landing. A .claude dir makes the non-repo root anchor."""
    import os
    proj = tmp_path / "workspace"
    (proj / ".claude").mkdir(parents=True)            # no .git here — pure Claude Code workspace
    sub = proj / "backend" / "discovery"
    sub.mkdir(parents=True)
    db = tmp_path / "m.db"
    monkeypatch.delenv("ENGRIM_PROJECT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_TAG", raising=False)
    monkeypatch.chdir(sub)
    main(["--db", str(db), "add", "-t", "fact", "-s", "anchored at workspace root"])  # -p auto
    proj_tag = sqlite3.connect(db).execute("SELECT project FROM memories").fetchone()[0]
    assert os.path.realpath(proj_tag) == os.path.realpath(str(proj))


def test_git_wins_over_nested_claude_marker(tmp_path, monkeypatch):
    """Nearest marker wins: a .git repo root anchors even if an ancestor also has a .claude dir."""
    import os
    outer = tmp_path / "outer"
    (outer / ".claude").mkdir(parents=True)
    repo = outer / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src"
    sub.mkdir(parents=True)
    db = tmp_path / "m.db"
    monkeypatch.delenv("ENGRIM_PROJECT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_TAG", raising=False)
    monkeypatch.chdir(sub)
    main(["--db", str(db), "add", "-t", "fact", "-s", "nearest root wins"])  # -p auto
    proj_tag = sqlite3.connect(db).execute("SELECT project FROM memories").fetchone()[0]
    assert os.path.realpath(proj_tag) == os.path.realpath(str(repo))


def test_home_claude_never_becomes_catch_all_anchor(tmp_path, monkeypatch):
    """`~/.claude` must NOT anchor: a non-repo dir under a HOME that has ~/.claude tags to the cwd
    itself, not to HOME — otherwise every loose project under home collapses into one bucket."""
    import os
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)            # the global ~/.claude
    loose = home / "loose_project"                    # a project with no marker of its own
    loose.mkdir()
    db = tmp_path / "m.db"
    monkeypatch.delenv("ENGRIM_PROJECT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_TAG", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~", str(home)) if p.startswith("~") else p)
    monkeypatch.chdir(loose)
    main(["--db", str(db), "add", "-t", "fact", "-s", "must not collapse to home"])  # -p auto
    proj_tag = sqlite3.connect(db).execute("SELECT project FROM memories").fetchone()[0]
    assert os.path.realpath(proj_tag) == os.path.realpath(str(loose))
    assert os.path.realpath(proj_tag) != os.path.realpath(str(home))


def test_env_project_override(tmp_path, monkeypatch):
    """$ENGRIM_PROJECT gives a stable tag (for sharing across host + containers)."""
    db = tmp_path / "m.db"
    monkeypatch.setenv("ENGRIM_PROJECT", "my-stable-tag")
    monkeypatch.chdir(tmp_path)
    main(["--db", str(db), "add", "-t", "fact", "-s", "x"])  # -p auto
    assert sqlite3.connect(db).execute("SELECT project FROM memories").fetchone()[0] == "my-stable-tag"


def test_explicit_project_beats_env(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setenv("ENGRIM_PROJECT", "env-tag")
    main(["--db", str(db), "add", "-p", "/explicit", "-t", "fact", "-s", "x"])
    assert sqlite3.connect(db).execute("SELECT project FROM memories").fetchone()[0] == "/explicit"


def test_recall_survives_code_symbols_and_punctuation(tmp_path, capsys):
    """A stranger searching code/punctuation must NOT crash FTS5 (the big flop bug)."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "use React useState hook in C++ bridge"])
    for q in ["C++", "useState()", 'foo"bar', "a AND b", "NEAR(x)", "::", "a-b/c"]:
        capsys.readouterr()
        main(["--db", str(db), "recall", "-p", "/p", "-q", q])  # must not raise
    capsys.readouterr()
    main(["--db", str(db), "recall", "-p", "/p", "-q", "useState()"])
    assert "useState" in capsys.readouterr().out  # and still finds it


def test_recall_all_punctuation_is_graceful(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "hello"])
    capsys.readouterr()
    main(["--db", str(db), "recall", "-p", "/p", "-q", "+++ (((  "])
    assert "no memories" in capsys.readouterr().out


def test_stats_reports_context_economics(tmp_path, capsys):
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "fact", "-s", "a stored fact about the system"])
    capsys.readouterr()
    main(["--db", str(db), "stats", "-p", "/p"])
    out = capsys.readouterr().out
    assert "context economics" in out and "tokens" in out


def test_bare_db_filename_no_dir(tmp_path, monkeypatch):
    """A relative db path with no directory must not crash makedirs."""
    monkeypatch.chdir(tmp_path)
    main(["--db", "local.db", "add", "-p", "/p", "-t", "fact", "-s", "ok"])
    assert (tmp_path / "local.db").exists()


def test_import_frontmatter(tmp_path):
    db = tmp_path / "m.db"
    note = tmp_path / "my-rule.md"
    note.write_text(
        "---\nname: my-rule\ndescription: prefer composition over inheritance\n"
        "metadata:\n  node_type: memory\n  type: feedback\n---\nBecause it keeps coupling low.\n"
    )
    main(["--db", str(db), "import", str(note), "-p", "/proj"])
    row = sqlite3.connect(db).execute("SELECT type, summary, detail, source FROM memories").fetchone()
    assert row[0] == "feedback"
    assert row[1] == "prefer composition over inheritance"
    assert "coupling" in row[2]
    assert row[3] == "import:my-rule.md"


def test_import_plain_md_uses_first_heading(tmp_path):
    db = tmp_path / "m.db"
    note = tmp_path / "plain.md"
    note.write_text("# The build is reproducible\nDetails here.\n")
    main(["--db", str(db), "import", str(note), "-p", "/proj"])
    assert sqlite3.connect(db).execute("SELECT summary FROM memories").fetchone()[0] == "The build is reproducible"


def test_import_dir_dedup_exclude_and_typemap(tmp_path):
    db = tmp_path / "m.db"
    d = tmp_path / "notes"
    d.mkdir()
    (d / "a.md").write_text("---\ndescription: alpha fact\nmetadata:\n  type: fact\n---\nbody a")
    (d / "b.md").write_text("---\ndescription: beta state\nmetadata:\n  type: project\n---\nbody b")
    (d / "INDEX.md").write_text("# index\nignore me")
    main(["--db", str(db), "import", str(d), "-p", "/proj", "--exclude", "INDEX"])
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2          # INDEX excluded
    assert c.execute("SELECT type FROM memories WHERE summary='beta state'").fetchone()[0] == "state"  # project->state
    main(["--db", str(db), "import", str(d), "-p", "/proj", "--exclude", "INDEX"])  # re-run
    assert c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2          # dedup, no growth


# --------------------------------------------------------------------------- global user-layer (Flavor A)

def test_global_record_co_loads_in_every_project(tmp_path, capsys):
    """A --global record rides along with EVERY project's boot pack, marked as global."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/projA", "-t", "fact", "-s", "projA detail only"])
    main(["--db", str(db), "add", "--global", "-t", "user", "-s", "always reply concisely"])
    capsys.readouterr()
    main(["--db", str(db), "context", "-p", "/projA"])
    out = capsys.readouterr().out
    assert "always reply concisely" in out      # global rides along
    assert "· global" in out                    # and is marked as such
    assert "projA detail only" in out


def test_global_does_not_pull_other_projects_records(tmp_path, capsys):
    """Co-loading is global-only: a sibling project's *project* records never leak across."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/projA", "-t", "fact", "-s", "projA secret detail"])
    main(["--db", str(db), "add", "--global", "-t", "user", "-s", "shared user truth"])
    capsys.readouterr()
    main(["--db", str(db), "context", "-p", "/projB"])
    out = capsys.readouterr().out
    assert "shared user truth" in out           # the global layer shows up
    assert "projA secret detail" not in out     # but projA's own record does NOT


def test_global_opt_out_with_env(tmp_path, capsys, monkeypatch):
    """ENGRIM_NO_GLOBAL fully disables the layer — behavior is exactly as before the feature."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "--global", "-t", "user", "-s", "global only record"])
    monkeypatch.setenv("ENGRIM_NO_GLOBAL", "1")
    capsys.readouterr()
    main(["--db", str(db), "context", "-p", "/projC"])
    assert "no memory" in capsys.readouterr().out   # nothing rides along when opted out


def test_global_surfaces_in_recall(tmp_path, capsys):
    """Recall in any project finds global records too, not just the project's own."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "--global", "-t", "user", "-s", "prefers terse commit messages"])
    capsys.readouterr()
    main(["--db", str(db), "recall", "-p", "/anyproject", "-q", "commit messages"])
    assert "terse commit messages" in capsys.readouterr().out


def test_global_layer_read_in_isolation(tmp_path, capsys):
    """Reading the global layer directly (-p __global__) does not double-count or recurse."""
    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "--global", "-t", "user", "-s", "the one global record"])
    capsys.readouterr()
    main(["--db", str(db), "context", "-p", "__global__"])
    out = capsys.readouterr().out
    assert out.count("the one global record") == 1


def test_semantic_floor_admits_paraphrase_near_match(tmp_path, capsys, monkeypatch):
    """A genuine paraphrase scoring in the 0.30-0.35 band (above noise, below the old 0.35 floor) must
    still be recalled. Query and record share no words, so the only path to a hit is semantic — this
    pins _SEM_FLOOR at 0.30 and guards against silently raising it back."""
    # Fake embedder: record -> [1,0,0]; query -> a vector at cosine 0.32 to it (between 0.30 and 0.35).
    def _fake_embed(text):
        if "alpha" in text:
            return [1.0, 0.0, 0.0]
        if "bravo" in text:
            return [0.32, 0.94737, 0.0]   # cosine 0.32 with [1,0,0]
        return [0.0, 0.0, 1.0]

    monkeypatch.setattr("engrim.cli._EMBEDDER_OVERRIDE", (_fake_embed, "test-embed"))

    db = tmp_path / "m.db"
    main(["--db", str(db), "add", "-p", "/p", "-t", "decision", "-s", "borderline paraphrase target alpha"])
    capsys.readouterr()
    main(["--db", str(db), "recall", "-p", "/p", "-q", "bravo charlie delta"])
    assert "borderline paraphrase target alpha" in capsys.readouterr().out
