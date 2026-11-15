from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, settings

templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("_flashes", []).append([category, message])


def get_flashed_messages(request: Request) -> list[list[str]]:
    return request.session.pop("_flashes", [])


def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


templates.env.globals.update(
    app_name=settings.app_name,
    get_flashed_messages=get_flashed_messages,
    # Limits shown in forms; globals so no page can break by forgetting to pass them.
    max_upload_mb=settings.max_upload_mb,
    max_download_hours=settings.max_download_hours,
)
templates.env.filters["timestamp"] = format_timestamp
