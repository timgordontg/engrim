"""OpenCode integration: the adapter events, the plugin file, and setup/uninstall wiring."""
import io
import json
import os
import shutil
import subprocess
import sys

import pytest

from engrim.cli import connect, main, add_memory
from engrim.hosts.opencode import hooks as oc
from engrim.hosts.opencode import wiring as host


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("ENGRIM_PROJECT", raising=False)
    return home


# ------------------------------------------------------------------ adapter events

def test_boot_returns_pack_for_reported_cwd(tmp_path):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "repo")
    os.makedirs(proj)
    c = connect(db)
    add_memory(c, project=proj, type="decision", summary="Chose SQLite over Postgres", tags=["db"], origin_agent="opencode")
    c.close()

    text = oc.handle_boot({"cwd": proj, "session_id": "s1"}, db_path=db, print_output=False)
    assert "memory restored" in text
    assert "Chose SQLite over Postgres" in text
    assert "via OpenCode" in text


def test_boot_honours_payload_budget(tmp_path):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "repo")
    os.makedirs(proj)
    c = connect(db)
    for i in range(40):
        add_memory(c, project=proj, type="fact", summary=f"fact number {i} " + "x" * 80)
    c.close()
    full = oc.handle_boot({"cwd": proj}, db_path=db, print_output=False)
    small = oc.handle_boot({"cwd": proj, "budget": 600}, db_path=db, print_output=False)
    junk = oc.handle_boot({"cwd": proj, "budget": "lots"}, db_path=db, print_output=False)
    assert len(small) < len(full)
    assert "fact number" in small
    assert junk == full                      # a bad budget falls back to the default, never errors


def test_boot_empty_project_is_quiet(tmp_path):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "empty")
    os.makedirs(proj)
    text = oc.handle_boot({"cwd": proj}, db_path=db, print_output=False)
    assert text.startswith("(no memory for project=")


def test_prompt_returns_relevant_slice_or_nothing(tmp_path):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "repo")
    os.makedirs(proj)
    c = connect(db)
    add_memory(c, project=proj, type="fact", summary="Deploy pipeline uses GitHub Actions with a matrix build",
               detail="ubuntu + windows runners")
    c.close()

    hit = oc.handle_prompt({"cwd": proj, "prompt": "why does the deploy pipeline matrix fail on windows"},
                           db_path=db, print_output=False)
    assert "Possibly-relevant project memory" in hit
    assert "GitHub Actions" in hit

    miss = oc.handle_prompt({"cwd": proj, "prompt": "hi"}, db_path=db, print_output=False)
    assert miss == ""


def test_stop_ingests_turns_idempotently(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "repo")
    os.makedirs(proj)
    turns = [
        {"id": "msg_u1", "role": "user", "text": "let's use uv instead of pip", "ts": 1757700000000},
        {"id": "msg_a1", "role": "assistant", "text": "Decision: switch to uv.\n[tool] bash: uv init",
         "ts": 1757700005000},
        {"id": "msg_x1", "role": "assistant", "text": "", "ts": 1757700006000},   # empty: skipped
        {"id": "msg_t1", "role": "tool", "text": "ignored role"},
    ]
    payload = {"cwd": proj, "session_id": "ses_1", "turns": turns}
    assert oc.handle_stop(payload, db_path=db, print_output=True) == 2
    assert json.loads(capsys.readouterr().out.strip()) == {"logged": 2, "ok": True}
    # resend the same session: nothing new is written
    assert oc.handle_stop(payload, db_path=db, print_output=False) == 0

    c = connect(db)
    rows = c.execute("SELECT session, role, content, msg_uuid, ts FROM log WHERE project=? ORDER BY id",
                     (proj,)).fetchall()
    assert [(r["role"], r["msg_uuid"], r["session"]) for r in rows] == [
        ("user", "msg_u1", "ses_1"), ("assistant", "msg_a1", "ses_1")]
    assert rows[0]["ts"].startswith("2025-09-1")          # epoch-ms converted to ISO
    assert "[tool] bash" in rows[1]["content"]


