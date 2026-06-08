# Deploying Nocturne

This guide covers the best practices for deploying Nocturne in a production-like environment, ensuring 24/7 operation, monitoring, and reliable recovery.

## 1. 24/7 on a miniPC

For continuous repository maintenance, it is recommended to run Nocturne on a dedicated machine, such as a miniPC or a home server.

### Systemd Integration

Nocturne includes a script to set up a user-level systemd service. This allows the daemon to start automatically on boot and restart if it crashes.

Run the installation script:
```bash
bash scripts/install-systemd.sh
```

### Enabling Linger

By default, user-level systemd services stop when the user logs out. To keep Nocturne running even when you are not logged in, enable "linger" for your user:
```bash
loginctl enable-linger $USER
```

### Managing the Service

Once installed, you can manage the Nocturne daemon using standard `systemctl` commands:
```bash
# Start the daemon
systemctl --user start nocturne

# Stop the daemon
systemctl --user stop nocturne

# Restart the daemon
systemctl --user restart nocturne

# Check status
systemctl --user status nocturne
```

## 2. Monitoring

Monitoring is crucial for ensuring that Nocturne is operating correctly and not getting stuck.

### Healthcheck Endpoint

Nocturne exposes a simple HTTP healthcheck endpoint (default: `http://127.0.0.1:8765/health`).
- **200 OK**: The daemon is running and has polled recently.
- **503 Service Unavailable**: The daemon is running but hasn't polled within the expected timeframe (stale).

You can use a tool like `curl` to check the health:
```bash
curl http://127.0.0.1:8765/health
```

### Logs

Nocturne logs to `stdout`, which is captured by `journald` when running under systemd. You can view the logs in real-time:
```bash
journalctl --user -u nocturne -f
```

### Discord Status Reports

If Discord integration is enabled, Nocturne will post a summary report to your configured channel after completing each task. This is the easiest way to keep an eye on what the agent is doing without logging into the server.

## 3. Backup & Recovery

Nocturne stores its state in a SQLite database located at `~/.local/state/nocturne/nocturne.db`.

### Database Backups

It is highly recommended to set up a cron job to back up the database daily. Here is an example cron entry:
```bash
0 2 * * * sqlite3 ~/.local/state/nocturne/nocturne.db ".backup /home/bailly/backups/nocturne-$(date +\%F).db"
```

### Recovery Procedure

If the database becomes corrupted or you need to move to a new machine:
1. Stop the Nocturne daemon.
2. Copy your backup file to `~/.local/state/nocturne/nocturne.db`.
3. Ensure the `checkout_path` for all repositories in your `config.yaml` exists and is a valid git repository.
4. Restart the daemon.

### Worktree State

Nocturne uses git worktrees for its operations. These are temporary and are usually cleaned up automatically. If you encounter issues with "stale worktrees," you can safely delete the directories under your configured `opencode.worktree_root` (default: `/tmp/nocturne`) while the daemon is stopped.

## 4. Promoting Real Repos

Once you are confident in Nocturne's performance in the sandbox, you can promote it to work on your real repositories.

### Checklist for Promotion

Before adding a real repository to Nocturne:
1. **Pass Sandbox Acceptance**: Ensure the current version of Nocturne passes the `bash scripts/m4_acceptance.sh` test suite in the sandbox.
2. **Clean Backlog**: Ensure the repository has clear, well-defined issues.
3. **Labeling**: Only issues with the `agent` label will be processed. This gives you fine-grained control over what the agent touches.
4. **Verify Command**: Ensure the `verify_cmd` in your config (e.g., `pytest` or `npm test`) is reliable and fast.

### Adding a Repository

Add a new entry to the `repos:` section in your `~/.config/nocturne/config.yaml`:
```yaml
repos:
  - slug: "your-org/your-repo"
    checkout_path: "~/projects/your-repo"
    label: "agent"
    base: "main"
    verify_cmd: "pytest -q"
    require_new_test: true
```

After updating the config, restart the daemon:
```bash
systemctl --user restart nocturne
```

## 5. Future Integrations (NOT in M1-M5)

While Nocturne is currently focused on Python and GitHub, several integrations are planned for future releases:

- **Fallow.tools**: Integration with `fallow.tools` for enhanced static analysis and refactoring of TypeScript and JavaScript repositories.
- **Docker Deployment**: A official Docker image for easier deployment across different cloud providers and container orchestrators.
- **Web UI Dashboard**: A browser-based dashboard for visualizing task history, performance metrics, and managing configuration without touching YAML files.
- **GitLab/Bitbucket Support**: Expanding beyond GitHub to support other popular version control platforms.

## 6. Token-budget Management

LLM usage can become expensive if not monitored. Nocturne includes built-in guardrails to help you manage your token budget.

### Budget Tracking

You can set a global token budget in your `config.yaml`:
```yaml
guardrails:
  token_budget: 2000000 # Total tokens allowed across all tasks
```

Nocturne tracks usage per provider and will stop processing new tasks if the budget is exceeded.

### Provider Quotas

Be aware of the rate limits and quotas imposed by your LLM providers (DashScope, Anthropic, OpenAI, etc.). If you hit a rate limit, Nocturne will back off and retry, but persistent quota issues will cause tasks to fail.

### Pausing the Daemon

If you notice Nocturne is consuming tokens too quickly or making mistakes, you can pause it via the Discord slash command `/pause` (if implemented) or by simply stopping the systemd service. This allows you to investigate and adjust your configuration or prompts before resuming.

### Cost Optimization

To minimize costs:
- Use more efficient models for reasoning and reporting (e.g., `qwen-plus` instead of `qwen-max`).
- Keep your issues well-scoped to reduce the number of iterations required.
- Use the `agent` label sparingly on complex issues.
