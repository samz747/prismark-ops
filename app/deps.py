"""Request context: authentication, CSRF, access checks, audit logging, form parsing and rendering."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import config
from .db import get_db
from .models import AuditLog, User, UserSession
from .permissions import AREAS, LEVEL_RANK, level_of
from .security import hash_token, tokens_match

SESSION_COOKIE = "pm_session"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ------------------------------------------------------------------ errors
class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    def __init__(self, message: str = "You do not have access to this area."):
        self.message = message


class NotFound(Exception):
    pass


class PasswordChangeRequired(Exception):
    pass


class FormError(Exception):
    def __init__(self, message: str):
        self.message = message


# ------------------------------------------------------------------ context
@dataclass
class Ctx:
    request: Request
    db: Session
    user: User
    session: UserSession
    flashes: list = field(default_factory=list)

    @property
    def csrf(self) -> str:
        return self.session.csrf_token

    def level(self, area: str) -> str:
        return level_of(self.user, area)

    def can(self, area: str, minimum: str = "own") -> bool:
        return LEVEL_RANK[self.level(area)] >= LEVEL_RANK[minimum]

    @property
    def is_admin(self) -> bool:
        return self.user.is_superadmin

    def require(self, area: str, minimum: str = "own") -> None:
        if not self.can(area, minimum):
            raise Forbidden()

    def require_admin(self) -> None:
        if not self.user.is_superadmin:
            raise Forbidden("Only the Managing Director (super admin) can do this.")

    def ip(self) -> str:
        return self.request.client.host if self.request.client else "unknown"


def _end_session(db: Session, sess: UserSession, reason: str) -> None:
    sess.ended_at = datetime.utcnow()
    sess.end_reason = reason
    db.commit()


async def get_ctx(request: Request, db: Session = Depends(get_db)) -> Ctx:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise LoginRequired()
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(raw))
                     .options(selectinload(UserSession.user).selectinload(User.permissions)))
    if sess is None or sess.ended_at is not None:
        raise LoginRequired()
    now = datetime.utcnow()
    if now - sess.last_seen_at > timedelta(minutes=config.SESSION_IDLE_MINUTES):
        _end_session(db, sess, "idle")
        raise LoginRequired()
    if now - sess.login_at > timedelta(hours=config.SESSION_MAX_HOURS):
        _end_session(db, sess, "expired")
        raise LoginRequired()
    if not sess.user.is_active:
        _end_session(db, sess, "revoked")
        raise LoginRequired()

    sess.last_seen_at = now
    ctx = Ctx(request=request, db=db, user=sess.user, session=sess)

    if request.method == "POST":
        form = await request.form()
        token = form.get("csrf_token") or request.headers.get("x-csrf-token")
        if not tokens_match(token, sess.csrf_token):
            db.commit()
            raise Forbidden("Your form expired or was not sent from this system. Reload the page and try again.")
    elif not request.url.path.startswith(("/static", "/files")):
        db.add(AuditLog(user_id=sess.user_id, session_id=sess.id, action="view",
                        summary=str(request.url.path)[:300], ip=ctx.ip()))

    # Users with a temporary password must change it before anything else.
    if sess.user.must_change_password and request.url.path not in ("/account/password", "/logout"):
        db.commit()
        raise PasswordChangeRequired()
    db.commit()
    return ctx


def audit(ctx: Ctx, action: str, entity: str | None = None, entity_id: int | None = None,
          summary: str | None = None, changes: dict | None = None) -> None:
    ctx.db.add(AuditLog(user_id=ctx.user.id, session_id=ctx.session.id, action=action, entity=entity,
                        entity_id=entity_id, summary=(summary or "")[:300],
                        changes=json.dumps(changes, default=str) if changes else None, ip=ctx.ip()))


def snapshot(obj, fields: list[str]) -> dict:
    return {f: getattr(obj, f) for f in fields}


def diff(before: dict, after_obj) -> dict:
    out = {}
    for f, old in before.items():
        new = getattr(after_obj, f)
        if str(old) != str(new):
            out[f] = [old, new]
    return out


# ------------------------------------------------------------------ form parsing (explicit fields only)
class F:
    """Wraps submitted form data and validates each field explicitly."""

    def __init__(self, form):
        self.form = form

    def str(self, name: str, required: bool = False, max_len: int = 300, label: str | None = None) -> str | None:
        raw = self.form.get(name)
        val = raw.strip() if isinstance(raw, str) else ""
        if required and not val:
            raise FormError(f"{label or name.replace('_', ' ').capitalize()} is required.")
        if len(val) > max_len:
            raise FormError(f"{label or name} is too long (max {max_len} characters).")
        return val or None

    def text(self, name: str, required: bool = False, label: str | None = None) -> str | None:
        return self.str(name, required, 5000, label)

    def choice(self, name: str, options, default=None, label: str | None = None):
        val = (self.form.get(name) or "").strip()
        if not val:
            return default
        if val not in options:
            raise FormError(f"Invalid value for {label or name}.")
        return val

    def int(self, name: str, required: bool = False, min_v: int | None = None, label: str | None = None):
        val = (self.form.get(name) or "").strip()
        if not val:
            if required:
                raise FormError(f"{label or name} is required.")
            return None
        try:
            num = int(val)
        except ValueError:
            raise FormError(f"{label or name} must be a whole number.")
        if min_v is not None and num < min_v:
            raise FormError(f"{label or name} must be at least {min_v}.")
        return num

    def money(self, name: str, required: bool = False, allow_zero: bool = True, label: str | None = None):
        val = (self.form.get(name) or "").replace(",", "").strip()
        if not val:
            if required:
                raise FormError(f"{label or name.replace('_', ' ').capitalize()} is required.")
            return Decimal(0)
        try:
            num = Decimal(val)
        except InvalidOperation:
            raise FormError(f"{label or name} must be a number.")
        if not num.is_finite() or num < 0 or (num == 0 and not allow_zero) or num > Decimal("1e15"):
            raise FormError(f"{label or name} must be a positive amount.")
        return num.quantize(Decimal("0.01"))

    def date(self, name: str, required: bool = False, label: str | None = None) -> date | None:
        val = (self.form.get(name) or "").strip()
        if not val:
            if required:
                raise FormError(f"{label or name.replace('_', ' ').capitalize()} is required.")
            return None
        try:
            return date.fromisoformat(val)
        except ValueError:
            raise FormError(f"{label or name} must be a valid date.")

    def bool(self, name: str) -> bool:
        return self.form.get(name) in ("1", "on", "true", "yes")

    def list(self, name: str) -> list[str]:
        return [v for v in self.form.getlist(name) if isinstance(v, str)]

    def currency(self, prefix: str = "") -> tuple[str, Decimal]:
        cur = self.choice(f"{prefix}currency", ("TZS", "USD"), "TZS")
        if cur == "TZS":
            return "TZS", Decimal(1)
        rate = self.money(f"{prefix}exchange_rate", required=True, allow_zero=False, label="Exchange rate")
        return "USD", rate


# ------------------------------------------------------------------ rendering
def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.state.flash = (message, kind)


def render(ctx: Ctx | None, template: str, request: Request | None = None, status: int = 200, **data) -> HTMLResponse:
    req = request or ctx.request
    base = {"request": req, "ctx": ctx, "AREAS": AREAS, "today": date.today(),
            "flash_msg": req.query_params.get("msg"), "flash_kind": req.query_params.get("k", "ok")}
    base.update(data)
    return templates.TemplateResponse(req, template, base, status_code=status)


def redirect(url: str, msg: str | None = None, kind: str = "ok") -> RedirectResponse:
    if msg:
        from urllib.parse import quote
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}msg={quote(msg)}&k={kind}"
    return RedirectResponse(url, status_code=303)


def safe_next(url: str | None) -> str:
    """Only allow local redirect targets (prevents open redirects)."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return "/"
