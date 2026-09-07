"""Context transports must return the compact content selected for the boot pack."""
from contextlib import closing
import io
import json

import pytest

import engrim.cli as cli
from engrim.mcp_server import serve


@pytest.fixture
def context_store(tmp_path):
    with closing(cli.connect(str(tmp_path / "memory.db"))) as conn:
        yield conn


def _mcp(conn, name, arguments):
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    output = io.StringIO()
    serve(conn, inp=io.StringIO(json.dumps(request) + "\n"), out=output)
    response = json.loads(output.getvalue())
    assert "error" not in response
    assert not response["result"].get("isError", False)
    text = response["result"]["content"][0]["text"]
    return json.loads(text), text


@pytest.mark.parametrize("summary,detail", [
    ("A short decision.", "D" * 50_000),
    ("S" * 10_000, None),
], ids=["large-detail", "large-summary"])
def test_mcp_context_returns_the_budgeted_content(context_store, summary, detail):
    cli.add_memory(context_store, project="/p", type="decision", summary=summary, detail=detail)

    result, text = _mcp(context_store, "engrim_context", {"project": "/p", "budget": 4000})

    assert result["loaded"] == 1
    assert len(text) < 4000
    assert len(result["records"][0]["summary"]) <= 200
    assert not result["records"][0].get("detail")


@pytest.mark.parametrize("summary,detail", [
    ("A short decision.", "D" * 50_000),
    ("S" * 10_000, None),
], ids=["large-detail", "large-summary"])
def test_cli_json_context_returns_the_budgeted_content(context_store, tmp_path, capsys, summary, detail):
    cli.add_memory(context_store, project="/p", type="decision", summary=summary, detail=detail)

    cli.main(["--db", str(tmp_path / "memory.db"), "context", "-p", "/p", "-b", "4000", "--json"])
    text = capsys.readouterr().out
    records = json.loads(text)

    assert len(records) == 1
    assert len(text) < 4000
    assert len(records[0]["summary"]) <= 200
    assert not records[0].get("detail")


def test_context_does_not_truncate_stored_memory_or_recall(context_store):
    summary = "Redis session storage " + "S" * 10_000
    detail = "D" * 50_000
    mid = cli.add_memory(context_store, project="/p", type="decision", summary=summary, detail=detail)

    _mcp(context_store, "engrim_context", {"project": "/p", "budget": 4000})
    stored = context_store.execute("SELECT summary, detail FROM memories WHERE id=?", (mid,)).fetchone()
    recalled, _ = _mcp(context_store, "engrim_recall", {"project": "/p", "query": "Redis"})

    assert stored["summary"] == recalled["records"][0]["summary"] == summary
    assert stored["detail"] == recalled["records"][0]["detail"] == detail


