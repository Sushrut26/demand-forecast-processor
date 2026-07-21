"""Shared Jinja2 templates instance and helpers."""

from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import settings

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# Values available to every template.
templates.env.globals["app_name"] = settings.APP_NAME


def humanize_access(access_until) -> str:
    """Return a friendly license-status string for banners."""
    if access_until is None:
        return "No active license"
    au = access_until
    if au.tzinfo is None:
        au = au.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if au <= now:
        return "License expired"
    remaining = au - now
    days = remaining.days
    hours = remaining.seconds // 3600
    if days > 0:
        return f"{days} day(s), {hours} hour(s) remaining"
    minutes = (remaining.seconds % 3600) // 60
    if hours > 0:
        return f"{hours} hour(s), {minutes} minute(s) remaining"
    return f"{minutes} minute(s) remaining"


templates.env.globals["humanize_access"] = humanize_access
