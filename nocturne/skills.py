"""Skill management module: install/list/enable/disable OpenCode skills.

Skills live in `~/.config/opencode/skills/<name>/SKILL.md`. Each skill MUST have
YAML frontmatter with `name` and `description` fields. Nocturne records install
metadata in `<skill-dir>/.nocturne-skill-meta.json`.

Source types supported by `install_skill`:
- HTTPS URL (rejects http://) → fetched via urllib with 30s timeout
- Local file path → read SKILL.md directly
- Local directory path → copy whole directory contents

Existing skills are NEVER silently overwritten. Re-installing the same skill
without `force=True` raises SkillExists. With `force=True`, the old version is
backed up to `.backup/<name>-<ISO-timestamp>/` first.
"""

from __future__ import annotations

import json
import shutil
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel

from nocturne._logging import get_logger

log = get_logger("nocturne.skills")

SKILLS_DIR = Path.home() / ".config" / "opencode" / "skills"


class SkillError(Exception):
    """Base class for skill management errors."""


class SkillExists(SkillError):
    """Raised when a skill already exists and force was not specified."""


class SkillInvalid(SkillError):
    """Raised when a skill source or its frontmatter is invalid."""


class SkillNotFound(SkillError):
    """Raised when a skill is not installed."""


class SkillMeta(BaseModel):
    """Metadata for an installed skill."""

    name: str
    description: str
    source: str
    version: str = "0.1.0"
    installed_at: datetime
    enabled: bool = True


def parse_frontmatter(text: str) -> dict[str, object]:
    """Extract YAML frontmatter between leading `---` markers.

    Returns the parsed dict. Raises SkillInvalid if:
    - No `---` markers found at file start
    - YAML parse fails
    - Required fields `name` or `description` are missing
    """
    if not text.startswith("---"):
        raise SkillInvalid("missing YAML frontmatter (no leading '---')")

    # Split: '---\n<yaml>\n---\n<rest>'
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillInvalid("missing YAML frontmatter (no leading '---')")

    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        raise SkillInvalid("unterminated YAML frontmatter (no closing '---')")

    yaml_text = "\n".join(lines[1:end_idx])

    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as e:
        raise SkillInvalid(f"invalid YAML frontmatter: {e}") from e

    if not isinstance(data, dict):
        raise SkillInvalid("YAML frontmatter must be a mapping")

    if "name" not in data or not data["name"]:
        raise SkillInvalid("missing required frontmatter field: name")

    if "description" not in data or not data["description"]:
        raise SkillInvalid("missing required frontmatter field: description")

    return data


def _fetch_url(url: str) -> bytes:
    """Fetch a URL with 30s timeout. HTTPS only."""
    if url.startswith("http://"):
        raise SkillInvalid("HTTPS required; http:// URLs are not accepted")
    if not url.startswith("https://"):
        raise SkillInvalid(f"unsupported URL scheme: {url}")

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (https enforced above)
            return resp.read()
    except Exception as e:
        raise SkillInvalid(f"failed to fetch {url}: {e}") from e


def _detect_source_type(source: str) -> str:
    """Return 'url' | 'dir' | 'file' or raise SkillInvalid."""
    if source.startswith("https://"):
        return "url"
    if source.startswith("http://"):
        raise SkillInvalid("HTTPS required; http:// URLs are not accepted")

    p = Path(source).expanduser()
    if p.is_dir():
        return "dir"
    if p.is_file():
        return "file"

    raise SkillInvalid(f"source not found or unsupported: {source}")