def test_context_counts_serialized_records_including_escaping(context_store):
    cli.add_memory(context_store, project="/p", type="decision",
                   summary='Use "Redis" at C:\\session for café data.\nKeep a backup.',
                   tags=["storage"], origin_agent="claude-code")

    result, _ = _mcp(context_store, "engrim_context", {"project": "/p", "budget": 4000})
    actual = sum(len(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
                 for r in result["records"])

    assert result["loaded"] == 1
    assert result["chars"] == actual
    assert result["records"][0]["origin_agent"] == "claude-code"
    assert json.loads(result["records"][0]["tags"]) == ["storage"]


def test_context_respects_the_exact_record_budget(context_store):
    cli.add_memory(context_store, project="/p", type="decision", summary="Use Redis for sessions.")
    full, _ = _mcp(context_store, "engrim_context", {"project": "/p", "budget": 4000})
    cost = sum(len(json.dumps(r, ensure_ascii=False, separators=(",", ":"))) for r in full["records"])

    fits, _ = _mcp(context_store, "engrim_context", {"project": "/p", "budget": cost})
    misses, _ = _mcp(context_store, "engrim_context", {"project": "/p", "budget": cost - 1})

    assert fits["loaded"] == 1 and fits["chars"] == cost
    assert misses["loaded"] == 0 and misses["chars"] == 0
    assert misses["total_active"] == 1


@pytest.mark.parametrize("budget", [0, -1, 4000])
def test_oversized_resume_pointer_cannot_bypass_json_budget(context_store, budget):
    cli.add_memory(context_store, project="/p", type="state", summary="R" * 10_000,
                   tags=["resume-pointer"])

    result, _ = _mcp(context_store, "engrim_context", {"project": "/p", "budget": budget})

    assert result["loaded"] == 0
    assert result["chars"] == 0
    assert result["total_active"] == 1


def test_resume_pointer_is_first_and_untruncated_when_it_fits(context_store):
    cli.add_memory(context_store, project="/p", type="decision", summary="Use Redis for sessions.")
    summary = "Next step: check session recovery. " * 10
    mid = cli.add_memory(context_store, project="/p", type="state", summary=summary,
                         tags=["resume-pointer"], detail="D" * 50_000)

    result, text = _mcp(context_store, "engrim_context", {"project": "/p", "budget": 4000})

    assert result["records"][0]["id"] == mid
    assert result["records"][0]["summary"] == summary
    assert len(text) < 4000


@pytest.mark.parametrize("budget", [4000, 20_000])
def test_older_resume_pointer_never_replaces_the_newest(context_store, budget):
    older = cli.add_memory(context_store, project="/p", type="state", summary="Deploy Redis.",
                           tags=["resume-pointer"])
    newest = cli.add_memory(context_store, project="/p", type="state",
                            summary="Stop Redis; migrate to Postgres. " + "R" * 10_000,
                            tags=["resume-pointer"])
    fact = cli.add_memory(context_store, project="/p", type="fact", summary="Useful current fact.")
    context_store.execute("UPDATE memories SET ts=? WHERE id=?", ("2026-09-06T12:00:00", older))
    context_store.execute("UPDATE memories SET ts=? WHERE id=?", ("2026-09-07T12:00:00", newest))
    context_store.commit()

    result, _ = _mcp(context_store, "engrim_context", {"project": "/p", "budget": budget})
    ids = [r["id"] for r in result["records"]]

    assert older not in ids
    assert fact in ids
    assert (newest in ids) == (budget == 20_000)
    if newest in ids:
        assert ids[0] == newest


@pytest.mark.parametrize("oversized", [False, True])
def test_resume_pointer_uses_the_latest_write_when_timestamps_tie(context_store, monkeypatch, oversized):
    monkeypatch.setattr(cli, "_now", lambda: "2026-09-07T12:00:00")
    older = cli.add_memory(context_store, project="/p", type="state", summary="Deploy Redis.",
                           tags=["resume-pointer"])
    summary = "Migrate sessions to Postgres." + ("R" * 10_000 if oversized else "")
    newest = cli.add_memory(context_store, project="/p", type="state", summary=summary,
                            tags=["resume-pointer"])
    fact = cli.add_memory(context_store, project="/p", type="fact", summary="Useful current fact.")

    result, _ = _mcp(context_store, "engrim_context", {"project": "/p", "budget": 4000})
    ids = [r["id"] for r in result["records"]]

    assert older not in ids
    assert ids == ([fact] if oversized else [newest, fact])


def test_non_context_fields_cannot_expand_the_pack(context_store):
    cli.add_memory(context_store, project="/p", type="decision", summary="Use Redis for sessions.",
                   links=["L" * 50_000], source="S" * 50_000)

    result, text = _mcp(context_store, "engrim_context", {"project": "/p", "budget": 4000})

    assert result["loaded"] == 1
    assert len(text) < 4000
    assert "links" not in result["records"][0]
    assert "source" not in result["records"][0]


def test_large_context_metadata_is_counted_before_selecting_a_record(context_store):
    cli.add_memory(context_store, project="/p", type="decision", summary="Use Redis for sessions.",
                   tags=["T" * 50_000])

    result, text = _mcp(context_store, "engrim_context", {"project": "/p", "budget": 4000})

    assert result["loaded"] == 0
    assert result["chars"] == 0
    assert len(text) < 4000


def test_many_short_records_fit_the_serialized_record_budget(context_store):
    for i in range(60):
        cli.add_memory(context_store, project="/p", type="fact", summary=f"Fact {i}.")

    result, _ = _mcp(context_store, "engrim_context", {"project": "/p", "budget": 4000})
    actual = sum(len(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
                 for r in result["records"])

    assert 0 < result["loaded"] < result["total_active"] == 60
    assert result["chars"] == actual <= 4000


def test_cli_json_and_mcp_share_the_same_compact_records(context_store, tmp_path, capsys):
    cli.add_memory(context_store, project="/p", type="decision", summary="S" * 10_000,
                   detail="D" * 50_000, origin_agent="claude-code")
    cli.add_memory(context_store, project=cli.GLOBAL_PROJECT, type="user", summary="Keep context concise.")
    cli.add_memory(context_store, project="/other", type="decision", summary="Other project.")
    cli.add_memory(context_store, project="/p", type="decision", summary="Old decision.", status="superseded")

    result, _ = _mcp(context_store, "engrim_context", {"project": "/p", "budget": 4000})
    cli.main(["--db", str(tmp_path / "memory.db"), "context", "-p", "/p", "-b", "4000", "--json"])
    records = json.loads(capsys.readouterr().out)

    assert records == result["records"]
    assert len(records) == result["total_active"] == 2
    assert {r["project"] for r in records} == {"/p", cli.GLOBAL_PROJECT}
    assert all(len(r["summary"]) <= 200 and not r.get("detail") for r in records)
