"""Prismark Operations System: application entry point."""
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import config
from .db import Base, SessionLocal, engine
from .deps import Forbidden, FormError, LoginRequired, NotFound, PasswordChangeRequired, templates
from .permissions import AREAS, LEVEL_LABELS, ROLES
from .uploads import DOC_TYPES

log = logging.getLogger("prismark")

app = FastAPI(title="Prismark Operations", docs_url=None, redoc_url=None, openapi_url=None)
if config.TRUSTED_HOSTS != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.TRUSTED_HOSTS)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                                    "font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
                                    "form-action 'self'")
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "same-origin"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    if config.COOKIE_SECURE:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not request.url.path.startswith("/static"):
        h["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")


# ------------------------------------------------------------------ template filters
def tzs(value, prefix: bool = True) -> str:
    if value is None:
        return "-"
    v = Decimal(value)
    s = f"{v:,.0f}"
    return f"TZS {s}" if prefix else s


def money(value, currency: str = "TZS") -> str:
    if value is None:
        return "-"
    v = Decimal(value)
    return f"{currency} {v:,.2f}" if currency != "TZS" else f"TZS {v:,.0f}"


def num(value) -> str:
    if value is None:
        return "-"
    v = Decimal(value)
    return f"{v:,.0f}" if v == v.to_integral() else f"{v:,.2f}"


def eat(value, fmt: str = "%d %b %Y, %H:%M") -> str:
    """Stored in UTC, shown in East Africa Time."""
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return (value + timedelta(hours=3)).strftime(fmt)
    return value.strftime("%d %b %Y")


def dfmt(value) -> str:
    if value is None:
        return "-"
    return value.strftime("%d %b %Y")


def extract(key, container):
    return container.get(key, key)


templates.env.filters.update(extract=extract, tzs=tzs, money=money, num=num, eat=eat, dfmt=dfmt)
templates.env.globals.update(ROLES=ROLES, AREAS=AREAS, LEVEL_LABELS=LEVEL_LABELS, DOC_TYPES=DOC_TYPES,
                             COMPANY=config.COMPANY_NAME)


# ------------------------------------------------------------------ error handling
def _back(request: Request) -> str:
    ref = request.headers.get("referer", "")
    if ref:
        parsed = urlparse(ref)
        if parsed.netloc == request.url.netloc:
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            # strip a previous message from the query
            path = path.split("msg=")[0].rstrip("?&")
            return path or "/"
    return "/"


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    nxt = request.url.path if request.method == "GET" else "/"
    resp = RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
    resp.delete_cookie("pm_session", path="/")
    return resp


@app.exception_handler(PasswordChangeRequired)
async def _pw_required(request: Request, exc: PasswordChangeRequired):
    return RedirectResponse("/account/password", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    return templates.TemplateResponse(request, "error.html", {"request": request, "ctx": None, "code": 403,
                                      "title": "Access restricted", "message": exc.message}, status_code=403)


@app.exception_handler(NotFound)
async def _not_found(request: Request, exc: NotFound):
    return templates.TemplateResponse(request, "error.html", {"request": request, "ctx": None, "code": 404,
                                      "title": "Not found", "message": "This record does not exist."}, status_code=404)


@app.exception_handler(404)
async def _404(request: Request, exc):
    return templates.TemplateResponse(request, "error.html", {"request": request, "ctx": None, "code": 404,
                                      "title": "Page not found", "message": "This page does not exist."}, status_code=404)


@app.exception_handler(FormError)
async def _form_error(request: Request, exc: FormError):
    back = _back(request)
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}msg={quote(exc.message)}&k=err", status_code=303)


@app.exception_handler(Exception)
async def _server_error(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)  # details go to the server log only
    return HTMLResponse("<h1>Something went wrong</h1><p>The error was logged. Please go back and try again.</p>",
                        status_code=500)


# ------------------------------------------------------------------ routers
from .routers import (admin, auth, customers, dashboard, expenses, files, finance, hr, orders,  # noqa: E402
                      reports, tasks, tax)

templates.env.globals["approvals_count"] = dashboard.approvals_count

for r in (auth, dashboard, customers, orders, finance, expenses, reports, tasks, hr, tax, admin, files):
    app.include_router(r.router)


@app.on_event("startup")
def _startup():
    Base.metadata.create_all(engine)
    _bootstrap()


def _bootstrap():
    """Create demo data or the first super admin when the database is empty."""
    from sqlalchemy import func, select

    from .models import Permission, User
    from .security import hash_password, password_problems

    db = SessionLocal()
    try:
        if db.scalar(select(func.count(User.id))):
            return
        if config.SEED_DEMO:
            from .demo import seed
            seed()
            return
        username, password = config.BOOTSTRAP_ADMIN_USERNAME, config.BOOTSTRAP_ADMIN_PASSWORD
        if not username or not password:
            return
        problems = password_problems(password, username)
        if problems:
            log.error("BOOTSTRAP_ADMIN_PASSWORD rejected: needs %s", ", ".join(problems))
            return
        u = User(username=username, full_name=config.BOOTSTRAP_ADMIN_NAME, password_hash=hash_password(password),
                 is_superadmin=True, must_change_password=True, job_title="Managing Director")
        u.permissions = [Permission(area=a, level="edit") for a in AREAS]
        db.add(u)
        db.commit()
        log.warning("First super admin '%s' created. Remove BOOTSTRAP_ADMIN_PASSWORD from the host settings.", username)
    finally:
        db.close()
