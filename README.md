# Nocturne

> Autonomous coding orchestrator. Picks up labelled GitHub issues, drives **opencode** to fix them, self-reviews every change, and ships clean PRs while you sleep.

[![tests](https://img.shields.io/badge/tests-520%20passing-brightgreen)](#testing)
[![python](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)
[![status](https://img.shields.io/badge/M1--M5-shipped-brightgreen)](#status)

## What it does

You label an issue `agent` on a configured repo. Nocturne:

1. **Triages** the issue (LLM classifies as `DOABLE`, `NEED_INPUT`, or `SKIP`)
2. **Spawns one opencode session** in an isolated git worktree off `main`
3. **opencode does the full cycle inline**:
   - Reads the issue, makes code changes + tests
   - Runs `verify_cmd` (e.g. `pytest -q`) until it passes
   - Invokes `@reviewer` (a read-only subagent) and addresses every finding regardless of severity
   - Loops review→fix up to `budget_attempts` times
   - Writes a markdown PR description to `.nocturne-pr-body.md`
4. **Nocturne reads the PR body file** and creates the PR with a rich title + body, authored as `Nocturne <nocturne@noreply.localhost>`
5. **Discord notifies** you per task completion (channel + mention configurable)
6. **NEED_INPUT issues get parked** with a question comment on the issue; resume them later with `nocturne resume --task-id … --answer …`
7. **SKIP issues get a marker comment** explaining why they were skipped (idempotent — never duplicates)

Nocturne never merges, never force-pushes, never touches `main`.

## Quick start

```bash
git clone https://github.com/ba1lly/nocturne
cd nocturne
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Interactive setup wizard — picks provider, models, Discord IDs, writes ~/.config/nocturne/config.yaml + env file
nocturne setup

# Run once against a single issue (no daemon)
nocturne run-once --repo OWNER/REPO --issue 42

# Or run continuously
nocturne daemon

# Or install as a user-mode systemd service
bash scripts/install-systemd.sh
systemctl --user start nocturne
journalctl --user -u nocturne -f
```

See [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) for the full walkthrough, [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for 24/7 deployment.

## Architecture

```
                         GitHub
                            │
                            ▼
   ┌────────────────────────────────────────────┐
   │  Nocturne daemon (poll loop)               │
   │  - fetch_eligible (gh)                     │
   │  - partition_eligible (skip done/parked)   │
   │  - triage_batch (LLM)                      │
   │  - process_task per DOABLE                 │
   │  - askflow.park_task per NEED_INPUT        │
   │  - post_skip_comment per SKIP              │
   └─────┬──────────────────────────────────────┘
         │ process_task
         ▼
   ┌────────────────────────────────────────────┐
   │  git worktree (off main)                   │
   │  .git/info/exclude:                        │
   │    .nocturne-pr-body.md                    │
   │    .reviews/                               │
   └─────┬──────────────────────────────────────┘
         │ opencode run --dir <wt> -- <task prompt>
         ▼
   ┌────────────────────────────────────────────┐
   │  opencode session (one subprocess)         │
   │  Step 1: make changes + tests              │
   │  Step 2: verify_cmd until exit 0           │
   │  Step 3: @reviewer (read-only subagent)    │
   │  Step 4: address every finding             │
   │  Step 5: re-review loop                    │
   │  Step 6: write .nocturne-pr-body.md        │
   │  Step 7: stop (no git ops)                 │
   └─────┬──────────────────────────────────────┘
         │ opencode exits
         ▼
   ┌────────────────────────────────────────────┐
   │  Nocturne (orchestrator)                   │
   │  - read .nocturne-pr-body.md               │
   │  - commit_push (squash to Nocturne identity)│
   │  - open_pr (rich title + body)             │
   │  - write review_runs row                   │
   │  - post Discord 🟢 #N <title>              │
   └────────────────────────────────────────────┘
```

### Key invariants

- **opencode never commits, pushes, or touches `main`.** The task prompt forbids it; nocturne handles all git operations.
- **commit_push squashes any stray opencode commits** via `git reset --soft origin/<base>` before re-committing under the Nocturne identity. Defense in depth against the prompt being ignored.
- **`.nocturne-pr-body.md` and `.reviews/` are excluded via `.git/info/exclude`** at worktree creation time, so `git add -A` can never sweep nocturne-internal artifacts into a real commit.
- **Pre-push hook prevents `--force` pushes.** Installed by `make_worktree`.

## Configuration

Start from [`config.example.yaml`](config.example.yaml). The wizard (`nocturne setup`) builds the same shape interactively.

Required environment (in `~/.config/nocturne/env` or your shell):

| Variable | Purpose |
|---|---|
| `DASHSCOPE_API_KEY` (or `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc. — depends on `providers.*.api_key_env`) | Provider auth for triage + report models |
| `NOCTURNE_DISCORD_TOKEN` | Discord bot token (required if `discord.enabled: true`) |

Required config fields:

- `github.owner` — your GitHub user/org
- `discord.channel_id` and `discord.mention_user_id` — non-zero before daemon start
- `repos[]` — at least one entry with `slug`, `checkout_path`, `verify_cmd`, `base`, `label`

Optional but useful:

- `persona.soul_path` — path to a markdown file shaping opencode's tone/style. Capped at 8192 chars; nocturne reads and injects on each task. The persona block is internal only — the prompt forbids leaking any of it into PRs/commits/code.
- `review.fallback_repos` — auto-install reviewer skill from these GitHub repos (via `gh repo clone`) if not already installed locally. Defaults to `["ba1lly/reviewer-config", "Defizoo/reviewer"]` (both private; the user's gh credentials handle access).
- `review.budget_attempts` — how many review→fix rounds opencode does per task (default `2`).
- `guardrails.global_wallclock_hours`, `guardrails.token_budget` — hard stops.

## CLI surface

```
nocturne setup              # interactive setup wizard
nocturne run-once           # one batch (--repo, optional --issue, optional --dry-run)
nocturne daemon             # continuous poll loop (optional --once for testing)
nocturne pause / unpause    # cross-process pause flag (via SQLite daemon_state)
nocturne status             # daemon + queue state
nocturne resume             # resume a parked task with an answer (--task-id, --answer)
nocturne soul show/set/edit # manage the persona file
nocturne skill install/list/enable/disable/uninstall  # manage opencode skills
nocturne version
```

## Status

### Shipped milestones

| Milestone | What it covers | Acceptance |
|---|---|---|
| M1 | Single-issue PR creation end-to-end | `scripts/m1_acceptance.sh` ✅ |
| M2 | Multi-issue batch + triage skip-comment idempotency | `scripts/m2_acceptance.sh` ✅ |
| M3 | Park + question comment + `nocturne resume` round-trip + sentinel false-positive guard | `scripts/m3_acceptance.sh` ✅ |
| M4 | Daemon + Discord (live bring-up) + SIGTERM/SIGKILL recovery | `scripts/m4_acceptance.sh` partial ✅ (Tests 1-2 in bash, Tests 3-5 in pytest behind `NOCTURNE_RUN_M4=1`) |
| M5 | Reviewer post-PR loop + systemd + healthcheck stale + multi-provider validation | `scripts/m5_acceptance.sh` mostly ✅ (Tests 1-4 PASS, Test 5 see [#1](https://github.com/ba1lly/nocturne/issues/1), Test 6 see [#2](https://github.com/ba1lly/nocturne/issues/2)) |

### Live-verified PRs (most recent)

| Run | PR | Title | Notes |
|---|---|---|---|
| M5 #3 | ba1lly/nocturne-playground#35 | Fix divide() off-by-one in src/playground/math.py | Approach 1 — rich body, @reviewer ran twice, no leaks |
| M5 #2 | ba1lly/nocturne-playground#32 | Fix divide() off-by-one in src/playground/math.py | Approach 1 first live success |

Branches, PR descriptions, and reviewer findings live entirely in the sandbox repo so the production repo's history stays clean.

### Open issues

- [#1 M5 Test 5: daemon /health stays 503 after `nocturne unpause`](https://github.com/ba1lly/nocturne/issues/1)
- [#2 M5 Test 6 (multi-provider validation) needs live run](https://github.com/ba1lly/nocturne/issues/2)
- [#3 systemd unit: `StartLimitIntervalSec` in wrong section](https://github.com/ba1lly/nocturne/issues/3)
- [#4 Cleanup: delete now-dead review.py + daemon._schedule_review](https://github.com/ba1lly/nocturne/issues/4)

## Testing

```bash
# Full test suite (no external calls)
.venv/bin/pytest -q                   # 520 tests, ~12s

# Type checks
.venv/bin/mypy nocturne

# Lint
.venv/bin/ruff check nocturne tests

# Live acceptance tests (each spawns a real opencode subprocess, ~30+ min)
bash scripts/m1_acceptance.sh
bash scripts/m2_acceptance.sh
bash scripts/m3_acceptance.sh
bash scripts/m4_acceptance.sh
bash scripts/m5_acceptance.sh
```

## Safety guarantees (enforced by code + tested)

- **No force-push, ever.** Pre-push hook in every worktree rejects `--force` / `+refspec`. `enforce_no_force_push` guards every `push` argv.
- **No commits on `main`.** `WorktreeContext.__exit__` asserts the worktree isn't on `base` after the loop.
- **No persona / operator info in artifacts.** The task prompt's final paragraph forbids it; the worktree's `.git/info/exclude` keeps `.nocturne-pr-body.md` (which contains persona context) out of commits.
- **Idempotent skip / question / install operations.** `triage.post_skip_comment`, `askflow.post_park_comment`, `skills.install_skill` all no-op cleanly when the side effect is already in place.
- **Dry-run is a true dry-run.** `nocturne run-once --dry-run` never writes to GitHub, never pushes a branch, and never posts a Discord notification.
- **Wallclock + token budgets.** `guardrails.global_wallclock_hours` and `guardrails.token_budget` are checked between tasks and break the batch when exceeded.

## License

MIT (see [LICENSE](LICENSE)).

## Contributing

Patches welcome via PR. Run the test suite + the three quality gates (pytest, mypy, ruff) before submitting. New features touching the orchestrator should land their own acceptance script under `scripts/` and be referenced from this README's milestone table.
