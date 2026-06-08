"""Tests for nocturne.skills (install/list/enable/disable/uninstall)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nocturne import skills as skills_mod
from nocturne.cli import app
from nocturne.skills import (
    SkillError,
    SkillExists,
    SkillInvalid,
    SkillMeta,
    SkillNotFound,
    disable_skill,
    enable_skill,
    install_skill,
    is_skill_enabled,
    list_skills,
    parse_frontmatter,
    uninstall_skill,
)


SAMPLE_FRONTMATTER = """---
name: reviewer
description: Code review skill
---

# Skill content

Some body text.
"""


@pytest.fixture
def skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate SKILLS_DIR to tmp_path/skills."""
    d = tmp_path / "skills"
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", d)
    return d


def _make_skill_file(tmp_path: Path, name: str = "reviewer", body: str = SAMPLE_FRONTMATTER) -> Path:
    src = tmp_path / f"{name}-SKILL.md"
    src.write_text(body, encoding="utf-8")
    return src


# ---- parse_frontmatter ----

def test_parse_frontmatter_extracts_name_description() -> None:
    fm = parse_frontmatter(SAMPLE_FRONTMATTER)
    assert fm["name"] == "reviewer"
    assert fm["description"] == "Code review skill"


def test_parse_frontmatter_rejects_missing_name() -> None:
    text = "---\ndescription: foo\n---\nbody"
    with pytest.raises(SkillInvalid, match="name"):
        parse_frontmatter(text)


def test_parse_frontmatter_rejects_missing_description() -> None:
    text = "---\nname: foo\n---\nbody"
    with pytest.raises(SkillInvalid, match="description"):
        parse_frontmatter(text)


def test_parse_frontmatter_rejects_no_yaml() -> None:
    with pytest.raises(SkillInvalid, match="frontmatter"):
        parse_frontmatter("# just markdown, no frontmatter")


def test_parse_frontmatter_rejects_unterminated() -> None:
    with pytest.raises(SkillInvalid, match="unterminated"):
        parse_frontmatter("---\nname: foo\ndescription: bar\nbody without close")


# ---- install_skill ----

def test_install_skill_from_file(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    name = install_skill(str(src))
    assert name == "reviewer"
    assert (skills_dir / "reviewer" / "SKILL.md").is_file()
    meta_path = skills_dir / "reviewer" / ".nocturne-skill-meta.json"
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text())
    assert meta["name"] == "reviewer"
    assert meta["source"] == str(src)
    assert meta["enabled"] is True


def test_install_skill_from_directory(tmp_path: Path, skills_dir: Path) -> None:
    skill_src = tmp_path / "reviewer-src"
    skill_src.mkdir()
    (skill_src / "SKILL.md").write_text(SAMPLE_FRONTMATTER, encoding="utf-8")
    (skill_src / "references").mkdir()
    (skill_src / "references" / "guide.md").write_text("guide content", encoding="utf-8")

    name = install_skill(str(skill_src))
    assert name == "reviewer"
    assert (skills_dir / "reviewer" / "SKILL.md").is_file()
    assert (skills_dir / "reviewer" / "references" / "guide.md").read_text() == "guide content"


def test_install_skill_rejects_http(skills_dir: Path) -> None:
    with pytest.raises(SkillInvalid, match="HTTPS"):
        install_skill("http://example.com/SKILL.md")


def test_install_skill_rejects_missing_source(skills_dir: Path) -> None:
    with pytest.raises(SkillInvalid):
        install_skill("/nonexistent/path/to/skill.md")


