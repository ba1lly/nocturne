"""Reviewer module — invoke a reviewer skill on PR diffs + parse findings.

Severity floor filtering per cfg.review.severity_floor.
Skill-not-installed raises with install hint.
Falls back to regex parsing if reviewer outputs malformed JSON.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import jinja2
from pydantic import BaseModel, Field

from nocturne._logging import get_logger
from nocturne.config import Config
from nocturne.gitwork import commit_push
from nocturne.skills import SKILLS_DIR, is_skill_enabled  # noqa: F401 (SKILLS_DIR re-export)

logger = get_logger("nocturne.review")

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


class ReviewError(Exception):
    pass


class SkillNotInstalled(ReviewError):
    pass


class ReviewFinding(BaseModel):
    severity: str = Field(default="info", description="info|low|medium|high|critical")
    file: str = ""
    line: Optional[int] = None
    category: str = ""
    message: str = ""
    suggested_fix: Optional[str] = None


class ReviewResult(BaseModel):
    clean: bool
    findings: list[ReviewFinding] = Field(default_factory=list)
    raw_output: str = ""
    attempts: int = 1
    skill_used: str = ""


_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "review_invocation.md.jinja2"


def _load_template() -> jinja2.Template:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATE_PATH.parent),
        autoescape=False,
        keep_trailing_newline=True,
    )
    return env.get_template(_TEMPLATE_PATH.name)


def _render_review_prompt(skill_name: str, pr_url: str, diff: str) -> str:
    return _load_template().render(skill_name=skill_name, pr_url=pr_url, diff=diff)


def _compute_diff(worktree: Path, base: str = "main") -> str:
    """Compute the diff between origin/<base>..HEAD in the worktree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "diff", f"origin/{base}..HEAD"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        # Fallback to local base without origin
        try:
            result = subprocess.run(
                ["git", "-C", str(worktree), "diff", f"{base}..HEAD"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            return result.stdout
        except Exception:
            return ""


def _extract_json_blocks(text: str) -> list[dict]:
    """Extract findings from the reviewer's output.

    Tries to find a JSON array block (last one wins if multiple).
    Falls back to regex line-by-line parsing for malformed output.
    """
    # 1. Look for JSON code blocks (```json ... ```)
    code_block_pattern = re.compile(r"```json\s*\n([\s\S]*?)\n```", re.MULTILINE)
    matches = code_block_pattern.findall(text)
    candidates = list(matches)

    # 2. Also try raw JSON arrays anywhere in the text
    array_pattern = re.compile(r"\[\s*(?:\{[^{}]*\}\s*,?\s*)*\]", re.DOTALL)
    candidates.extend(array_pattern.findall(text))

    for raw in reversed(candidates):
        try:
            parsed = json.loads(raw.strip())
            if isinstance(parsed, list) and all(isinstance(x, dict) for x in parsed):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue

    # 3. Regex fallback for malformed output (per plan spec)
    fallback: list[dict] = []
    line_pattern = re.compile(
        r"^\s*\[(info|low|medium|high|critical)\]\s+([^:]+):(\d+)\s*-\s*(.+?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    for match in line_pattern.finditer(text):
        fallback.append({
            "severity": match.group(1).lower(),
            "file": match.group(2).strip(),
            "line": int(match.group(3)),
            "category": "",
            "message": match.group(4).strip(),
            "suggested_fix": None,
        })
    if fallback:
        logger.warning(
            "review output JSON parse failed; recovered %s findings via regex fallback",
            len(fallback),
        )
    return fallback


def _filter_by_severity_floor(findings: list[ReviewFinding], floor: str) -> list[ReviewFinding]:
    """Keep only findings at or above the severity floor."""
    floor_value = SEVERITY_ORDER.get(floor.lower(), 0)
    return [f for f in findings if SEVERITY_ORDER.get(f.severity.lower(), 0) >= floor_value]


def review_pr(
    pr_url: str,
    worktree: Path,
    cfg: Config,
    base: str = "main",
) -> ReviewResult:
    """Invoke the reviewer skill on a PR diff. Returns ReviewResult with findings."""
    skill_name = cfg.review.skill_name
    if not is_skill_enabled(skill_name):
        raise SkillNotInstalled(
            f"reviewer skill '{skill_name}' is not installed. "
            f"Run `nocturne skill install <source>` to install it."
        )

    diff = _compute_diff(worktree, base=base)
    if not diff.strip():
        logger.info("empty diff for %s; reporting clean", pr_url)
        return ReviewResult(
            clean=True, findings=[], raw_output="", attempts=1, skill_used=skill_name,
        )

    prompt = _render_review_prompt(skill_name, pr_url, diff)

    # Write the prompt to a temp file in the worktree and invoke OpenCode.
    # We use subprocess directly (not opencode_driver.run) because the review
    # flow does not use a Task model — it uses the reasoning model on a diff.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", dir=None, delete=False
    ) as f:
        f.write(prompt)
        prompt_path = f.name

    try:
        result = subprocess.run(
            [
                cfg.opencode.command, "run",
                "--model", cfg.models.reasoning,
                "--dir", str(worktree),
                "--format", "json",
                "-f", prompt_path,
            ],
            capture_output=True, text=True,
            timeout=cfg.opencode.timeout_min * 60,
            check=False,
        )
        raw_output = result.stdout + "\n" + result.stderr
    except subprocess.TimeoutExpired:
        logger.warning("review subprocess timed out for %s", pr_url)
        return ReviewResult(
            clean=False, findings=[], raw_output="timeout",
            attempts=1, skill_used=skill_name,
        )
    finally:
        try:
            Path(prompt_path).unlink(missing_ok=True)
        except Exception:
            pass

    # Extract text from OpenCode's --format json NDJSON output.
    # Each line is a JSON event; we collect "text" or "content" fields.
    raw_text_parts: list[str] = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            raw_text_parts.append(line)
            continue
        if isinstance(event, dict):
            text = event.get("text") or event.get("content")
            if isinstance(text, str):
                raw_text_parts.append(text)
            else:
                part = event.get("part")
                if isinstance(part, dict):
                    part_text = part.get("text")
                    if isinstance(part_text, str):
                        raw_text_parts.append(part_text)
    combined_text = "\n".join(raw_text_parts) if raw_text_parts else raw_output

    # Parse findings
    findings_dicts = _extract_json_blocks(combined_text)
    findings: list[ReviewFinding] = []
    for fd in findings_dicts:
        try:
            findings.append(ReviewFinding(**fd))
        except Exception as e:
            logger.warning("skipping malformed finding: %s (%s)", fd, e)

    # Severity floor filter
    findings = _filter_by_severity_floor(findings, cfg.review.severity_floor)
    clean = len(findings) == 0

    return ReviewResult(
        clean=clean,
        findings=findings,
        raw_output=combined_text[:10000],  # cap for memory
        attempts=1,
        skill_used=skill_name,
    )


class ApplyFixesResult(BaseModel):
    commits_added: int = 0
    verify_passed: bool = False
    fix_attempts: int = 0


def _render_fix_prompt(findings: list[ReviewFinding], pr_url: str) -> str:
    """Build a prompt instructing OpenCode to fix the listed findings."""
    body_lines = [
        f"You are addressing reviewer findings on PR {pr_url}.",
        "",
        "Apply minimal targeted fixes for each of the following findings.",
        "DO NOT refactor unrelated code. DO NOT add new features.",
        "",
        "Findings:",
        "",
    ]
    for i, f in enumerate(findings, 1):
        line_str = str(f.line) if f.line is not None else "?"
        body_lines.append(
            f"{i}. [{f.severity}] {f.file}:{line_str} ({f.category}): {f.message}"
        )
        if f.suggested_fix:
            body_lines.append(f"   Suggested fix: {f.suggested_fix}")
    body_lines.append("")
    body_lines.append("After applying fixes, the verification command will be run.")
    body_lines.append("Add new tests if needed to cover the fixes.")
    return "\n".join(body_lines)


def apply_fixes(
    pr_url: str,
    findings: list[ReviewFinding],
    worktree: Path,
    cfg: Config,
    attempt: int = 1,
    base: str = "main",
) -> ApplyFixesResult:
    """Invoke OpenCode to apply fixes for the findings; commit + push (append-only).

    Lets opencode pick its own model unless cfg.models.coding is explicitly set.
    Calls gitwork.commit_push which enforces no force-push.
    """
    if not findings:
        return ApplyFixesResult(commits_added=0, verify_passed=True, fix_attempts=attempt)

    prompt = _render_fix_prompt(findings, pr_url)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", dir=None, delete=False,
    ) as f:
        f.write(prompt)
        prompt_path = f.name

    opencode_args = [
        cfg.opencode.command, "run",
        "--dir", str(worktree),
        "--format", "json",
        "-f", prompt_path,
    ]
    if cfg.models.coding:
        opencode_args.extend(["--model", cfg.models.coding])

    try:
        result = subprocess.run(
            opencode_args,
            capture_output=True, text=True,
            timeout=cfg.opencode.timeout_min * 60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("apply_fixes opencode timed out for %s", pr_url)
        return ApplyFixesResult(commits_added=0, verify_passed=False, fix_attempts=attempt)
    finally:
        try:
            Path(prompt_path).unlink(missing_ok=True)
        except Exception:
            pass

    if result.returncode != 0:
        logger.warning(
            "apply_fixes opencode failed (exit %s): %s",
            result.returncode, (result.stderr or "")[:500],
        )
        return ApplyFixesResult(commits_added=0, verify_passed=False, fix_attempts=attempt)

    status_result = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    if not status_result.stdout.strip():
        logger.info("apply_fixes for %s: no changes after opencode run", pr_url)
        return ApplyFixesResult(commits_added=0, verify_passed=False, fix_attempts=attempt)

    # Pre-commit gate: run the configured verify_cmd before pushing fix commits.
    # The reviewer's "fix" is only acceptable if verification passes.
    verify_cmd = _verify_cmd_for_pr(pr_url, cfg)
    if verify_cmd is not None:
        verify_result = subprocess.run(
            verify_cmd, shell=True, cwd=str(worktree),
            capture_output=True, text=True, timeout=600,
            check=False,
        )
        if verify_result.returncode != 0:
            logger.warning(
                "apply_fixes verify failed for %s (exit %s): %s",
                pr_url, verify_result.returncode, (verify_result.stderr or "")[:400],
            )
            return ApplyFixesResult(commits_added=0, verify_passed=False, fix_attempts=attempt)

    commit_msg = (
        f"fix(review): address {len(findings)} reviewer findings [round {attempt}]"
    )
    try:
        commit_push(worktree, commit_msg, base)
    except Exception as e:
        logger.warning("commit_push raised in apply_fixes: %s", e)
        return ApplyFixesResult(commits_added=0, verify_passed=False, fix_attempts=attempt)

    return ApplyFixesResult(commits_added=1, verify_passed=True, fix_attempts=attempt)


def _verify_cmd_for_pr(pr_url: str, cfg: Config) -> Optional[str]:
    """Resolve the verify_cmd to use for a given PR URL.

    Matches against the repos allowlist by slug. Returns None if no match
    (caller should skip the verify gate rather than fail closed).
    """
    import re as _re
    m = _re.search(r"github\.com/([^/]+/[^/]+)/pull/", pr_url)
    if not m:
        return None
    slug = m.group(1)
    for r in cfg.repos:
        if r.slug == slug:
            return r.verify_cmd
    return None


def review_fix_loop(
    pr_url: str,
    worktree: Path,
    cfg: Config,
    store,
    task_id: Optional[str] = None,
    base: str = "main",
) -> ReviewResult:
    """Run the full review→fix loop until clean OR budget exhausted.

    Records each attempt in store.review_runs table.
    Returns the final ReviewResult.
    """
    budget = cfg.review.budget_attempts
    run_id = store.start_review_run(task_id, pr_url)
    final_result = ReviewResult(
        clean=False, findings=[], raw_output="",
        attempts=0, skill_used=cfg.review.skill_name,
    )

    try:
        for attempt in range(1, budget + 1):
            try:
                result = review_pr(pr_url, worktree, cfg, base=base)
            except SkillNotInstalled:
                raise
            except Exception as e:
                logger.warning("review_pr raised on attempt %s: %s", attempt, e)
                break
            final_result = result
            final_result.attempts = attempt
            if result.clean:
                logger.info(
                    "review clean for %s after %s attempt(s)", pr_url, attempt,
                )
                break
            logger.info(
                "review attempt %s: %s — applying fixes",
                attempt, findings_summary(result.findings),
            )
            fix_result = apply_fixes(
                pr_url, result.findings, worktree, cfg, attempt=attempt,
            )
            if fix_result.commits_added == 0:
                logger.warning(
                    "no fixes applied at attempt %s; aborting fix loop", attempt,
                )
                break
    finally:
        store.end_review_run(run_id, final_result.attempts, final_result.clean)

    return final_result


def findings_summary(findings: list[ReviewFinding]) -> str:
    """Format for Discord/log: '3 findings: 1 high, 2 info'."""
    if not findings:
        return "no findings"
    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    parts = [
        f"{n} {sev}"
        for sev, n in sorted(by_sev.items(), key=lambda kv: -SEVERITY_ORDER.get(kv[0], 0))
    ]
    return f"{len(findings)} findings: " + ", ".join(parts)
