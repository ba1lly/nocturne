"""Tests for nocturne.review — reviewer skill invocation + finding parsing."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from nocturne import review as review_mod
from nocturne.config import Config, load_test_config
from nocturne.review import (
    ReviewFinding,
    ReviewResult,
    SkillNotInstalled,
    findings_summary,
    review_pr,
)


def _cfg() -> Config:
    _ = os.environ.setdefault("DASHSCOPE_API_KEY", "secret")
    return load_test_config(Path(__file__).resolve().parents[1] / "config.example.yaml")


def _ndjson_text_event(text: str) -> str:
    """Build a single NDJSON line carrying a text event with `text`."""
    return json.dumps({"type": "text", "text": text})


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["opencode"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _patch_skill_enabled(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(review_mod, "is_skill_enabled", lambda name: enabled)


def _patch_diff(monkeypatch: pytest.MonkeyPatch, diff: str) -> None:
    monkeypatch.setattr(review_mod, "_compute_diff", lambda worktree, base="main": diff)


def _patch_opencode(
    monkeypatch: pytest.MonkeyPatch, stdout: str = "", *, raises=None,
) -> list[tuple]:
    """Patch subprocess.run inside review module. Returns a list of call records."""
    calls: list[tuple] = []
    orig_run = subprocess.run

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        # Real git diff happens via _compute_diff which we patch separately.
        # Distinguish: if first arg is 'git', delegate to real subprocess (shouldn't happen
        # if _patch_diff was called); otherwise return fake opencode output.
        if isinstance(args, (list, tuple)) and len(args) > 0 and args[0] == "git":
            return orig_run(args, **kwargs)
        if raises is not None:
            raise raises
        return _fake_completed(stdout=stdout)

    monkeypatch.setattr(review_mod.subprocess, "run", fake_run)
    return calls


# ---------- Tests ----------


def test_review_falls_back_to_opencode_default_when_skill_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When the named skill isn't installed AND each fallback repo is inaccessible,
    review_pr must NOT raise — it falls back to the opencode-default prompt and
    reports skill_used='opencode-default'."""
    from nocturne.skills import SkillInvalid

    _patch_skill_enabled(monkeypatch, False)
    monkeypatch.setattr(
        "nocturne.review.install_skill_from_github",
        lambda repo, force=False: (_ for _ in ()).throw(SkillInvalid(f"no access to {repo}")),
    )
    _patch_diff(monkeypatch, "diff --git a/x b/x\n+hello\n")
    ndjson = _ndjson_text_event("```json\n[]\n```")
    captured_prompts: list[str] = []
    orig_run = subprocess.run

    def fake_run(args, **kwargs):
        if isinstance(args, (list, tuple)) and len(args) > 0 and args[0] == "git":
            return orig_run(args, **kwargs)
        for i, a in enumerate(args):
            if a == "-f" and i + 1 < len(args):
                captured_prompts.append(Path(args[i + 1]).read_text(encoding="utf-8"))
                break
        return _fake_completed(stdout=ndjson)

    monkeypatch.setattr(review_mod.subprocess, "run", fake_run)

    cfg = _cfg()
    result = review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)

    assert result.skill_used == "opencode-default"
    assert result.clean is True
    assert len(captured_prompts) == 1
    assert "You are running the" not in captured_prompts[0]
    assert "no specialised reviewer skill" in captured_prompts[0].lower()


def test_review_clean_returns_no_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_skill_enabled(monkeypatch, True)
    _patch_diff(monkeypatch, "diff --git a/x b/x\n+hello\n")
    ndjson = _ndjson_text_event("All good!\n\n```json\n[]\n```")
    _patch_opencode(monkeypatch, stdout=ndjson)
    cfg = _cfg()

    result = review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)

    assert isinstance(result, ReviewResult)
    assert result.clean is True
    assert result.findings == []
    assert result.skill_used == cfg.review.skill_name


