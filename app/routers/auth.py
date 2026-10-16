import re

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Role, User
from app.security import (
    MAX_PASSWORD_BYTES,
    get_current_user,
    hash_password,
    login_user,
    logout_user,
    safe_next_url,
    verify_password,
)
from app.templating import flash, templates

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/signup")
def signup_page(request: Request, user: User | None = Depends(get_current_user)):
    if user:
        return _redirect("/dashboard")
    return templates.TemplateResponse(request, "signup.html", {"form": {}, "errors": []})


@router.post("/signup")
def signup(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    role: str = Form(...),
    db: Session = Depends(get_db),
):
    name, email = name.strip(), email.strip().lower()
    errors = []
    if not name:
        errors.append("Name is required.")
    if not EMAIL_RE.match(email):
        errors.append("Enter a valid email address.")
    if role not in {r.value for r in Role}:
        errors.append("Choose whether you are a teacher or a student.")
    if len(password) < MIN_PASSWORD_LEN:
        errors.append(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        errors.append(f"Password must be at most {MAX_PASSWORD_BYTES} bytes.")
    if password != confirm_password:
        errors.append("Passwords do not match.")
    if not errors and db.scalar(select(User.id).where(User.email == email)):
        errors.append("An account with this email already exists.")

    if errors:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"form": {"name": name, "email": email, "role": role}, "errors": errors},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user = User(name=name, email=email, password_hash=hash_password(password), role=Role(role))
    db.add(user)
    db.commit()
    login_user(request, user)
    flash(request, f"Welcome, {user.name}!", "success")
    return _redirect("/dashboard")


@router.get("/login")
def login_page(request: Request, next: str | None = None, user: User | None = Depends(get_current_user)):
    if user:
        return _redirect(safe_next_url(next))
    return templates.TemplateResponse(request, "login.html", {"email": "", "next": next or "", "error": None})


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"email": email, "next": next, "error": "Invalid email or password."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    login_user(request, user)
    return _redirect(safe_next_url(next))


@router.post("/logout")
def logout(request: Request):
    logout_user(request)
    flash(request, "You have been logged out.", "info")
    return _redirect("/login")
