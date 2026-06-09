"""Triage classifier - DOABLE / SKIP / NEED_INPUT with priority ordering.

Combines Task 20 (classification + LangGraph node) and Task 21 (idempotent
skip-comment posting). The public surface:

  - classify(issue, cfg)       - single-issue LLM classification (fallback SKIP)
  - already_commented_skip(...) - gh-based marker check (returns False on gh error)
  - post_skip_comment(...)     - idempotent + non-blocking comment poster
  - triage_batch(issues, cfg)   - classify + post skip comments + sort
  - build_triage_graph()       - LangGraph wiring (single-node M2 form)

Outcome semantics are locked to TriageOutcome's Literal["DOABLE","SKIP","NEED_INPUT"]
via pydantic validation. Any out-of-band outcome (PARTIAL, SPLIT, ESCALATE, ...)
triggers the parse-error fallback to SKIP.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, TypedDict

from nocturne._gh_retry import GhError, run_gh
from nocturne._logging import get_logger
from nocturne.config import Config, get_api_key, provider_of
from nocturne.models import Task, TriageOutcome, TriageResult

logger = get_logger("nocturne.triage")

# Marker used to detect Nocturne-authored skip comments (idempotency).
# Exact form is part of the public contract - do NOT add trailing whitespace.
NOCTURNE_SKIP_MARKER = "<!-- nocturne-skip -->"

def _load_rubric() -> str:
    """Load the triage rubric from disk; fall back to an inline minimal rubric."""
    path = Path(__file__).parent / "prompts" / "triage_rubric.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (
            "Classify issues as DOABLE / SKIP / NEED_INPUT. Output strict JSON "
            "with keys: outcome, priority (0-100 integer), reason (short string)."
        )


TRIAGE_RUBRIC: str = _load_rubric()


class TriageError(Exception):
    """Raised for unrecoverable triage-framework errors (e.g. langgraph missing)."""


def classify(issue: Task, cfg: Config) -> TriageResult:
    """Classify a single issue via the reasoning LLM.

    Any error (network, parse, invalid outcome value, validation) falls back
    to a SKIP result with reason="triage parse error: <ExceptionType>" so the
    caller can continue processing the batch.
    """
    try:
        # Lazy import so tests can patch openai.OpenAI via the mock_openai fixture.
        from openai import OpenAI

        provider_name = provider_of(cfg.models.reasoning)
        provider_cfg = cfg.providers[provider_name]
        api_key = get_api_key(cfg, provider_name)

        client = OpenAI(base_url=provider_cfg.base_url, api_key=api_key)
        model = cfg.models.reasoning.split("/", 1)[1]

        response = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": TRIAGE_RUBRIC},
                {
                    "role": "user",
                    "content": f"Issue #{issue.issue_number}: {issue.title}\n\n{issue.body or ''}",
                },
            ],
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("empty LLM response content")

        parsed = json.loads(content)

        # Clamp priority into pydantic's [0, 100] range - the rubric instructs
        # the model to stay in range; clamping keeps us robust to model drift
        # without losing the signal entirely.
        raw_priority = int(parsed.get("priority", 50))
        priority = max(0, min(100, raw_priority))

        outcome_value = parsed["outcome"]  # TriageOutcome validator rejects unknown labels
        reason = str(parsed.get("reason", ""))[:200]

        return TriageResult(
            task_id=issue.id,
            doable=(outcome_value == "DOABLE"),
            outcome=TriageOutcome(outcome_value),
            priority=priority,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 - fallback path is intentional
        logger.warning(
            "triage parse error for %s: %s - falling back to SKIP", issue.id, exc
        )
        return TriageResult(
            task_id=issue.id,
            doable=False,
            outcome=TriageOutcome("SKIP"),
            priority=0,
            reason=f"triage parse error: {type(exc).__name__}",
        )


def already_commented_skip(repo_slug: str, issue_number: int) -> bool:
    """Return True iff the issue already has a Nocturne-skip comment.

    Uses `gh issue view --json comments --jq <filter>` so the gh CLI does the
    filtering server-side. Any gh-side error returns False (best-effort: a
    duplicate comment is recoverable; a hard failure here is not).
    """
    try:
        output = run_gh(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo_slug,
                "--json",
                "comments",
                "--jq",
                f'.comments[] | select(.body | startswith("{NOCTURNE_SKIP_MARKER}"))',
            ]
        )
        return bool(output and output.strip())
    except GhError as exc:
        logger.warning(
            "could not check existing skip comments on %s#%s: %s",
            repo_slug,
            issue_number,
            exc,
        )
        return False


def post_skip_comment(repo_slug: str, issue_number: int, reason: str) -> None:
    """Idempotently post a skip comment.

    Skips posting if a marker comment is already present; on gh failure,
    logs a warning and returns (non-blocking by contract).
    """
    if already_commented_skip(repo_slug, issue_number):
        logger.info(
            "skip comment already exists on %s#%s - not posting again",
            repo_slug,
            issue_number,
        )
        return

    body = (
        f"{NOCTURNE_SKIP_MARKER}\n"
        f"[Nocturne triage] Skipped: {reason}\n\n"
        f"This issue was classified as out-of-scope by Nocturne. Common reasons: "
        f"vague requirements, architectural scope, design decisions needed, "
        f"security/risky surface, or no clear test path. "
        f"Feel free to refine the issue and remove the `agent` label to re-eligible."
    )

    try:
        run_gh(
            [
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                repo_slug,
                "--body",
                body,
            ]
        )
        logger.info("posted skip comment on %s#%s", repo_slug, issue_number)
    except GhError as exc:
        logger.warning(
            "failed to post skip comment on %s#%s (non-blocking): %s",
            repo_slug,
            issue_number,
            exc,
        )


# Sort priority used by triage_batch's stable sort (lower rank = earlier).
_OUTCOME_RANK = {"DOABLE": 0, "NEED_INPUT": 1, "SKIP": 2}


def triage_batch(
    issues: list[Task], cfg: Config, *, dry_run: bool = False,
) -> list[tuple[Task, TriageResult]]:
    """Classify each issue, post skip comments for SKIPs, return sorted pairs.

    Sort order:
      1. DOABLE first, descending by priority
      2. NEED_INPUT next, descending by priority
      3. SKIP last, descending by priority

    Skip-comment posting is best-effort: any exception raised by
    post_skip_comment is logged and swallowed so the rest of the batch
    continues to be classified. When dry_run=True, classification still
    runs but skip comments are NOT posted to GitHub - preserves dry-run's
    "no external side effects" contract.
    """
    results: list[tuple[Task, TriageResult]] = []
    for task in issues:
        tr = classify(task, cfg)
        results.append((task, tr))
        if tr.outcome == "SKIP" and not dry_run:
            try:
                post_skip_comment(task.repo_slug, task.issue_number, tr.reason)
            except Exception as exc:  # noqa: BLE001 - batch must keep going
                logger.warning(
                    "post_skip_comment unexpectedly raised on %s#%s (swallowed): %s",
                    task.repo_slug,
                    task.issue_number,
                    exc,
                )
        elif tr.outcome == "SKIP" and dry_run:
            logger.info(
                "dry-run: would have posted skip comment on %s#%s (reason=%s)",
                task.repo_slug, task.issue_number, tr.reason,
            )

    results.sort(key=lambda pair: (_OUTCOME_RANK[pair[1].outcome], -pair[1].priority))
    return results


# ---------------------------------------------------------------------------
# LangGraph wiring (M2 single-node form; M3 / Task 26 extends with interrupts)
# ---------------------------------------------------------------------------


class TriageState(TypedDict):
    """State for the triage LangGraph node."""

    issues: list[Task]
    cfg: Optional[Config]
    triaged: list[tuple[Task, TriageResult]]


def build_triage_graph():  # type: ignore[no-untyped-def]
    """Build a compiled LangGraph with a single triage node.

    M3 (Task 26 / askflow) will extend this graph with an interrupt-capable
    NEED_INPUT branch. For M2 the graph is intentionally minimal so the
    classification + skip-comment behavior is exercised directly via
    triage_batch.
    """
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise TriageError(
            "langgraph not installed - install it to use the triage graph"
        ) from exc

    def triage_node(state: TriageState) -> dict[str, list[tuple[Task, TriageResult]]]:
        cfg = state.get("cfg")
        if cfg is None:
            return {"triaged": []}
        triaged = triage_batch(state["issues"], cfg)
        return {"triaged": triaged}

    graph = StateGraph(TriageState)
    graph.add_node("triage", triage_node)
    graph.set_entry_point("triage")
    graph.add_edge("triage", END)
    return graph.compile()
