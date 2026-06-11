from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Optional, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MODEL_PATTERN = r"^[a-z][a-z0-9-]*/[a-zA-Z0-9._-]+$"
REPO_SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"


class ConfigError(Exception):
    pass


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GitHubConfig(StrictBaseModel):
    owner: str = Field(min_length=1)


class SandboxConfig(StrictBaseModel):
    repo_name: str = "nocturne-playground"
    checkout_path: str | None = None

    @model_validator(mode="after")
    def _set_checkout_path(self) -> "SandboxConfig":
        if self.checkout_path is None:
            self.checkout_path = f"~/projects/{self.repo_name}-checkout"
        return self


class ProviderConfig(StrictBaseModel):
    base_url: str
    api_key_env: str


class ModelsConfig(StrictBaseModel):
    reasoning: str = Field(pattern=MODEL_PATTERN)
    report: str = Field(pattern=MODEL_PATTERN)
    coding: Optional[str] = Field(default=None, pattern=MODEL_PATTERN)


class OpenCodeConfig(StrictBaseModel):
    command: str = "opencode"
    timeout_min: int = 25
    worktree_root: str = "/tmp/nocturne"
    # Failed/aborted worktrees are kept for post-mortem inspection, but reaped
    # once older than this many hours so they cannot exhaust disk. Far longer
    # than any single run (timeout_min), so an in-flight worktree is never hit.
    # 0 disables reaping.
    worktree_ttl_hours: int = 48


class RepoConfig(StrictBaseModel):
    slug: str = Field(pattern=REPO_SLUG_PATTERN)
    checkout_path: str
    label: str = "agent"
    base: str = "main"
    verify_cmd: str
    require_new_test: bool = True

    @field_validator("checkout_path", mode="before")
    @classmethod
    def _validate_checkout_path(cls, value: str | Path) -> str:
        path = Path(value).expanduser().resolve()
        if not path.is_dir() or not (path / ".git").is_dir():
            raise ConfigError(f"checkout_path must be an existing git dir: {path}")
        return str(path)


class GuardrailsConfig(StrictBaseModel):
    max_attempts: int = 3
    per_task_timeout_min: int = 25
    global_wallclock_hours: int = 8
    token_budget: int = 2_000_000
    allow_force_push: Literal[False] = Field(default=False)
    allow_auto_merge: Literal[False] = Field(default=False)

    @field_validator("allow_force_push", "allow_auto_merge", mode="before")
    @classmethod
    def _reject_true(cls, value: object, info) -> object:
        if value is True:
            raise ConfigError(f"{info.field_name} cannot be true")
        return value


class DiscordConfig(StrictBaseModel):
    enabled: bool = True
    bot_token_env: str = "NOCTURNE_DISCORD_TOKEN"
    channel_id: int = 0
    mention_user_id: int = 0

    @model_validator(mode="after")
    def _check_ids_when_enabled(self) -> "DiscordConfig":
        if not self.enabled:
            return self
        if self.channel_id == 0:
            raise ConfigError("channel_id must be non-zero when discord.enabled is true")
        if self.mention_user_id == 0:
            raise ConfigError("mention_user_id must be non-zero when discord.enabled is true")
        return self


class DaemonConfig(StrictBaseModel):
    poll_interval_sec: int = 300
    quiet_hours: list[int] = Field(default_factory=list)
    # IANA timezone name (e.g. "America/New_York") that quiet_hours are
    # interpreted in. None = UTC (backwards-compatible default).
    quiet_hours_tz: str | None = None

    @field_validator("quiet_hours_tz")
    @classmethod
    def _validate_tz(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"invalid quiet_hours_tz: {value!r}") from exc
        return value


class ReactionsConfig(StrictBaseModel):
    """Post-PR feedback loop: after Nocturne opens a PR it can keep shepherding
    it toward merge-ready by reacting to CI failures and review comments.

    It NEVER merges - ``approved-and-green`` only notifies a human, who makes
    the merge call. Auto-merge is intentionally absent and blocked by
    ``guardrails.enforce_no_auto_merge``.
    """

    enabled: bool = False
    # React to failing CI by re-dispatching the agent to fix the branch.
    fix_failing_ci: bool = True
    # React to a reviewer's "changes requested" by addressing the comments.
    address_review_comments: bool = True
    # Notify when a watched PR is approved and green (never merges it).
    notify_when_ready: bool = True
    # Hard cap on autonomous fix attempts per PR before escalating to a human.
    max_fix_attempts: int = 3
    # Stop watching a PR after this many hours regardless of state.
    watch_ttl_hours: int = 168


