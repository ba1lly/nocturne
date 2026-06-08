from __future__ import annotations

from pathlib import Path

import jinja2

from nocturne.config import Config, ConfigError
from nocturne.models import Task

_SOUL_CACHE: dict[str, tuple[int, str]] = {}
_MAX_SOUL_CHARS = 8192


def load_soul(cfg: Config) -> str | None:
    if not cfg.persona.enabled:
        return None

    if cfg.persona.soul_path is None:
        return None

    path = Path(cfg.persona.soul_path).expanduser()
    if not path.is_file():
        return None

    mtime = path.stat().st_mtime_ns
    cache_key = str(path)
    cached = _SOUL_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        content = cached[1]
    else:
        content = path.read_text()
        _SOUL_CACHE[cache_key] = (mtime, content)

    if len(content) > _MAX_SOUL_CHARS:
        raise ConfigError("soul.md exceeds 8192 char cap")

    if not content.strip():
        return None

    return content


def _template_env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(Path(__file__).parent),
        autoescape=False,
        keep_trailing_newline=True,
    )


def render_task_prompt(task: Task, cfg: Config, prior_failure: str | None = None) -> str:
    template = _template_env().get_template("task.md.jinja2")
    return template.render(
        soul=load_soul(cfg),
        issue_number=task.issue_number,
        issue_title=task.title,
        issue_body=task.body,
        verify_cmd=task.verify_cmd,
        require_new_test=task.require_new_test,
        branch=task.branch,
        prior_failure=prior_failure,
    )


def render_review_prompt(diff: str, skill_name: str, cfg: Config) -> str:
    soul = load_soul(cfg)
    template = _template_env().from_string(
        "\n".join(
            [
                "{% if soul %}---",
                "# Persona",
                "{{ soul }}",
                "---",
                "",
                "{% endif %}You are running the {{ skill_name }} skill. Review the following diff and report findings:",
                "```diff",
                "{{ diff }}",
                "```",
                "",
            ]
        )
    )
    return template.render(soul=soul, skill_name=skill_name, diff=diff)