def test_review_with_skill_invokes_slash_command_not_inline_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When the reviewer skill is available, review_pr must delegate to the
    user's /review-pr slash command via 'opencode run --command', not render
    a custom prompt to a temp file. The slash command already loads the
    skill and runs the multi-agent pipeline."""
    _patch_skill_enabled(monkeypatch, True)
    _patch_diff(monkeypatch, "diff --git a/x b/x\n+hello\n")
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        if isinstance(args, (list, tuple)) and len(args) > 0 and args[0] == "git":
            return subprocess.run(args, **kwargs)
        captured.append(list(args))
        return _fake_completed(stdout=_ndjson_text_event("```json\n[]\n```"))

    monkeypatch.setattr(review_mod.subprocess, "run", fake_run)
    cfg = _cfg()
    pr_url = "https://github.com/ba1lly/nocturne-playground/pull/42"

    review_pr(pr_url, tmp_path, cfg)

    assert len(captured) == 1
    args = captured[0]
    assert "--command" in args
    assert args[args.index("--command") + 1] == cfg.review.slash_command
    assert pr_url in args
    assert "-f" not in args, "must not pass a custom prompt file when delegating to the slash command"
    assert "--model" not in args, "let opencode + the skill pick the model"


def test_review_skill_path_honors_custom_slash_command_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """cfg.review.slash_command is configurable; review_pr passes whatever
    the operator set, not a hardcoded 'review-pr'."""
    _patch_skill_enabled(monkeypatch, True)
    _patch_diff(monkeypatch, "diff --git a/x b/x\n+hi\n")
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        if isinstance(args, (list, tuple)) and len(args) > 0 and args[0] == "git":
            return subprocess.run(args, **kwargs)
        captured.append(list(args))
        return _fake_completed(stdout=_ndjson_text_event("```json\n[]\n```"))

    monkeypatch.setattr(review_mod.subprocess, "run", fake_run)
    cfg = _cfg()
    cfg.review.slash_command = "my-custom-review"

    review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)

    assert len(captured) == 1
    assert captured[0][captured[0].index("--command") + 1] == "my-custom-review"


def test_review_finds_high_severity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_skill_enabled(monkeypatch, True)
    _patch_diff(monkeypatch, "diff --git a/x b/x\n+sql=...\n")
    payload = json.dumps(
        [
            {
                "severity": "high",
                "file": "x.py",
                "line": 42,
                "category": "security",
                "message": "sql injection",
            }
        ]
    )
    ndjson = _ndjson_text_event(f"Looking...\n```json\n{payload}\n```")
    _patch_opencode(monkeypatch, stdout=ndjson)
    cfg = _cfg()

    result = review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)

    assert result.clean is False
    assert len(result.findings) == 1
    assert result.findings[0].severity == "high"
    assert result.findings[0].file == "x.py"
    assert result.findings[0].line == 42
    assert result.findings[0].category == "security"
    assert "sql" in result.findings[0].message.lower()


def test_review_filters_by_severity_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_skill_enabled(monkeypatch, True)
    _patch_diff(monkeypatch, "diff --git a/x b/x\n+foo\n")
    payload = json.dumps(
        [
            {"severity": "info", "file": "a.py", "line": 1, "category": "n", "message": "nit"},
            {"severity": "low", "file": "b.py", "line": 2, "category": "n", "message": "small"},
            {"severity": "high", "file": "c.py", "line": 3, "category": "s", "message": "bug"},
        ]
    )
    ndjson = _ndjson_text_event(f"```json\n{payload}\n```")
    _patch_opencode(monkeypatch, stdout=ndjson)

    cfg = _cfg()
    cfg.review.severity_floor = "high"

    result = review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)

    assert len(result.findings) == 1
    assert result.findings[0].severity == "high"
    assert result.clean is False


def test_review_severity_floor_info_keeps_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_skill_enabled(monkeypatch, True)
    _patch_diff(monkeypatch, "diff --git a/x b/x\n+foo\n")
    payload = json.dumps(
        [
            {"severity": "info", "file": "a.py", "line": 1, "category": "n", "message": "nit"},
            {"severity": "low", "file": "b.py", "line": 2, "category": "n", "message": "small"},
            {"severity": "high", "file": "c.py", "line": 3, "category": "s", "message": "bug"},
        ]
    )
    ndjson = _ndjson_text_event(f"```json\n{payload}\n```")
    _patch_opencode(monkeypatch, stdout=ndjson)

    cfg = _cfg()
    cfg.review.severity_floor = "info"

    result = review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)

    assert len(result.findings) == 3
    assert {f.severity for f in result.findings} == {"info", "low", "high"}


def test_review_handles_multiple_json_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_skill_enabled(monkeypatch, True)
    _patch_diff(monkeypatch, "diff --git a/x b/x\n+foo\n")
    first = json.dumps([{"severity": "info", "file": "ignored.py", "message": "old"}])
    final = json.dumps([{"severity": "high", "file": "kept.py", "line": 9, "message": "real"}])
    text = (
        f"Initial thoughts:\n```json\n{first}\n```\n"
        f"Wait, on review here is the final:\n```json\n{final}\n```\n"
    )
    ndjson = _ndjson_text_event(text)
    _patch_opencode(monkeypatch, stdout=ndjson)
    cfg = _cfg()

    result = review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)

    assert len(result.findings) == 1
    assert result.findings[0].file == "kept.py"
    assert result.findings[0].severity == "high"


def test_review_regex_fallback_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_skill_enabled(monkeypatch, True)
    _patch_diff(monkeypatch, "diff --git a/x b/x\n+foo\n")
    text = "Here are my findings:\n[high] foo.py:42 - bug here\n[low] bar.py:7 - small thing\n"
    ndjson = _ndjson_text_event(text)
    _patch_opencode(monkeypatch, stdout=ndjson)
    cfg = _cfg()
    cfg.review.severity_floor = "info"

    result = review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)

    assert len(result.findings) == 2
    sevs = {f.severity for f in result.findings}
    assert sevs == {"high", "low"}
    high = next(f for f in result.findings if f.severity == "high")
    assert high.file == "foo.py"
    assert high.line == 42
    assert "bug here" in high.message


def test_review_empty_diff_returns_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_skill_enabled(monkeypatch, True)
    _patch_diff(monkeypatch, "")  # empty diff
    calls = _patch_opencode(monkeypatch, stdout="should not be called")
    cfg = _cfg()

    result = review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)

    assert result.clean is True
    assert result.findings == []
    # opencode subprocess.run should NOT have been called for empty diff
    opencode_calls = [
        c for c in calls
        if isinstance(c[0], (list, tuple)) and len(c[0]) > 0 and c[0][0] != "git"
    ]
    assert opencode_calls == []


def test_review_timeout_returns_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_skill_enabled(monkeypatch, True)
    _patch_diff(monkeypatch, "diff --git a/x b/x\n+foo\n")
    _patch_opencode(
        monkeypatch,
        raises=subprocess.TimeoutExpired(cmd=["opencode"], timeout=1),
    )
    cfg = _cfg()

    result = review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)

    assert result.clean is False
    assert result.raw_output == "timeout"
    assert result.findings == []


def test_findings_summary_groups_by_severity() -> None:
    findings = [
        ReviewFinding(severity="high", file="a.py", message="x"),
        ReviewFinding(severity="high", file="b.py", message="y"),
        ReviewFinding(severity="medium", file="c.py", message="z"),
    ]
    summary = findings_summary(findings)
    assert summary == "3 findings: 2 high, 1 medium"


def test_findings_summary_empty() -> None:
    assert findings_summary([]) == "no findings"


def test_skill_missing_message_includes_install_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When no skill is available AND fallback to opencode default is disabled,
    the SkillNotInstalled message must name the configured fallback_repos so
    the operator knows what gh access to grant."""
    _patch_skill_enabled(monkeypatch, False)
    monkeypatch.setattr(
        "nocturne.review.install_skill_from_github",
        lambda repo, force=False: (_ for _ in ()).throw(
            __import__("nocturne.skills", fromlist=["SkillInvalid"]).SkillInvalid(f"no access to {repo}")
        ),
    )
    cfg = _cfg()
    cfg.review.use_opencode_default_when_unavailable = False
    cfg.review.fallback_repos = ["someorg/reviewer", "otherorg/reviewer"]

    with pytest.raises(SkillNotInstalled) as excinfo:
        review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)
    msg = str(excinfo.value)
    assert "could not be resolved" in msg
    assert "someorg/reviewer" in msg or "fallback_repos" in msg
    assert "use_opencode_default_when_unavailable" in msg


