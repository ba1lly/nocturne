from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from nocturne.config import ConfigError, load_config, load_test_config


def _make_git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    return root


def _write_config(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _base_config(checkout_path: Path) -> dict[str, Any]:
    return {
        "github": {"owner": "ba1lly"},
        "sandbox": {},
        "providers": {
            "alibaba-coding-plan": {
                "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                "api_key_env": "DASHSCOPE_API_KEY",
            }
        },
        "models": {
            "reasoning": "alibaba-coding-plan/qwen3.6-plus",
            "coding": "alibaba-coding-plan/qwen3-coder-plus",
            "report": "alibaba-coding-plan/qwen3.6-plus",
        },
        "opencode": {},
        "repos": [
            {
                "slug": "ba1lly/nocturne-playground",
                "checkout_path": str(checkout_path),
                "verify_cmd": "pytest -q",
            }
        ],
        "guardrails": {},
        "discord": {"channel_id": 123, "mention_user_id": 456},
        "daemon": {},
        "review": {},
        "healthcheck": {},
        "persona": {},
    }


def test_load_config_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    config_path = _write_config(tmp_path / "config.yaml", _base_config(checkout))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    cfg = load_config(config_path)

    assert cfg.github.owner == "ba1lly"
    assert cfg.sandbox.repo_name == "nocturne-playground"
    assert cfg.review.enabled is True
    assert cfg.review.severity_floor == "info"
    assert Path(cfg.repos[0].checkout_path).is_absolute()


def test_missing_env_var_raises_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    config_path = _write_config(tmp_path / "config.yaml", _base_config(checkout))
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path)

    assert "DASHSCOPE_API_KEY" in str(excinfo.value)


def test_discord_channel_zero_raises_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    data = _base_config(checkout)
    data["discord"]["channel_id"] = 0
    config_path = _write_config(tmp_path / "config.yaml", data)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path)

    assert "channel_id" in str(excinfo.value)


def test_discord_mention_zero_raises_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    data = _base_config(checkout)
    data["discord"]["mention_user_id"] = 0
    config_path = _write_config(tmp_path / "config.yaml", data)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path)

    assert "mention_user_id" in str(excinfo.value)


def test_unknown_top_level_key_raises_validation_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    data = _base_config(checkout)
    data["unexpected"] = {}
    config_path = _write_config(tmp_path / "config.yaml", data)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_model_without_slash_raises_validation_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    data = _base_config(checkout)
    data["models"]["coding"] = "qwen3-coder-plus"
    config_path = _write_config(tmp_path / "config.yaml", data)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_allow_force_push_true_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    data = _base_config(checkout)
    data["guardrails"]["allow_force_push"] = True
    config_path = _write_config(tmp_path / "config.yaml", data)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path)

    assert "allow_force_push" in str(excinfo.value)


def test_github_owner_required_non_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    data = _base_config(checkout)
    data["github"]["owner"] = ""
    config_path = _write_config(tmp_path / "config.yaml", data)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_sandbox_repo_name_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    config_path = _write_config(tmp_path / "config.yaml", _base_config(checkout))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    cfg = load_config(config_path)

    assert cfg.sandbox.repo_name == "nocturne-playground"


def test_review_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    config_path = _write_config(tmp_path / "config.yaml", _base_config(checkout))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    cfg = load_config(config_path)

    assert cfg.review.enabled is True
    assert cfg.review.severity_floor == "info"


def test_unknown_model_provider_raises_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    data = _base_config(checkout)
    data["models"]["coding"] = "missing/qwen3-coder-plus"
    config_path = _write_config(tmp_path / "config.yaml", data)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path)

    assert "missing/qwen3-coder-plus" in str(excinfo.value)


def test_repo_verify_cmd_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    data = _base_config(checkout)
    del data["repos"][0]["verify_cmd"]
    config_path = _write_config(tmp_path / "config.yaml", data)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_test_config_patches_discord_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _make_git_repo(tmp_path / "repo")
    data = _base_config(checkout)
    data["discord"]["channel_id"] = 0
    data["discord"]["mention_user_id"] = 0
    config_path = _write_config(tmp_path / "config.yaml", data)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    cfg = load_test_config(config_path, channel_id=1, user_id=2)

    assert cfg.discord.channel_id == 1
    assert cfg.discord.mention_user_id == 2
