# engrim in GitHub Agentic Workflows

Two workflows that give a [gh-aw](https://github.github.com/gh-aw/) agent memory across runs, and a restart instead of an auto-compaction when its context fills — engrim's [continue-as-clear workflow](../../README.md#8-continue-as-clear-workflow) with a harness restart standing in for `/clear`.

| File | What it is |
|---|---|
| `.github/workflows/issue-triage.md` | A small triage agent (Claude Code engine) that reads each new issue, posts one comment and applies one label. It declares engrim's MCP server, the pre-steps and post-steps, and the prompt section that teaches the agent how to use its memory. No gh-aw imports. |
| `.github/workflows/engrim-memory.yml` | A plain workflow that fires when a triage run completes and folds that run's store into the repository's canonical store with `engrim merge`, one run at a time. |
| `.github/lib/claude/context-restart.sh` | The Claude Code hook: blocks the compaction, nudges the model, ends the session once the resume-pointer is written and the turn has ended, writes the crash pointer. |
| `.github/lib/claude/settings.json` | Wires the hook to its five events. Installed into the agent's HOME with the script. |
| `.github/lib/artifact.py` | The newest unexpired artifact with an exact name, optionally downloaded. Standard library. |
| `.github/lib/engrim/store.py` | `count`, `capture` (sqlite's online backup API) and `retire` on a store. Standard library; runs inside the server's image with the script on stdin. |

## How a run goes

1. **Seed.** A pre-step downloads the newest `engrim-memory` artifact and copies it to `/tmp/gh-aw/engrim/memory.db`. No artifact yet means an empty store, never a failed run.
2. **Serve.** gh-aw's MCP gateway starts engrim's stdio server in a stock `python:3.13.15-alpine3.24` container with the wheel mounted read-only on `PYTHONPATH`, `--network none`, `ENGRIM_EMBED=off` (pure lexical, standard library only) and the store mounted read-write from `/tmp`. The agent sees `engrim_context`, `engrim_add` and `engrim_recall`.
3. **Work.** The prompt's first instruction is `engrim_context`. Records mean the agent is either resuming this job (an active `resume-pointer`) or reading what earlier runs learned. It writes records at phase boundaries and keeps one honest pointer that names every comment and label already emitted.
4. **Restart instead of compacting.** When Claude Code's auto-compaction threshold trips, the `PreCompact` hook blocks the compaction (exit 2) and marks the session; the next tool result carries a `PostToolUse` nudge — "your context is nearly full: finish, or write your resume-pointer". When the model writes an `engrim_add` tagged `resume-pointer`, the hook asks it to end its turn, and the `Stop` hook then sends `SIGTERM` to Claude Code (exit 143), which gh-aw's harness treats as a signal termination and retries as a **fresh run**. That run boots from the memory pack. If the wall comes first (a "prompt is too long" 400), the harness retries anyway and a `StopFailure` hook leaves a mechanical crash pointer that `SessionStart` hands to the next session.
5. **Capture.** A post-step (`if: always()`) takes a consistent copy of the store with sqlite's online backup API and uploads it as `engrim-memory-run-<run id>`.
6. **Merge.** `engrim-memory.yml` fires on `workflow_run: completed`, downloads the canonical store and that run's store, runs `engrim merge` (content-keyed, so ids never collide; status monotonic, so a retirement on either side wins; idempotent), retires every active `resume-pointer` in the result (a pointer describes a working tree that no longer exists, and the next run must not mistake it for its own), uploads the result as `engrim-memory` and deletes the run store. Its concurrency group with `cancel-in-progress: false` and `queue: max` makes GitHub run one merge at a time and keep every pending one, so two agent runs that end together get two merges, in order.

## Install

1. Copy `.github/` — the two workflow files and `lib/` — into your repository.
2. Replace the placeholders: `my-app` (`ENGRIM_PROJECT`, the stable project tag, and the `User-Agent` in `lib/artifact.py`) and `my-bot` (the login of the GitHub App or bot whose own issues must not be triaged; gh-aw files one when a run fails). The repository itself needs no edit: GitHub fills `GITHUB_REPOSITORY`.
3. Pick the model and set the key. `engine.model` is `claude-sonnet-4-6`, the newest Anthropic model with a 200k default window; the key is the `ANTHROPIC_API_KEY` secret. The commented line beside them declares the window of a model Claude Code does not know (`CLAUDE_CODE_MAX_CONTEXT_TOKENS`). Then compile:

   ```bash
   gh aw compile
   ```

4. Merge to the default branch. `workflow_run` triggers fire only for workflow files on the default branch, so the merge workflow is live once it is there. Until then runs still upload their stores, and the first merge folds them in order.
5. Watch a run: the seed step prints `seeding N records, M active`, the capture step `captured N records, M active`, and the merge run `merged: +A add, ~S status, K skip` then `retired K resume-pointer(s); now N records, M active`.

Runner requirements: Docker (the gateway needs it anyway; the seed, capture and merge steps run one-shot containers of the same image so file ownership on the store matches the server's), `python3` for the two scripts, `curl` and `sha256sum` for the wheel — all on `ubuntu-latest`.

## Things to know

- **The hooks live in the agent's HOME, not the checkout.** The pre-step copies `lib/claude/settings.json` to `$HOME/.claude/` and `lib/claude/context-restart.sh` under `$HOME/.claude/hooks/`. Claude Code reads user settings in `--print` mode, the checkout's `.claude/` stays untouched, and the script is a no-op unless `GH_AW_SAFE_OUTPUTS` is set, so a developer's local session never runs it.
- **Do not set `DISABLE_AUTO_COMPACT`.** The compaction threshold is the trigger; disabling auto-compaction removes it. It sits at a fraction of the window Claude Code believes the model has, so prefer a model with a 200k default window (Sonnet 5 and Opus 5 are 1M natively). Tune where it fires with `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, or declare the real window with `CLAUDE_CODE_MAX_CONTEXT_TOKENS` if your model is not one Claude Code recognises.
- **`harness.max-retries` is the restart budget.** Each restart is a fresh Claude Code process; `timeout-minutes` still bounds the job.
- **The restart is a signal because nothing else restarts.** The harness retries a failed process and takes exit 0 as done, and no hook outcome makes Claude Code exit non-zero: `continue: false`, a failing `Stop` hook, a blocking one and a failing `SessionEnd` hook all exit 0 (measured on 2.1.247). `SIGTERM` is the one exit the harness maps straight to a fresh run, so the `Stop` hook sends it once the model has ended its turn, at a turn boundary with the transcript flushed.
- **Only the merge workflow makes canonical stores.** It is where pointers are retired; engrim itself pins the newest `resume-pointer` in the boot pack and never retires one. A store seeded from anywhere else (a run artifact by hand, say) may still carry an active pointer, and the agent would boot as if resuming it.
- **Records are notes, never instructions.** The store persists across issues, so text derived from one issue can reach a run on another. The prompt says so; keep saying so.
- **Pin by tag, not digest, in `mcp-servers.container`.** The gateway's config schema accepts `name:tag` only. Pin the wheel by SHA-256 instead.
- **WAL and read-only mounts.** engrim stores are WAL-mode; SQLite needs to create the `-shm` file even to read one, so the merge mounts both stores read-write (`merge` never writes its source).
- **Docker-in-Docker runners.** If your runner's Docker daemon has its own filesystem (ARC in dind mode, for example), `/tmp` inside the gateway's containers is the daemon's, not the runner's; keep the store copies going through containers as these steps do, and mount the wheel from a path both sides share.
- **Growth.** The store grows without bound; `engrim_context`'s budget bounds what the model sees. Prune with `engrim prune` on a schedule if that matters to you.

## Adapting it

Add more agentic workflows by copying the `mcp-servers`, `steps` and `post-steps` blocks into each one and listing their display names in `engrim-memory.yml`'s `workflows:`; `lib/` serves all of them, a single canonical store is shared by all of them, and the merge queue serializes their uploads. gh-aw's `imports:` can hold the shared frontmatter once it stops changing.
