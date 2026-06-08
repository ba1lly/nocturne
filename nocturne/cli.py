"""Nocturne CLI: Autonomous coding orchestrator."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import typer

from nocturne import __version__
from nocturne._logging import SECRET_REGEX, get_logger, setup_logging
from nocturne._opencode_check import ProviderNotRegistered, check_all_models_available
from nocturne.config import Config, ConfigError, load_config
from nocturne.models import RunReport
from nocturne.orchestrator import process_task, run_batch
from nocturne.reporter import summarize, write_report
from nocturne.sources import github_issues
from nocturne.store import Store

log = get_logger("nocturne.cli")

app = typer.Typer(
    name="nocturne",
    help="Autonomous coding orchestrator",
    no_args_is_help=True,
)


class _CliState:
    """Module-level state for global options."""

    config: Path = Path.home() / ".config/nocturne/config.yaml"
    state_dir: Path = Path.home() / ".local/state/nocturne"
    verbose: bool = False


_state = _CliState()


@app.callback()
def global_callback(
    config: str = typer.Option(
        "~/.config/nocturne/config.yaml",
        "--config",
        help="Path to config file",
    ),
    state_dir: str = typer.Option(
        "~/.local/state/nocturne",
        "--state-dir",
        help="Path to state directory",
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable verbose logging"),
) -> None:
    """Global options for all commands."""
    _state.config = Path(config).expanduser()
    _state.state_dir = Path(state_dir).expanduser()
    _state.verbose = verbose


def _autoload_env_file() -> None:
    """Auto-source ~/.config/nocturne/env (or the dir adjacent to --config) into os.environ.

    Idempotent — only sets keys not already present, so the user's shell exports always win.
    KEY=VALUE format; lines starting with # or blank are skipped.
    """
    import os
    candidates = [
        Path(_state.config).expanduser().parent / "env",
        Path.home() / ".config" / "nocturne" / "env",
    ]
    seen: set[Path] = set()
    for env_file in candidates:
        env_file = env_file.resolve()
        if env_file in seen or not env_file.exists():
            continue
        seen.add(env_file)
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _load_cfg(cfg_path: Path) -> Config:
    """Load config, exit 2 on error.

    Auto-sources the env file so users don't have to `set -a; source ~/.config/nocturne/env`
    in every shell. Existing env vars always take precedence over the file.
    """
    _autoload_env_file()
    try:
        return load_config(cfg_path)
    except FileNotFoundError:
        typer.secho(f"config file not found: {cfg_path}", fg="red", err=True)
        raise typer.Exit(2)
    except ConfigError as e:
        typer.secho(f"config error: {e}", fg="red", err=True)
        raise typer.Exit(2)


def _setup_runtime(cfg: Config, state_dir: Path) -> None:
    """Setup logging and check model availability."""
    level = "DEBUG" if _state.verbose else "INFO"
    setup_logging(state_dir, level)

    try:
        check_all_models_available(cfg)
    except ProviderNotRegistered as e:
        typer.secho(f"provider not registered: {e}", fg="red", err=True)
        raise typer.Exit(2)


@app.command()
def setup(
    owner: str | None = typer.Option(None, "--owner", help="GitHub owner (required in --non-interactive)"),
    sandbox_repo: str = typer.Option("nocturne-playground", "--sandbox-repo"),
    provider: str = typer.Option("alibaba-coding-plan", "--provider"),
    reasoning_model: str | None = typer.Option(None, "--reasoning-model"),
    coding_model: str | None = typer.Option(None, "--coding-model"),
    report_model: str | None = typer.Option(None, "--report-model"),
    discord_channel: str = typer.Option("0", "--discord-channel"),
    discord_user: str = typer.Option("0", "--discord-user"),
    api_key_env: str | None = typer.Option(
        None, "--api-key-env",
        help="Override the env var name (default depends on provider). MUST be a NAME, not a key value.",
    ),
    config_dir: Path = typer.Option(
        Path.home() / ".config" / "nocturne", "--config-dir"
    ),
    install_reviewer: bool = typer.Option(False, "--install-reviewer"),
    non_interactive: bool = typer.Option(False, "--non-interactive"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Interactive wizard — writes ~/.config/nocturne/config.yaml + optional env file."""
    from nocturne.setup_wizard import ENV_NAME_RE, PROVIDERS, WizardAnswers, run_wizard

    if provider not in PROVIDERS:
        typer.secho(
            f"ERROR: unknown provider '{provider}'. Valid: {', '.join(PROVIDERS)}",
            fg="red", err=True,
        )
        raise typer.Exit(2)

    if api_key_env is not None and not ENV_NAME_RE.match(api_key_env):
        typer.secho(
            f"ERROR: --api-key-env must be an ENV VAR NAME like DASHSCOPE_API_KEY, "
            f"not an actual key value. Got: {api_key_env!r}",
            fg="red", err=True,
        )
        raise typer.Exit(2)

    prefill = WizardAnswers(
        owner=owner or "",
        sandbox_repo=sandbox_repo,
        provider=provider,
        reasoning_model=reasoning_model or "",
        coding_model=coding_model or "",
        report_model=report_model or "",
        discord_channel=discord_channel,
        discord_user=discord_user,
        api_key_env_override=api_key_env,
        install_reviewer=install_reviewer,
    )
    run_wizard(
        config_dir=config_dir,
        force=force,
        non_interactive=non_interactive,
        prefill=prefill,
    )


