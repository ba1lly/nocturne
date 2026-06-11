# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportAny=false, reportExplicitAny=false, reportUnusedParameter=false

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nocturne.config import (
    Config,
    DaemonConfig,
    DiscordConfig,
    GitHubConfig,
    GuardrailsConfig,
    HealthcheckConfig,
    ModelsConfig,
    OpenCodeConfig,
    PersonaConfig,
    ProviderConfig,
    RepoConfig,
    ReviewConfig,
    SandboxConfig,
)
from nocturne.guardrails import (
    GuardrailViolation,
    WorktreeContext,
    assert_no_sensitive_paths,
    assert_not_main_branch,
    check_repo_allowed,
    check_token_budget,
    check_wallclock,
    enforce_no_auto_merge,
    enforce_no_dangerous_opencode_flags,
    enforce_no_force_push,
    find_sensitive_paths,
)


def _make_git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    return root


def _config(tmp_path: Path) -> Config:
    checkout = _make_git_repo(tmp_path / "repo")
    repo = RepoConfig(
        slug="owner/nocturne",
        checkout_path=str(checkout),
        verify_cmd="pytest -q",
    )
    return Config(
        github=GitHubConfig(owner="owner"),
        sandbox=SandboxConfig(repo_name="nocturne-playground"),
        providers={"alibaba-coding-plan": ProviderConfig(base_url="https://example.com", api_key_env="DASHSCOPE_API_KEY")},
        models=ModelsConfig(
            reasoning="alibaba-coding-plan/qwen3.6-plus",
            coding="alibaba-coding-plan/qwen3-coder-plus",
            report="alibaba-coding-plan/qwen3.6-plus",
        ),
        opencode=OpenCodeConfig(),
        repos=[repo],
        guardrails=GuardrailsConfig(),
        discord=DiscordConfig(channel_id=1, mention_user_id=2),
        daemon=DaemonConfig(),
        review=ReviewConfig(),
        healthcheck=HealthcheckConfig(),
        persona=PersonaConfig(),
    )


@pytest.mark.parametrize(
    "args",
    [
        ["git", "push", "--force"],
        ["git", "push", "-f"],
        ["git", "push", "--force-with-lease"],
        ["git", "push", "origin", "+main"],
        ["git", "push", "origin", "+refs/heads/main:main"],
    ],
)
def test_enforce_no_force_push_blocks_dangerous_variants(args: list[str]) -> None:
    with pytest.raises(GuardrailViolation):
        enforce_no_force_push(args)


@pytest.mark.parametrize(
    "args",
    [
        ["git", "push"],
        ["git", "push", "origin", "main"],
        ["git", "push", "-u", "origin", "HEAD"],
    ],
)
def test_enforce_no_force_push_accepts_safe_variants(args: list[str]) -> None:
    enforce_no_force_push(args)


@pytest.mark.parametrize(
    "args",
    [
        ["gh", "pr", "merge"],
        ["gh", "pr", "merge", "--auto", "1234"],
        ["gh", "pr", "merge", "--squash"],
    ],
)
def test_enforce_no_auto_merge_blocks_merge(args: list[str]) -> None:
    with pytest.raises(GuardrailViolation):
        enforce_no_auto_merge(args)


@pytest.mark.parametrize("args", [["gh", "pr", "create"], ["gh", "pr", "view"]])
def test_enforce_no_auto_merge_accepts_non_merge(args: list[str]) -> None:
    enforce_no_auto_merge(args)


def test_enforce_no_dangerous_opencode_flags_blocks_skip_permissions() -> None:
    with pytest.raises(GuardrailViolation):
        enforce_no_dangerous_opencode_flags(["opencode", "run", "--model", "x", "--dangerously-skip-permissions"])


def test_enforce_no_dangerous_opencode_flags_accepts_safe_args() -> None:
    enforce_no_dangerous_opencode_flags(["opencode", "run", "--model", "x"])


