# Getting Started with Nocturne

Welcome to Nocturne, your autonomous agent for GitHub issue resolution and repository maintenance. This guide will walk you through the process of setting up Nocturne from scratch, from prerequisites to your first autonomous run.

## 1. Prerequisites

Before you begin, ensure your environment meets the following requirements:

- **Python 3.11+**: Nocturne is built using modern Python features and requires version 3.11 or higher. You can check your version with `python3 --version`.
- **GitHub CLI (`gh`)**: Nocturne uses the GitHub CLI for repository interactions, issue management, and PR creation. Ensure it is installed and authenticated:
  ```bash
  gh auth status
  ```
  If not authenticated, run `gh auth login`.
- **OpenCode**: Nocturne relies on OpenCode for its core agentic capabilities. Ensure `opencode` is installed and available in your PATH.
- **Provider API Key**: You need at least one API key from a supported LLM provider. By default, Nocturne is configured to use Alibaba's DashScope (`DASHSCOPE_API_KEY`), but it also supports Anthropic, OpenAI, and others.
- **Discord (Optional for HITL)**: If you want to use the Human-In-The-Loop (HITL) features via Discord, you'll need:
  - A Discord application and bot token.
  - The `discord.py` library installed in your environment.
  - A dedicated Discord channel for the bot.
  - Your Discord User ID for pings.

## 2. Fork + Clone

The recommended way to start with Nocturne is to fork the main repository and clone it locally. This allows you to maintain your own configuration and potentially contribute back.

### Forking the Repository

Use the GitHub CLI to fork the repository:
```bash
gh repo fork ba1lly/nocturne --clone --remote
```
This command will:
1. Create a fork of `ba1lly/nocturne` under your GitHub account.
2. Clone the fork to your local machine.
3. Set up a remote named `upstream` pointing to the original repository.

Alternatively, you can clone your fork directly if you've already forked it via the web UI:
```bash
git clone https://github.com/<your-username>/nocturne.git
cd nocturne
```

## 3. First-time Setup

Nocturne provides an interactive setup script to help you generate your initial configuration.

### Running the Setup Script

Execute the setup script from the root of the repository:
```bash
bash scripts/setup.sh
```

The script will prompt you for several key pieces of information:
- **GitHub owner**: Your GitHub username or organization name where the sandbox and other repos reside.
- **Sandbox repo name**: The name of the repository Nocturne will use for its initial "dogfooding" tests (default: `nocturne-playground`).
- **Discord channel ID**: The ID of the Discord channel where Nocturne will post status updates and requests for input.
- **Discord mention user ID**: Your Discord User ID so Nocturne can ping you when it needs attention.
- **Provider API key env var name**: The name of the environment variable that holds your LLM provider's API key (default: `DASHSCOPE_API_KEY`).

### Configuration File

The setup script writes your configuration to `~/.config/nocturne/config.yaml`. This file is excluded from version control to protect your personal settings.

### Environment Variables

Ensure you have the required environment variables set in your shell. You might want to add these to your `.bashrc` or `.zshrc`:
```bash
export DASHSCOPE_API_KEY="your-api-key-here"
export NOCTURNE_DISCORD_TOKEN="your-discord-bot-token-here"
```

## 4. Bootstrap Sandbox

Nocturne uses a "sandbox" repository to safely test its capabilities before you let it loose on your real projects. The `scripts/bootstrap_sandbox.sh` script automates the creation and seeding of this repository.

### Running the Bootstrap Script

```bash
bash scripts/bootstrap_sandbox.sh
```

This script will:
1. Create a new repository on GitHub (e.g., `your-username/nocturne-playground`) if it doesn't exist.
2. Clone it to a local directory (default: `~/projects/nocturne-playground-checkout`).
3. Seed it with a basic Python project structure, including a buggy math module and some initial tests.
4. Create five initial issues in the repository, ranging from simple bug fixes to complex refactoring tasks.

### Why these issues?

The seeded issues are designed to test different aspects of Nocturne:
- **Bug Fix**: A simple off-by-one error in a division function.
- **Feature Addition**: Adding a new function with tests.
- **Vague Request**: An "improve the math module" request to test triage and clarification.
- **Over-scoped Refactor**: A request for a massive refactor to test boundary detection and rejection.
- **Sentinel Documentation**: A task involving a specific sentinel string to test HITL triggers.

## 5. Install Reviewer Skill

Nocturne can use specialized "skills" to enhance its performance. One such skill is the `reviewer` skill, which provides a multi-agent review pipeline for its own PRs.

### Installation

If you have the reviewer skill available locally (e.g., at `~/.agents/skills/reviewer/`), you can install it using the `nocturne` CLI:
```bash
nocturne skill install ~/.agents/skills/reviewer/
```

Once installed, Nocturne will automatically invoke this skill during its "Review" phase (M5+) to ensure high-quality code output.

## 6. First Run

Now that everything is set up, you can run Nocturne for the first time.

### Single Batch Mode

To process a specific issue once and then exit, use the `run-once` command:
```bash
nocturne run-once --repo your-username/nocturne-playground --issue 1
```
This is great for testing and seeing exactly how the agent behaves.

### Daemon Mode

For continuous operation, run Nocturne in daemon mode:
```bash
nocturne daemon
```
In this mode, Nocturne will periodically poll your configured repositories for new issues with the `agent` label and process them automatically.

### Monitoring Progress

You can monitor Nocturne's progress through:
- **Console Output**: Detailed logs of the agent's thoughts and actions.
- **Discord**: Status reports and pings for input (if enabled).
- **Reports**: Nocturne generates a JSON report for each task in `~/.local/state/nocturne/reports/`.

## 7. Troubleshooting

If you encounter issues, check the following common problems:

### ConfigError: DASHSCOPE_API_KEY env var not set
Ensure the environment variable name matches what you provided during setup and that it is exported in your current shell.

### ProviderNotRegistered
If you see an error about a provider not being registered in OpenCode, run the registration check script:
```bash
bash scripts/check_opencode_provider.sh
```
Follow the instructions to register your provider with OpenCode.

### Discord ping not arriving
- Verify that `channel_id` and `mention_user_id` in `~/.config/nocturne/config.yaml` are correct.
- Ensure `NOCTURNE_DISCORD_TOKEN` is set correctly.
- Make sure the bot has been invited to the channel and has "Send Messages" and "Mention Everyone" permissions.

### Worktree conflict
If the daemon crashes or is killed forcefully, it might leave behind stale git worktrees. Nocturne tries to clean these up on restart, but you can also run:
```bash
git worktree prune
```
in your repository checkout directory.

### Stuck "running" task
If a task is marked as "running" in the database but the process is gone, the daemon will automatically mark it as failed on the next restart. You can check the status of tasks with:
```bash
nocturne status
```

For more advanced deployment options, see [docs/DEPLOYMENT.md](DEPLOYMENT.md).