def test_install_skill_rejects_existing_without_force(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    install_skill(str(src))
    with pytest.raises(SkillExists, match="already installed"):
        install_skill(str(src))


def test_install_skill_force_backs_up_old(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    install_skill(str(src))

    # Modify and re-install with force
    updated = SAMPLE_FRONTMATTER.replace("Code review skill", "Code review skill v2")
    src2 = tmp_path / "reviewer-v2.md"
    src2.write_text(updated, encoding="utf-8")
    install_skill(str(src2), force=True)

    backup_root = skills_dir / ".backup"
    assert backup_root.is_dir()
    backups = list(backup_root.iterdir())
    assert any(b.name.startswith("reviewer-") for b in backups)
    # New content present
    new_content = (skills_dir / "reviewer" / "SKILL.md").read_text()
    assert "v2" in new_content


def test_install_skill_from_https_url(tmp_path: Path, skills_dir: Path) -> None:
    """install_skill from HTTPS URL uses urllib (mocked)."""
    from io import BytesIO

    class FakeResp:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._bio = BytesIO(data)

        def read(self) -> bytes:
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("nocturne.skills.urllib.request.urlopen", return_value=FakeResp(SAMPLE_FRONTMATTER.encode())):
        name = install_skill("https://example.com/SKILL.md")
    assert name == "reviewer"
    assert (skills_dir / "reviewer" / "SKILL.md").is_file()


# ---- list_skills / is_skill_enabled ----

def test_list_skills_returns_installed(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    install_skill(str(src))
    result = list_skills()
    assert len(result) == 1
    assert result[0].name == "reviewer"
    assert result[0].enabled is True


def test_list_skills_empty_when_no_dir(skills_dir: Path) -> None:
    assert list_skills() == []


def test_list_skills_skips_backup_dir(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    install_skill(str(src))
    install_skill(str(src), force=True)
    result = list_skills()
    assert len(result) == 1


# ---- enable / disable roundtrip ----

def test_enable_disable_roundtrip(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    install_skill(str(src))
    assert is_skill_enabled("reviewer") is True

    disable_skill("reviewer")
    assert is_skill_enabled("reviewer") is False
    assert (skills_dir / "reviewer" / "SKILL.md.disabled").exists()
    assert not (skills_dir / "reviewer" / "SKILL.md").exists()

    enable_skill("reviewer")
    assert is_skill_enabled("reviewer") is True
    assert (skills_dir / "reviewer" / "SKILL.md").exists()
    assert not (skills_dir / "reviewer" / "SKILL.md.disabled").exists()


def test_disable_missing_raises(skills_dir: Path) -> None:
    with pytest.raises(SkillNotFound):
        disable_skill("does-not-exist")


def test_enable_missing_raises(skills_dir: Path) -> None:
    with pytest.raises(SkillNotFound):
        enable_skill("does-not-exist")


# ---- uninstall ----

def test_uninstall_removes_dir(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    install_skill(str(src))
    assert (skills_dir / "reviewer").is_dir()
    uninstall_skill("reviewer")
    assert not (skills_dir / "reviewer").exists()


def test_uninstall_missing_raises(skills_dir: Path) -> None:
    with pytest.raises(SkillNotFound):
        uninstall_skill("does-not-exist")


# ---- CLI tests ----

def test_cli_skill_install_from_file_via_runner(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["skill", "install", str(src)])
    assert result.exit_code == 0, result.output
    assert "Installed: reviewer" in result.output


def test_cli_skill_list_empty(skills_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0
    assert "no skills installed" in result.output


def test_cli_skill_list_with_one(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    install_skill(str(src))
    runner = CliRunner()
    result = runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0
    assert "reviewer" in result.output


def test_cli_skill_install_rejects_http(skills_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["skill", "install", "http://example.com/SKILL.md"])
    assert result.exit_code == 2
    assert "HTTPS" in result.output


def test_cli_skill_install_already_installed_exit_2(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    install_skill(str(src))
    runner = CliRunner()
    result = runner.invoke(app, ["skill", "install", str(src)])
    assert result.exit_code == 2
    assert "already installed" in result.output


def test_cli_skill_disable_enable_info(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    install_skill(str(src))
    runner = CliRunner()

    r1 = runner.invoke(app, ["skill", "disable", "reviewer"])
    assert r1.exit_code == 0
    assert "Disabled: reviewer" in r1.output
    assert not is_skill_enabled("reviewer")

    r2 = runner.invoke(app, ["skill", "info", "reviewer"])
    assert r2.exit_code == 0
    assert "Name: reviewer" in r2.output
    assert "Enabled: False" in r2.output

    r3 = runner.invoke(app, ["skill", "enable", "reviewer"])
    assert r3.exit_code == 0
    assert is_skill_enabled("reviewer")


def test_cli_skill_uninstall_with_yes(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    install_skill(str(src))
    runner = CliRunner()
    result = runner.invoke(app, ["skill", "uninstall", "reviewer", "--yes"])
    assert result.exit_code == 0
    assert "Uninstalled: reviewer" in result.output
    assert not (skills_dir / "reviewer").exists()


def test_cli_skill_uninstall_aborted(tmp_path: Path, skills_dir: Path) -> None:
    src = _make_skill_file(tmp_path)
    install_skill(str(src))
    runner = CliRunner()
    result = runner.invoke(app, ["skill", "uninstall", "reviewer"], input="n\n")
    assert result.exit_code == 0
    assert "Aborted" in result.output
    assert (skills_dir / "reviewer").exists()


def test_skill_meta_serializes() -> None:
    from datetime import datetime, timezone

    m = SkillMeta(
        name="x",
        description="y",
        source="https://e.com/s.md",
        installed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    data = m.model_dump(mode="json")
    assert data["name"] == "x"
    assert data["enabled"] is True
    assert data["version"] == "0.1.0"


def test_skill_error_hierarchy() -> None:
    assert issubclass(SkillExists, SkillError)
    assert issubclass(SkillInvalid, SkillError)
    assert issubclass(SkillNotFound, SkillError)
