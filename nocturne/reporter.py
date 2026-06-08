from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import jinja2

from nocturne.models import RunReport
from nocturne.config import Config, get_api_key, provider_of
from nocturne._logging import get_logger

logger = get_logger("nocturne.reporter")


def _human_duration(start: datetime, end: Optional[datetime]) -> str:
    """Convert timedelta to human-readable format like '5m 30s'."""
    if end is None:
        return "in progress"
    
    delta = end - start
    total_seconds = int(delta.total_seconds())
    
    if total_seconds < 0:
        return "invalid"
    
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    
    if minutes == 0:
        return f"{seconds}s"
    elif seconds == 0:
        return f"{minutes}m"
    else:
        return f"{minutes}m {seconds}s"


def write_report(report: RunReport, reports_dir: Path) -> Path:
    """Write a RunReport to a Markdown file in reports_dir.
    
    Returns the path to the written file.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    
    # Filesystem-safe ISO timestamp: replace colons with dashes
    filename = report.started_at.strftime("%Y-%m-%dT%H-%M-%S") + ".md"
    report_path = reports_dir / filename
    
    # Load and render template
    template_dir = Path(__file__).parent / "templates"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(template_dir),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template("report.md.jinja2")
    
    context = {
        "started_at": report.started_at.isoformat(),
        "ended_at": report.ended_at.isoformat(),
        "duration_human": _human_duration(report.started_at, report.ended_at),
        "summary": report.summary,
        "done": report.done,
        "parked": report.parked,
        "skipped": report.skipped,
        "errors": report.errors,
        "token_usage": report.token_usage,
    }
    
    rendered = template.render(context)
    report_path.write_text(rendered, encoding="utf-8")
    
    return report_path


def _deterministic_summary(report: RunReport) -> str:
    """Fallback summary when LLM is unavailable."""
    return f"{len(report.done)} done, {len(report.parked)} parked, {len(report.skipped)} skipped, {len(report.errors)} errors."


def summarize(report: RunReport, cfg: Config) -> str:
    """Summarize a RunReport using LLM, with deterministic fallback."""
    # Empty run
    if not report.done and not report.parked and not report.skipped and not report.errors:
        return "Empty run."
    
    try:
        # Lazy import to allow test mocking
        from openai import OpenAI
        
        provider_name = provider_of(cfg.models.report)
        provider_cfg = cfg.providers[provider_name]
        api_key = get_api_key(cfg, provider_name)
        
        client = OpenAI(base_url=provider_cfg.base_url, api_key=api_key)
        
        # Build serialized report (JSON-safe, no raw objects)
        serialized = {
            "done": [
                {"issue": task.issue_number, "title": task.title}
                for task in report.done
            ],
            "parked": [
                {"issue": task.issue_number, "title": task.title, "question": task.question}
                for task in report.parked
            ],
            "skipped": [
                {"issue": entry[0], "reason": entry[1]}
                for entry in report.skipped
            ],
            "errors": report.errors,
        }
        
        # Extract model name (part after provider prefix)
        model_name = cfg.models.report.split("/", 1)[1]
        
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {
                    "role": "system",
                    "content": "You summarize Nocturne autonomous coding runs in one paragraph, factual and concise.",
                },
                {
                    "role": "user",
                    "content": f"Summarize this run: {json.dumps(serialized)}",
                },
            ],
        )
        
        return response.choices[0].message.content.strip()
    
    except Exception as e:
        logger.warning(f"LLM summarization failed: {e}; using deterministic fallback")
        return _deterministic_summary(report)


def discord_message(report: RunReport) -> str:
    """Generate a Discord-friendly summary (≤280 chars)."""
    # Determine emoji based on state
    if report.errors:
        emoji = "🔴"
    elif report.parked:
        emoji = "🟡"
    else:
        emoji = "🟢"
    
    # Build base message
    message = f"{emoji} Nocturne: {len(report.done)} done, {len(report.parked)} parked, {len(report.errors)} errors"
    
    # Append first PR URL if available
    if report.done and report.done[0].pr_url:
        message += f" — PR: {report.done[0].pr_url}"
    
    # Truncate to 280 chars
    if len(message) > 280:
        return message[:277] + "..."
    
    return message
