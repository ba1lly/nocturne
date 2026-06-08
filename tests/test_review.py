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


def test_review_skill_not_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_skill_enabled(monkeypatch, False)
    cfg = _cfg()
    with pytest.raises(SkillNotInstalled) as excinfo:
        review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)
    assert "nocturne skill install" in str(excinfo.value)
    assert cfg.review.skill_name in str(excinfo.value)


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
    """Alias of test_review_skill_not_installed — explicit assertion on hint format."""
    _patch_skill_enabled(monkeypatch, False)
    cfg = _cfg()
    with pytest.raises(SkillNotInstalled) as excinfo:
        review_pr("https://github.com/x/y/pull/1", tmp_path, cfg)
    msg = str(excinfo.value)
    assert "not installed" in msg
    assert "nocturne skill install" in msg


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
