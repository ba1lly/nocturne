from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from nocturne.config import (
    Config,
    ConfigError,
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
from nocturne.models import Task
from nocturne.prompts.render import load_soul, render_review_prompt, render_task_prompt

PROJECT_DIR = Path("/home/bailly/projects/nocturne")


def make_config(*, enabled: bool = False, soul_path: str | None = None) -> Config:
    return Config(
        github=GitHubConfig(owner="octo"),
        sandbox=SandboxConfig(checkout_path=str(PROJECT_DIR)),
        providers={"alibaba-coding-plan": ProviderConfig(base_url="https://example.invalid", api_key_env="ALIBABA_API_KEY")},
        models=ModelsConfig(
            reasoning="alibaba-coding-plan/reasoning",
            coding="alibaba-coding-plan/coding",
            report="alibaba-coding-plan/report",
        ),
        opencode=OpenCodeConfig(),
        repos=[
            RepoConfig(
                slug="octo/nocturne",
                checkout_path=str(PROJECT_DIR),
                verify_cmd="pytest",
            )
        ],
        guardrails=GuardrailsConfig(),
        discord=DiscordConfig(channel_id=1, mention_user_id=1),
        daemon=DaemonConfig(),
        review=ReviewConfig(),
        healthcheck=HealthcheckConfig(),
        persona=PersonaConfig(enabled=enabled, soul_path=soul_path),
    )


def make_task(
    *,
    issue_number: int = 7,
    title: str = "Add task prompt",
    body: str = "Implement the prompt template and render helpers.",
    verify_cmd: str = ".venv/bin/pytest -q tests/test_prompts.py",
    require_new_test: bool = True,
    branch: str = "nocturne/issue-7",
) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id="r#1",
        repo_slug="octo/nocturne",
        checkout_path=str(PROJECT_DIR),
        issue_number=issue_number,
        title=title,
        body=body,
        base="main",
        verify_cmd=verify_cmd,
        require_new_test=require_new_test,
        coding_model="alibaba-coding-plan/coding",
        branch=branch,
        status="selected",
        attempts=0,
        created_at=now,
        updated_at=now,
    )


def test_disabled_persona_hides_block(tmp_path: Path) -> None:
    soul = tmp_path / "soul.md"
    _ = soul.write_text("hello")
    out = render_task_prompt(make_task(), make_config(enabled=False, soul_path=str(soul)))
    assert "# Persona" not in out


def test_missing_soul_file_hides_block(tmp_path: Path) -> None:
    out = render_task_prompt(make_task(), make_config(enabled=True, soul_path=str(tmp_path / "missing.md")))
    assert "# Persona" not in out


def test_valid_soul_injects_persona_block(tmp_path: Path) -> None:
    soul = tmp_path / "soul.md"
    text = "You are precise and pragmatic.\n" + ("x" * 1000)
    _ = soul.write_text(text)
    out = render_task_prompt(make_task(), make_config(enabled=True, soul_path=str(soul)))
    assert "# Persona" in out
    assert text in out


def test_oversize_soul_raises_before_render(tmp_path: Path) -> None:
    soul = tmp_path / "big.md"
    _ = soul.write_text("x" * 9000)
    cfg = make_config(enabled=True, soul_path=str(tmp_path / "small.md"))
    cfg.persona.soul_path = str(soul)
    with pytest.raises(ConfigError, match="soul.md exceeds 8192 char cap"):
        _ = load_soul(cfg)


def test_sentinel_appears_exactly_once() -> None:
    out = render_task_prompt(make_task(), make_config())
    assert out.count("##NOCTURNE_NEED_INPUT##") == 1


def test_require_new_test_includes_instruction() -> None:
    out = render_task_prompt(make_task(require_new_test=True), make_config())
    assert "You MUST add at least one new test" in out


def test_require_new_test_false_omits_instruction() -> None:
    out = render_task_prompt(make_task(require_new_test=False), make_config())
    assert "You MUST add at least one new test" not in out


def test_prior_failure_includes_failure_block() -> None:
    out = render_task_prompt(make_task(), make_config(), prior_failure="boom")
    assert "boom" in out
    assert "Your previous attempt failed verification" in out


def test_prior_failure_none_omits_failure_block() -> None:
    out = render_task_prompt(make_task(), make_config())
    assert "Your previous attempt failed" not in out


def test_forbidden_actions_section_interpolates_branch() -> None:
    out = render_task_prompt(make_task(branch="nocturne/issue-7-branch"), make_config())
    assert "Do NOT run `git commit`" in out
    assert "`git push`" in out
    assert "`gh pr`" in out
    assert "orchestrator handles all git operations" in out
    assert "You are on branch `nocturne/issue-7-branch`" in out
    assert "do not switch branches or modify `main`" in out


def test_template_includes_self_review_workflow() -> None:
    """Regression guard for Approach 1 (single-session) architecture:
    the task prompt MUST tell opencode to invoke @reviewer, address every
    finding, and write .nocturne-pr-body.md."""
    out = render_task_prompt(make_task(), make_config())
    assert "@reviewer" in out
    assert ".nocturne-pr-body.md" in out
    assert "every severity" in out or "all findings" in out.lower() or "regardless of severity" in out
    assert "Closes #" in out


def test_template_includes_budget_attempts_from_config() -> None:
    cfg = make_config()
    cfg.review.budget_attempts = 7
    out = render_task_prompt(make_task(), cfg)
    assert "7 times" in out


def test_soul_cache_uses_mtime_and_skips_second_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    soul = tmp_path / "soul.md"
    _ = soul.write_text("cache me")
    cfg = make_config(enabled=True, soul_path=str(soul))

    reads = {"count": 0}
    original_read_text = Path.read_text

    def counted_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if self == soul:
            reads["count"] += 1
        return original_read_text(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", counted_read_text, raising=False)

    assert load_soul(cfg) == "cache me"
    assert load_soul(cfg) == "cache me"
    assert reads["count"] == 1


def test_render_review_prompt_injects_soul(tmp_path: Path) -> None:
    soul = tmp_path / "soul.md"
    _ = soul.write_text("Review carefully.")
    out = render_review_prompt("diff --git a/x b/x", "reviewer", make_config(enabled=True, soul_path=str(soul)))
    assert "# Persona" in out
    assert "Review carefully." in out
    assert "You are running the reviewer skill." in out
