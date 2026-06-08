#!/usr/bin/env python3
"""Discord E2E harness — drives the bot tree programmatically for M4 acceptance.

Modes:
  fetch-latest --channel <id> --limit <N>     Print N latest messages as JSON
  reply --msg-id <id> --text "<text>" --channel <id>  Post a reply to a message
  invoke-command --user-id <id> --name <cmd>  Invoke slash command callback

Requires NOCTURNE_DISCORD_TOKEN in env. Skipped by default (live env required).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


def _require_token() -> str:
    """Require NOCTURNE_DISCORD_TOKEN env var."""
    token = os.environ.get("NOCTURNE_DISCORD_TOKEN")
    if not token:
        print("ERROR: NOCTURNE_DISCORD_TOKEN not set", file=sys.stderr)
        sys.exit(2)
    return token


async def fetch_latest(channel_id: int, limit: int) -> None:
    """Fetch latest messages from a channel and print as JSON."""
    import discord

    token = _require_token()
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)

            messages = []
            async for m in channel.history(limit=limit):
                embeds_data = []
                for e in m.embeds:
                    embed_dict = {"title": e.title, "description": e.description}
                    if e.footer:
                        embed_dict["footer"] = e.footer.text
                    embeds_data.append(embed_dict)

                msg_dict = {
                    "id": m.id,
                    "content": m.content,
                    "author_id": m.author.id,
                    "embeds": embeds_data,
                    "reference_msg_id": (m.reference.message_id if m.reference else None),
                }
                messages.append(msg_dict)

            print(json.dumps(messages, indent=2))
        finally:
            await client.close()

    await client.start(token)


async def reply_to(msg_id: int, text: str, channel_id: int) -> None:
    """Post a reply to a specific message."""
    import discord

    token = _require_token()
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)

            msg = await channel.fetch_message(msg_id)
            await msg.reply(text)
            print(f"replied to {msg_id}")
        finally:
            await client.close()

    await client.start(token)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Discord E2E harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # fetch-latest subcommand
    f = sub.add_parser("fetch-latest")
    f.add_argument("--channel", type=int, required=True, help="Channel ID")
    f.add_argument("--limit", type=int, default=10, help="Number of messages to fetch")

    # reply subcommand
    r = sub.add_parser("reply")
    r.add_argument("--msg-id", type=int, required=True, help="Message ID to reply to")
    r.add_argument("--text", type=str, required=True, help="Reply text")
    r.add_argument("--channel", type=int, required=True, help="Channel ID")

    # invoke-command subcommand (placeholder for future implementation)
    i = sub.add_parser("invoke-command")
    i.add_argument("--user-id", type=int, required=True, help="User ID")
    i.add_argument("--name", type=str, required=True, help="Command name")
    i.add_argument("args", nargs="*", help="Command arguments")

    args = parser.parse_args()

    if args.cmd == "fetch-latest":
        asyncio.run(fetch_latest(args.channel, args.limit))
    elif args.cmd == "reply":
        asyncio.run(reply_to(args.msg_id, args.text, args.channel))
    elif args.cmd == "invoke-command":
        print(
            "ERROR: invoke-command requires live Daemon instance (deferred implementation)",
            file=sys.stderr,
        )
        sys.exit(2)
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