def test_hook_cli_dispatch(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "m.db")
    proj = str(tmp_path / "repo")
    os.makedirs(proj)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "cwd": proj, "session_id": "s", "turns": [{"id": "m1", "role": "user", "text": "hello world"}]})))
    main(["--db", db, "hook", "--agent", "opencode", "--event", "stop"])
    assert json.loads(capsys.readouterr().out.strip()) == {"logged": 1, "ok": True}

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": proj})))
    main(["--db", db, "hook", "--agent", "opencode", "--event", "boot"])
    assert "no memory for project" in capsys.readouterr().out


# ------------------------------------------------------------------ plugin + setup

def test_render_plugin_bakes_quoted_binary():
    src = host.render_plugin('"C:\\Users\\tim\\Scripts\\engrim.EXE"')
    assert 'const ENGRIM = "C:/Users/tim/Scripts/engrim.EXE"' in src
    assert "__ENGRIM_BIN__" not in src
    for hook in ("experimental.chat.system.transform", "chat.message", "experimental.chat.messages.transform",
                 "experimental.session.compacting", "session.idle"):
        assert hook in src


def test_setup_opencode_dry_run(fake_home, tmp_path, capsys):
    main(["--db", str(tmp_path / "m.db"), "setup", "--opencode", "--dry-run"])
    out = capsys.readouterr().out
    assert "[dry-run] Would write OpenCode plugin" in out
    assert "[dry-run] Would register engrim MCP server" in out
    assert not (fake_home / ".config" / "opencode").exists()


