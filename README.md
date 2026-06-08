# nocturne

Project scaffold for Nocturne.

## Configuration

Start from `config.example.yaml` and copy it to `nocturne.yaml` for local use.

Required environment variables:
- `DASHSCOPE_API_KEY` for the configured provider key
- `NOCTURNE_DISCORD_TOKEN` for the Discord bot token

`discord.channel_id` and `discord.mention_user_id` must be non-zero before daemon startup.

See `docs/GETTING_STARTED.md` for the full setup flow.