@app.command(name="run-once")
def run_once(
    repo: str = typer.Option(..., "--repo", help="Repository slug (owner/repo)"),
    issue: int | None = typer.Option(
        None,
        "--issue",
        help="Issue number. Omit to batch-process all eligible issues in --repo.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Skip push and PR creation"),
) -> None:
    """Process a single issue, or all eligible issues when --issue is omitted."""
    cfg = _load_cfg(_state.config)
    _setup_runtime(cfg, _state.state_dir)

    repo_cfg = None
    for r in cfg.repos:
        if r.slug == repo:
            repo_cfg = r
            break

    if repo_cfg is None:
        typer.secho(f"repo not in allowlist: {repo}", fg="red", err=True)
        raise typer.Exit(2)

    _state.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(_state.state_dir / "nocturne.db")

    if dry_run:
        log.info("Would process task; pushing and PR creation disabled.")

    if issue is None:
        report = run_batch(repo_cfg, cfg, store, dry_run=dry_run)
        report.summary = summarize(report, cfg)
        report_path = write_report(report, _state.state_dir / "reports")
        typer.echo(
            f"Batch done. {len(report.done)} done, {len(report.parked)} parked, "
            f"{len(report.skipped)} skipped, {len(report.errors)} errors. Report: {report_path}"
        )
        if len(report.errors) == 0:
            raise typer.Exit(0)
        else:
            raise typer.Exit(1)

    try:
        task = github_issues.fetch_one(repo, issue, repo_cfg)
    except github_issues.GhError as e:
        typer.secho(f"github error: {e}", fg="red", err=True)
        raise typer.Exit(2)

    store.insert_task(task)

    result_task = process_task(task, cfg, store, dry_run=dry_run)

    now = datetime.now(timezone.utc)
    report = RunReport(
        started_at=now,
        ended_at=now,
        done=[result_task] if result_task.status == "done" else [],
        parked=[],
        skipped=[],
        errors=[],
        summary="",
        token_usage=0,
    )

    report.summary = summarize(report, cfg)

    report_path = write_report(report, _state.state_dir / "reports")

    typer.echo(f"Done. Status: {result_task.status}. Report: {report_path}")

    if result_task.status == "done":
        raise typer.Exit(0)
    else:
        raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print version."""
    typer.echo(f"nocturne {__version__}")


@app.command()
def status() -> None:
    """Show daemon and task queue status."""
    _load_cfg(_state.config)  # validates config exists + parses
    _state.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(_state.state_dir / "nocturne.db")

    # Count tasks by status
    statuses = ["selected", "running", "done", "parked", "skipped", "failed", "aborted"]
    counts = {}
    for s in statuses:
        try:
            counts[s] = len(store.list_by_status(s))
        except Exception:
            counts[s] = 0

    # Get daemon paused flag
    try:
        paused_flag = store.get_daemon_flag("paused")
        paused = paused_flag == "1"
    except Exception:
        paused = False

    # Recent PRs: query tasks with pr_url not null, ordered by updated_at desc, limit 5
    recent_prs = []
    try:
        rows = store._conn.execute(
            "SELECT id, issue_number, title, pr_url, updated_at FROM tasks "
            "WHERE pr_url IS NOT NULL AND pr_url != '' "
            "ORDER BY updated_at DESC LIMIT 5"
        ).fetchall()
        recent_prs = [
            {"id": r[0], "issue_number": r[1], "title": r[2], "pr_url": r[3], "updated_at": r[4]}
            for r in rows
        ]
    except Exception:
        pass

    # Parked tasks: list with task_id and question
    parked_list = []
    try:
        for t in store.list_by_status("parked"):
            parked_list.append({
                "id": t.id, "issue_number": t.issue_number,
                "question": (t.question or "")[:80],
            })
    except Exception:
        pass

    # Print using rich Table
    from rich.console import Console
    from rich.table import Table
    console = Console()

    # Header
    console.print(f"\n[bold]Nocturne Status[/bold] (paused: {'yes' if paused else 'no'})\n")

    # Counts table
    counts_table = Table(title="Tasks by Status")
    counts_table.add_column("Status", style="cyan")
    counts_table.add_column("Count", style="green", justify="right")
    for s in statuses:
        counts_table.add_row(s, str(counts[s]))
    console.print(counts_table)

    # Parked
    if parked_list:
        parked_table = Table(title=f"Parked ({len(parked_list)})")
        parked_table.add_column("Task ID", style="yellow")
        parked_table.add_column("Issue", style="green")
        parked_table.add_column("Question", style="white")
        for p in parked_list:
            parked_table.add_row(p["id"], f"#{p['issue_number']}", p["question"])
        console.print(parked_table)

    # Recent PRs
    if recent_prs:
        pr_table = Table(title="Recent PRs (last 5)")
        pr_table.add_column("Issue", style="green")
        pr_table.add_column("Title", style="white")
        pr_table.add_column("PR URL", style="cyan")
        for pr in recent_prs:
            title = (pr["title"] or "")[:40]
            pr_table.add_row(f"#{pr['issue_number']}", title, pr["pr_url"])
        console.print(pr_table)


@app.command()
def daemon(
    once: bool = typer.Option(False, "--once", help="Run one poll cycle then exit (for testing)"),
) -> None:
    """Run the Nocturne daemon (continuous poll loop)."""
    cfg = _load_cfg(_state.config)
    _state.state_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(_state.state_dir, "INFO" if not _state.verbose else "DEBUG")

    try:
        check_all_models_available(cfg)
    except ProviderNotRegistered as e:
        typer.secho(f"ERROR: model availability check failed: {e}", fg="red", err=True)
        raise typer.Exit(2)

    store = Store(_state.state_dir / "nocturne.db")

    from nocturne.daemon_recovery import reconcile
    try:
        summary = reconcile(cfg, store)
        typer.echo(f"Startup recovery: {summary}")
    except Exception as e:
        typer.secho(f"WARNING: reconcile failed (continuing): {e}", fg="yellow", err=True)

    if once:
        import asyncio

        from nocturne.daemon import Daemon
        d = Daemon(cfg, store, bot=None)
        result = asyncio.run(d.run_one_cycle())
        typer.echo(f"One cycle complete: {result}")
        return

    from nocturne.daemon import run_daemon
    try:
        run_daemon(cfg, store)
    except KeyboardInterrupt:
        typer.echo("Daemon stopped (KeyboardInterrupt).")


@app.command()
def pause() -> None:
    """Pause the running daemon (cross-process via SQLite flag)."""
    _load_cfg(_state.config)  # validates config exists + parses
    _state.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(_state.state_dir / "nocturne.db")
    store.set_daemon_flag("paused", "1")
    typer.echo("Daemon pause flag set (running daemon will honor within 1 poll cycle).")


@app.command()
def unpause() -> None:
    """Unpause the daemon (cross-process via SQLite flag).

    Named 'unpause' (NOT 'resume') to avoid collision with `nocturne resume --task-id ...`
    which is for parked-task resume.
    """
    _load_cfg(_state.config)  # validates config exists + parses
    _state.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(_state.state_dir / "nocturne.db")
    store.set_daemon_flag("paused", "0")
    typer.echo("Daemon unpause flag set (running daemon will resume within 1 poll cycle).")


# Soul subcommand group
soul_app = typer.Typer(name="soul", help="Manage Nocturne persona file")
app.add_typer(soul_app, name="soul")


@soul_app.command(name="show")
def soul_show() -> None:
    """Show soul.md contents."""
    cfg = _load_cfg(_state.config)

    if not cfg.persona.enabled:
        typer.echo("(persona disabled in config)")
        return

    soul_path = cfg.persona.soul_path
    if soul_path is None:
        typer.echo("(no soul.md configured)")
        return

    path = Path(soul_path).expanduser()

    if not path.exists() or not path.is_file():
        typer.echo("(no soul.md configured)")
        return

    content = path.read_text(encoding="utf-8")
    redacted = SECRET_REGEX.sub("***", content)
    typer.echo(redacted)


@soul_app.command(name="set")
def soul_set(path: Path = typer.Argument(..., help="Source file")) -> None:
    """Install soul.md from a file."""
    # Resolve and validate source
    source = path.expanduser().resolve()
    if not source.exists():
        typer.secho(f"source file not found: {source}", fg="red", err=True)
        raise typer.Exit(2)

    content = source.read_text(encoding="utf-8")

    # Check size
    if len(content) > 8192:
        typer.secho("soul.md exceeds 8192 char cap", fg="red", err=True)
        raise typer.Exit(2)

    # Check deny patterns
    DENY_REGEX = re.compile(
        r"gho_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{40,}|AKIA[0-9A-Z]{16}|xox[bap]-[A-Za-z0-9-]{30,}|AIza[0-9A-Za-z_-]{35}"
    )
    if DENY_REGEX.search(content):
        typer.secho("soul.md contains secret pattern (denied)", fg="red", err=True)
        raise typer.Exit(2)

    # Load config and resolve destination
    cfg = _load_cfg(_state.config)
    soul_path = cfg.persona.soul_path
    if soul_path is None:
        typer.secho("soul_path not configured", fg="red", err=True)
        raise typer.Exit(2)

    dest = Path(soul_path).expanduser()

    # Install
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)

    typer.echo(f"Installed soul.md to {dest}")


@soul_app.command(name="edit")
def soul_edit() -> None:
    """Edit soul.md in $EDITOR."""
    cfg = _load_cfg(_state.config)
    soul_path = cfg.persona.soul_path
    if soul_path is None:
        typer.secho("soul_path not configured", fg="red", err=True)
        raise typer.Exit(2)

    dest = Path(soul_path).expanduser()

    # Create if missing
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("", encoding="utf-8")

    # Backup if exists
    backup = dest.with_suffix(dest.suffix + ".bak")
    if dest.exists():
        shutil.copyfile(dest, backup)

    # Open editor
    editor = os.environ.get("EDITOR", "vi")
    try:
        subprocess.run([editor, str(dest)], check=True)
    except subprocess.CalledProcessError:
        typer.secho("editor exited with error", fg="red", err=True)
        raise typer.Exit(2)

    # Validate on save
    content = dest.read_text(encoding="utf-8")

    if len(content) > 8192:
        typer.secho("soul.md exceeds 8192 char cap; reverting", fg="red", err=True)
        if backup.exists():
            shutil.copyfile(backup, dest)
        raise typer.Exit(2)

    DENY_REGEX = re.compile(
        r"gho_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{40,}|AKIA[0-9A-Z]{16}|xox[bap]-[A-Za-z0-9-]{30,}|AIza[0-9A-Za-z_-]{35}"
    )
    if DENY_REGEX.search(content):
        typer.secho("soul.md contains secret pattern (denied); reverting", fg="red", err=True)
        if backup.exists():
            shutil.copyfile(backup, dest)
        raise typer.Exit(2)

    typer.echo(f"Edited {dest}")


# Skill subcommand group
skill_app = typer.Typer(name="skill", help="Manage OpenCode skills used by Nocturne")
app.add_typer(skill_app, name="skill")


@skill_app.command(name="install")
def skill_install(
    source: str = typer.Argument(..., help="URL, file path, or directory"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing skill"),
) -> None:
    """Install a skill from URL, local file, or local directory."""
    from nocturne.skills import SkillError, SkillExists, SkillInvalid, install_skill

    try:
        name = install_skill(source, force=force)
        typer.echo(f"Installed: {name}")
    except SkillExists as e:
        typer.secho(f"ERROR: {e}", fg="red", err=True)
        raise typer.Exit(2)
    except SkillInvalid as e:
        typer.secho(f"ERROR: {e}", fg="red", err=True)
        raise typer.Exit(2)
    except SkillError as e:
        typer.secho(f"ERROR: {e}", fg="red", err=True)
        raise typer.Exit(1)


@skill_app.command(name="list")
def skill_list() -> None:
    """List all installed skills."""
    from rich.console import Console
    from rich.table import Table

    from nocturne.skills import list_skills

    skills = list_skills()
    if not skills:
        typer.echo("(no skills installed)")
        return
    table = Table(title=f"Installed skills ({len(skills)})")
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="white")
    table.add_column("Enabled", style="green")
    for s in skills:
        table.add_row(s.name, s.description[:60], "yes" if s.enabled else "no")
    Console().print(table)


@skill_app.command(name="enable")
def skill_enable(name: str = typer.Argument(...)) -> None:
    """Re-enable a previously disabled skill."""
    from nocturne.skills import SkillNotFound, enable_skill

    try:
        enable_skill(name)
        typer.echo(f"Enabled: {name}")
    except SkillNotFound as e:
        typer.secho(f"ERROR: {e}", fg="red", err=True)
        raise typer.Exit(1)


@skill_app.command(name="disable")
def skill_disable(name: str = typer.Argument(...)) -> None:
    """Disable a skill (OpenCode will skip it)."""
    from nocturne.skills import SkillNotFound, disable_skill

    try:
        disable_skill(name)
        typer.echo(f"Disabled: {name}")
    except SkillNotFound as e:
        typer.secho(f"ERROR: {e}", fg="red", err=True)
        raise typer.Exit(1)


@skill_app.command(name="info")
def skill_info(name: str = typer.Argument(...)) -> None:
    """Show details for an installed skill."""
    from nocturne.skills import list_skills

    skills = list_skills()
    s = next((x for x in skills if x.name == name), None)
    if s is None:
        typer.secho(f"ERROR: skill not found: {name}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"Name: {s.name}")
    typer.echo(f"Description: {s.description}")
    typer.echo(f"Source: {s.source}")
    typer.echo(f"Installed: {s.installed_at}")
    typer.echo(f"Enabled: {s.enabled}")


@skill_app.command(name="uninstall")
def skill_uninstall(
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Uninstall a skill (removes the skill directory)."""
    from nocturne.skills import SkillNotFound, uninstall_skill

    if not yes:
        confirm = typer.confirm(f"Uninstall skill '{name}'?", default=False)
        if not confirm:
            typer.echo("Aborted.")
            return
    try:
        uninstall_skill(name)
        typer.echo(f"Uninstalled: {name}")
    except SkillNotFound as e:
        typer.secho(f"ERROR: {e}", fg="red", err=True)
        raise typer.Exit(1)