def test_setup_opencode_merges_config_and_is_idempotent(fake_home, tmp_path, capsys):
    cfg_dir = fake_home / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "model": "anthropic/claude-sonnet-5",
        "mcp": {"other": {"type": "remote", "url": "https://x"}},
    }), encoding="utf-8")
    (cfg_dir / "AGENTS.md").write_text("# my rules\n", encoding="utf-8")

    main(["--db", str(tmp_path / "m.db"), "setup", "--opencode"])
    out = capsys.readouterr().out
    assert "✓ wrote OpenCode plugin" in out
    assert "✓ registered MCP server" in out
    assert "✓ added usage note" in out

    cfg = json.loads((cfg_dir / "opencode.json").read_text(encoding="utf-8"))
    assert cfg["model"] == "anthropic/claude-sonnet-5"            # untouched
    assert cfg["mcp"]["other"]["url"] == "https://x"               # untouched
    engrim = cfg["mcp"]["engrim"]
    assert engrim["type"] == "local" and engrim["enabled"] is True
    assert engrim["command"][1:] == ["serve", "--mcp"]

    plugin = cfg_dir / "plugins" / "engrim.js"
    assert plugin.exists()
    assert "export default EngrimPlugin" in plugin.read_text(encoding="utf-8")

    agents = (cfg_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith("# my rules\n")
    assert "engrim_recall" in agents

    # second run: no duplicates, no churn
    main(["--db", str(tmp_path / "m.db"), "setup", "--opencode"])
    out = capsys.readouterr().out
    assert "✓ MCP server already registered" in out
    assert "✓ AGENTS.md already mentions engrim" in out
    assert (cfg_dir / "AGENTS.md").read_text(encoding="utf-8").count("engrim_recall") == 1


def test_setup_opencode_refuses_to_rewrite_commented_jsonc(fake_home, tmp_path, capsys):
    cfg_dir = fake_home / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text('{\n  // my comment\n  "model": "x"\n}\n', encoding="utf-8")
    main(["--db", str(tmp_path / "m.db"), "setup", "--opencode"])
    # the commented file is left alone; the entry lands in a fresh opencode.json beside it
    assert "// my comment" in (cfg_dir / "opencode.jsonc").read_text(encoding="utf-8")
    cfg = json.loads((cfg_dir / "opencode.json").read_text(encoding="utf-8"))
    assert "engrim" in cfg["mcp"]


def test_setup_autodetects_opencode(fake_home, tmp_path, capsys, monkeypatch):
    (fake_home / ".config" / "opencode").mkdir(parents=True)
    main(["--db", str(tmp_path / "m.db"), "setup", "--dry-run"])
    out = capsys.readouterr().out
    assert "OpenCode (~/.config/opencode)" in out
    assert "[dry-run] Would write OpenCode plugin" in out


def test_setup_respects_xdg_config_home(fake_home, tmp_path, capsys, monkeypatch):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    main(["--db", str(tmp_path / "m.db"), "setup", "--opencode"])
    assert (xdg / "opencode" / "plugins" / "engrim.js").exists()
    assert not (fake_home / ".config").exists()


def test_uninstall_opencode_reverses_setup(fake_home, tmp_path, capsys):
    cfg_dir = fake_home / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(json.dumps({"model": "x"}), encoding="utf-8")
    (cfg_dir / "AGENTS.md").write_text("# mine\n", encoding="utf-8")
    db = str(tmp_path / "m.db")
    main(["--db", db, "setup", "--opencode"])
    main(["--db", db, "uninstall", "--opencode"])
    out = capsys.readouterr().out
    assert "✓ removed" in out
    assert not (cfg_dir / "plugins" / "engrim.js").exists()
    cfg = json.loads((cfg_dir / "opencode.json").read_text(encoding="utf-8"))
    assert cfg == {"model": "x", "$schema": "https://opencode.ai/config.json"}
    assert (cfg_dir / "AGENTS.md").read_text(encoding="utf-8") == "# mine\n"


def test_opencode_is_a_valid_origin_agent(tmp_path):
    db = str(tmp_path / "m.db")
    main(["--db", db, "add", "-p", "/p", "-t", "decision", "-s", "from opencode", "--origin-agent", "opencode"])
    c = connect(db)
    assert c.execute("SELECT origin_agent FROM memories").fetchone()[0] == "opencode"


# ------------------------------------------------------------------ plugin flush semantics (review fixes)

def test_stop_hook_exits_nonzero_when_ingest_fails(tmp_path, capsys):
    """The plugin only marks message ids as logged when the stop hook exits 0, so a failed ingest
    must NOT look like success - neither in-process nor as a real child process."""
    bad_db = str(tmp_path)   # a directory, not a file: sqlite can't open it
    payload = {"cwd": str(tmp_path), "session_id": "s", "turns": [{"id": "u", "role": "user", "text": "hi"}]}
    with pytest.raises(SystemExit) as ei:
        oc.handle_stop(payload, db_path=bad_db, print_output=True)
    assert ei.value.code == 1
    assert json.loads(capsys.readouterr().out.strip()) == {"logged": 0, "ok": False}

    # What the plugin actually observes is the child's exit status. With an unopenable db, main()'s
    # own connect() raises before dispatch, so this half proves the CLI-level contract (non-zero exit
    # on a failed run), not the handle_stop path - that is the in-process assertion above.
    r = subprocess.run([sys.executable, "-c", "from engrim.cli import main; import sys; "
                        "main(['--db', sys.argv[1], 'hook', '--agent', 'opencode', '--event', 'stop'])", bad_db],
                       input=json.dumps(payload), capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert r.returncode != 0


def test_plugin_source_has_review_fixes():
    src = host.render_plugin("/usr/local/bin/engrim")
    assert "info.summary" in src                 # compaction summaries are never logged as turns
    assert "if (r.ok)" in src                    # ids commit only after a successful ingest
    assert "cmd|bat" in src and "ComSpec" in src  # Windows .cmd/.bat shims go through cmd.exe
    assert "PROMPT_TIMEOUT_MS" in src            # per-message minder has a short leash
    assert "MINDER_WAIT_MS" in src               # ...and the model call doesn't block on it
    # The slice rides the user message as a synthetic part, never the system prompt: a system prompt
    # that changes per prompt invalidates the provider's prefix cache for the whole context.
    assert "synthetic: true" in src
    assert "output.system.push(m)" not in src
    assert "ENGRIM_BOOT_BUDGET" in src and "budget: BOOT_BUDGET" in src   # README's budget knob is real
    assert host.AGENTS_MD.splitlines()[0] in src   # one usage text, baked from the AGENTS block


def test_boot_and_prompt_fail_quietly_on_unreadable_db(tmp_path, capsys):
    bad_db = str(tmp_path)   # a directory
    payload = {"cwd": str(tmp_path), "session_id": "s", "prompt": "anything"}
    assert oc.handle_boot(payload, db_path=bad_db).startswith("engrim context error:")
    assert oc.handle_prompt(payload, db_path=bad_db) == ""
    assert capsys.readouterr().out.count("\n") == 1   # boot printed its one-line error; prompt printed nothing


def test_setup_opencode_rejects_non_object_mcp_before_writing_plugin(fake_home, tmp_path, capsys):
    cfg_dir = fake_home / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text('{"mcp": "nope"}', encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "m.db"), "setup", "--opencode"])
    assert not (cfg_dir / "plugins" / "engrim.js").exists()   # nothing half-wired


