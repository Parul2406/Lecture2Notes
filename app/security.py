from urllib.parse import quote

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

# bcrypt only looks at the first 72 bytes; we reject longer passwords instead of silently truncating.
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def login_user(request: Request, user: User) -> None:
    request.session.clear()
    request.session["user_id"] = user.id


def logout_user(request: Request) -> None:
    request.session.clear()


def safe_next_url(url: str | None, default: str = "/dashboard") -> str:
    """Only allow local redirects to avoid open-redirects via ?next=."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return default


class LoginRequired(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None:
        request.session.clear()
    return user


def require_user(request: Request, user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
        next_url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise LoginRequired(quote(next_url))
    return user


def require_teacher(user: User = Depends(require_user)) -> User:
    if not user.is_teacher:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only teachers can do this")
    return user
