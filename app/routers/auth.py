import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..deps import SESSION_COOKIE, Ctx, F, FormError, audit, get_ctx, redirect, render, safe_next
from ..models import AuditLog, LoginAttempt, User, UserSession
from ..security import (hash_password, ip_rate_limited, new_session_token, password_problems,
                        register_failed_login, tokens_match, verify_password)

router = APIRouter()
PRE_COOKIE = "pm_pre"


def _login_page(request: Request, error: str | None = None, status: int = 200):
    token = secrets.token_urlsafe(24)
    resp = render(None, "login.html", request=request, status=status, error=error, pre_token=token,
                  next=safe_next(request.query_params.get("next")))
    resp.set_cookie(PRE_COOKIE, token, httponly=True, secure=config.COOKIE_SECURE, samesite="strict",
                    max_age=1800, path="/login")
    return resp


@router.get("/login")
def login_page(request: Request):
    return _login_page(request)


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    if not tokens_match(form.get("pre_token"), request.cookies.get(PRE_COOKIE)):
        return _login_page(request, "Your login form expired. Please try again.", 400)
    ip = request.client.host if request.client else "unknown"
    username = str(form.get("username") or "").strip().lower()[:40]
    password = str(form.get("password") or "")[:200]

    if ip_rate_limited(db, ip):
        return _login_page(request, "Too many failed attempts from this network. Wait a minute and try again.", 429)

    user = db.scalar(select(User).where(User.username == username))
    now = datetime.utcnow()
    locked = user is not None and user.locked_until is not None and user.locked_until > now
    ok = verify_password(user, password) and user is not None and user.is_active and not locked

    db.add(LoginAttempt(ip=ip, username=username, success=ok))
    if not ok:
        if user is not None and not locked:
            register_failed_login(db, user)
        db.add(AuditLog(user_id=user.id if user else None, action="login_failed",
                        summary=f"Failed login for '{username}'" + (" (account locked)" if locked else ""), ip=ip))
        db.commit()
        msg = "Incorrect username or password."
        if locked:
            msg = "This account is temporarily locked after repeated failed attempts. Try later or ask the MD to unlock it."
        return _login_page(request, msg, 401)

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    raw, token_hash = new_session_token()
    sess = UserSession(token_hash=token_hash, csrf_token=secrets.token_urlsafe(32), user_id=user.id, ip=ip,
                       user_agent=(request.headers.get("user-agent") or "")[:255])
    db.add(sess)
    db.flush()
    db.add(AuditLog(user_id=user.id, session_id=sess.id, action="login", summary="Signed in", ip=ip))
    db.commit()

    target = "/account/password" if user.must_change_password else safe_next(form.get("next"))
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(SESSION_COOKIE, raw, httponly=True, secure=config.COOKIE_SECURE, samesite="lax", path="/")
    resp.delete_cookie(PRE_COOKIE, path="/login")
    return resp


@router.post("/logout")
async def logout(ctx: Ctx = Depends(get_ctx)):
    ctx.session.ended_at = datetime.utcnow()
    ctx.session.end_reason = "logout"
    audit(ctx, "logout", summary="Signed out")
    ctx.db.commit()
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/account/password")
def password_page(ctx: Ctx = Depends(get_ctx)):
    return render(ctx, "change_password.html")


@router.post("/account/password")
async def change_password(ctx: Ctx = Depends(get_ctx)):
    f = F(await ctx.request.form())
    current = str(f.form.get("current") or "")
    new = str(f.form.get("new") or "")
    confirm = str(f.form.get("confirm") or "")
    if not verify_password(ctx.user, current):
        raise FormError("Your current password is not correct.")
    if new != confirm:
        raise FormError("The new passwords do not match.")
    if new == current:
        raise FormError("Choose a password different from the current one.")
    problems = password_problems(new, ctx.user.username)
    if problems:
        raise FormError("Password needs: " + ", ".join(problems) + ".")
    ctx.user.password_hash = hash_password(new)
    ctx.user.must_change_password = False
    # End every other session of this user.
    for s in ctx.db.scalars(select(UserSession).where(UserSession.user_id == ctx.user.id,
                                                      UserSession.ended_at.is_(None),
                                                      UserSession.id != ctx.session.id)):
        s.ended_at = datetime.utcnow()
        s.end_reason = "password_changed"
    audit(ctx, "password_change", "user", ctx.user.id, "Changed own password")
    ctx.db.commit()
    return redirect("/", "Password updated.")
