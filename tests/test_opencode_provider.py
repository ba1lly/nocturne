from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess

import pytest

import nocturne._opencode_check as opcheck
from nocturne.config import Config, load_test_config


def _cfg() -> Config:
    _ = os.environ.setdefault("DASHSCOPE_API_KEY", "secret")
    return load_test_config(Path(__file__).resolve().parents[1] / "config.example.yaml")


def _opencode_run_failure(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
    return CompletedProcess(args=["opencode", "models"], returncode=1, stdout="", stderr="boom")


def _opencode_run_missing(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
    raise FileNotFoundError("opencode")


def test_list_opencode_models_parses_valid_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = CompletedProcess(
        args=["opencode", "models"],
        returncode=0,
        stdout=(
            "garbage\n"
            "alibaba-coding-plan/qwen3-coder-plus\n"
            "not-a-model\n"
            "anthropic/claude-3-5-sonnet\n"
            "openai/gpt-5\n"
            "123bad/model\n"
        ),
        stderr="",
    )

    def _run_success(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        return completed

    monkeypatch.setattr("nocturne._opencode_check.subprocess.run", _run_success)

    assert opcheck.list_opencode_models() == {
        "alibaba-coding-plan/qwen3-coder-plus",
        "anthropic/claude-3-5-sonnet",
        "openai/gpt-5",
    }


@pytest.mark.parametrize("side_effect", [_opencode_run_failure, _opencode_run_missing])
def test_list_opencode_models_returns_empty_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    side_effect: Callable[..., CompletedProcess[str]],
) -> None:
    monkeypatch.setattr("nocturne._opencode_check.subprocess.run", side_effect)

    assert opcheck.list_opencode_models() == set()


def test_check_all_models_available_passes_when_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg()
    monkeypatch.setattr(
        opcheck,
        "list_opencode_models",
        lambda: {
            cfg.models.reasoning,
            cfg.models.coding,
            cfg.models.report,
        },
    )

    opcheck.check_all_models_available(cfg)


def test_check_all_models_available_raises_with_missing_models(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg()
    cfg.models.coding = "alibaba-coding-plan/qwen3-coder-plus"
    monkeypatch.setattr(
        opcheck,
        "list_opencode_models",
        lambda: {cfg.models.reasoning, cfg.models.report},
    )

    with pytest.raises(opcheck.ProviderNotRegistered) as excinfo:
        opcheck.check_all_models_available(cfg)

    message = str(excinfo.value)
    assert f"Models not registered with OpenCode: {sorted([cfg.models.coding])}" in message
    assert f"Providers needing registration: {sorted(['alibaba-coding-plan'])}" in message


def test_check_all_models_available_skips_when_coding_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg()
    cfg.models.coding = None

    def boom() -> set[str]:
        raise AssertionError("list_opencode_models must not be called when coding is unset")

    monkeypatch.setattr(opcheck, "list_opencode_models", boom)

    opcheck.check_all_models_available(cfg)


def test_check_all_models_available_skips_when_opencode_cli_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg()
    cfg.models.coding = "alibaba-coding-plan/qwen3-coder-plus"
    monkeypatch.setattr(opcheck, "list_opencode_models", lambda: set())

    opcheck.check_all_models_available(cfg)


def test_assert_provider_registered_passes_for_known_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        opcheck,
        "list_opencode_models",
        lambda: {"alibaba-coding-plan/qwen3-coder-plus"},
    )

    opcheck.assert_provider_registered("alibaba-coding-plan")


def test_assert_provider_registered_raises_for_missing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opcheck, "list_opencode_models", lambda: {"alibaba-coding-plan/qwen3-coder-plus"})

    with pytest.raises(opcheck.ProviderNotRegistered):
        opcheck.assert_provider_registered("anthropic")


def test_check_all_models_available_reports_provider_hint_for_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg()
    monkeypatch.setattr(
        opcheck,
        "list_opencode_models",
        lambda: {"alibaba-coding-plan/qwen3-coder-plus", "alibaba-coding-plan/qwen3.6-plus"},
    )

    cfg.models.coding = "anthropic/claude-3-5-sonnet"

    with pytest.raises(opcheck.ProviderNotRegistered) as excinfo:
        opcheck.check_all_models_available(cfg)

    message = str(excinfo.value)
    assert "anthropic/claude-3-5-sonnet" in message
    assert "anthropic" in message
