"""Interactive setup wizard — writes ~/.config/nocturne/config.yaml + optional env file.

Replaces the previous bash scripts/setup.sh. typer.prompt + rich give bulletproof
input handling (no terminal state desync, real hide_input, validated choices).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

PROVIDERS: dict[str, dict[str, str]] = {
    "alibaba-coding-plan": {
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "reasoning": "qwen3.6-plus",
        "coding": "qwen3-coder-plus",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
        "reasoning": "claude-opus-4-5",
        "coding": "claude-sonnet-4-5",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "reasoning": "gpt-5",
        "coding": "gpt-5",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "reasoning": "moonshot-v1-32k",
        "coding": "moonshot-v1-32k",
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPUAI_API_KEY",
        "reasoning": "glm-4.6",
        "coding": "glm-4.6",
    },
}

ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass
class WizardAnswers:
    owner: str = ""
    sandbox_repo: str = "nocturne-playground"
    provider: str = "alibaba-coding-plan"
    reasoning_model: str = ""
    coding_model: str = ""
    report_model: str = ""
    discord_channel: str = "0"
    discord_user: str = "0"
    api_key_value: Optional[str] = field(default=None, repr=False)
    discord_token_value: Optional[str] = field(default=None, repr=False)
    install_reviewer: bool = False
    api_key_env_override: Optional[str] = None

    @property
    def api_key_env(self) -> str:
        return self.api_key_env_override or PROVIDERS[self.provider]["api_key_env"]

    @property
    def discord_enabled(self) -> bool:
        return self.discord_channel != "0" or self.discord_user != "0"


def _validate_env_name(value: str) -> None:
    if not ENV_NAME_RE.match(value):
        raise typer.BadParameter(
            f"expected an ENV VAR NAME (e.g. DASHSCOPE_API_KEY), got: {value!r}"
        )


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_suffix(path.suffix + f".bak.{stamp}")
    shutil.copy2(path, bak)
    return bak


def _scan_for_leaked_secrets(config_path: Path) -> list[str]:
    """Return suspicious api_key_env values from an existing config (likely leaked secrets)."""
    if not config_path.exists():
        return []
    suspicious: list[str] = []
    for line in config_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*api_key_env:\s*\"?([^\"\s#]+)\"?", line)
        if m:
            val = m.group(1)
            if not ENV_NAME_RE.match(val):
                suspicious.append(val)
    return suspicious


def _detect_provider_from_existing_config(config_path: Path) -> Optional[str]:
    if not config_path.exists():
        return None
    text = config_path.read_text(encoding="utf-8")
    for p in PROVIDERS:
        if re.search(rf"\b{re.escape(p)}\b", text):
            return p
    return None


def _check_opencode_models(answers: WizardAnswers, console: Console) -> None:
    """Warn-only opencode catalog check. Never blocks the wizard."""
    try:
        result = subprocess.run(
            ["opencode", "models"], capture_output=True, text=True, timeout=10, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    if result.returncode != 0:
        return
    available = set(result.stdout.splitlines())
    for model in {
        f"{answers.provider}/{answers.reasoning_model}",
        f"{answers.provider}/{answers.coding_model}",
        f"{answers.provider}/{answers.report_model}",
    }:
        if model not in available:
            console.print(
                f"  [yellow]⚠[/yellow] [bold]{model}[/bold] not in 'opencode models' output — "
                "verify with: [cyan]bash scripts/check_opencode_provider.sh[/cyan]"
            )


def _step1_github(answers: WizardAnswers, console: Console) -> None:
    console.print("\n[bold cyan][1/6] GitHub repository[/bold cyan]")
    while not answers.owner:
        answers.owner = typer.prompt("  owner (your username or org)", default=answers.owner or None)
    answers.sandbox_repo = typer.prompt("  sandbox repo name", default=answers.sandbox_repo)


def _step2_provider(answers: WizardAnswers, console: Console) -> None:
    console.print("\n[bold cyan][2/6] LLM provider[/bold cyan]")
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(style="bold")
    table.add_column(style="dim")
    for i, (name, info) in enumerate(PROVIDERS.items(), 1):
        table.add_row(f"  {i})", name, f"env: {info['api_key_env']}  models: {info['reasoning']} / {info['coding']}")
    console.print(table)
    choice = Prompt.ask(
        "  pick by number or name",
        choices=[*[str(i) for i in range(1, len(PROVIDERS) + 1)], *PROVIDERS.keys()],
        default=str(list(PROVIDERS.keys()).index(answers.provider) + 1),
        show_choices=False,
    )
    if choice.isdigit():
        answers.provider = list(PROVIDERS.keys())[int(choice) - 1]
    else:
        answers.provider = choice


def _step3_models(answers: WizardAnswers, console: Console) -> None:
    p = PROVIDERS[answers.provider]
    console.print(f"\n[bold cyan][3/6] Models for {answers.provider}[/bold cyan] (Enter to accept defaults)")
    answers.reasoning_model = typer.prompt("  reasoning", default=answers.reasoning_model or p["reasoning"])
    answers.coding_model = typer.prompt("  coding   ", default=answers.coding_model or p["coding"])
    answers.report_model = typer.prompt("  report   ", default=answers.report_model or answers.reasoning_model)
    _check_opencode_models(answers, console)


def _step4_discord(answers: WizardAnswers, console: Console) -> None:
    console.print("\n[bold cyan][4/6] Discord HITL[/bold cyan] (parked-task notifications + slash commands)")
    console.print("  [dim]Set channel ID + user ID to 0 to skip Discord entirely.[/dim]")
    answers.discord_channel = typer.prompt("  channel ID (0 to skip)", default=answers.discord_channel)
    answers.discord_user = typer.prompt("  mention user ID (0 to skip)", default=answers.discord_user)


def _step5_secrets(answers: WizardAnswers, console: Console) -> None:
    console.print("\n[bold cyan][5/6] Secret tokens[/bold cyan]")
    console.print(
        "  [dim]input HIDDEN; press Enter to skip. Stored in ~/.config/nocturne/env (mode 600).[/dim]"
    )
    env_name = answers.api_key_env
    existing = os.environ.get(env_name, "")
    if existing:
        if typer.confirm(f"  {env_name} already set in shell — reuse it?", default=True):
            answers.api_key_value = existing
        else:
            answers.api_key_value = typer.prompt(f"  {env_name}", hide_input=True, default="", show_default=False) or None
    else:
        answers.api_key_value = typer.prompt(f"  {env_name}", hide_input=True, default="", show_default=False) or None

    if answers.discord_enabled:
        existing_tok = os.environ.get("NOCTURNE_DISCORD_TOKEN", "")
        if existing_tok:
            if typer.confirm("  NOCTURNE_DISCORD_TOKEN already set in shell — reuse it?", default=True):
                answers.discord_token_value = existing_tok
            else:
                answers.discord_token_value = (
                    typer.prompt("  NOCTURNE_DISCORD_TOKEN (bot token)", hide_input=True, default="", show_default=False) or None
                )
        else:
            answers.discord_token_value = (
                typer.prompt("  NOCTURNE_DISCORD_TOKEN (bot token)", hide_input=True, default="", show_default=False) or None
            )


def _step6_reviewer(answers: WizardAnswers, console: Console) -> None:
    console.print("\n[bold cyan][6/6] Reviewer skill[/bold cyan]")
    reviewer_dir = Path.home() / ".agents" / "skills" / "reviewer"
    if reviewer_dir.exists():
        answers.install_reviewer = typer.confirm(
            f"  install from {reviewer_dir}?", default=answers.install_reviewer
        )
    else:
        console.print(f"  [dim](skipped — {reviewer_dir} not found)[/dim]")
        answers.install_reviewer = False


def _print_summary(answers: WizardAnswers, console: Console) -> None:
    table = Table(title="Review your choices", show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column(style="bold")
    table.add_row("GitHub owner", answers.owner)
    table.add_row("Sandbox repo", f"{answers.owner}/{answers.sandbox_repo}")
    table.add_row("Provider", answers.provider)
    table.add_row("Reasoning model", f"{answers.provider}/{answers.reasoning_model}")
    table.add_row("Coding model", f"{answers.provider}/{answers.coding_model}")
    table.add_row("Report model", f"{answers.provider}/{answers.report_model}")
    table.add_row("Discord channel", answers.discord_channel)
    table.add_row("Discord user", answers.discord_user)
    table.add_row("Discord enabled", "yes" if answers.discord_enabled else "no")
    table.add_row(
        f"{answers.api_key_env} value",
        "[green]provided (env file)[/green]" if answers.api_key_value else "[dim]skipped[/dim]",
    )
    table.add_row(
        "NOCTURNE_DISCORD_TOKEN",
        "[green]provided (env file)[/green]" if answers.discord_token_value else "[dim]skipped[/dim]",
    )
    table.add_row("Reviewer skill", "[green]will install[/green]" if answers.install_reviewer else "[dim]skipped[/dim]")
    console.print()
    console.print(table)


def _render_config_yaml(a: WizardAnswers) -> str:
    p = PROVIDERS[a.provider]
    return (
        f"# Nocturne configuration — generated by `nocturne setup`\n"
        f"github:\n"
        f"  owner: \"{a.owner}\"\n"
        f"\n"
        f"sandbox:\n"
        f"  repo_name: \"{a.sandbox_repo}\"\n"
        f"  checkout_path: \"~/projects/{a.sandbox_repo}-checkout\"\n"
        f"\n"
        f"providers:\n"
        f"  {a.provider}:\n"
        f"    base_url: \"{p['base_url']}\"\n"
        f"    api_key_env: \"{a.api_key_env}\"\n"
        f"\n"
        f"models:\n"
        f"  reasoning: \"{a.provider}/{a.reasoning_model}\"\n"
        f"  coding: \"{a.provider}/{a.coding_model}\"\n"
        f"  report: \"{a.provider}/{a.report_model}\"\n"
        f"\n"
        f"opencode:\n"
        f"  command: \"opencode\"\n"
        f"  timeout_min: 25\n"
        f"  worktree_root: \"/tmp/nocturne\"\n"
        f"\n"
        f"repos:\n"
        f"  - slug: \"{a.owner}/{a.sandbox_repo}\"\n"
        f"    checkout_path: \"~/projects/{a.sandbox_repo}-checkout\"\n"
        f"    label: \"agent\"\n"
        f"    base: \"main\"\n"
        f"    verify_cmd: \"pytest -q\"\n"
        f"    require_new_test: true\n"
        f"\n"
        f"guardrails:\n"
        f"  max_attempts: 3\n"
        f"  per_task_timeout_min: 25\n"
        f"  global_wallclock_hours: 8\n"
        f"  token_budget: 2_000_000\n"
        f"  allow_force_push: false\n"
        f"  allow_auto_merge: false\n"
        f"\n"
        f"discord:\n"
        f"  enabled: {'true' if a.discord_enabled else 'false'}\n"
        f"  bot_token_env: \"NOCTURNE_DISCORD_TOKEN\"\n"
        f"  channel_id: {a.discord_channel}\n"
        f"  mention_user_id: {a.discord_user}\n"
        f"\n"
        f"daemon:\n"
        f"  poll_interval_sec: 300\n"
        f"  quiet_hours: []\n"
        f"\n"
        f"review:\n"
        f"  enabled: true\n"
        f"  budget_attempts: 2\n"
        f"  severity_floor: \"info\"\n"
        f"  skill_name: \"reviewer\"\n"
        f"  append_only: true\n"
        f"\n"
        f"healthcheck:\n"
        f"  enabled: true\n"
        f"  bind_host: \"127.0.0.1\"\n"
        f"  bind_port: 8765\n"
        f"  staleness_factor: 2\n"
        f"\n"
        f"persona:\n"
        f"  enabled: true\n"
        f"  soul_path: \"~/.config/nocturne/soul.md\"\n"
    )


def _write_env_file(env_file: Path, answers: WizardAnswers, console: Console) -> bool:
    if not answers.api_key_value and not answers.discord_token_value:
        return False
    existing_lines: list[str] = []
    if env_file.exists():
        bak = _backup(env_file)
        console.print(f"  [dim]→ backed up existing env file to {bak}[/dim]")
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{answers.api_key_env}=") or line.startswith("NOCTURNE_DISCORD_TOKEN="):
                continue
            existing_lines.append(line)
    new_lines = list(existing_lines)
    if answers.api_key_value:
        new_lines.append(f"{answers.api_key_env}={answers.api_key_value}")
    if answers.discord_token_value:
        new_lines.append(f"NOCTURNE_DISCORD_TOKEN={answers.discord_token_value}")
    env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    env_file.chmod(0o600)
    return True


def _install_reviewer_skill(console: Console) -> None:
    from nocturne.skills import SkillError, SkillExists, install_skill

    src = Path.home() / ".agents" / "skills" / "reviewer"
    if not src.exists():
        console.print(f"  [yellow]⚠[/yellow] {src} not found; cannot install")
        return
    try:
        name = install_skill(str(src), force=False)
        console.print(f"  [green]✓[/green] installed skill: {name}")
    except SkillExists:
        console.print("  [dim]→ reviewer skill already installed (use `nocturne skill install --force` to overwrite)[/dim]")
    except SkillError as e:
        console.print(f"  [yellow]⚠[/yellow] skill install failed: {e}")


def run_wizard(
    config_dir: Path,
    force: bool = False,
    non_interactive: bool = False,
    prefill: Optional[WizardAnswers] = None,
) -> WizardAnswers:
    """Drive the interactive wizard. Returns the populated WizardAnswers."""
    console = Console()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.yaml"
    env_file = config_dir / "env"

    if config_file.exists():
        leaked = _scan_for_leaked_secrets(config_file)
        if leaked:
            typer.secho("\n⚠ SECURITY WARNING", fg="red", bold=True, err=True)
            typer.secho("  Existing config has suspicious api_key_env value(s):", err=True)
            for v in leaked:
                typer.secho(f"    {v}", fg="yellow", err=True)
            typer.secho(
                "  This may be an actual API key. Rotate it before continuing if so.",
                fg="red", err=True,
            )
        if not force:
            if non_interactive or not os.isatty(0):
                typer.secho(
                    f"ERROR: {config_file} already exists. Pass --force to overwrite "
                    "(a .bak will be created).",
                    fg="red", err=True,
                )
                raise typer.Exit(2)
            if not typer.confirm(
                f"\nConfig already exists at {config_file}. Overwrite (a .bak will be created)?",
                default=False,
            ):
                typer.echo("Aborted.")
                raise typer.Exit(1)
        if config_file.exists():
            bak = _backup(config_file)
            console.print(f"  [dim]→ backed up existing config to {bak}[/dim]")

    answers = prefill or WizardAnswers()
    detected = _detect_provider_from_existing_config(config_file)
    if detected:
        answers.provider = detected

    if non_interactive:
        if not answers.owner:
            typer.secho(
                "ERROR: --owner is required in non-interactive mode.",
                fg="red", err=True,
            )
            raise typer.Exit(2)
        _validate_env_name(answers.api_key_env)
        if not answers.reasoning_model:
            answers.reasoning_model = PROVIDERS[answers.provider]["reasoning"]
        if not answers.coding_model:
            answers.coding_model = PROVIDERS[answers.provider]["coding"]
        if not answers.report_model:
            answers.report_model = answers.reasoning_model
    else:
        console.print("\n[bold]Nocturne setup[/bold]")
        console.print(
            "[dim]API keys + Discord token live in your shell env or ~/.config/nocturne/env, "
            "NEVER in config.yaml.[/dim]"
        )
        confirmed = False
        while not confirmed:
            _step1_github(answers, console)
            _step2_provider(answers, console)
            _step3_models(answers, console)
            _step4_discord(answers, console)
            _step5_secrets(answers, console)
            _step6_reviewer(answers, console)
            _print_summary(answers, console)
            choice = Prompt.ask(
                "\nProceed and write config?",
                choices=["y", "n", "edit"],
                default="y",
                show_choices=True,
            ).lower()
            if choice == "y":
                confirmed = True
            elif choice == "n":
                console.print("Aborted.")
                raise typer.Exit(1)
            else:
                console.print("\n[dim]── re-entering wizard ──[/dim]")

    config_file.write_text(_render_config_yaml(answers), encoding="utf-8")
    config_file.chmod(0o600)
    console.print(f"\n  [green]✓[/green] wrote {config_file}")

    if _write_env_file(env_file, answers, console):
        console.print(f"  [green]✓[/green] wrote {env_file} (mode 600)")
        console.print(
            "    [bold red]⚠ Contains secret token values.[/bold red] Do NOT commit, paste, or share."
        )
        console.print(f"    Load in current shell: [cyan]set -a; source {env_file}; set +a[/cyan]")

    if answers.install_reviewer:
        _install_reviewer_skill(console)

    repo_root = Path(__file__).parent.parent
    console.print("\n[bold]Next steps[/bold]")
    if not answers.api_key_value and not os.environ.get(answers.api_key_env):
        console.print(f"  1. Export your provider key:  [cyan]export {answers.api_key_env}='...'[/cyan]")
    if answers.discord_enabled and not answers.discord_token_value and not os.environ.get("NOCTURNE_DISCORD_TOKEN"):
        console.print("  2. Export your bot token:     [cyan]export NOCTURNE_DISCORD_TOKEN='...'[/cyan]")
    console.print(
        f"  3. Verify provider catalog:   [cyan]bash {repo_root}/scripts/check_opencode_provider.sh[/cyan]"
    )
    console.print(
        f"  4. Bootstrap sandbox:         [cyan]GITHUB_OWNER={answers.owner} SANDBOX_REPO={answers.sandbox_repo} bash {repo_root}/scripts/bootstrap_sandbox.sh[/cyan]"
    )
    console.print(
        f"  5. First run:                 [cyan]{repo_root}/.venv/bin/nocturne run-once --repo {answers.owner}/{answers.sandbox_repo} --issue 1[/cyan]"
    )
    return answers
