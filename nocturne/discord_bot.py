"""Discord bot — single-process asyncio integration with daemon.

Sends NEED_INPUT messages, routes reply-based answers back to askflow.resume_with_answer.
Persists message_id ↔ task_id mapping in SQLite to survive bot restart.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional, cast

from nocturne._logging import get_logger
from nocturne.config import Config
from nocturne.store import Store

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

    def __init__(self, cfg: Config, store: Store, resume_callback: ResumeCallback):
        if discord is None:
            raise RuntimeError("discord.py not installed")
        self.cfg = cfg
        self.store = store
        self.resume_callback = resume_callback
        intents = discord.Intents.default()
        intents.message_content = True  # required to read reply text
        self._client = discord.Client(intents=intents)
        # In-memory cache (msg_id → task_id); also persisted in SQLite for restart recovery
        self._task_messages: dict[int, str] = {}
        # Wire event handlers
        self._client.event(self.on_ready)
        self._client.event(self.on_message)

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


def make_bot(cfg: Config, store: Store, resume_callback: ResumeCallback) -> NocturneBot:
    """Factory for NocturneBot. Raises if discord.py missing or discord disabled."""
    if not cfg.discord.enabled:
        raise RuntimeError("Discord is disabled in config (cfg.discord.enabled=False)")
    return NocturneBot(cfg, store, resume_callback)