_HARNESS = r"""
import fs from "node:fs"
import { EngrimPlugin } from "./engrim.mjs"
const dir = new URL(".", import.meta.url).pathname
const msgs = [
  { info: { id: "u1", role: "user", time: { created: 1 } }, parts: [{ type: "text", text: "hello" }] },
  { info: { id: "a1", role: "assistant", time: { created: 2, completed: 3 } }, parts: [{ type: "text", text: "hi" }] },
  { info: { id: "s1", role: "assistant", summary: true, time: { created: 4, completed: 5 } }, parts: [{ type: "text", text: "SUMMARY" }] },
]
const client = { session: { messages: async () => ({ data: msgs }) } }
const plugin = await EngrimPlugin({ client, directory: "/repo" })
const idle = () => plugin.event({ event: { type: "session.idle", properties: { sessionID: "S" } } })
const stopFile = dir + "last-stop.json"
const take = () => { const t = JSON.parse(fs.readFileSync(stopFile, "utf8")).turns.map(t => t.id); fs.unlinkSync(stopFile); return t }
fs.writeFileSync(dir + "FAIL_STOP", "")
await idle(); const first = take()                    // ingest failed: nothing is marked seen
fs.unlinkSync(dir + "FAIL_STOP")
await idle(); const second = take()                   // same turns are retried
await idle(); const third = fs.existsSync(stopFile)   // and after success nothing is resent
await plugin.event({ event: { type: "session.deleted", properties: { info: { id: "S" } } } })
await idle(); const afterDelete = take()               // seen-set dropped with the session: full resend
const bootFile = dir + "last-boot.json"
const sys1 = { system: [] }; await plugin["experimental.chat.system.transform"]({ sessionID: "S" }, sys1)
fs.unlinkSync(bootFile)
const sys2 = { system: [] }; await plugin["experimental.chat.system.transform"]({ sessionID: "S" }, sys2)
const cachedBoot = !fs.existsSync(bootFile)            // second call served from cache: no spawn
await plugin.event({ event: { type: "session.compacted", properties: { sessionID: "S" } } })
const sys3 = { system: [] }; await plugin["experimental.chat.system.transform"]({ sessionID: "S" }, sys3)
const rebootAfterCompact = fs.existsSync(bootFile)     // compaction invalidates the cache: re-spawned
const packOk = sys1.system.length === 1 && sys1.system[0].startsWith("PACK")
// Per-prompt minder: the slice rides the user message it was pulled for, never the system prompt.
const user = (id, t) => ({ info: { id, role: "user" }, parts: [{ type: "text", text: t }] })
const asst = (id) => ({ info: { id, role: "assistant" }, parts: [{ type: "text", text: "ok" }] })
const req = (...m) => ({ messages: m })
const tail = (r, i) => r.messages[i].parts
fs.writeFileSync(dir + "SLOW_PROMPT", "")                 // cold spawn: slower than MINDER_WAIT_MS
await plugin["chat.message"]({ sessionID: "S", messageID: "u2" }, { message: { id: "u2" }, parts: [{ type: "text", text: "what did we decide?" }] })
const r1 = req(user("u1", "hello"), asst("a1"), user("u2", "what did we decide?"))
await plugin["experimental.chat.messages.transform"]({}, r1)
const sysAfterPrompt = { system: [] }; await plugin["experimental.chat.system.transform"]({ sessionID: "S" }, sysAfterPrompt)
const firstRequestBare = tail(r1, 2).length === 1        // slice not ready: request 1 goes out without it
await new Promise((r) => setTimeout(r, 2000))
const r2 = req(user("u1", "hello"), asst("a1"), user("u2", "what did we decide?"))
await plugin["experimental.chat.messages.transform"]({}, r2)
const t2 = tail(r2, 2)
const secondRequestHasSlice = t2.length === 2 && t2[1].type === "text" && t2[1].synthetic === true && t2[1].text === "SLICE"
const historyUntouched = tail(r2, 0).length === 1       // u1 had no minder pull: nothing appended
const systemStable = sysAfterPrompt.system.length === 1 && sysAfterPrompt.system[0].startsWith("PACK")
fs.unlinkSync(dir + "SLOW_PROMPT")
await plugin["chat.message"]({ sessionID: "S", messageID: "u3" }, { message: { id: "u3" }, parts: [{ type: "text", text: "and then?" }] })
const r3 = req(user("u1", "hello"), asst("a1"), user("u2", "what did we decide?"), asst("a2"), user("u3", "and then?"))
await plugin["experimental.chat.messages.transform"]({}, r3)
const earlierSliceKept = tail(r3, 2).length === 2 && tail(r3, 2)[1].text === "SLICE"   // history renders identically
const fastSliceOnFirstRequest = tail(r3, 4).length === 2 && tail(r3, 4)[1].text === "SLICE"  // warm spawn beats the wait
await plugin.event({ event: { type: "session.deleted", properties: { info: { id: "S" } } } })
const r4 = req(user("u2", "what did we decide?"), user("u3", "and then?"))
await plugin["experimental.chat.messages.transform"]({}, r4)
const droppedWithSession = tail(r4, 0).length === 1 && tail(r4, 1).length === 1
console.log(JSON.stringify({ first, second, third, afterDelete, cachedBoot, rebootAfterCompact, packOk,
  firstRequestBare, secondRequestHasSlice, historyUntouched, systemStable, earlierSliceKept, fastSliceOnFirstRequest, droppedWithSession }))
"""

