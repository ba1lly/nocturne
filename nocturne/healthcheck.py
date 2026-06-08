"""Healthcheck HTTP endpoint — aiohttp /health and /metrics on loopback.

Used for external monitoring (uptimerobot, custom Discord pulse, systemd watchdog).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from nocturne._logging import get_logger
from nocturne.config import Config
from nocturne.store import Store

if TYPE_CHECKING:
    from nocturne.daemon import Daemon

logger = get_logger("nocturne.healthcheck")


class Healthcheck:
    """aiohttp app exposing /health and /metrics endpoints."""

    cfg: Config
    store: Store
    daemon: Optional["Daemon"]
    _started_at: float
    _runner: Optional[object]
    _site: Optional[object]

    def __init__(
        self,
        cfg: Config,
        store: Store,
        daemon: Optional["Daemon"] = None,
    ):
        self.cfg = cfg
        self.store = store
        self.daemon = daemon
        self._started_at = time.time()
        self._runner = None
        self._site = None

    def _staleness_threshold_s(self) -> float:
        return float(self.cfg.healthcheck.staleness_factor) * float(self.cfg.daemon.poll_interval_sec)

    def _is_stale(self) -> tuple[bool, float]:
        """Return (is_stale, age_seconds). age=infinity if _last_poll_at is None."""
        if self.daemon is None:
            return (False, 0.0)
        lpa = self.daemon.last_poll_at
        if lpa is None:
            return (True, float("inf"))
        age = (datetime.now(timezone.utc) - lpa).total_seconds()
        return (age > self._staleness_threshold_s(), age)

    async def _sqlite_ok(self) -> bool:
        try:
            await asyncio.wait_for(asyncio.to_thread(self._sqlite_ping), timeout=1.0)
            return True
        except Exception:
            return False

    def _sqlite_ping(self) -> None:
        """Sync ping — runs in to_thread."""
        self.store._conn.execute("SELECT 1").fetchone()

    async def _queue_depth(self) -> int:
        try:
            return await asyncio.to_thread(lambda: len(self.store.list_by_status("selected")))
        except Exception:
            return 0

    async def _parked_count(self) -> int:
        try:
            return await asyncio.to_thread(lambda: len(self.store.list_by_status("parked")))
        except Exception:
            return 0

    async def health_handler(self, request: object) -> object:  # type: ignore[no-untyped-def]
        from aiohttp import web
        is_stale, age = self._is_stale()
        sqlite_ok = await self._sqlite_ok()
        daemon_alive = self.daemon is not None and not self.daemon._stop.is_set()
        queue_depth = await self._queue_depth()
        parked_count = await self._parked_count()
        uptime_s = int(time.time() - self._started_at)
        healthy = daemon_alive and sqlite_ok and not is_stale
        body = {
            "status": "healthy" if healthy else "stale",
            "daemon_alive": daemon_alive,
            "sqlite_ok": sqlite_ok,
            "last_poll_age_s": age if age != float("inf") else None,
            "staleness_threshold_s": self._staleness_threshold_s(),
            "queue_depth": queue_depth,
            "parked_count": parked_count,
            "uptime_s": uptime_s,
        }
        status_code = 200 if healthy else 503
        return web.json_response(body, status=status_code)

    async def metrics_handler(self, request: object) -> object:  # type: ignore[no-untyped-def]
        from aiohttp import web
        counts: dict[str, int] = {}
        for s in ("done", "failed", "parked", "skipped", "aborted", "running", "selected"):
            try:
                def _count_status(status: str) -> int:
                    return len(self.store.list_by_status(status))  # type: ignore[arg-type]
                counts[s] = await asyncio.to_thread(_count_status, s)
            except Exception:
                counts[s] = 0
        # Prometheus exposition format
        lines = [
            "# HELP nocturne_tasks Total tasks by status",
            "# TYPE nocturne_tasks gauge",
        ]
        for status, n in counts.items():
            lines.append(f'nocturne_tasks{{status="{status}"}} {n}')
        text = "\n".join(lines) + "\n"
        return web.Response(text=text, content_type="text/plain")

    async def start(self) -> None:
        from aiohttp import web
        app = web.Application()
        app.router.add_get("/health", self.health_handler)
        app.router.add_get("/metrics", self.metrics_handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            host=self.cfg.healthcheck.bind_host,
            port=self.cfg.healthcheck.bind_port,
        )
        await self._site.start()
        logger.info(
            "healthcheck listening on http://%s:%s",
            self.cfg.healthcheck.bind_host,
            self.cfg.healthcheck.bind_port,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            logger.info("healthcheck stopped")