def test_malformed_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Alias of test_review_regex_fallback_on_malformed_json — explicit regex coverage."""
    _patch_skill_enabled(monkeypatch, True)
    _patch_diff(monkeypatch, "diff --git a/x b/x\n+foo\n")
    text = "[critical] db/migrations.py:120 - dropping production table"
    ndjson = _ndjson_text_event(text)
    _patch_opencode(monkeypatch, stdout=ndjson)
    cfg = _cfg()
    cfg.review.severity_floor = "info"

    result = review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)

    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.severity == "critical"
    assert f.file == "db/migrations.py"
    assert f.line == 120


@pytest.mark.skip(
    reason=(
        "Live smoke deferred — requires reviewer skill installed + OpenCode env. "
        "Unit tests cover the contract."
    )
)
def test_real_review_smoke() -> None:  # pragma: no cover - deferred
    pass


# ---------- apply_fixes + review_fix_loop tests (Task 39) ----------


from nocturne.review import (  # noqa: E402
    ApplyFixesResult,
    apply_fixes,
    review_fix_loop,
)


def _patch_apply_fixes_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    opencode_returncode: int = 0,
    opencode_raises=None,
    git_status_stdout: str = " M file.py\n",
) -> list[tuple]:
    """Patch review_mod.subprocess.run for apply_fixes tests.

    Distinguishes git status calls (return given stdout) from opencode calls
    (return given returncode or raise).
    """
    calls: list[tuple] = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if (
            isinstance(args, (list, tuple))
            and len(args) >= 4
            and args[0] == "git"
            and "status" in args
        ):
            return _fake_completed(stdout=git_status_stdout)
        if opencode_raises is not None:
            raise opencode_raises
        return _fake_completed(returncode=opencode_returncode, stdout="ok", stderr="")

    monkeypatch.setattr(review_mod.subprocess, "run", fake_run)
    return calls


def _record_commit_push(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Replace commit_push with a recording fake."""
    recorded: list[tuple] = []

    def fake_commit_push(wt, message, base):
        recorded.append((wt, message))

    monkeypatch.setattr(review_mod, "commit_push", fake_commit_push)
    return recorded


