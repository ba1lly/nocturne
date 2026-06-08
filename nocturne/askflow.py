# pyright: reportTypedDictNotRequiredAccess=false
"""Askflow — sentinel-based NEED_INPUT detection, park persistence, resume answer
injection, and LangGraph HITL graph with interrupt + idempotent post-interrupt
side effects.

Combines Tasks 25 (primitives) and 26 (LangGraph interrupt graph).

CRITICAL invariant (Metis directive on interrupt semantics):
LangGraph re-runs an interrupted node from the TOP on resume. Therefore any
externally-observable side effect (e.g. `gh issue comment`) MUST live in a
SEPARATE node that runs AFTER the node containing `interrupt()`. The
`post_park_comment_node` exists precisely for this reason — moving the comment
post inside `park_node` would cause a duplicate comment on every resume.

Sentinel detection delegates to `opencode_driver.detect_sentinel` (last-event
only). We never substring-match on raw text — that's the false-positive vector
Metis flagged for Sandbox Issue #5 (literal sentinel in issue body).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict, cast

from nocturne._logging import get_logger
from nocturne.config import Config
from nocturne.models import (
    OpenCodeResult,
    ParkedTask,
    Task,
    TriageResult,
    VerifyResult,
)
from nocturne.store import Store

logger = get_logger("nocturne.askflow")


# ---------------------------------------------------------------------------
# Task 25 primitives
# ---------------------------------------------------------------------------


class AskflowError(Exception):
    """Raised for askflow-level invariant violations (empty answer, not-parked, ...)."""


def detect_need_input(result: OpenCodeResult) -> Optional[str]:
    """Return the question if the sentinel was seen in the LAST text event; else None.

    Delegates entirely to Task 13's opencode_driver — the OpenCodeResult already
    carries `sentinel_seen` and `need_input_question` populated by
    `detect_sentinel()` at run time. We NEVER substring-match on raw text
    here — that would mis-fire on literal sentinels embedded in issue bodies
    (Metis: Sandbox Issue #5).
    """
    if result.sentinel_seen and result.need_input_question:
        return result.need_input_question
    return None


def park_task(task: Task, question: str, store: Store) -> ParkedTask:
    """Persist a task in parked state with a question. Returns the ParkedTask.

    Side effects: updates `tasks.status = 'parked'` + inserts into
    `parked_questions`. Idempotent at the store level (multiple calls with the
    same task_id are tolerated; latest question wins).
    """
    if not question or not question.strip():
        raise AskflowError("park_task requires non-empty question")

    store.park_task(task.id, question)

    task_fields = {k: v for k, v in task.model_dump().items() if k != "question"}
    parked = ParkedTask(
        **task_fields,
        question=question,
        parked_at=datetime.now(timezone.utc),
    )
    # Mirror the on-disk status so the in-memory object is consistent.
    parked.status = "parked"
    logger.info("parked task %s with question (len=%s)", task.id, len(question))
    return parked


def auto_question_from_failure(verify_result: VerifyResult, attempts: int) -> str:
    """Auto-generate a clarifying question after max-attempts exhaustion.

    The question embeds an error excerpt (stderr preferred, falling back to
    stdout) capped at 800 chars to keep the eventual gh comment readable.
    """
    err_source = verify_result.stderr or verify_result.stdout or "(no output)"
    err = err_source[:800]
    reason = verify_result.reason or "verify_cmd failed"
    return (
        f"Last attempt failed verification after {attempts} retries. "
        f"Reason: {reason}\n\n"
        f"Last error excerpt:\n```\n{err}\n```\n\n"
        f"How should I proceed? (e.g., 'add test for empty list', "
        f"'use a different algorithm')."
    )


def resume_task(task_id: str, answer: str, store: Store) -> Task:
    """Resume a parked task with a human-provided answer. Returns the refreshed Task.

    Validates: task exists, status == 'parked', answer is non-empty. Raises
    AskflowError on violation. On success, the store transitions
    status → 'selected' and persists the answer.
    """
    if not answer or not answer.strip():
        raise AskflowError("resume_task requires non-empty answer")

    task = store.get_task(task_id)
    if task is None:
        raise AskflowError(f"task not found: {task_id}")
    if task.status != "parked":
        raise AskflowError(
            f"task {task_id} is not parked (status={task.status})"
        )

    store.resume_task(task_id, answer)

    refreshed = store.get_task(task_id)
    if refreshed is None:
        raise AskflowError(f"task {task_id} disappeared after resume")

    logger.info("resumed task %s (answer len=%s)", task_id, len(answer))
    return refreshed


def render_resume_prompt(
    task: Task, cfg: Config, prior_failure: Optional[str] = None
) -> str:
    """Wrap `render_task_prompt` with the human answer prepended.

    Format: "Human responded to your earlier question: <answer>\\n\\nNow continue:\\n\\n<base prompt>".
    If the task has no answer (or an empty/whitespace-only one), the base prompt
    is returned unchanged.
    """
    # Late import keeps the jinja env optional at import time for the rest of
    # the module (tests that don't render prompts can import askflow without
    # the prompts package being fully initialized).
    from nocturne.prompts.render import render_task_prompt

    base = render_task_prompt(task, cfg, prior_failure=prior_failure)
    answer = (task.answer or "").strip()
    if not answer:
        return base
    return (
        f"Human responded to your earlier question: {answer}\n\n"
        f"Now continue:\n\n"
        f"{base}"
    )


def list_parked(store: Store) -> list[ParkedTask]:
    """Return all currently-parked tasks as ParkedTask instances.

    Uses `tasks.updated_at` as the `parked_at` proxy — when a task is parked,
    the store sets updated_at to the same timestamp written into
    parked_questions. Tasks whose question column is empty (corrupt row) are
    given a placeholder so the ParkedTask validator does not reject them.
    """
    out: list[ParkedTask] = []
    for t in store.list_by_status("parked"):
        question = t.question or "(no question recorded)"
        task_fields = {
            k: v for k, v in t.model_dump().items() if k not in ("question",)
        }
        out.append(
            ParkedTask(
                **task_fields,
                question=question,
                parked_at=t.updated_at,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Task 26 LangGraph HITL graph
# ---------------------------------------------------------------------------
#
# CRITICAL INVARIANT (Metis directive): any side effect BEFORE interrupt()
# re-runs on resume. So `gh issue comment` notification posts MUST live in
# `post_park_comment_node` (AFTER the interrupt), NOT in `park_node` itself.
# ---------------------------------------------------------------------------


# Distinct from triage's NOCTURNE_SKIP_MARKER — questions and skips are different
# event classes and must not collide on the gh comment-search filter.
QUESTION_MARKER = "<!-- nocturne-question -->"


class HITLState(TypedDict, total=False):
    """State threaded through the HITL graph."""

    task: Task
    cfg: Config
    triage_result: Optional[TriageResult]
    opencode_result: Optional[OpenCodeResult]
    verify_result: Optional[VerifyResult]
    question: Optional[str]
    answer: Optional[str]
    pr_url: Optional[str]
    attempt: int


def post_park_comment_node(state: HITLState, store: Store) -> dict[str, Any]:
    """Post the parked question as a gh issue comment.

    Runs AFTER `park_node`'s interrupt resumes. Idempotent: checks for an
    existing question marker via `gh issue view --jq` before posting. gh
    failures are non-blocking (logged + swallowed).

    Exposed at module level so unit tests can exercise it directly without
    spinning up the full graph (the graph's interrupt machinery is hard to
    drive in pytest).
    """
    from nocturne._gh_retry import GhError, run_gh

    task = state["task"]
    question = state.get("question") or ""

    # Idempotency check — if a question marker comment already exists, skip.
    try:
        existing = run_gh(
            [
                "gh",
                "issue",
                "view",
                str(task.issue_number),
                "--repo",
                task.repo_slug,
                "--json",
                "comments",
                "--jq",
                f'.comments[] | select(.body | startswith("{QUESTION_MARKER}"))',
            ]
        )
        if existing and existing.strip():
            logger.info(
                "question comment already exists on %s#%s; skipping post",
                task.repo_slug,
                task.issue_number,
            )
            return {}
    except GhError as exc:
        logger.warning(
            "could not check existing question comments on %s#%s: %s",
            task.repo_slug,
            task.issue_number,
            exc,
        )
        # Fall through and attempt to post — one duplicate is acceptable; a
        # silent miss is not.

    body = f"{QUESTION_MARKER}\n[Nocturne] {question}"
    try:
        run_gh(
            [
                "gh",
                "issue",
                "comment",
                str(task.issue_number),
                "--repo",
                task.repo_slug,
                "--body",
                body,
            ]
        )
        logger.info(
            "posted question comment on %s#%s",
            task.repo_slug,
            task.issue_number,
        )
    except GhError as exc:
        logger.warning(
            "failed to post question comment on %s#%s (non-blocking): %s",
            task.repo_slug,
            task.issue_number,
            exc,
        )

    return {}


def build_hitl_graph(cfg: Config, store: Store):  # type: ignore[no-untyped-def]
    """Build a compiled LangGraph HITL graph for one task lifecycle.

    Flow:
        triage_node
          ├─ SKIP        → END
          ├─ NEED_INPUT  → park_node → interrupt → post_park_comment_node → run_node
          └─ DOABLE      → run_node
        run_node → (sentinel? park_node : verify_node)
        verify_node → (pass → END | retry → run_node | exhausted → park_node)

    Uses MemorySaver as the default checkpointer. Full SqliteSaver-backed
    persistence is configured per cfg but its restart-survival is exercised
    in M3 live acceptance (Task 29) rather than in-unit (langgraph-checkpoint-
    sqlite v3 exposes `from_conn_string` as a context manager, so the file-
    backed checkpoint is held by the daemon process, not constructed here).
    """
    try:
        from langgraph.graph import END, StateGraph
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.types import interrupt
    except ImportError as exc:
        raise AskflowError(f"langgraph not installed: {exc}") from exc

    checkpointer = MemorySaver()

    # Late imports keep the graph's heavy deps out of askflow's import path
    # for callers that only need the Task 25 primitives.
    from nocturne.opencode_driver import run as opencode_run
    from nocturne.triage import classify as triage_classify
    from nocturne.verifier import verify as do_verify

    def triage_node(state: HITLState) -> dict[str, Any]:
        tr = triage_classify(state["task"], state["cfg"])
        return {"triage_result": tr}

    def route_after_triage(state: HITLState) -> str:
        tr = state.get("triage_result")
        if tr is None:
            return "end_skip"
        if tr.outcome == "SKIP":
            return "end_skip"
        if tr.outcome == "NEED_INPUT":
            return "park"
        return "run"  # DOABLE

    def run_node(state: HITLState) -> dict[str, Any]:
        task = state["task"]
        cfg_local = state["cfg"]
        attempt = state.get("attempt", 0) + 1
        try:
            result = opencode_run(task, Path(task.checkout_path), cfg_local)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning("opencode_run raised in HITL graph: %s", exc)
            result = OpenCodeResult(
                exit_code=-1,
                events=[],
                sentinel_seen=False,
                need_input_question=None,
                pid=None,
                error_events=[{"type": "exception", "message": str(exc)}],
            )
        return {"opencode_result": result, "attempt": attempt}

    def route_after_run(state: HITLState) -> str:
        result = state.get("opencode_result")
        if result is None:
            return "end_fail"
        if detect_need_input(result):
            return "park"
        return "verify"

    def park_node(state: HITLState) -> dict[str, Any]:
        """Persist parked state; emit interrupt() with question for human.

        CRITICAL: NO `gh issue comment` here. interrupt() halts execution; on
        resume this node re-runs from the top with `answer` injected via the
        Command(resume=...) protocol. Posting a comment here would duplicate
        it on every resume.
        """
        task = state["task"]
        opencode_result = state.get("opencode_result")
        if opencode_result and opencode_result.need_input_question:
            question = opencode_result.need_input_question
        else:
            tr = state.get("triage_result")
            question = (tr.reason if tr else None) or "Need clarification to proceed."

        # Idempotent persistence: if already parked, skip the second write so
        # parked_questions does not accumulate duplicate rows on resume re-runs.
        existing = store.get_task(task.id)
        if existing is not None and existing.status == "parked":
            logger.info(
                "task %s already parked; skipping persistence", task.id
            )
        else:
            park_task(task, question, store)

        # interrupt() halts the graph until Command(resume=answer) is invoked.
        # The return value is the answer payload supplied at resume time.
        answer = interrupt({"task_id": task.id, "question": question})
        return {"question": question, "answer": answer}

    def post_park_node(state: HITLState) -> dict[str, Any]:
        """Side-effect node that runs AFTER interrupt resumes."""
        return post_park_comment_node(state, store)

    def verify_node(state: HITLState) -> dict[str, Any]:
        task = state["task"]
        try:
            v = do_verify(task, Path(task.checkout_path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify raised in HITL graph: %s", exc)
            v = VerifyResult(
                passed=False,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                new_test_added=False,
                reason=f"verify raised: {exc}",
            )
        return {"verify_result": v}

    def route_after_verify(state: HITLState) -> str:
        v = state.get("verify_result")
        if v is not None and v.passed:
            return "end_done"
        attempt = state.get("attempt", 0)
        max_attempts = state["cfg"].guardrails.max_attempts
        if attempt >= max_attempts:
            return "park"
        return "run"

    graph = StateGraph(HITLState)
    graph.add_node("triage", triage_node)
    graph.add_node("run", run_node)
    graph.add_node("park", park_node)
    graph.add_node("post_park_comment", post_park_node)
    graph.add_node("verify", verify_node)

    graph.set_entry_point("triage")
    graph.add_conditional_edges(
        "triage",
        route_after_triage,
        {"end_skip": END, "park": "park", "run": "run"},
    )
    graph.add_conditional_edges(
        "run",
        route_after_run,
        {"park": "park", "verify": "verify", "end_fail": END},
    )
    graph.add_edge("park", "post_park_comment")
    graph.add_edge("post_park_comment", "run")
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {"end_done": END, "park": "park", "run": "run"},
    )

    return graph.compile(checkpointer=checkpointer)


def resume_with_answer(
    task_id: str, answer: str, cfg: Config, store: Store
) -> Task:
    """Resume a parked task by persisting the answer and flipping status.

    In M3 the orchestrator polls for `status='selected'` tasks on its next
    batch cycle and processes them through the normal `process_task` path
    with the answer injected via `render_resume_prompt`. Graph-level
    re-entry via `Command(resume=...)` is reserved for the Discord bot
    (Task 30) which holds the live graph instance.

    Parameters `cfg` is accepted for API symmetry with the CLI / Discord
    callers (Task 27, Task 30) even though this lightweight variant does
    not need it.
    """
    del cfg  # reserved for future graph re-entry (Task 30)
    return resume_task(task_id, answer, store)


# Re-export the module-level constant so callers (tests, CLI) can refer to it
# without importing implementation details.
__all__ = [
    "AskflowError",
    "QUESTION_MARKER",
    "HITLState",
    "auto_question_from_failure",
    "build_hitl_graph",
    "detect_need_input",
    "list_parked",
    "park_task",
    "post_park_comment_node",
    "render_resume_prompt",
    "resume_task",
    "resume_with_answer",
]

# Silence the unused-import warning for `cast` — kept available for tests/IDEs.
_ = cast
