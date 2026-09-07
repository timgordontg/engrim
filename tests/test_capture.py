"""Capture means a decision was preserved, not merely that its topic is familiar."""
from contextlib import closing

import pytest

import engrim.cli as cli


AFFIRMATIVE = "We decided to use Redis for session storage."
NEGATIVE = "We decided not to use Redis for session storage."


@pytest.fixture(params=["lexical", "semantic"])
def capture_store(tmp_path, monkeypatch, request):
    if request.param == "semantic":
        # Deliberately indistinguishable vectors: similarity must not overrule negation.
        monkeypatch.setattr(cli, "_EMBEDDER_OVERRIDE", (lambda text: [1.0, 0.0], "test"))
    with closing(cli.connect(str(tmp_path / "memory.db"))) as conn:
        yield conn


@pytest.mark.parametrize("saved,new", [(AFFIRMATIVE, NEGATIVE), (NEGATIVE, AFFIRMATIVE)])
def test_opposite_decision_is_not_captured(capture_store, saved, new):
    cli.add_memory(capture_store, project="/p", type="decision", summary=saved)

    assert not cli._is_captured(capture_store, "/p", new)


@pytest.mark.parametrize("decision", [AFFIRMATIVE, NEGATIVE])
def test_identical_decision_is_captured(capture_store, decision):
    cli.add_memory(capture_store, project="/p", type="decision", summary=decision)

    assert cli._is_captured(capture_store, "/p", decision)


def test_review_flags_the_reversed_decision(capture_store, tmp_path, capsys):
    cli.add_memory(capture_store, project="/p", type="decision", summary=AFFIRMATIVE)
    capture_store.execute(
        "INSERT INTO log(ts, project, session, role, content) VALUES(?,?,?,?,?)",
        ("2026-09-07T12:00:00", "/p", "s1", "user", NEGATIVE),
    )
    capture_store.commit()

    cli.main(["--db", str(tmp_path / "memory.db"), "review", "-p", "/p"])
    output = capsys.readouterr().out

    assert "0 look captured, 1 may not be" in output
    assert NEGATIVE in output


@pytest.mark.parametrize("negative", [
    "We decided to never use Redis for session storage.",
    "We decided: no Redis for session storage.",
    "We decided we don't use Redis for session storage.",
    "We decided we don’t use Redis for session storage.",
    "We decided we cannot use Redis for session storage.",
    "We decided session storage must work without Redis.",
])
def test_explicit_negation_is_not_lost(capture_store, negative):
    cli.add_memory(capture_store, project="/p", type="decision", summary=AFFIRMATIVE)

    assert not cli._is_captured(capture_store, "/p", negative)


def test_negation_scope_cannot_be_verified_by_counting_not(capture_store):
    cli.add_memory(capture_store, project="/p", type="decision",
                   summary="We decided to use Redis, not Postgres, for session storage.")

    assert not cli._is_captured(
        capture_store, "/p", "We decided to use Postgres, not Redis, for session storage.",
    )


def test_semantic_fallback_cannot_accept_a_negated_paraphrase(capture_store):
    cli.add_memory(capture_store, project="/p", type="decision",
                   summary="Redis backs our sessions.")
    assert not cli._lexical_overlap_captured(capture_store, "/p", NEGATIVE)

    assert not cli._is_captured(capture_store, "/p", NEGATIVE)


def test_negation_in_detail_is_not_lost(capture_store):
    cli.add_memory(capture_store, project="/p", type="decision",
                   summary="Session persistence", detail=NEGATIVE)

    assert not cli._is_captured(capture_store, "/p", AFFIRMATIVE)


def test_matching_negated_statement_in_detail_is_captured(capture_store):
    cli.add_memory(capture_store, project="/p", type="decision",
                   summary="Session persistence",
                   detail=NEGATIVE + " Postgres already handles persistence.")

    assert cli._is_captured(capture_store, "/p", NEGATIVE)


def test_matching_statement_is_not_blocked_by_unrelated_negation(capture_store):
    cli.add_memory(capture_store, project="/p", type="decision",
                   summary=AFFIRMATIVE, detail="No downtime is expected.")

    assert cli._is_captured(capture_store, "/p", AFFIRMATIVE)


def test_matching_negated_statement_ignores_case_and_whitespace(capture_store):
    cli.add_memory(capture_store, project="/p", type="decision", summary=NEGATIVE.upper())

    assert cli._is_captured(capture_store, "/p", "We decided  not to use Redis for session storage")


def test_reversal_clears_after_the_new_decision_is_saved(capture_store):
    cli.add_memory(capture_store, project="/p", type="decision", summary=AFFIRMATIVE)
    assert not cli._is_captured(capture_store, "/p", NEGATIVE)

    cli.add_memory(capture_store, project="/p", type="decision", summary=NEGATIVE)

    assert cli._is_captured(capture_store, "/p", NEGATIVE)


def test_negated_reversal_remains_in_the_boot_tail_and_count(capture_store):
    cli.add_memory(capture_store, project="/p", type="decision", summary=AFFIRMATIVE)
    capture_store.execute(
        "INSERT INTO log(ts, project, session, role, content) VALUES(?,?,?,?,?)",
        ("2026-09-07T12:00:00", "/p", "s1", "user", NEGATIVE),
    )
    capture_store.commit()

    assert cli._uncaptured_count(capture_store, "/p") == 1
    assert [snippet for _, snippet in cli._recent_tail(capture_store, "/p")] == [NEGATIVE]


def test_pre_fix_semantic_verdict_is_recomputed(capture_store):
    import json
    import os

    cli.add_memory(capture_store, project="/p", type="decision", summary=AFFIRMATIVE)
    row = capture_store.execute(
        "SELECT MAX(ts), COUNT(*) FROM memories WHERE project=? AND status='active'", ("/p",),
    ).fetchone()
    old_key = f"{row[0]}|{row[1]}|{os.environ.get('ENGRIM_EMBED', '').strip().lower()}"
    cli._meta_set(capture_store, "/p", "cap_cache", json.dumps({
        "k": old_key, "v": {cli._snippet_key(NEGATIVE): 1},
    }))

    assert not cli._is_captured(capture_store, "/p", NEGATIVE)


def test_pre_fix_uncaptured_count_is_recomputed(capture_store):
    import os

    cli.add_memory(capture_store, project="/p", type="decision", summary=AFFIRMATIVE)
    ts = "2026-09-07T12:00:00"
    capture_store.execute(
        "INSERT INTO log(ts, project, session, role, content) VALUES(?,?,?,?,?)",
        (ts, "/p", "s1", "user", NEGATIVE),
    )
    capture_store.commit()
    row = capture_store.execute(
        "SELECT MAX(ts), COUNT(*) FROM memories WHERE project=? AND status='active'", ("/p",),
    ).fetchone()
    old_key = f"{ts}|{row[0]}|{row[1]}|{os.environ.get('ENGRIM_EMBED', '').strip().lower()}"
    cli._meta_set(capture_store, "/p", "unc_cache", f"{old_key}=0")

    assert cli._uncaptured_count(capture_store, "/p") == 1