def _finding(severity: str = "high", file: str = "x.py") -> ReviewFinding:
    return ReviewFinding(
        severity=severity, file=file, line=10, category="bug", message="fix it",
    )


def test_apply_fixes_with_findings_calls_opencode_and_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls = _patch_apply_fixes_subprocess(monkeypatch)
    recorded = _record_commit_push(monkeypatch)
    cfg = _cfg()
    cfg.models.coding = "alibaba-coding-plan/qwen3-coder-plus"

    result = apply_fixes(
        "https://github.com/x/y/pull/1",
        [_finding(), _finding(severity="medium", file="y.py")],
        tmp_path,
        cfg,
        attempt=1,
    )

    assert isinstance(result, ApplyFixesResult)
    assert result.commits_added == 1
    assert result.verify_passed is True
    assert result.fix_attempts == 1
    assert len(recorded) == 1
    wt_arg, msg_arg = recorded[0]
    assert wt_arg == tmp_path
    assert msg_arg == "fix(review): address 2 reviewer findings [round 1]"
    opencode_calls = [
        c for c in calls
        if isinstance(c[0], (list, tuple)) and len(c[0]) > 0 and c[0][0] != "git"
    ]
    assert len(opencode_calls) == 1
    args, _ = opencode_calls[0]
    assert "--model" in args
    assert args[args.index("--model") + 1] == cfg.models.coding


def test_apply_fixes_omits_model_when_coding_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls = _patch_apply_fixes_subprocess(monkeypatch)
    _record_commit_push(monkeypatch)
    cfg = _cfg()
    cfg.models.coding = None

    apply_fixes(
        "https://github.com/x/y/pull/2",
        [_finding()],
        tmp_path,
        cfg,
        attempt=1,
    )

    opencode_calls = [
        c for c in calls
        if isinstance(c[0], (list, tuple)) and len(c[0]) > 0 and c[0][0] != "git"
    ]
    assert len(opencode_calls) == 1
    args, _ = opencode_calls[0]
    assert "--model" not in args, "review.apply_fixes must let opencode pick its model when cfg.models.coding is unset"


def test_apply_fixes_no_changes_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_apply_fixes_subprocess(monkeypatch, git_status_stdout="")
    recorded = _record_commit_push(monkeypatch)
    cfg = _cfg()

    result = apply_fixes(
        "https://github.com/x/y/pull/1", [_finding()], tmp_path, cfg, attempt=1,
    )

    assert result.commits_added == 0
    assert result.verify_passed is False
    assert recorded == []


def test_apply_fixes_opencode_failure_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_apply_fixes_subprocess(monkeypatch, opencode_returncode=1)
    recorded = _record_commit_push(monkeypatch)
    cfg = _cfg()

    result = apply_fixes(
        "https://github.com/x/y/pull/1", [_finding()], tmp_path, cfg, attempt=1,
    )

    assert result.commits_added == 0
    assert result.verify_passed is False
    assert recorded == []


