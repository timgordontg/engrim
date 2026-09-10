#!/bin/sh
# Continue-as-clear for a gh-aw run: the Claude Code hook behind settings.json.
# A pre-step in the agentic workflow installs both into the agent's HOME. Five events:
#   PreCompact   block the auto-compaction and mark "context nearly full".
#   PostToolUse  while marked: nudge the model to finish or to write its
#                engrim resume-pointer; once an engrim_add tagged
#                resume-pointer lands, ask for the end of the turn.
#   Stop         the turn ended with a restart requested: end the session
#                with SIGTERM, at the turn boundary, so gh-aw's harness
#                starts a fresh run from the memory pack. (A signal, because
#                the harness restarts only a failed process, and no hook
#                outcome makes Claude Code exit non-zero on its own —
#                `continue: false` and a failing Stop hook both exit 0.)
#   StopFailure  the wall came first (a "prompt is too long" 400): write a
#                mechanical crash pointer for the fresh run.
#   SessionStart clear the marks; hand a crash pointer, if any, to the model.
set -u
event="${1:-}"
state_dir="${GH_AW_CONTEXT_HOOK_STATE_DIR:-/tmp/gh-aw/agent}"
mark="$state_dir/context-nearly-full"
restart="$state_dir/restart-requested"
crash_pointer="$state_dir/crash-pointer.md"
log="$state_dir/context-restart.log"

# A no-op outside a gh-aw run.
if [ -z "${GH_AW_SAFE_OUTPUTS:-}" ]; then cat >/dev/null; exit 0; fi
mkdir -p "$state_dir" 2>/dev/null || true
payload="$(cat)"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
note() { printf '%s %s %s\n' "$(stamp)" "$event" "$*" >>"$log" 2>/dev/null || true; }

# The Claude Code process above this hook: the nearest ancestor named claude.
claude_pid() {
  p=$PPID
  while [ "$p" -gt 1 ] 2>/dev/null; do
    c="$(ps -o comm= -p "$p" 2>/dev/null | tr -d ' ')"
    case "$c" in *claude*) echo "$p"; return;; esac
    p="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"; [ -n "$p" ] || break
  done
  echo "$PPID"
}

# True when the PostToolUse payload is an engrim_add tagged resume-pointer.
has_resume_pointer() {
  printf '%s' "$1" | python3 -c '
import json, sys
d = json.load(sys.stdin)
tags = (d.get("tool_input") or {}).get("tags")
if isinstance(tags, str):
    tags = [t.strip() for t in tags.split(",")]
pointer = (d.get("tool_name") or "").endswith("engrim_add") and isinstance(tags, list) and "resume-pointer" in tags
sys.exit(0 if pointer else 1)
' 2>/dev/null
}

# The nudge a tool result carries while the mark is set.
nudge() {
  printf '%s' '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"Your context is nearly full and auto-compaction is off for this run. Either finish within two or three turns, or record where you are with engrim_add (type state, tag resume-pointer: what is done, what is next, and every comment or label you have already emitted). Once that record lands, end your turn: this session ends and a fresh one resumes from the memory pack — expected, not an error."}}'
}

# What the model reads once its pointer is recorded.
end_of_turn() {
  printf '%s' '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"Your resume-pointer is recorded. End your turn now, with no further tool calls; the session restarts from the memory pack."}}'
}

# From the StopFailure payload: the error, the last tool calls in the
# transcript, and git status — mechanical, for the next session to read.
write_crash_pointer() {
  printf '%s' "$1" | python3 -c '
import json, subprocess, sys
d = json.load(sys.stdin)
out = ["# Crash pointer (mechanical, written by the StopFailure hook)", "",
       "Error: %s — %s" % (d.get("error"), str(d.get("error_details") or "")[:300]), "",
       "Last tool calls, oldest first:"]
calls = []
try:
    for line in open(d.get("transcript_path") or "", encoding="utf-8", errors="replace"):
        try:
            m = json.loads(line)
        except Exception:
            continue
        if m.get("type") == "assistant":
            for b in (m.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    calls.append("%s %s" % (b.get("name"), json.dumps(b.get("input"))[:200]))
except Exception as e:
    calls.append("(transcript unreadable: %s)" % e)
out += ["- " + c for c in calls[-12:]]
try:
    status = subprocess.run(["git", "status", "--short"], capture_output=True, text=True,
                            timeout=20, cwd=d.get("cwd") or None).stdout.strip()[:2000]
    out += ["", "git status --short at the crash:", "```", status or "(clean)", "```"]
except Exception as e:
    out.append("(git status failed: %s)" % e)
sys.stdout.write("\n".join(out) + "\n")
' >"$crash_pointer" 2>/dev/null
}

case "$event" in
  start)
    rm -f "$mark" "$restart"
    if [ -s "$crash_pointer" ]; then
      printf 'The previous session of this run ended at the context wall. Its mechanical record follows; `engrim_context` is the curated one.\n\n'
      cat "$crash_pointer"; mv -f "$crash_pointer" "$crash_pointer.$(stamp)" 2>/dev/null || true
    fi
    exit 0 ;;
  precompact)
    : >"$mark"; note "compaction blocked; marked"
    echo "Compaction blocked: finish within a few turns, or write your engrim resume-pointer and the session restarts from it." >&2
    exit 2 ;;
  posttool)
    [ -e "$mark" ] || exit 0
    if [ -e "$restart" ]; then
      end_of_turn
    elif has_resume_pointer "$payload"; then
      : >"$restart"; note "resume-pointer written; end of turn requested"
      end_of_turn
    else
      nudge
    fi
    exit 0 ;;
  stop)
    [ -e "$restart" ] || exit 0
    pid="$(claude_pid)"; note "turn ended with a restart requested; SIGTERM to $pid"
    rm -f "$mark" "$restart"
    kill -TERM "$pid" 2>/dev/null || true
    exit 0 ;;
  stopfailure)
    write_crash_pointer "$payload" || note "crash pointer not written"
    exit 0 ;;
  *) exit 0 ;;
esac
