"""Tests for nocturne.discord_bot — bot logic only (no live Discord)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from nocturne.store import Store


# -- fixtures + fakes --


@pytest.fixture
def fake_cfg():
    """Programmatically-built Config covering discord + everything load_config needs."""
    from nocturne.config import (
        Config,
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
    return Config(
        github=GitHubConfig(owner="ba1lly"),
        sandbox=SandboxConfig(),
        providers={
            "alibaba-coding-plan": ProviderConfig(
                base_url="https://x", api_key_env="DASHSCOPE_API_KEY",
            )
        },
        models=ModelsConfig(
            reasoning="alibaba-coding-plan/qwen3.6-plus",
            report="alibaba-coding-plan/qwen3.6-plus",
            coding="alibaba-coding-plan/qwen3-coder-plus",
        ),
        opencode=OpenCodeConfig(),
        repos=[
            RepoConfig(
                slug="ba1lly/sandbox",
                checkout_path="/home/bailly/projects/nocturne",  # has .git/
                label="agent",
                base="main",
                verify_cmd="pytest -q",
                require_new_test=False,
            )
        ],
        guardrails=GuardrailsConfig(),
        discord=DiscordConfig(channel_id=1234, mention_user_id=5678),
        daemon=DaemonConfig(),
        review=ReviewConfig(),
        healthcheck=HealthcheckConfig(),
        persona=PersonaConfig(enabled=False, soul_path=None),
    )


@pytest.fixture
def fake_discord(monkeypatch):
    """Stub the discord module so NocturneBot can be constructed without live Discord."""
    import nocturne.discord_bot as bot_module
    fake = MagicMock()
    # Intents
    fake.Intents.default.return_value = MagicMock()
    # Client class
    client_instance = MagicMock()
    client_instance.user = MagicMock(id=999)
    client_instance.get_channel = MagicMock()
    # event registration — return the registered fn unchanged
    client_instance.event = MagicMock(side_effect=lambda fn: fn)
    fake.Client.return_value = client_instance
    # Embed class
    embed_instance = MagicMock()
    embed_instance.set_footer = MagicMock(return_value=embed_instance)
    fake.Embed.return_value = embed_instance
    monkeypatch.setattr(bot_module, "discord", fake)
    return fake, client_instance


# -- Store extension tests --


def test_store_add_and_get_discord_message(inmem_store):
    inmem_store.add_discord_message(12345, "ba1lly/sandbox#42")
    assert inmem_store.get_discord_message_task(12345) == "ba1lly/sandbox#42"


def test_store_get_discord_message_returns_none_when_missing(inmem_store):
    assert inmem_store.get_discord_message_task(99999) is None


def test_store_add_discord_message_idempotent_replace(inmem_store):
    inmem_store.add_discord_message(12345, "a/b#1")
    inmem_store.add_discord_message(12345, "c/d#2")  # same msg_id → replace
    assert inmem_store.get_discord_message_task(12345) == "c/d#2"


# -- NocturneBot construction tests --


def test_make_bot_disabled_raises(inmem_store, fake_cfg, fake_discord):
    from nocturne.discord_bot import make_bot
    fake_cfg.discord.enabled = False

    async def cb(t, a):
        return None

    with pytest.raises(RuntimeError, match="disabled"):
        make_bot(fake_cfg, inmem_store, cb)


def test_make_bot_returns_instance(inmem_store, fake_cfg, fake_discord):
    from nocturne.discord_bot import make_bot

    async def cb(t, a):
        return None

    bot = make_bot(fake_cfg, inmem_store, cb)
    assert bot is not None


# -- send_need_input tests --


@pytest.mark.asyncio
async def test_send_need_input_persists_mapping(inmem_store, fake_cfg, fake_discord):
    from nocturne.discord_bot import NocturneBot
    _fake, client = fake_discord
    fake_channel = MagicMock()
    sent_msg = MagicMock(id=11111)
    fake_channel.send = AsyncMock(return_value=sent_msg)
    client.get_channel.return_value = fake_channel

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    msg_id = await bot.send_need_input("ba1lly/sandbox#42", 42, "why?")
    assert msg_id == 11111
    # In-memory cache
    assert bot._task_messages[11111] == "ba1lly/sandbox#42"
    # SQLite persistence
    assert inmem_store.get_discord_message_task(11111) == "ba1lly/sandbox#42"


@pytest.mark.asyncio
async def test_send_need_input_channel_missing_raises(inmem_store, fake_cfg, fake_discord):
    from nocturne.discord_bot import NocturneBot
    _fake, client = fake_discord
    client.get_channel.return_value = None

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    with pytest.raises(RuntimeError, match="channel"):
        await bot.send_need_input("x", 1, "q")


# -- on_message reply routing tests --


@pytest.mark.asyncio
async def test_reply_invokes_resume(inmem_store, fake_cfg, fake_discord):
    """Reply to a known NEED_INPUT message → resume_callback called."""
    from nocturne.discord_bot import NocturneBot
    _fake, client = fake_discord
    inmem_store.add_discord_message(11111, "ba1lly/sandbox#42")
    callback_called: list[tuple[str, str]] = []

    async def cb(t, a):
        callback_called.append((t, a))

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    msg = MagicMock()
    msg.author.id = 7777  # not the bot's user (999)
    msg.reference.message_id = 11111
    msg.content = "my answer"
    msg.add_reaction = AsyncMock()
    await bot.on_message(msg)
    # Give the scheduled task a chance to run
    await asyncio.sleep(0.05)
    assert callback_called == [("ba1lly/sandbox#42", "my answer")]
    msg.add_reaction.assert_awaited_with("✅")


@pytest.mark.asyncio
async def test_garbage_reply_ignored(inmem_store, fake_cfg, fake_discord):
    """Reply with no task_id correlation → no callback."""
    from nocturne.discord_bot import NocturneBot
    _fake, client = fake_discord
    callback_called: list[tuple[str, str]] = []

    async def cb(t, a):
        callback_called.append((t, a))

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    msg = MagicMock()
    msg.author.id = 7777
    msg.reference.message_id = 99999  # not in the store
    msg.content = "garbage"
    msg.add_reaction = AsyncMock()
    await bot.on_message(msg)
    await asyncio.sleep(0.05)
    assert callback_called == []
    msg.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignore_self_messages(inmem_store, fake_cfg, fake_discord):
    """Bot's own messages are ignored even if they're replies."""
    from nocturne.discord_bot import NocturneBot
    _fake, client = fake_discord
    callback_called: list[tuple[str, str]] = []

    async def cb(t, a):
        callback_called.append((t, a))

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    msg = MagicMock()
    msg.author.id = 999  # bot's own user id
    msg.reference.message_id = 11111
    msg.content = "self"
    await bot.on_message(msg)
    await asyncio.sleep(0.05)
    assert callback_called == []


@pytest.mark.asyncio
async def test_non_reply_messages_ignored(inmem_store, fake_cfg, fake_discord):
    """Plain messages (no reply reference) are ignored."""
    from nocturne.discord_bot import NocturneBot
    _fake, client = fake_discord
    callback_called: list[tuple[str, str]] = []

    async def cb(t, a):
        callback_called.append((t, a))

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    msg = MagicMock()
    msg.author.id = 7777
    msg.reference = None
    msg.content = "hello"
    await bot.on_message(msg)
    await asyncio.sleep(0.05)
    assert callback_called == []


@pytest.mark.asyncio
async def test_mapping_persists_across_restart(inmem_store, fake_cfg, fake_discord):
    """Bot restart: in-memory cache empty, SQLite lookup recovers task_id."""
    from nocturne.discord_bot import NocturneBot
    _fake, client = fake_discord
    # Persist a mapping
    inmem_store.add_discord_message(22222, "ba1lly/sandbox#7")
    callback_called: list[tuple[str, str]] = []

    async def cb(t, a):
        callback_called.append((t, a))

    # Fresh bot — in-memory cache empty
    bot = NocturneBot(fake_cfg, inmem_store, cb)
    assert bot._task_messages.get(22222) is None
    msg = MagicMock()
    msg.author.id = 7777
    msg.reference.message_id = 22222
    msg.content = "after-restart answer"
    msg.add_reaction = AsyncMock()
    await bot.on_message(msg)
    await asyncio.sleep(0.05)
    # SQLite lookup recovered the mapping
    assert callback_called == [("ba1lly/sandbox#7", "after-restart answer")]


@pytest.mark.asyncio
async def test_callback_failure_reacts_with_x(inmem_store, fake_cfg, fake_discord):
    """resume_callback raises → ❌ reaction added; no crash."""
    from nocturne.discord_bot import NocturneBot
    _fake, client = fake_discord
    inmem_store.add_discord_message(33333, "x#1")

    async def cb(t, a):
        raise RuntimeError("boom")

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    msg = MagicMock()
    msg.author.id = 7777
    msg.reference.message_id = 33333
    msg.content = "answer"
    msg.add_reaction = AsyncMock()
    await bot.on_message(msg)
    await asyncio.sleep(0.05)
    msg.add_reaction.assert_awaited_with("❌")


# -- send_status_msg tests --


@pytest.mark.asyncio
async def test_send_status_msg_returns_id(inmem_store, fake_cfg, fake_discord):
    from nocturne.discord_bot import NocturneBot
    _fake, client = fake_discord
    fake_channel = MagicMock()
    sent = MagicMock(id=44444)
    fake_channel.send = AsyncMock(return_value=sent)
    client.get_channel.return_value = fake_channel

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    mid = await bot.send_status_msg("hello world")
    assert mid == 44444


@pytest.mark.asyncio
async def test_send_status_msg_no_channel_returns_none(inmem_store, fake_cfg, fake_discord):
    from nocturne.discord_bot import NocturneBot
    _fake, client = fake_discord
    client.get_channel.return_value = None

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    mid = await bot.send_status_msg("hello")
    assert mid is None


# Touch the imports kept for compatibility with the spec scaffolding.
_ = (datetime, timezone, Store)