class ReviewConfig(StrictBaseModel):
    enabled: bool = True
    budget_attempts: int = 2
    # Reserved for future severity-filtered @reviewer wiring - unused as of
    # Approach 1 (commits 774a67f, 0155364, 541af97).
    severity_floor: Literal["info", "low", "medium", "high", "critical"] = "info"
    skill_name: str = "reviewer"
    # Reserved for future use - Approach 1 enforces append-only review history
    # implicitly via PR body file; the Literal[True] guard locks the invariant.
    append_only: Literal[True] = True
    # Reserved for future fallback-skill resolution; not read by Approach 1.
    fallback_repos: list[str] = Field(
        default_factory=lambda: ["ba1lly/reviewer-config", "Defizoo/reviewer"]
    )
    # Reserved for future use - Approach 1 always uses OpenCode default.
    use_opencode_default_when_unavailable: bool = True


class HealthcheckConfig(StrictBaseModel):
    enabled: bool = True
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    staleness_factor: int = 2


class PersonaConfig(StrictBaseModel):
    soul_path: Optional[str] = "~/.config/nocturne/soul.md"
    enabled: bool = True

    @model_validator(mode="after")
    def _validate_soul_path(self) -> "PersonaConfig":
        if self.soul_path is None:
            return self
        path = Path(self.soul_path).expanduser()
        if path.is_file() and len(path.read_text()) > 8192:
            raise ConfigError(f"soul_path exceeds 8192 chars: {path}")
        return self


class Config(StrictBaseModel):
    github: GitHubConfig
    sandbox: SandboxConfig
    providers: dict[str, ProviderConfig]
    models: ModelsConfig
    opencode: OpenCodeConfig
    repos: list[RepoConfig] = Field(min_length=1)
    guardrails: GuardrailsConfig
    discord: DiscordConfig
    daemon: DaemonConfig
    review: ReviewConfig
    healthcheck: HealthcheckConfig
    persona: PersonaConfig
    reactions: ReactionsConfig = Field(default_factory=ReactionsConfig)


def provider_of(model_string: str) -> str:
    return model_string.split("/", 1)[0]


def get_api_key(cfg: Config, provider_name: str) -> str:
    try:
        env_name = cfg.providers[provider_name].api_key_env
    except KeyError as exc:
        raise ConfigError(f"unknown provider: {provider_name}") from exc

    value = os.environ.get(env_name)
    if not value:
        raise ConfigError(f"missing environment variable: {env_name}")
    return value


def _load_yaml(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text())
    return data or {}


def _validate_env_vars(cfg: Config) -> None:
    missing: list[str] = []
    seen: set[str] = set()
    model_strings = [cfg.models.reasoning, cfg.models.report]
    if cfg.models.coding is not None:
        model_strings.append(cfg.models.coding)
    for model_string in model_strings:
        provider_name = provider_of(model_string)
        if provider_name not in cfg.providers:
            raise ConfigError(f"unknown provider for model: {model_string}")
        if provider_name in seen:
            continue
        seen.add(provider_name)
        env_name = cfg.providers[provider_name].api_key_env
        if not os.environ.get(env_name):
            missing.append(env_name)
    if missing:
        raise ConfigError(f"missing environment variables: {', '.join(missing)}")


def _load_config_data(path: str | Path) -> Config:
    raw = _load_yaml(path)
    cfg = Config.model_validate(raw)
    _validate_env_vars(cfg)
    return cfg


def load_config(path: str | Path) -> Config:
    return _load_config_data(path)


def load_test_config(path: str | Path = "config.example.yaml", channel_id: int = 1, user_id: int = 1) -> Config:
    raw = _load_yaml(path)
    raw.setdefault("discord", {})
    discord = cast(dict[str, Any], raw["discord"])
    discord["channel_id"] = channel_id
    discord["mention_user_id"] = user_id
    project_root = str(Path(__file__).resolve().parent.parent)
    for repo in raw.get("repos", []):
        if isinstance(repo, dict):
            repo["checkout_path"] = project_root
    sandbox = raw.get("sandbox")
    if isinstance(sandbox, dict) and "checkout_path" in sandbox:
        sandbox["checkout_path"] = project_root
    cfg = Config.model_validate(raw)
    _validate_env_vars(cfg)
    return cfg
