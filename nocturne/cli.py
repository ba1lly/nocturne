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
from nocturne.reporter import summarize, write_report
from nocturne.sources import github_issues
from nocturne.store import Store
from nocturne.models import RunReport
from nocturne.orchestrator import process_task, run_batch

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


def _load_cfg(cfg_path: Path) -> Config:
    """Load config, exit 2 on error."""
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
    """Show status (not implemented in M1)."""
    typer.echo("(not implemented in M1)")


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
        from rich.table import Table
        from rich.console import Console

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
