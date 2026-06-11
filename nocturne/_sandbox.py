"""Shared process-isolation helpers for credential scrubbing.

Nocturne runs two subprocesses over agent-influenceable content inside the
worktree: opencode (the coding agent) and the verify command (which executes
agent-authored test code). Neither legitimately needs the operator's git remote
credentials, so both run with a scrubbed environment built here.

This is the env-hardening layer only. Full OS-level isolation (network egress
allowlist, filesystem confinement, syscall/seccomp, memory limits) is a separate
boundary that wraps these same two subprocesses.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

# Credential environment variables stripped before spawning either subprocess.
#   - GH_* / GITHUB_*: gh CLI and git-over-HTTPS token auth.
#   - SSH_AUTH_SOCK / SSH_AGENT_PID: the operator's ssh-agent (key forwarding).
CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
)

# ssh invocation that refuses every credential source: ignores the agent
# (IdentityAgent=none), uses no on-disk key (IdentitiesOnly + IdentityFile
# /dev/null), and never prompts (BatchMode). Any git push/fetch over SSH from a
# child fails fast instead of authenticating as the operator.
HARDENED_GIT_SSH_COMMAND = (
    "ssh -o IdentityAgent=none -o IdentitiesOnly=yes -o IdentityFile=/dev/null "
    "-o BatchMode=yes -o PreferredAuthentications=publickey "
    "-o PasswordAuthentication=no -o KbdInteractiveAuthentication=no"
)


def scrubbed_env(
    base: Mapping[str, str] | None = None,
    *,
    strip: Iterable[str] = (),
    gh_config_dir: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a subprocess environment with credentials removed and git remote
    auth hardened.

    Args:
        base: source environment (defaults to ``os.environ``).
        strip: additional variable names to remove (e.g. provider API keys for
            the verify subprocess, which needs no model access).
        gh_config_dir: if given, point ``GH_CONFIG_DIR`` at this throwaway dir so
            gh cannot read the operator's stored auth (hosts.yml).
        extra: variables to set/override after scrubbing (e.g. the provider key
            opencode needs).
    """
    src = os.environ if base is None else base
    drop = set(CREDENTIAL_ENV_VARS) | set(strip)
    env = {k: v for k, v in src.items() if k not in drop}
    # Neutralise SSH-based git auth (on-disk keys and agent alike) and never
    # block on a credential prompt.
    env["GIT_SSH_COMMAND"] = HARDENED_GIT_SSH_COMMAND
    env["GIT_TERMINAL_PROMPT"] = "0"
    if gh_config_dir is not None:
        env["GH_CONFIG_DIR"] = gh_config_dir
    if extra:
        env.update(extra)
    return env
