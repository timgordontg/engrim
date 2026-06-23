"""MCP stdio server — drives engrim.mcp_server.serve() with a JSON-RPC script and
asserts the responses, with no MCP client or network needed."""
import io
import json
import sqlite3

from engrim.cli import connect
from engrim.mcp_server import serve, TOOLS


def _run(conn, requests):
    """Feed JSON-RPC messages through serve() and return the parsed response frames."""
    inp = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    out = io.StringIO()
    serve(conn, inp=inp, out=out)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_initialize_and_tools_list(tmp_path):
    conn = connect(str(tmp_path / "m.db"))
    resp = _run(conn, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},  # no response expected
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    assert len(resp) == 2                                    # the notification produced no frame
    init = resp[0]["result"]
    assert init["serverInfo"]["name"] == "engrim"
    assert init["protocolVersion"] == "2025-06-18"           # client version echoed
    names = {t["name"] for t in resp[1]["result"]["tools"]}
    assert names == {"engrim_recall", "engrim_context", "engrim_add"}
    assert {t["name"] for t in TOOLS} == names


def test_add_then_recall_and_context(tmp_path):
    db = str(tmp_path / "m.db")
    conn = connect(db)
    resp = _run(conn, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "engrim_add",
            "arguments": {"type": "decision", "summary": "retrieval beats stuffing the context window",
                          "project": "/proj", "tags": ["memory", "design"]}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "engrim_add",
            "arguments": {"type": "fact", "summary": "unrelated note about coffee", "project": "/proj"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "engrim_recall",
            "arguments": {"query": "retrieval context", "project": "/proj"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "engrim_context", "arguments": {"project": "/proj"}}},
    ])

    add_res = json.loads(resp[0]["result"]["content"][0]["text"])
    assert add_res["id"] == 1 and add_res["type"] == "decision"
    # the record really landed in the store
    c = sqlite3.connect(db)
    assert c.execute("SELECT summary FROM memories WHERE id=1").fetchone()[0] == \
        "retrieval beats stuffing the context window"
    assert json.loads(c.execute("SELECT tags FROM memories WHERE id=1").fetchone()[0]) == ["memory", "design"]

    recall_res = json.loads(resp[2]["result"]["content"][0]["text"])
    summaries = [r["summary"] for r in recall_res["records"]]
    assert "retrieval beats stuffing the context window" in summaries

    ctx_res = json.loads(resp[3]["result"]["content"][0]["text"])
    assert ctx_res["loaded"] >= 1
    assert any("retrieval beats" in r["summary"] for r in ctx_res["records"])


def test_unknown_tool_and_bad_args_are_in_band_errors(tmp_path):
    conn = connect(str(tmp_path / "m.db"))
    resp = _run(conn, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "engrim_nope", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "engrim_add", "arguments": {"type": "bogus", "summary": "x", "project": "/p"}}},
    ])
    assert resp[0]["error"]["code"] == -32602                # unknown tool -> JSON-RPC error
    assert resp[1]["result"]["isError"] is True              # bad tool args -> in-band tool error
    assert "type must be one of" in resp[1]["result"]["content"][0]["text"]
