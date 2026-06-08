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


class ReviewConfig(StrictBaseModel):
    enabled: bool = True
    budget_attempts: int = 2
    severity_floor: Literal["info", "low", "medium", "high", "critical"] = "info"
    skill_name: str = "reviewer"
    slash_command: str = "review-pr"
    append_only: Literal[True] = True
    fallback_repos: list[str] = Field(
        default_factory=lambda: ["ba1lly/reviewer-config", "Defizoo/reviewer"]
    )
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
    cfg = Config.model_validate(raw)
    _validate_env_vars(cfg)
    return cfg
