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


templates.env.globals.update(app_name=settings.app_name, get_flashed_messages=get_flashed_messages)
templates.env.filters["timestamp"] = format_timestamp