_FAKE_ENGRIM = """#!/bin/sh
ev="$5"; cat > "$(dirname "$0")/last-$ev.json"
if [ "$ev" = stop ] && [ -e "$(dirname "$0")/FAIL_STOP" ]; then exit 1; fi
[ "$ev" = boot ] && echo PACK
if [ "$ev" = prompt ]; then [ -e "$(dirname "$0")/SLOW_PROMPT" ] && sleep 3; echo SLICE; fi
[ "$ev" = stop ] && echo '{"logged":1,"ok":true}'
exit 0
"""


# NOTE: this skips on Windows, so the cmd.exe shim branch in spawnEngrim has string-presence
# coverage only (test_plugin_source_has_review_fixes). Verify manually there: `engrim setup
# --opencode` with a pipx-installed engrim.cmd, then confirm the boot pack appears in a session.
@pytest.mark.skipif(shutil.which("node") is None or sys.platform == "win32",
                    reason="needs node and a POSIX shell")
def test_plugin_flush_skips_summary_and_retries_failed_ingest(tmp_path):
    """Drive the real plugin under node with a fake engrim: compaction summaries are never sent,
    a failed stop-ingest leaves the turns pending for the next idle, and a successful one
    retires them. Then the minder: its slice is appended to the user message it was pulled for
    (on the first request it is ready for, and identically on every later one), never to the
    system prompt, and is dropped with the session."""
    fake = tmp_path / "fake-engrim.sh"
    fake.write_text(_FAKE_ENGRIM, encoding="utf-8")
    fake.chmod(0o755)
    (tmp_path / "engrim.mjs").write_text(host.render_plugin(str(fake)), encoding="utf-8")
    (tmp_path / "harness.mjs").write_text(_HARNESS, encoding="utf-8")
    r = subprocess.run(["node", "harness.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {
        "first": ["u1", "a1"], "second": ["u1", "a1"], "third": False,
        "afterDelete": ["u1", "a1"], "cachedBoot": True, "rebootAfterCompact": True, "packOk": True,
        "firstRequestBare": True, "secondRequestHasSlice": True, "historyUntouched": True, "systemStable": True,
        "earlierSliceKept": True, "fastSliceOnFirstRequest": True, "droppedWithSession": True}