def _write_meta(skill_dir: Path, meta: SkillMeta) -> None:
    """Serialize SkillMeta to .nocturne-skill-meta.json."""
    payload = meta.model_dump(mode="json")
    (skill_dir / ".nocturne-skill-meta.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def _read_meta(skill_dir: Path) -> SkillMeta | None:
    """Read .nocturne-skill-meta.json if present."""
    meta_path = skill_dir / ".nocturne-skill-meta.json"
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return SkillMeta(**data)
    except Exception as e:
        log.warning("failed to read meta %s: %s", meta_path, e)
        return None


def install_skill(source: str, force: bool = False) -> str:
    """Install a skill from URL, local file, or local directory.

    Returns the installed skill name. Raises:
    - SkillInvalid: bad URL scheme / unreadable source / bad frontmatter
    - SkillExists: skill already installed and force=False
    """
    src_type = _detect_source_type(source)

    extra_dir_files: list[tuple[str, bytes]] = []  # (relpath, bytes) for dir sources

    if src_type == "url":
        raw = _fetch_url(source)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise SkillInvalid(f"skill content is not UTF-8: {e}") from e
    elif src_type == "file":
        p = Path(source).expanduser()
        content = p.read_text(encoding="utf-8")
    elif src_type == "dir":
        d = Path(source).expanduser()
        skill_md = d / "SKILL.md"
        if not skill_md.is_file():
            raise SkillInvalid(f"directory does not contain SKILL.md: {d}")
        content = skill_md.read_text(encoding="utf-8")
        # Collect ancillary files (excluding SKILL.md and meta) for copy
        for child in d.rglob("*"):
            if child.is_file() and child != skill_md:
                rel = child.relative_to(d)
                # skip our own meta file
                if rel.name == ".nocturne-skill-meta.json":
                    continue
                extra_dir_files.append((str(rel), child.read_bytes()))
    else:  # pragma: no cover - defensive
        raise SkillInvalid(f"unknown source type: {src_type}")

    fm = parse_frontmatter(content)
    name = str(fm["name"]).strip()
    description = str(fm["description"]).strip()
    version = str(fm.get("version", "0.1.0"))

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    skill_dir = SKILLS_DIR / name
    main_md = skill_dir / "SKILL.md"

    already_installed = main_md.exists() or (skill_dir / "SKILL.md.disabled").exists()

    if already_installed and not force:
        raise SkillExists(
            f"skill '{name}' already installed; pass --force to overwrite"
        )

    if already_installed and force:
        # Backup whole skill_dir
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_root = SKILLS_DIR / ".backup"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_dest = backup_root / f"{name}-{ts}"
        shutil.move(str(skill_dir), str(backup_dest))
        log.info("backed up old skill to %s", backup_dest)

    skill_dir.mkdir(parents=True, exist_ok=True)
    main_md.write_text(content, encoding="utf-8")

    # Copy ancillary files for dir sources
    for relpath, data in extra_dir_files:
        dest = skill_dir / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    meta = SkillMeta(
        name=name,
        description=description,
        source=source,
        version=version,
        installed_at=datetime.now(timezone.utc),
        enabled=True,
    )
    _write_meta(skill_dir, meta)

    log.info("installed skill %s from %s", name, source)
    return name


def list_skills() -> list[SkillMeta]:
    """Scan SKILLS_DIR; return SkillMeta for each installed skill."""
    if not SKILLS_DIR.exists():
        return []

    out: list[SkillMeta] = []
    for child in sorted(SKILLS_DIR.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            # skip .backup and other hidden dirs
            continue

        meta = _read_meta(child)
        if meta is not None:
            # Sync enabled flag with file system reality
            enabled = is_skill_enabled(child.name)
            if meta.enabled != enabled:
                meta = meta.model_copy(update={"enabled": enabled})
            out.append(meta)
            continue

        # No meta: parse frontmatter from SKILL.md or SKILL.md.disabled
        md = child / "SKILL.md"
        disabled_md = child / "SKILL.md.disabled"
        target = md if md.exists() else (disabled_md if disabled_md.exists() else None)
        if target is None:
            continue
        try:
            fm = parse_frontmatter(target.read_text(encoding="utf-8"))
        except SkillInvalid as e:
            log.warning("skipping skill dir %s: %s", child, e)
            continue
        out.append(
            SkillMeta(
                name=str(fm["name"]),
                description=str(fm["description"]),
                source=str(target),
                version=str(fm.get("version", "0.1.0")),
                installed_at=datetime.fromtimestamp(
                    target.stat().st_mtime, tz=timezone.utc
                ),
                enabled=md.exists(),
            )
        )

    return out


def is_skill_enabled(name: str) -> bool:
    """Return True if `SKILLS_DIR / name / SKILL.md` exists (not .disabled)."""
    return (SKILLS_DIR / name / "SKILL.md").exists()


def enable_skill(name: str) -> None:
    """Re-enable a disabled skill (rename SKILL.md.disabled → SKILL.md)."""
    skill_dir = SKILLS_DIR / name
    if not skill_dir.is_dir():
        raise SkillNotFound(f"skill not found: {name}")

    disabled = skill_dir / "SKILL.md.disabled"
    enabled = skill_dir / "SKILL.md"
    if disabled.exists() and not enabled.exists():
        disabled.rename(enabled)

    # Update meta
    meta = _read_meta(skill_dir)
    if meta is not None:
        meta = meta.model_copy(update={"enabled": True})
        _write_meta(skill_dir, meta)


def disable_skill(name: str) -> None:
    """Disable a skill (rename SKILL.md → SKILL.md.disabled)."""
    skill_dir = SKILLS_DIR / name
    if not skill_dir.is_dir():
        raise SkillNotFound(f"skill not found: {name}")

    enabled = skill_dir / "SKILL.md"
    disabled = skill_dir / "SKILL.md.disabled"
    if enabled.exists() and not disabled.exists():
        enabled.rename(disabled)

    meta = _read_meta(skill_dir)
    if meta is not None:
        meta = meta.model_copy(update={"enabled": False})
        _write_meta(skill_dir, meta)


def uninstall_skill(name: str) -> None:
    """Remove the skill directory entirely."""
    skill_dir = SKILLS_DIR / name
    if not skill_dir.is_dir():
        raise SkillNotFound(f"skill not found: {name}")
    shutil.rmtree(skill_dir)
    log.info("uninstalled skill %s", name)
