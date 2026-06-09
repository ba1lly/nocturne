"""Discord bot - single-process asyncio integration with daemon.

Sends NEED_INPUT messages, routes reply-based answers back to askflow.resume_with_answer.
Persists message_id ↔ task_id mapping in SQLite to survive bot restart.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, cast

from nocturne._logging import get_logger
from nocturne.config import Config
from nocturne.store import Store

if TYPE_CHECKING:
    from nocturne.daemon import Daemon

logger = get_logger("nocturne.discord_bot")

# Type alias for the resume callback shape
ResumeCallback = Callable[[str, str], Awaitable[None]]

# Lazy import discord at the module level so test patches can target it
try:
    import discord
except ImportError:
    discord = None  # type: ignore[assignment]


class NocturneBot:
    """Async Discord client wrapping discord.Client.

    Single-asyncio-loop architecture: bot runs in the same event loop as daemon.
    Reply-based correlation: when a user replies to a NEED_INPUT message, the bot
    looks up the task_id (in-memory cache first, falling back to SQLite for restart
    resilience) and invokes the resume_callback.
    """

    def __init__(
        self,
        cfg: Config,
        store: Store,
        resume_callback: ResumeCallback,
        daemon: Optional["Daemon"] = None,
    ):
        if discord is None:
            raise RuntimeError("discord.py not installed")
        self.cfg = cfg
        self.store = store
        self.resume_callback = resume_callback
        self.daemon = daemon
        intents = discord.Intents.default()
        intents.message_content = True  # required to read reply text
        self._client = discord.Client(intents=intents)
        # In-memory cache (msg_id → task_id); also persisted in SQLite for restart recovery
        self._task_messages: dict[int, str] = {}
        # Wire event handlers
        self._client.event(self.on_ready)
        self._client.event(self.on_message)
        self._tree: Any = (
            discord.app_commands.CommandTree(self._client)
            if discord is not None
            else None
        )
        self._register_commands()

    def _is_authorized(self, user_id: int) -> bool:
        """Check that a Discord user matches the authorized mention_user_id."""
        return user_id == self.cfg.discord.mention_user_id

    def _register_commands(self) -> None:
        """Register all 7 slash commands on the bot's CommandTree.

        For testability, each command's callback is exposed as
        ``self._cmd_<name>`` so unit tests can invoke it directly,
        bypassing the discord.app_commands decorator machinery.
        """
        if self._tree is None:
            return

        cfg = self.cfg
        store = self.store
        bot_self = self

        async def _unauthorized(interaction: Any) -> None:
            await interaction.response.send_message(
                "not authorized", ephemeral=True
            )

        @self._tree.command(name="status", description="Show Nocturne queue status")
        async def cmd_status(interaction: Any) -> None:
            if not bot_self._is_authorized(interaction.user.id):
                await _unauthorized(interaction)
                return
            counts: dict[str, int] = {}
            for s in (
                "selected",
                "running",
                "done",
                "parked",
                "skipped",
                "failed",
                "aborted",
            ):
                try:
                    counts[s] = len(store.list_by_status(s))  # type: ignore[arg-type]
                except Exception:
                    counts[s] = 0
            paused = bot_self.daemon.is_paused() if bot_self.daemon else False
            tokens = bot_self.daemon.tokens_used if bot_self.daemon else 0
            text = (
                f"queue: {counts['selected']} | running: {counts['running']} | "
                f"parked: {counts['parked']} | PRs: {counts['done']} | "
                f"paused: {paused} | tokens: {tokens}"
            )
            await interaction.response.send_message(text, ephemeral=True)

        @self._tree.command(
            name="answer", description="Provide an answer to a parked task"
        )
        async def cmd_answer(
            interaction: Any, task_id: str, text: str
        ) -> None:
            if not bot_self._is_authorized(interaction.user.id):
                await _unauthorized(interaction)
                return
            try:
                await bot_self.resume_callback(task_id, text)
                await interaction.response.send_message(
                    f"resumed {task_id}", ephemeral=True
                )
            except Exception as e:
                await interaction.response.send_message(
                    f"failed: {e}", ephemeral=True
                )

        @self._tree.command(
            name="queue", description="List queued and parked task IDs"
        )
        async def cmd_queue(interaction: Any) -> None:
            if not bot_self._is_authorized(interaction.user.id):
                await _unauthorized(interaction)
                return
            selected = store.list_by_status("selected")  # type: ignore[arg-type]
            parked = store.list_by_status("parked")  # type: ignore[arg-type]
            if not selected and not parked:
                await interaction.response.send_message(
                    "(queue empty)", ephemeral=True
                )
                return
            lines = ["**Selected:**"] + [f"- {t.id}" for t in selected[:10]]
            lines += ["**Parked:**"] + [
                f"- {t.id}: {(t.question or '')[:60]}" for t in parked[:10]
            ]
            await interaction.response.send_message(
                "\n".join(lines)[:2000], ephemeral=True
            )

        @self._tree.command(name="skip", description="Skip a task (mark skipped)")
        async def cmd_skip(interaction: Any, task_id: str) -> None:
            if not bot_self._is_authorized(interaction.user.id):
                await _unauthorized(interaction)
                return
            task = store.get_task(task_id)
            if task is None:
                await interaction.response.send_message(
                    f"task not found: {task_id}", ephemeral=True
                )
                return
            store.update_status(task_id, "skipped")
            try:
                from nocturne.sources.github_issues import comment
                comment(
                    task.repo_slug,
                    task.issue_number,
                    f"Manually skipped via Discord by <@{interaction.user.id}>.",
                )
            except Exception as e:
                logger.warning("could not post skip comment: %s", e)
            await interaction.response.send_message(
                f"skipped {task_id}", ephemeral=True
            )

        @self._tree.command(name="pause", description="Pause the daemon")
        async def cmd_pause(interaction: Any) -> None:
            if not bot_self._is_authorized(interaction.user.id):
                await _unauthorized(interaction)
                return
            if bot_self.daemon is None:
                await interaction.response.send_message(
                    "(no daemon attached)", ephemeral=True
                )
                return
            bot_self.daemon.pause()
            await interaction.response.send_message("paused", ephemeral=True)

        @self._tree.command(name="resume", description="Resume the daemon")
        async def cmd_resume(interaction: Any) -> None:
            if not bot_self._is_authorized(interaction.user.id):
                await _unauthorized(interaction)
                return
            if bot_self.daemon is None:
                await interaction.response.send_message(
                    "(no daemon attached)", ephemeral=True
                )
                return
            bot_self.daemon.resume()
            await interaction.response.send_message("resumed", ephemeral=True)

        @self._tree.command(
            name="run", description="Trigger an immediate batch run on a repo"
        )
        async def cmd_run(interaction: Any, repo: str) -> None:
            if not bot_self._is_authorized(interaction.user.id):
                await _unauthorized(interaction)
                return
            repo_cfg = next((r for r in cfg.repos if r.slug == repo), None)
            if repo_cfg is None:
                await interaction.response.send_message(
                    f"repo not in allowlist: {repo}", ephemeral=True
                )
                return
            from nocturne.orchestrator import run_batch

            async def _do_run() -> None:
                try:
                    await asyncio.to_thread(run_batch, repo_cfg, cfg, store)
                except Exception as e:
                    logger.error("triggered run failed: %s", e)

            _ = asyncio.create_task(_do_run())
            await interaction.response.send_message(
                f"started run for {repo}", ephemeral=True
            )

        bot_self._cmd_status = cmd_status
        bot_self._cmd_answer = cmd_answer
        bot_self._cmd_queue = cmd_queue
        bot_self._cmd_skip = cmd_skip
        bot_self._cmd_pause = cmd_pause
        bot_self._cmd_resume = cmd_resume
        bot_self._cmd_run = cmd_run

    @property
    def user(self):  # type: ignore[no-untyped-def]
        return self._client.user

    async def on_ready(self) -> None:
        logger.info("nocturne bot connected as %s", self._client.user)

    async def on_message(self, message) -> None:  # type: ignore[no-untyped-def]
        # Ignore self
        if self._client.user is not None and message.author.id == self._client.user.id:
            return
        # Only care about replies
        ref = getattr(message, "reference", None)
        if ref is None or getattr(ref, "message_id", None) is None:
            return
        ref_msg_id = ref.message_id
        # Look up task_id from in-memory cache first, then SQLite
        task_id = self._task_messages.get(ref_msg_id)
        if task_id is None:
            task_id = await asyncio.to_thread(
                self.store.get_discord_message_task, ref_msg_id
            )
        if task_id is None:
            logger.info("unmatched reply (msg_id=%s), ignoring", ref_msg_id)
            return

        # Invoke resume_callback in a separate task so we don't block the bot event loop
        async def _run_resume(t_id: str, ans: str) -> None:
            try:
                await self.resume_callback(t_id, ans)
                try:
                    await message.add_reaction("✅")
                except Exception:
                    pass  # reaction is decorative; don't fail on it
            except Exception as e:
                logger.error("resume_callback raised for %s: %s", t_id, e)
                try:
                    await message.add_reaction("❌")
                except Exception:
                    pass

        _ = asyncio.create_task(_run_resume(task_id, message.content))

    async def send_need_input(
        self,
        task_id: str,
        issue_number: int,
        question: str,
    ) -> int:
        """Send a NEED_INPUT prompt to the configured channel. Returns Discord message ID."""
        assert discord is not None  # narrowed by __init__ guard
        channel = self._client.get_channel(self.cfg.discord.channel_id)
        if channel is None:
            raise RuntimeError(
                f"channel {self.cfg.discord.channel_id} not found (bot not connected?)"
            )
        embed = discord.Embed(
            title=f"[Nocturne] Question for issue #{issue_number}",
            description=question[:4000],  # Discord embed description limit
            color=0xFFC107,
        )
        embed.set_footer(text=f"task_id: {task_id}")
        mention = f"<@{self.cfg.discord.mention_user_id}>"
        msg = await cast(Any, channel).send(mention, embed=embed)
        # Persist both in-memory and SQLite for restart resilience
        self._task_messages[msg.id] = task_id
        await asyncio.to_thread(self.store.add_discord_message, msg.id, task_id)
        logger.info("sent NEED_INPUT for %s (msg_id=%s)", task_id, msg.id)
        return msg.id

    async def send_status_msg(self, text: str) -> Optional[int]:
        """Send a plain status message (no NEED_INPUT correlation). Used by Task 35 reporter."""
        channel = self._client.get_channel(self.cfg.discord.channel_id)
        if channel is None:
            logger.warning(
                "status channel %s not found; dropping message",
                self.cfg.discord.channel_id,
            )
            return None
        msg = await cast(Any, channel).send(text[:2000])  # Discord message limit
        return msg.id

    async def start(self) -> None:
        """Connect and run the bot until stopped. Reads token from env."""
        import os
        token = os.environ.get(self.cfg.discord.bot_token_env, "")
        if not token:
            raise RuntimeError(f"env var {self.cfg.discord.bot_token_env} not set")
        await self._client.start(token)

    async def close(self) -> None:
        await self._client.close()


def make_bot(
    cfg: Config,
    store: Store,
    resume_callback: ResumeCallback,
    daemon: Optional["Daemon"] = None,
) -> NocturneBot:
    """Factory for NocturneBot. Raises if discord.py missing or discord disabled."""
    if not cfg.discord.enabled:
        raise RuntimeError("Discord is disabled in config (cfg.discord.enabled=False)")
    return NocturneBot(cfg, store, resume_callback, daemon=daemon)
