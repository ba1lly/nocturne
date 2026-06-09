"""Tests for nocturne.discord_bot - bot logic only (no live Discord)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
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
                checkout_path=str(Path(__file__).resolve().parents[1]),  # has .git/
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


def _build_fake_discord(monkeypatch):
    import nocturne.discord_bot as bot_module
    fake = MagicMock()
    fake.Intents.default.return_value = MagicMock()
    client_instance = MagicMock()
    client_instance.user = MagicMock(id=999)
    client_instance.get_channel = MagicMock()
    client_instance.event = MagicMock(side_effect=lambda fn: fn)
    fake.Client.return_value = client_instance
    embed_instance = MagicMock()
    embed_instance.set_footer = MagicMock(return_value=embed_instance)
    fake.Embed.return_value = embed_instance
    tree = MagicMock()
    tree.command = MagicMock(side_effect=lambda *a, **k: lambda fn: fn)
    fake.app_commands = MagicMock()
    fake.app_commands.CommandTree.return_value = tree
    monkeypatch.setattr(bot_module, "discord", fake)
    return fake, client_instance, tree


@pytest.fixture
def fake_discord(monkeypatch):
    """Stub the discord module so NocturneBot can be constructed without live Discord."""
    fake, client_instance, _tree = _build_fake_discord(monkeypatch)
    return fake, client_instance


@pytest.fixture
def fake_discord_with_commands(monkeypatch):
    """Stub discord including app_commands.CommandTree decorator pass-through."""
    return _build_fake_discord(monkeypatch)


def _make_interaction(user_id: int):
    interaction = MagicMock()
    interaction.user = MagicMock(id=user_id)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


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

    # Fresh bot - in-memory cache empty
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


# -- Slash command tests --


def _seed_task(store, *, task_id="x/y#1", status="selected", question=None):
    from nocturne.models import Task
    t = Task(
        id=task_id,
        repo_slug=task_id.split("#")[0],
        checkout_path="/tmp/x",
        issue_number=int(task_id.split("#")[1]),
        title="t",
        body="b",
        base="main",
        verify_cmd="pytest",
        require_new_test=False,
        coding_model="x/y",
        branch="b",
        status=status,
        attempts=0,
        question=question,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    store.insert_task(t)
    return t


@pytest.mark.asyncio
async def test_cmd_status(inmem_store, fake_cfg, fake_discord_with_commands):
    from nocturne.discord_bot import NocturneBot
    fake_daemon = MagicMock()
    fake_daemon.is_paused.return_value = False
    fake_daemon.tokens_used = 12345

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb, daemon=fake_daemon)
    interaction = _make_interaction(fake_cfg.discord.mention_user_id)
    await bot._cmd_status(interaction)
    interaction.response.send_message.assert_awaited()
    args, _kwargs = interaction.response.send_message.call_args
    text = args[0]
    assert "queue:" in text and "parked:" in text and "PRs:" in text
    assert "12345" in text


@pytest.mark.asyncio
async def test_cmd_unauthorized(inmem_store, fake_cfg, fake_discord_with_commands):
    from nocturne.discord_bot import NocturneBot

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    interaction = _make_interaction(user_id=0)
    await bot._cmd_status(interaction)
    args, _kwargs = interaction.response.send_message.call_args
    assert "not authorized" in args[0]


@pytest.mark.asyncio
async def test_cmd_pause_calls_daemon_pause(inmem_store, fake_cfg, fake_discord_with_commands):
    from nocturne.discord_bot import NocturneBot
    fake_daemon = MagicMock()

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb, daemon=fake_daemon)
    interaction = _make_interaction(fake_cfg.discord.mention_user_id)
    await bot._cmd_pause(interaction)
    fake_daemon.pause.assert_called_once()
    args, _kwargs = interaction.response.send_message.call_args
    assert args[0] == "paused"


@pytest.mark.asyncio
async def test_cmd_resume_calls_daemon_resume(inmem_store, fake_cfg, fake_discord_with_commands):
    from nocturne.discord_bot import NocturneBot
    fake_daemon = MagicMock()

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb, daemon=fake_daemon)
    interaction = _make_interaction(fake_cfg.discord.mention_user_id)
    await bot._cmd_resume(interaction)
    fake_daemon.resume.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_pause_no_daemon_replies(inmem_store, fake_cfg, fake_discord_with_commands):
    from nocturne.discord_bot import NocturneBot

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb, daemon=None)
    interaction = _make_interaction(fake_cfg.discord.mention_user_id)
    await bot._cmd_pause(interaction)
    args, _kwargs = interaction.response.send_message.call_args
    assert "no daemon" in args[0].lower()


@pytest.mark.asyncio
async def test_cmd_queue_empty(inmem_store, fake_cfg, fake_discord_with_commands):
    from nocturne.discord_bot import NocturneBot

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    interaction = _make_interaction(fake_cfg.discord.mention_user_id)
    await bot._cmd_queue(interaction)
    args, _kwargs = interaction.response.send_message.call_args
    assert "empty" in args[0].lower()


@pytest.mark.asyncio
async def test_cmd_queue_populated(inmem_store, fake_cfg, fake_discord_with_commands):
    from nocturne.discord_bot import NocturneBot
    _seed_task(inmem_store, task_id="x/y#1", status="selected")
    _seed_task(inmem_store, task_id="a/b#2", status="parked", question="why?")

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    interaction = _make_interaction(fake_cfg.discord.mention_user_id)
    await bot._cmd_queue(interaction)
    args, _kwargs = interaction.response.send_message.call_args
    text = args[0]
    assert "x/y#1" in text and "a/b#2" in text and "why?" in text


@pytest.mark.asyncio
async def test_cmd_skip_marks_task_skipped(
    inmem_store, fake_cfg, fake_discord_with_commands, monkeypatch
):
    from nocturne.discord_bot import NocturneBot
    _seed_task(inmem_store, task_id="x/y#1", status="selected")
    import nocturne.sources.github_issues as gh
    monkeypatch.setattr(gh, "comment", lambda *a, **k: None)

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    interaction = _make_interaction(fake_cfg.discord.mention_user_id)
    await bot._cmd_skip(interaction, "x/y#1")
    updated = inmem_store.get_task("x/y#1")
    assert updated is not None and updated.status == "skipped"


@pytest.mark.asyncio
async def test_cmd_skip_unknown_task(inmem_store, fake_cfg, fake_discord_with_commands):
    from nocturne.discord_bot import NocturneBot

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    interaction = _make_interaction(fake_cfg.discord.mention_user_id)
    await bot._cmd_skip(interaction, "nope/nope#42")
    args, _kwargs = interaction.response.send_message.call_args
    assert "not found" in args[0]


@pytest.mark.asyncio
async def test_cmd_answer_invokes_resume_callback(
    inmem_store, fake_cfg, fake_discord_with_commands
):
    from nocturne.discord_bot import NocturneBot
    captured: list[tuple[str, str]] = []

    async def cb(task_id, answer):
        captured.append((task_id, answer))

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    interaction = _make_interaction(fake_cfg.discord.mention_user_id)
    await bot._cmd_answer(interaction, "x/y#1", "my answer")
    assert captured == [("x/y#1", "my answer")]


@pytest.mark.asyncio
async def test_cmd_run_unallowed_repo(inmem_store, fake_cfg, fake_discord_with_commands):
    from nocturne.discord_bot import NocturneBot

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    interaction = _make_interaction(fake_cfg.discord.mention_user_id)
    await bot._cmd_run(interaction, "evil/repo")
    args, _kwargs = interaction.response.send_message.call_args
    assert "not in allowlist" in args[0]


@pytest.mark.asyncio
async def test_cmd_run_allowed_repo_schedules(
    inmem_store, fake_cfg, fake_discord_with_commands, monkeypatch
):
    import nocturne.orchestrator as orch
    from nocturne.discord_bot import NocturneBot
    called = {"n": 0}

    def fake_run_batch(repo_cfg, cfg, store, *, dry_run=False):
        called["n"] += 1
        return None

    monkeypatch.setattr(orch, "run_batch", fake_run_batch)

    async def cb(t, a):
        return None

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    interaction = _make_interaction(fake_cfg.discord.mention_user_id)
    await bot._cmd_run(interaction, "ba1lly/sandbox")
    args, _kwargs = interaction.response.send_message.call_args
    assert "started run" in args[0]
    await asyncio.sleep(0.05)
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_cmd_answer_unauthorized(inmem_store, fake_cfg, fake_discord_with_commands):
    from nocturne.discord_bot import NocturneBot
    captured: list[tuple[str, str]] = []

    async def cb(task_id, answer):
        captured.append((task_id, answer))

    bot = NocturneBot(fake_cfg, inmem_store, cb)
    interaction = _make_interaction(user_id=0)
    await bot._cmd_answer(interaction, "x/y#1", "my answer")
    assert captured == []
    args, _kwargs = interaction.response.send_message.call_args
    assert "not authorized" in args[0]


_ = (datetime, timezone, Store)