@app.command()
def resume(
    task_id: str | None = typer.Option(None, "--task-id", "-t", help="Parked task id (e.g., owner/repo#123)"),
    answer: str | None = typer.Option(None, "--answer", "-a", help="Answer text (if omitted, prompted interactively)"),
    list_parked: bool = typer.Option(False, "--list", "-l", help="List all parked tasks"),
) -> None:
    """Resume a parked task with a human answer, OR list all parked tasks via --list."""
    cfg = _load_cfg(_state.config)
    setup_logging(_state.state_dir, "DEBUG" if _state.verbose else "INFO")
    _state.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(_state.state_dir / "nocturne.db")

    if list_parked:
        from nocturne.askflow import list_parked as _list_parked

        parked = _list_parked(store)
        if not parked:
            typer.echo("(no parked tasks)")
            return

        # Use rich Table for clean output
        from rich.console import Console
        from rich.table import Table

        table = Table(title=f"Parked tasks ({len(parked)})")
        table.add_column("Task ID", style="cyan", no_wrap=False)
        table.add_column("Issue", style="green")
        table.add_column("Title", style="white", no_wrap=False, max_width=40)
        table.add_column("Question", style="yellow", no_wrap=False, max_width=60)
        for p in parked:
            q = (p.question or "")[:200] + ("..." if p.question and len(p.question) > 200 else "")
            title = (p.title or "")[:38] + ("..." if p.title and len(p.title) > 38 else "")
            table.add_row(p.id, f"#{p.issue_number}", title, q)
        Console().print(table)
        return

    if not task_id:
        typer.secho("ERROR: --task-id required (or use --list to see parked tasks)", fg="red", err=True)
        raise typer.Exit(2)

    # Validate task_id format: owner/repo#issue_number
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*#\d+$", task_id):
        typer.secho(f"ERROR: invalid task_id format (expected owner/repo#N): {task_id}", fg="red", err=True)
        raise typer.Exit(2)

    # Lookup task before invoking
    task = store.get_task(task_id)
    if task is None:
        typer.secho(f"ERROR: task not found: {task_id}", fg="red", err=True)
        raise typer.Exit(1)
    if task.status != "parked":
        typer.secho(f"ERROR: task {task_id} is not parked (status={task.status})", fg="red", err=True)
        raise typer.Exit(1)

    # Interactive prompt if --answer not provided
    if not answer:
        typer.echo(f"Question for task {task_id}:")
        typer.echo(f"  {task.question or '(no question recorded)'}")
        answer = typer.prompt("Your answer", prompt_suffix=": ")

    if not answer or not answer.strip():
        typer.secho("ERROR: answer cannot be empty", fg="red", err=True)
        raise typer.Exit(2)

    # Invoke askflow.resume_with_answer
    from nocturne.askflow import resume_with_answer

    try:
        result = resume_with_answer(task_id, answer, cfg, store)
    except Exception as e:
        typer.secho(f"ERROR: resume failed: {e}", fg="red", err=True)
        raise typer.Exit(1)

    typer.echo(f"Resumed task {task_id}. Status: {result.status}")
    # Log at DEBUG (NOT INFO) per plan must-not-do: "Do NOT log the answer at INFO level"
    log_resume = get_logger("nocturne.cli.resume")
    log_resume.debug("resumed task %s with answer (len=%s)", task_id, len(answer))