def test_apply_fixes_timeout_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_apply_fixes_subprocess(
        monkeypatch,
        opencode_raises=subprocess.TimeoutExpired(cmd=["opencode"], timeout=1),
    )
    recorded = _record_commit_push(monkeypatch)
    cfg = _cfg()

    result = apply_fixes(
        "https://github.com/x/y/pull/1", [_finding()], tmp_path, cfg, attempt=1,
    )

    assert result.commits_added == 0
    assert result.verify_passed is False
    assert recorded == []


def test_fix_appends_no_force(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """apply_fixes must call commit_push with (worktree, message) — no --force anywhere."""
    _patch_apply_fixes_subprocess(monkeypatch)
    recorded = _record_commit_push(monkeypatch)
    cfg = _cfg()

    _ = apply_fixes(
        "https://github.com/x/y/pull/1", [_finding()], tmp_path, cfg, attempt=2,
    )

    assert len(recorded) == 1
    wt_arg, msg_arg = recorded[0]
    assert wt_arg == tmp_path
    assert msg_arg == "fix(review): address 1 reviewer findings [round 2]"
    # Defensive: scan all positional args for accidental --force
    for arg in recorded[0]:
        if isinstance(arg, str):
            assert "--force" not in arg
        elif isinstance(arg, (list, tuple)):
            assert "--force" not in arg


def test_apply_fixes_empty_findings_short_circuits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Empty findings list → no subprocess, no commit, verify_passed=True."""
    calls = _patch_apply_fixes_subprocess(monkeypatch)
    recorded = _record_commit_push(monkeypatch)
    cfg = _cfg()

    result = apply_fixes(
        "https://github.com/x/y/pull/1", [], tmp_path, cfg, attempt=1,
    )

    assert result.commits_added == 0
    assert result.verify_passed is True
    assert calls == []
    assert recorded == []


def test_review_fix_loop_clean_on_first_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inmem_store,
) -> None:
    cfg = _cfg()
    clean_result = ReviewResult(
        clean=True, findings=[], raw_output="", attempts=1, skill_used=cfg.review.skill_name,
    )
    review_calls: list[int] = []

    def fake_review_pr(pr_url, worktree, cfg, base="main"):
        review_calls.append(1)
        return clean_result

    apply_calls: list[int] = []

    def fake_apply_fixes(*args, **kwargs):
        apply_calls.append(1)
        return ApplyFixesResult(commits_added=1, verify_passed=True, fix_attempts=1)

    monkeypatch.setattr(review_mod, "review_pr", fake_review_pr)
    monkeypatch.setattr(review_mod, "apply_fixes", fake_apply_fixes)

    final = review_fix_loop(
        "https://github.com/x/y/pull/1", tmp_path, cfg, inmem_store,
        task_id="ba1lly/sandbox#1",
    )

    assert final.clean is True
    assert final.attempts == 1
    assert len(review_calls) == 1
    assert apply_calls == []
    rows = inmem_store.list_review_runs_for_pr("https://github.com/x/y/pull/1")
    assert len(rows) == 1
    assert rows[0]["clean"] is True
    assert rows[0]["attempts"] == 1
    assert rows[0]["ended_at"] is not None
    assert rows[0]["task_id"] == "ba1lly/sandbox#1"


def test_review_fix_loop_fixes_then_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inmem_store,
) -> None:
    cfg = _cfg()
    cfg.review.budget_attempts = 3
    findings = [_finding()]
    dirty = ReviewResult(
        clean=False, findings=findings, raw_output="", attempts=1, skill_used=cfg.review.skill_name,
    )
    clean = ReviewResult(
        clean=True, findings=[], raw_output="", attempts=1, skill_used=cfg.review.skill_name,
    )
    queue = [dirty, clean]
    review_calls: list[int] = []

    def fake_review_pr(pr_url, worktree, cfg, base="main"):
        review_calls.append(1)
        return queue.pop(0)

    apply_calls: list[int] = []

    def fake_apply_fixes(*args, **kwargs):
        apply_calls.append(1)
        return ApplyFixesResult(commits_added=1, verify_passed=True, fix_attempts=1)

    monkeypatch.setattr(review_mod, "review_pr", fake_review_pr)
    monkeypatch.setattr(review_mod, "apply_fixes", fake_apply_fixes)

    final = review_fix_loop(
        "https://github.com/x/y/pull/1", tmp_path, cfg, inmem_store,
        task_id="ba1lly/sandbox#2",
    )

    assert final.clean is True
    assert final.attempts == 2
    assert len(review_calls) == 2
    assert len(apply_calls) == 1
    rows = inmem_store.list_review_runs_for_pr("https://github.com/x/y/pull/1")
    assert len(rows) == 1
    assert rows[0]["clean"] is True
    assert rows[0]["attempts"] == 2


def test_budget_exhaustion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inmem_store,
) -> None:
    cfg = _cfg()
    cfg.review.budget_attempts = 2
    findings = [_finding()]
    dirty = ReviewResult(
        clean=False, findings=findings, raw_output="", attempts=1, skill_used=cfg.review.skill_name,
    )
    review_calls: list[int] = []

    def fake_review_pr(pr_url, worktree, cfg, base="main"):
        review_calls.append(1)
        return dirty

    apply_calls: list[int] = []

    def fake_apply_fixes(*args, **kwargs):
        apply_calls.append(1)
        return ApplyFixesResult(commits_added=1, verify_passed=True, fix_attempts=1)

    monkeypatch.setattr(review_mod, "review_pr", fake_review_pr)
    monkeypatch.setattr(review_mod, "apply_fixes", fake_apply_fixes)

    final = review_fix_loop(
        "https://github.com/x/y/pull/2", tmp_path, cfg, inmem_store,
        task_id="ba1lly/sandbox#3",
    )

    assert final.clean is False
    assert final.attempts == 2
    assert len(review_calls) == 2
    assert len(apply_calls) == 2
    rows = inmem_store.list_review_runs_for_pr("https://github.com/x/y/pull/2")
    assert len(rows) == 1
    assert rows[0]["clean"] is False
    assert rows[0]["attempts"] == 2


def test_review_fix_loop_aborts_when_no_fix_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inmem_store,
) -> None:
    """If apply_fixes returns commits_added=0, the loop must abort early."""
    cfg = _cfg()
    cfg.review.budget_attempts = 5
    findings = [_finding()]
    dirty = ReviewResult(
        clean=False, findings=findings, raw_output="", attempts=1, skill_used=cfg.review.skill_name,
    )
    review_calls: list[int] = []

    def fake_review_pr(pr_url, worktree, cfg, base="main"):
        review_calls.append(1)
        return dirty

    apply_calls: list[int] = []

    def fake_apply_fixes(*args, **kwargs):
        apply_calls.append(1)
        return ApplyFixesResult(commits_added=0, verify_passed=False, fix_attempts=1)

    monkeypatch.setattr(review_mod, "review_pr", fake_review_pr)
    monkeypatch.setattr(review_mod, "apply_fixes", fake_apply_fixes)

    final = review_fix_loop(
        "https://github.com/x/y/pull/3", tmp_path, cfg, inmem_store,
    )

    assert final.clean is False
    # Loop aborts after first failed apply_fixes — only 1 review + 1 apply
    assert len(review_calls) == 1
    assert len(apply_calls) == 1


# ---------- Store review_runs tests (Task 39) ----------


def test_start_review_run_and_get(inmem_store) -> None:
    run_id = inmem_store.start_review_run("task-1", "https://github.com/x/y/pull/1")
    assert run_id > 0
    row = inmem_store.get_review_run(run_id)
    assert row is not None
    assert row["task_id"] == "task-1"
    assert row["pr_url"] == "https://github.com/x/y/pull/1"
    assert row["attempts"] == 0
    assert row["clean"] is False
    assert row["started_at"] is not None
    assert row["ended_at"] is None


def test_end_review_run_marks_clean(inmem_store) -> None:
    run_id = inmem_store.start_review_run("task-2", "https://github.com/x/y/pull/2")
    inmem_store.end_review_run(run_id, attempts=3, clean=True)
    row = inmem_store.get_review_run(run_id)
    assert row is not None
    assert row["attempts"] == 3
    assert row["clean"] is True
    assert row["ended_at"] is not None


def test_list_review_runs_for_pr_orders_most_recent_first(inmem_store) -> None:
    import time
    pr_url = "https://github.com/x/y/pull/9"
    first = inmem_store.start_review_run("t-a", pr_url)
    time.sleep(0.005)
    second = inmem_store.start_review_run("t-b", pr_url)
    rows = inmem_store.list_review_runs_for_pr(pr_url)
    assert [r["id"] for r in rows] == [second, first]