def test_assert_not_main_branch_raises_when_current_branch_matches_base(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(stdout="main\n")

    monkeypatch.setattr("nocturne.guardrails.subprocess.run", fake_run)

    with pytest.raises(GuardrailViolation):
        assert_not_main_branch(tmp_path, "main")


def test_assert_not_main_branch_allows_non_main_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(stdout="nocturne/issue-1-1\n")

    monkeypatch.setattr("nocturne.guardrails.subprocess.run", fake_run)

    assert_not_main_branch(tmp_path, "main")


def test_check_repo_allowed_returns_repo_config(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    repo = check_repo_allowed("owner/nocturne", cfg)

    assert repo.slug == "owner/nocturne"
    assert repo.verify_cmd == "pytest -q"


def test_check_repo_allowed_raises_for_non_allowlisted_repo(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    with pytest.raises(GuardrailViolation):
        check_repo_allowed("other/nocturne", cfg)


def test_check_wallclock_within_budget_returns_remaining_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    fake_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    class FakeDateTime:
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            return fake_now

    monkeypatch.setattr("nocturne.guardrails.datetime", FakeDateTime)

    remaining = check_wallclock(started, cfg)

    assert remaining > timedelta(hours=0)


def test_check_wallclock_raises_when_over_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    fake_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    started = fake_now - timedelta(hours=cfg.guardrails.global_wallclock_hours + 1)

    class FakeDateTime:
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            return fake_now

    monkeypatch.setattr("nocturne.guardrails.datetime", FakeDateTime)

    with pytest.raises(GuardrailViolation):
        check_wallclock(started, cfg)


def test_check_token_budget_allows_under_budget(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    check_token_budget(cfg.guardrails.token_budget - 1, cfg)


@pytest.mark.parametrize("used", [2_000_000, 2_000_001])
def test_check_token_budget_raises_at_or_over_budget(tmp_path: Path, used: int) -> None:
    cfg = _config(tmp_path)

    with pytest.raises(GuardrailViolation):
        check_token_budget(used, cfg)


def test_worktree_context_runs_assertion_on_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[Path, str]] = []

    def fake_assert_not_main_branch(worktree_path: Path, expected_base: str) -> None:
        calls.append((worktree_path, expected_base))

    monkeypatch.setattr("nocturne.guardrails.assert_not_main_branch", fake_assert_not_main_branch)

    with WorktreeContext(tmp_path, "main"):
        pass

    assert calls == [(tmp_path, "main")]


def test_worktree_context_skips_assertion_on_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[Path, str]] = []

    def fake_assert_not_main_branch(worktree_path: Path, expected_base: str) -> None:
        calls.append((worktree_path, expected_base))

    monkeypatch.setattr("nocturne.guardrails.assert_not_main_branch", fake_assert_not_main_branch)

    with pytest.raises(RuntimeError):
        with WorktreeContext(tmp_path, "main"):
            raise RuntimeError("boom")

    assert calls == []


# --------------------------------------------------------------------------------------
# Sensitive-path scope guard (prompt-injection defence)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    ".github/workflows/release.yml",
    ".github/workflows/ci.yaml",
    ".github/actions/deploy/action.yml",
    ".env",
    ".env.production",
    "config/.env.local",
    "deploy/id_rsa",
    "keys/server.pem",
    "secrets/tls.key",
    "service.p12",
    "home/.ssh/authorized_keys",
    "infra/.aws/credentials",
    ".npmrc",
    "nested/dir/.netrc",
    "credentials",
])
def test_find_sensitive_paths_flags_dangerous(path: str) -> None:
    assert find_sensitive_paths([path]) == [path]


@pytest.mark.parametrize("path", [
    "src/main.py",
    "tests/test_thing.py",
    "README.md",
    ".github/ISSUE_TEMPLATE/bug.md",
    ".github/dependabot.yml",
    "docs/env.md",
    "src/environment.py",
    "package.json",
    "keystore.py",
])
def test_find_sensitive_paths_allows_normal(path: str) -> None:
    assert find_sensitive_paths([path]) == []


def test_assert_no_sensitive_paths_raises_on_workflow() -> None:
    with pytest.raises(GuardrailViolation) as excinfo:
        assert_no_sensitive_paths(["src/ok.py", ".github/workflows/evil.yml"])
    assert ".github/workflows/evil.yml" in str(excinfo.value)


def test_assert_no_sensitive_paths_passes_clean_diff() -> None:
    assert_no_sensitive_paths(["src/a.py", "tests/b.py", "README.md"])


def test_assert_no_sensitive_paths_lists_all_offenders() -> None:
    with pytest.raises(GuardrailViolation) as excinfo:
        assert_no_sensitive_paths([".env", "ok.py", "id_ed25519"])
    msg = str(excinfo.value)
    assert ".env" in msg and "id_ed25519" in msg
