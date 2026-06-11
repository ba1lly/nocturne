from __future__ import annotations

import pytest

from nocturne._sandbox import CREDENTIAL_ENV_VARS, scrubbed_env


def test_scrubbed_env_strips_all_credential_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in CREDENTIAL_ENV_VARS:
        monkeypatch.setenv(var, "secret")
    monkeypatch.setenv("PATH", "/usr/bin")  # ordinary var survives

    env = scrubbed_env()

    for var in CREDENTIAL_ENV_VARS:
        assert var not in env
    assert env["PATH"] == "/usr/bin"


def test_scrubbed_env_hardens_git_ssh_and_disables_prompt() -> None:
    env = scrubbed_env(base={})
    assert "IdentityAgent=none" in env["GIT_SSH_COMMAND"]
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_scrubbed_env_extra_strip_removes_named_vars() -> None:
    env = scrubbed_env(base={"DASHSCOPE_API_KEY": "k", "KEEP": "1"}, strip={"DASHSCOPE_API_KEY"})
    assert "DASHSCOPE_API_KEY" not in env
    assert env["KEEP"] == "1"


def test_scrubbed_env_sets_gh_config_dir_and_extra() -> None:
    env = scrubbed_env(base={}, gh_config_dir="/tmp/iso", extra={"OPENCODE_PROVIDER_API_KEY": "pk"})
    assert env["GH_CONFIG_DIR"] == "/tmp/iso"
    assert env["OPENCODE_PROVIDER_API_KEY"] == "pk"


def test_scrubbed_env_does_not_mutate_base() -> None:
    base = {"GH_TOKEN": "secret", "PATH": "/bin"}
    scrubbed_env(base=base)
    assert base == {"GH_TOKEN": "secret", "PATH": "/bin"}
