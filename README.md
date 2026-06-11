# Nocturne

> The autonomous coding agent that works the night shift. Built on opencode.

[![CI](https://img.shields.io/github/actions/workflow/status/ba1lly/nocturne/ci.yml?branch=main&label=CI)](https://github.com/ba1lly/nocturne/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/ba1lly/nocturne?label=release)](https://github.com/ba1lly/nocturne/releases/latest)
[![python](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)
[![license](https://img.shields.io/github/license/ba1lly/nocturne)](LICENSE)

## What it does

You label an issue `agent` on a configured repo. Nocturne:

1. **Triages** the issue (LLM classifies as `DOABLE`, `NEED_INPUT`, or `SKIP`)
2. **Spawns one opencode session** in an isolated git worktree off `main`
3. **opencode does the full cycle inline**:
   - Reads the issue, makes code changes + tests
   - Runs `verify_cmd` (e.g. `pytest -q`) until it passes
   - Invokes the reviewer (custom skill if installed, otherwise opencode's built-in `/review`) and addresses every finding regardless of severity
   - Loops review->fix up to `budget_attempts` times
   - Writes a markdown PR description to `.nocturne-pr-body.md`
4. **Nocturne reads the PR body file** and creates the PR with a rich title + body, authored as `Nocturne <nocturne@noreply.localhost>`
5. **Discord notifies** you per task completion (channel + mention configurable)
6. **NEED_INPUT issues get parked** with a question comment on the issue; resume them later with `nocturne resume --task-id ... --answer ...`
7. **SKIP issues get a marker comment** explaining why they were skipped (idempotent - never duplicates)

Optionally, once a PR is open Nocturne can **keep shepherding it** (set `reactions.enabled: true`):

- **Failing CI** -> re-dispatches the agent on the same branch to fix and pushes a follow-up commit
- **Reviewer requested changes** -> addresses the comments on the same branch
- **Approved and green** -> notifies you it's ready (you merge; it never does)

Nocturne never merges, never force-pushes, never touches `main`.

## Quick start

```bash
git clone https://github.com/ba1lly/nocturne
cd nocturne
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Interactive setup wizard - picks provider, models, Discord IDs, writes ~/.config/nocturne/config.yaml + env file
nocturne setup

# Optional: enable the Nocturne persona (style + tone shaping for the coding agent; see soul.md)
mkdir -p ~/.config/nocturne && cp soul.md ~/.config/nocturne/soul.md

# Run once against a single issue (no daemon)
nocturne run-once --repo OWNER/REPO --issue 42

# Or batch all eligible (agent-labelled) issues for one repo
nocturne run-once --repo OWNER/REPO

# Or run continuously across every repo in your config
nocturne daemon

# Or install as a user-mode systemd service
bash scripts/install-systemd.sh
systemctl --user start nocturne
journalctl --user -u nocturne -f
```

See [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) for the full walkthrough, [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for 24/7 deployment.

## How it picks up work

**Multi-repo by default.** Nocturne tracks every repository listed under `repos[]` in `~/.config/nocturne/config.yaml`. Each entry declares its own GitHub slug, local clone path, base branch, eligibility label, and `verify_cmd`:

```yaml
repos:
  - slug: "owner/repo-a"
    checkout_path: "~/projects/repo-a"
    label: "agent"
    base: "main"
    verify_cmd: "pytest -q"
    require_new_test: true
  - slug: "owner/repo-b"
    checkout_path: "~/projects/repo-b"
    label: "nocturne"           # different label per repo is fine
    base: "main"
    verify_cmd: "npm test"
    require_new_test: false
```

| Mode | Behaviour |
|---|---|
| `nocturne daemon` | Polls **every repo** in `repos[]` once per cycle (`daemon.poll_interval_sec`, default 300s) |
| `nocturne run-once --repo X/Y` | Processes **one** repo X/Y; batches all eligible issues on it |
| `nocturne run-once --repo X/Y --issue 42` | Processes **one** specific issue on one repo |

**Eligibility.** An issue is eligible iff it is OPEN, has the configured `label` (default `agent`), and is not already tracked in Nocturne's local SQLite store as `done`, `parked`, `skipped`, `failed`, or `aborted`. Resumed parked tasks (status `selected` with an answer) bypass triage and go straight to the work loop.

**One task at a time, ever.** Nocturne runs strictly serial - one opencode session per cycle, no parallel issue processing. This is intentional: it keeps git worktree state, token spend, and Discord HITL latency easy to reason about.

**Quiet hours + budgets** (all in `daemon:` / `guardrails:`):
- `quiet_hours: [0, 1, 2, 3, 4]` - daemon skips poll cycles when the current hour is in this list (UTC by default; set `quiet_hours_tz` to an IANA name like `America/New_York` to use local hours)
- `poll_interval_sec: 300` - wait between cycles
- `global_wallclock_hours: 8` - daemon stops scheduling new work after this rolling window
- `token_budget: 2_000_000` - hard stop when cumulative token usage exceeds this. Usage is measured from each opencode session's event stream (input + output + reasoning + cache tokens) and accumulated per task; sessions that report no usage count as 0
- `max_attempts: 3` - per-issue retry budget before marking `failed`
- `per_task_timeout_min: 25` - kill opencode subprocess after this

**Pause / unpause** are cross-process (SQLite-backed):

```bash
nocturne pause      # writes daemon_state.paused = '1'; running daemon stops scheduling next cycle
nocturne unpause    # clears the flag; daemon resumes within <=5s (poll-interval-aware)
nocturne status     # daemon + queue snapshot (counts by status, parked questions, recent PRs)
```

## Reviewer

Up front: Nocturne uses a configurable reviewer-skill chain. The defaults in `cfg.review.fallback_repos` point at **private repos used by the maintainer's team**. If you don't have access to them (which is the case for everyone outside the maintainer's team), Nocturne transparently falls back to **opencode's built-in `/review` slash command**. You always get a working review step regardless of what's installed locally - no setup required.

Resolution order at task-render time:

1. **Local skill** at `~/.config/opencode/skills/reviewer/` exists -> prompt invokes the `@reviewer` subagent
2. **`nocturne skill install-reviewer` was run** and one of `cfg.review.fallback_repos` was accessible to your `gh` credentials -> same `@reviewer` invocation, sourced from that repo
3. **Neither** -> prompt invokes opencode's built-in `/review` slash command

The behaviour contract is identical across all three paths: report findings at every severity, then the coding agent addresses every finding and re-verifies. Loops up to `cfg.review.budget_attempts` times (default 2).

To install your own custom reviewer skill (your team's preferred style, your own SKILL.md, a forked review skill, etc.):

```bash
nocturne skill install <SOURCE>          # local path, URL, or owner/repo
nocturne skill install-reviewer          # try cfg.review.fallback_repos in order, then /review
nocturne skill list                      # what's installed and enabled
nocturne skill disable reviewer          # temporarily switch to /review fallback
```

To override the chain with your own private skill sources, edit `~/.config/nocturne/config.yaml`:

```yaml
review:
  fallback_repos:
    - "your-org/your-reviewer-skill"
    - "your-org/another-reviewer-skill"
```

If you don't install anything, the `/review` fallback is automatic.

## Architecture

```
                         GitHub
                            │
                            ▼
   ┌────────────────────────────────────────────┐
   │  Nocturne daemon (poll loop, multi-repo)   │
   │  - per repo: fetch_eligible (gh)           │
   │  - partition_eligible (skip done/parked)   │
   │  - triage_batch (LLM)                      │
   │  - process_task per DOABLE                 │
   │  - askflow.park_task per NEED_INPUT        │
   │  - post_skip_comment per SKIP              │
   └─────┬──────────────────────────────────────┘
         │ process_task
         ▼
   ┌────────────────────────────────────────────┐
   │  git worktree (off main, per attempt)      │
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
   │  Step 3: invoke reviewer (skill or /review)│
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
- **Pre-push hook prevents `--force` pushes.** Installed by `make_worktree` per worktree; rejects deletions; allows new-branch pushes.
- **Single asyncio event loop** shared by the daemon poll loop, Discord bot coroutine, and aiohttp healthcheck - no IPC, no separate processes.

## Configuration

Start from [`config.example.yaml`](config.example.yaml). The wizard (`nocturne setup`) builds the same shape interactively.

**Required environment variables** (in `~/.config/nocturne/env` or your shell):

| Variable | Purpose |
|---|---|
| `<PROVIDER>_API_KEY` (e.g. `DASHSCOPE_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) | Provider auth for triage + report models; exact name comes from `providers.*.api_key_env` |
| `NOCTURNE_DISCORD_TOKEN` | Discord bot token (required if `discord.enabled: true`) |

**Required config fields:**

- `github.owner` - your GitHub user/org
- `discord.channel_id` and `discord.mention_user_id` - non-zero before daemon start (or set `discord.enabled: false`)
- `repos[]` - at least one entry with `slug`, `checkout_path`, `verify_cmd`, `base`, `label`

**Multi-provider models** - `models.reasoning`, `models.report`, optional `models.coding` are full `provider/model` strings (e.g. `anthropic/claude-sonnet-4-5`, `openai/gpt-5`). The `<provider>` prefix must exist in your `providers:` map. Mix providers per role freely.

**Optional but useful:**

- `persona.soul_path` - path to a markdown file shaping opencode's tone/style. Default is `~/.config/nocturne/soul.md`; a starter persona ships at [`soul.md`](soul.md) in the repo root. Copy it (`cp soul.md ~/.config/nocturne/soul.md`) or point `persona.soul_path` at the repo file. Capped at 8192 chars; injected on each task. The persona block is internal only - the prompt forbids leaking any of it into PRs/commits/code.
- `review.budget_attempts` - how many review->fix rounds opencode does per task (default `2`).
- `review.fallback_repos` - list of GitHub repo slugs to try as reviewer-skill sources (used by `nocturne skill install-reviewer`). Empty list = always fall back to opencode's `/review`.
- `guardrails.global_wallclock_hours`, `guardrails.token_budget` - hard stops.
- `daemon.quiet_hours` - list of hours to skip cycles (UTC unless `daemon.quiet_hours_tz` is set to an IANA timezone name).
- `healthcheck.bind_port` - change from default 8765 if it collides.
- `reactions.enabled` - opt into the post-PR feedback loop (default `false`). When on, the **daemon** (not `run-once`) watches PRs Nocturne opened and reacts: `reactions.fix_failing_ci` re-dispatches the agent to fix failing CI, `reactions.address_review_comments` handles a reviewer's "changes requested", and `reactions.notify_when_ready` pings you when a PR is approved and green. It never merges. `reactions.max_fix_attempts` (default `3`) caps autonomous fixes per PR before escalating; `reactions.watch_ttl_hours` (default `168`) stops watching after that long. To fix CI it reads the failing job's logs, so the target repo's CI must surface them (e.g. GitHub Actions).

## CLI surface

```
nocturne setup                       # interactive setup wizard
nocturne run-once                    # one batch (--repo required; optional --issue, --dry-run)
nocturne daemon                      # continuous poll loop (optional --once for testing)
nocturne pause / unpause             # cross-process pause flag (via SQLite daemon_state)
nocturne status                      # daemon + queue snapshot
nocturne resume                      # resume a parked task with an answer (--task-id, --answer)
nocturne soul show/set/edit          # manage the persona file
nocturne skill install               # install a SKILL.md from local path, URL, or owner/repo
nocturne skill install-reviewer      # install reviewer skill via configured sources
nocturne skill list/info             # inspect installed skills
nocturne skill enable/disable        # toggle a skill without uninstalling
nocturne skill uninstall             # remove a skill
nocturne version
```

## Operational concerns

**Healthcheck endpoint.** Daemon serves `GET 127.0.0.1:8765/health` (host/port configurable):

```bash
curl -sS http://127.0.0.1:8765/health    # 200 = healthy, 503 = stale or paused
```

Returns 200 iff: daemon is running, SQLite is reachable, `last_poll_at` is within `staleness_factor * poll_interval_sec`, and a brief startup grace period has elapsed. Returns 503 otherwise. Bind to localhost only - no auth.

**24/7 deployment via user-mode systemd** (never root):

```bash
bash scripts/install-systemd.sh          # idempotent install
systemctl --user enable --now nocturne   # start + boot-survive (also: loginctl enable-linger)
journalctl --user -u nocturne -f         # follow logs
bash scripts/uninstall-systemd.sh        # clean removal
```

**Crash recovery.** On startup the daemon runs `git worktree prune`, reconciles stale worktree registrations, then checks every task with `status='running'` against its stored `opencode_pid`. Dead PIDs flip the task to `failed`; the next cycle treats it as eligible again.

**State lives in SQLite** at `~/.local/state/nocturne/nocturne.db` (override with `--state-dir`). All connections use `PRAGMA busy_timeout = 5000` + WAL mode so `nocturne pause` from a separate shell is visible to the running daemon within one poll tick.

## Testing

```bash
.venv/bin/pytest -q                   # unit suite, no external calls
.venv/bin/mypy nocturne               # type check
.venv/bin/ruff check nocturne tests   # lint

# Live acceptance scripts (spawn real opencode subprocesses + create real PRs against a sandbox repo)
bash scripts/m1_acceptance.sh
bash scripts/m2_acceptance.sh
bash scripts/m3_acceptance.sh
bash scripts/m4_acceptance.sh
bash scripts/m5_acceptance.sh
```

All acceptance scripts run against a configurable sandbox (`SANDBOX_REPO` env var) so you can point them at your own playground without touching production repos.

## Safety guarantees (enforced by code + tested)

- **No force-push, ever.** Per-worktree pre-push hook rejects `--force` / `+refspec`; `enforce_no_force_push` guards every `push` argv.
- **No commits on `main`.** `WorktreeContext.__exit__` asserts the worktree isn't on `base` after the loop. Branch naming includes attempt counter (`nocturne/issue-{N}-{attempt}`) to make retries collision-safe.
- **No auto-merge.** Nocturne never calls `gh pr merge`. Whoever reviews the PR merges it.
- **No persona / operator info in artifacts.** The task prompt's final paragraph forbids it; the worktree's `.git/info/exclude` keeps `.nocturne-pr-body.md` (which contains persona context) out of commits.
- **No `--dangerously-skip-permissions`** on any opencode invocation - `enforce_no_dangerous_opencode_flags` rejects it in the argv before subprocess spawn.
- **Idempotent skip / question / install operations.** `triage.post_skip_comment`, `askflow.post_park_comment`, `skills.install_skill` all no-op cleanly when the side effect is already in place.
- **Dry-run is a true dry-run.** `nocturne run-once --dry-run` never writes to GitHub, never pushes a branch, never posts a Discord notification.
- **Wallclock + token budgets.** `guardrails.global_wallclock_hours` and `guardrails.token_budget` are checked between tasks and break the batch when exceeded.
- **Issue-state recheck before PR.** After opencode exits, Nocturne calls `gh issue view --json state`; if the issue was closed during execution, the task is marked `aborted` and no PR is created.
- **Sandbox-only by default.** `check_repo_allowed` rejects any `repo_slug` not listed in `cfg.repos`. No path for a stray prompt or LLM hallucination to target an unconfigured repo.

## License

MIT (see [LICENSE](LICENSE)).

## Contributing

Patches welcome via PR. Run the test suite + the three quality gates (pytest, mypy, ruff) before submitting. New features touching the orchestrator should land their own acceptance script under `scripts/` alongside the existing `m1-m5` scripts and exercise the path live before merging.
