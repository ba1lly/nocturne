import subprocess
from pathlib import Path

import pytest
import yaml

# test fixture: not a config dependency
SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "setup.sh"

def run_setup(*args, cwd=None):
    return subprocess.run(
        ["bash", str(SCRIPT_PATH)] + list(args),
        capture_output=True,
        text=True,
        cwd=cwd
    )

def test_setup_non_interactive_writes_config(tmp_path):
    config_dir = tmp_path / "config"
    result = run_setup(
        "--non-interactive",
        "--owner", "test-owner",
        "--sandbox-repo", "test-sandbox",
        "--discord-channel", "12345",
        "--discord-user", "67890",
        "--api-key-env", "TEST_API_KEY",
        "--config-dir", str(config_dir)
    )

    assert result.returncode == 0
    config_file = config_dir / "config.yaml"
    assert config_file.exists()

    with open(config_file) as f:
        cfg = yaml.safe_load(f)

    assert cfg["github"]["owner"] == "test-owner"
    assert cfg["sandbox"]["repo_name"] == "test-sandbox"
    assert cfg["discord"]["channel_id"] == 12345
    assert cfg["discord"]["mention_user_id"] == 67890
    assert cfg["providers"]["alibaba-coding-plan"]["api_key_env"] == "TEST_API_KEY"
    # Check if ba1lly/nocturne-playground was replaced in repos
    assert cfg["repos"][0]["slug"] == "test-owner/test-sandbox"

def test_setup_requires_owner(tmp_path):
    config_dir = tmp_path / "config"
    result = run_setup(
        "--non-interactive",
        "--config-dir", str(config_dir)
    )
    assert result.returncode == 2
    assert "ERROR: --owner is required" in result.stderr

def test_setup_existing_config_rejected_without_force(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("existing: config")

    result = run_setup(
        "--non-interactive",
        "--owner", "test-owner",
        "--config-dir", str(config_dir)
    )
    assert result.returncode == 2
    assert "already exists" in result.stderr
    assert config_file.read_text() == "existing: config"

def test_setup_force_overwrites(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("existing: config")

    result = run_setup(
        "--non-interactive",
        "--owner", "test-owner",
        "--config-dir", str(config_dir),
        "--force"
    )
    assert result.returncode == 0
    assert config_file.read_text() != "existing: config"

    with open(config_file) as f:
        cfg = yaml.safe_load(f)
    assert cfg["github"]["owner"] == "test-owner"

def test_setup_help_shows_usage():
    result = run_setup("--help")
    assert result.returncode == 0
    assert "nocturne setup" in result.stdout

def test_no_hardcoded_owner_in_production():
    repo_root = Path(__file__).parent.parent
    # Scan nocturne/ and scripts/
    # We exclude comments containing "# test fixture" or "# example"
    # We also exclude the setup.sh itself since it contains the replacement logic
    # and config.example.yaml since it's the template.

    result = subprocess.run(
        ["grep", "-rn", "ba1lly/", "nocturne/", "scripts/"],
        capture_output=True,
        text=True,
        cwd=repo_root
    )

    lines = result.stdout.splitlines()
    filtered_lines = []
    for line in lines:
        if "# test fixture" in line or "# example" in line:
            continue
        if "scripts/setup.sh" in line:
            continue
        if "scripts/m" in line and "_acceptance.sh" in line:
            continue
            if "ba1lly/reviewer-config" in line:
                continue
            filtered_lines.append(line)

    assert not filtered_lines, f"Found hardcoded 'ba1lly/' in production code:\n" + "\n".join(filtered_lines)
