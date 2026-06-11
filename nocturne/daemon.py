"""Daemon loop - single-process asyncio sharing event loop with Discord bot.

Polls each repo every ``cfg.daemon.poll_interval_sec``; respects ``quiet_hours``;
honors ``token_budget`` halt; survives SIGTERM gracefully; supports cross-process
pause via SQLite ``daemon_state.paused`` flag.
"""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from nocturne._logging import get_logger
from nocturne.config import Config
from nocturne.store import Store

if TYPE_CHECKING:
    from nocturne.discord_bot import NocturneBot
    from nocturne.models import Task

logger = get_logger("nocturne.daemon")


class DaemonError(Exception):
    pass


class Daemon:
    """Async poll-loop daemon. Shares event loop with optional NocturneBot."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        bot: Optional["NocturneBot"] = None,
    ):
        self.cfg = cfg
        self.store = store
        self.bot = bot
        # In-process pause state: SET = running, CLEAR = paused.
        self._paused = asyncio.Event()
        self._paused.set()  # default: running
        self._stop = asyncio.Event()
        self._tokens_used = 0
        self._last_poll_at: Optional[datetime] = None

    def is_paused(self) -> bool:
        """Return True if the daemon is currently paused (in-process view)."""
        return not self._paused.is_set()

    @property
    def tokens_used(self) -> int:
        return self._tokens_used

    @property
    def last_poll_at(self) -> Optional[datetime]:
        return self._last_poll_at

    async def wait_for_resume(self, timeout: Optional[float] = None) -> bool:
        """In-process convenience for callers awaiting resume.

        The poll loop does NOT use this - it polls SQLite directly to support
        cross-process pause.
        """
        try:
            await asyncio.wait_for(self._paused.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def pause(self) -> None:
        """In-process pause convenience. Writes the SQLite flag for cross-process visibility."""
        self.store.set_daemon_flag("paused", "1")
        self._paused.clear()

    def resume(self) -> None:
        """In-process resume convenience."""
        self.store.set_daemon_flag("paused", "0")
        self._paused.set()

    async def _check_paused_flag(self) -> bool:
        """Read the SQLite paused flag (cross-process). Sync in-process Event accordingly."""
        flag = await asyncio.to_thread(self.store.get_daemon_flag, "paused")
        is_paused = flag == "1"
        if is_paused:
            self._paused.clear()
        else:
            self._paused.set()
        return is_paused

    def _is_quiet_hour(self) -> bool:
        """Check whether the current hour is in cfg.daemon.quiet_hours.

        Hours are interpreted in ``cfg.daemon.quiet_hours_tz`` when set,
        otherwise UTC. For a "night shift" tool, operators usually want their
        local night, so the tz override avoids a surprising UTC-only footgun.
        """
        quiet = list(self.cfg.daemon.quiet_hours or [])
        if not quiet:
            return False
        tz_name = self.cfg.daemon.quiet_hours_tz
        if tz_name is None:
            now_hour = datetime.now(timezone.utc).hour
        else:
            from zoneinfo import ZoneInfo
            now_hour = datetime.now(ZoneInfo(tz_name)).hour
        return now_hour in quiet

    def _is_budget_exhausted(self) -> bool:
        """Check if tokens used exceeded the configured token budget."""
        return self._tokens_used > self.cfg.guardrails.token_budget

    async def _process_and_report(
        self,
        task: "Task",
        cycle_summary: dict[str, Any],
        cycle_done: list,
        cycle_failed: list,
        *,
        err_prefix: str,
    ) -> bool:
        """Run one task through process_task, report it, and account its tokens.

        Shared by the resumed and DOABLE paths so they can never drift. Returns
        True once the cumulative token budget is exhausted (caller stops
        scheduling new work for the rest of the cycle).
        """
        from nocturne.orchestrator import process_task

        cycle_summary["doable"] += 1
        try:
            result_task = await asyncio.to_thread(
                process_task, task, self.cfg, self.store
            )
        except Exception as e:
            logger.error("process_task raised for %s %s: %s", err_prefix, task.id, e)
            cycle_summary["errors"].append(f"{err_prefix}:{task.id}:{e}")
            return False

        if result_task.status == "done":
            cycle_summary["processed_done"] += 1
            cycle_done.append(result_task)
        else:
            cycle_summary["processed_failed"] += 1
            cycle_failed.append(result_task)

        if self.bot is not None:
            from nocturne.reporter import post_task_report
            try:
                await post_task_report(result_task, self.bot)
            except Exception as e:
                logger.warning("Discord task report failed (non-blocking): %s", e)

        # Real measured usage from the opencode event stream (0 if unavailable),
        # replacing the old flat per-task heuristic.
        self._tokens_used += result_task.token_usage
        return self._is_budget_exhausted()

    async def run_one_cycle(self) -> dict[str, Any]:
        """Execute ONE full poll cycle across all repos. Returns a summary dict.

        Used by ``nocturne daemon --once`` (CLI testing) and by the main poll loop.
        """
        from nocturne.orchestrator import _dispatch_triaged, partition_eligible
        from nocturne.sources.github_issues import fetch_eligible
        from nocturne.triage import triage_batch

        cycle_summary: dict[str, Any] = {
            "fetched": 0,
            "doable": 0,
            "skip": 0,
            "need_input": 0,
            "processed_done": 0,
            "processed_failed": 0,
            "errors": [],
        }
        cycle_done: list = []
        cycle_failed: list = []

        # Check pause flag at top.
        if await self._check_paused_flag():
            cycle_summary["paused"] = True
            return cycle_summary

        if self._is_quiet_hour():
            cycle_summary["quiet_hour"] = True
            return cycle_summary

        if self._is_budget_exhausted():
            cycle_summary["budget_exhausted"] = True
            return cycle_summary

        cycle_started_at = datetime.now(timezone.utc)
        for repo_cfg in self.cfg.repos:
            try:
                issues = await asyncio.to_thread(fetch_eligible, repo_cfg)
            except Exception as e:
                logger.warning("fetch_eligible failed for %s: %s", repo_cfg.slug, e)
                cycle_summary["errors"].append(f"fetch:{repo_cfg.slug}:{e}")
                continue
            cycle_summary["fetched"] += len(issues)

            to_triage, resumed = partition_eligible(issues, self.store)

            for task in resumed:
                if await self._process_and_report(
                    task, cycle_summary, cycle_done, cycle_failed, err_prefix="resumed"
                ):
                    cycle_summary["budget_exhausted"] = True
                    break

            if cycle_summary.get("budget_exhausted") or not to_triage:
                if cycle_summary.get("budget_exhausted"):
                    break
                continue

            try:
                triaged = await asyncio.to_thread(triage_batch, to_triage, self.cfg)
            except Exception as e:
                logger.warning("triage_batch failed for %s: %s", repo_cfg.slug, e)
                cycle_summary["errors"].append(f"triage:{repo_cfg.slug}:{e}")
                continue

            for task, tr in triaged:
                if tr.outcome == "DOABLE":
                    if await self._process_and_report(
                        task, cycle_summary, cycle_done, cycle_failed, err_prefix="process"
                    ):
                        logger.warning(
                            "token budget exhausted mid-cycle; halting new scheduling"
                        )
                        cycle_summary["budget_exhausted"] = True
                        break
                elif tr.outcome == "NEED_INPUT":
                    # Park the issue and post the clarifying question. triage_batch
                    # does NOT do this, so without dispatch the daemon would
                    # silently re-triage NEED_INPUT issues every cycle and never
                    # ask the user. _dispatch_triaged parks + comments (it never
                    # calls process_task for NEED_INPUT).
                    cycle_summary["need_input"] += 1
                    try:
                        await asyncio.to_thread(
                            _dispatch_triaged, task, tr, self.cfg, self.store
                        )
                    except Exception as e:
                        logger.warning("park (need_input) failed for %s: %s", task.id, e)
                        cycle_summary["errors"].append(f"park:{task.id}:{e}")
                elif tr.outcome == "SKIP":
                    # triage_batch already posted the idempotent skip comment.
                    cycle_summary["skip"] += 1

            if cycle_summary.get("budget_exhausted"):
                break

        cycle_ended_at = datetime.now(timezone.utc)
        self._last_poll_at = cycle_ended_at

        if self.bot is not None and (cycle_done or cycle_failed or cycle_summary["errors"]):
            try:
                from nocturne.models import RunReport
                from nocturne.reporter import post_run_report
                report = RunReport(
                    started_at=cycle_started_at,
                    ended_at=cycle_ended_at,
                    done=cycle_done,
                    parked=[],
                    skipped=[],
                    errors=[str(e) for e in cycle_summary["errors"]],
                    summary="",
                    token_usage=self._tokens_used,
                )
                await post_run_report(report, self.bot)
            except Exception as e:
                logger.warning("Discord run report failed (non-blocking): %s", e)

        return cycle_summary

    async def _poll_loop(self) -> None:
        """Main poll loop. Polls SQLite pause flag, then runs cycles."""
        pause_tick_sec = min(1.0, max(0.1, float(self.cfg.daemon.poll_interval_sec)))

        while not self._stop.is_set():
            # Check pause flag (cross-process) at TOP of each iteration.
            is_paused = await self._check_paused_flag()
            if is_paused:
                # CRITICAL: continue iterating with short sleep (NOT
                # wait_for(_paused)) so we re-read the flag and can observe
                # resume from another process.
                await asyncio.sleep(pause_tick_sec)
                continue

            if self._is_quiet_hour():
                # Quiet-hour skip - also does NOT update _last_poll_at
                # (intentional staleness signal).
                await asyncio.sleep(60)
                continue

            if self._is_budget_exhausted():
                logger.warning("token budget exceeded; halting new scheduling")
                await asyncio.sleep(60)
                continue

            # Normal poll cycle.
            try:
                cycle_task = asyncio.create_task(
                    self.run_one_cycle(), name="poll_cycle"
                )
                while not cycle_task.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(cycle_task), timeout=pause_tick_sec
                        )
                    except asyncio.TimeoutError:
                        if not await self._check_paused_flag():
                            self._last_poll_at = datetime.now(timezone.utc)
                _ = cycle_task.result()
            except Exception as e:
                logger.error("poll cycle raised: %s", e)
                # Continue iterating despite errors (graceful degradation).

            # Sleep until next poll, respecting stop.
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=float(self.cfg.daemon.poll_interval_sec),
                )
                # _stop was set during sleep.
                return
            except asyncio.TimeoutError:
                pass  # normal: timeout reached, next iteration

    async def _install_signal_handlers(self) -> None:
        """Install SIGTERM/SIGINT handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: self._stop.set())
            except NotImplementedError:
                # Windows or unusual loop - skip (tests don't exercise this path).
                pass

    async def run(self) -> None:
        """Main entry: install signal handlers, gather poll_loop + bot."""
        await self._install_signal_handlers()

        healthcheck = None
        if self.cfg.healthcheck.enabled:
            try:
                from nocturne.healthcheck import Healthcheck
                healthcheck = Healthcheck(self.cfg, self.store, daemon=self)
                await healthcheck.start()
            except Exception as e:
                logger.warning("could not start healthcheck (continuing without): %s", e)
                healthcheck = None

        tasks = [asyncio.create_task(self._poll_loop(), name="poll_loop")]
        if self.bot is not None:
            tasks.append(asyncio.create_task(self.bot.start(), name="discord_bot"))

        logger.info(
            "daemon started (poll_interval=%ss, repos=%s, bot=%s)",
            self.cfg.daemon.poll_interval_sec,
            len(self.cfg.repos),
            "yes" if self.bot else "no",
        )

        try:
            # Wait for either _stop or any task to finish; a bot crash surfaces
            # here too.
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            del done
            for t in pending:
                t.cancel()
            # Drain cancelled tasks.
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            if healthcheck is not None:
                try:
                    await healthcheck.stop()
                except Exception as e:
                    logger.warning("healthcheck.stop raised: %s", e)
            if self.bot is not None:
                try:
                    await self.bot.close()
                except Exception as e:
                    logger.warning("bot.close raised: %s", e)
            try:
                self.store.close()
            except Exception:
                pass
            logger.info("daemon shutdown complete")


def run_daemon(cfg: Config, store: Store) -> None:
    """Build bot (if cfg.discord.enabled), wire resume + daemon refs, run via asyncio.run."""
    daemon = Daemon(cfg, store, bot=None)
    if cfg.discord.enabled:
        try:
            from nocturne.askflow import resume_with_answer
            from nocturne.discord_bot import make_bot

            async def _resume_cb(task_id: str, answer: str) -> None:
                await asyncio.to_thread(resume_with_answer, task_id, answer, cfg, store)

            daemon.bot = make_bot(cfg, store, _resume_cb, daemon=daemon)
        except Exception as e:
            logger.warning("could not create Discord bot (continuing without): %s", e)

    asyncio.run(daemon.run())
