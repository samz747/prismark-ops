import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from ..deps import Ctx, F, FormError, audit, get_ctx, redirect, render
from ..models import AuditLog, Permission, Setting, User, UserSession
from ..permissions import AREAS, AREA_HELP, LEVELS, ROLES, template_for_roles
from ..security import generate_temp_password, hash_password
from ..services import DEFAULT_SETTINGS, SETTING_LABELS, get_or_404, parse_bands, setting

router = APIRouter(prefix="/admin")
USERNAME_RE = re.compile(r"^[a-z0-9._]{3,40}$")


def _active_admins(ctx: Ctx) -> int:
    return ctx.db.scalar(select(func.count(User.id)).where(User.is_superadmin.is_(True), User.is_active.is_(True))) or 0


@router.get("/users")
def users(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    rows = ctx.db.scalars(select(User).order_by(User.is_active.desc(), User.full_name)).all()
    online = {s.user_id for s in ctx.db.scalars(select(UserSession).where(
        UserSession.ended_at.is_(None), UserSession.last_seen_at > datetime.utcnow() - timedelta(minutes=30)))}
    return render(ctx, "admin/users.html", users=rows, online=online, now=datetime.utcnow())


@router.get("/users/new")
def new_user(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    return render(ctx, "admin/user_form.html", u=None, perms={a: "none" for a in AREAS}, LEVELS=LEVELS,
                  AREA_HELP=AREA_HELP, templates_json={r: template_for_roles([r]) for r in ROLES})


def _apply_perms(ctx: Ctx, user: User, f: F) -> dict:
    changes = {}
    current = {p.area: p for p in user.permissions}
    for area in AREAS:
        lvl = f.choice(f"perm_{area}", LEVELS, "none")
        if area in current:
            if current[area].level != lvl:
                changes[area] = [current[area].level, lvl]
                current[area].level = lvl
        else:
            user.permissions.append(Permission(area=area, level=lvl))
            changes[area] = [None, lvl]
    return changes


@router.post("/users/new")
async def create_user(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    f = F(await ctx.request.form())
    username = (f.str("username", True, 40) or "").lower()
    if not USERNAME_RE.match(username):
        raise FormError("Username: 3 to 40 characters using lower case letters, numbers, dot or underscore.")
    if ctx.db.scalar(select(User.id).where(User.username == username)):
        raise FormError("That username is already taken.")
    roles = [r for r in f.list("roles") if r in ROLES]
    temp = generate_temp_password()
    user = User(username=username, full_name=f.str("full_name", True, 120, "Full name"),
                email=f.str("email", max_len=160), phone=f.str("phone", max_len=40),
                job_title=f.str("job_title", max_len=80), roles=",".join(roles),
                is_superadmin=f.bool("is_superadmin"), password_hash=hash_password(temp),
                must_change_password=True, created_by_id=ctx.user.id)
    ctx.db.add(user)
    ctx.db.flush()
    _apply_perms(ctx, user, f)
    audit(ctx, "create", "user", user.id, f"Created user {username} ({', '.join(ROLES[r] for r in roles) or 'no role'})"
          + (" as super admin" if user.is_superadmin else ""))
    ctx.db.commit()
    return render(ctx, "admin/user_created.html", u=user, temp=temp)


@router.get("/users/{uid}")
def edit_user(uid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    u = get_or_404(ctx.db, User, uid)
    perms = {a: "none" for a in AREAS} | {p.area: p.level for p in u.permissions}
    sessions = ctx.db.scalars(select(UserSession).where(UserSession.user_id == uid)
                              .order_by(UserSession.login_at.desc()).limit(10)).all()
    return render(ctx, "admin/user_form.html", u=u, perms=perms, LEVELS=LEVELS, AREA_HELP=AREA_HELP,
                  sessions=sessions, templates_json={r: template_for_roles([r]) for r in ROLES})


@router.post("/users/{uid}")
async def update_user(uid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    u = get_or_404(ctx.db, User, uid)
    f = F(await ctx.request.form())
    before = {"full_name": u.full_name, "email": u.email, "phone": u.phone, "job_title": u.job_title,
              "roles": u.roles, "is_superadmin": u.is_superadmin}
    u.full_name = f.str("full_name", True, 120, "Full name")
    u.email = f.str("email", max_len=160)
    u.phone = f.str("phone", max_len=40)
    u.job_title = f.str("job_title", max_len=80)
    u.roles = ",".join(r for r in f.list("roles") if r in ROLES)
    make_admin = f.bool("is_superadmin")
    if u.is_superadmin and not make_admin and _active_admins(ctx) <= 1:
        raise FormError("At least one active super admin must remain.")
    if u.id == ctx.user.id and not make_admin:
        raise FormError("You cannot remove your own super admin rights.")
    u.is_superadmin = make_admin
    changes = {k: [v, getattr(u, k)] for k, v in before.items() if str(v) != str(getattr(u, k))}
    perm_changes = _apply_perms(ctx, u, f)
    if perm_changes:
        changes["access"] = perm_changes
    audit(ctx, "update", "user", u.id, f"Updated user {u.username}", changes or None)
    ctx.db.commit()
    return redirect(f"/admin/users/{uid}", "User saved.")


@router.post("/users/{uid}/action")
async def user_action(uid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    u = get_or_404(ctx.db, User, uid)
    f = F(await ctx.request.form())
    act = f.choice("action", ("reset_password", "unlock", "deactivate", "activate", "end_sessions"))
    now = datetime.utcnow()

    def end_all(reason):
        for s in ctx.db.scalars(select(UserSession).where(UserSession.user_id == uid, UserSession.ended_at.is_(None))):
            s.ended_at, s.end_reason = now, reason

    if act == "reset_password":
        temp = generate_temp_password()
        u.password_hash = hash_password(temp)
        u.must_change_password = True
        u.failed_logins, u.locked_until = 0, None
        end_all("revoked")
        audit(ctx, "reset_password", "user", uid, f"Reset password for {u.username}")
        ctx.db.commit()
        return render(ctx, "admin/user_created.html", u=u, temp=temp, reset=True)
    if act == "unlock":
        u.failed_logins, u.locked_until = 0, None
        msg = "Account unlocked."
    elif act == "deactivate":
        if u.id == ctx.user.id:
            raise FormError("You cannot deactivate your own account.")
        if u.is_superadmin and _active_admins(ctx) <= 1:
            raise FormError("At least one active super admin must remain.")
        u.is_active = False
        end_all("revoked")
        msg = "User deactivated and signed out."
    elif act == "activate":
        u.is_active = True
        msg = "User activated."
    else:
        end_all("revoked")
        msg = "All sessions ended."
    audit(ctx, act, "user", uid, f"{act.replace('_', ' ').capitalize()}: {u.username}")
    ctx.db.commit()
    return redirect(f"/admin/users/{uid}", msg)


@router.get("/activity")
def activity(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    q = ctx.request.query_params
    stmt = select(AuditLog).order_by(AuditLog.at.desc())
    uid = q.get("user")
    if uid and uid.isdigit():
        stmt = stmt.where(AuditLog.user_id == int(uid))
    sid = q.get("session")
    if sid and sid.isdigit():
        stmt = stmt.where(AuditLog.session_id == int(sid))
    action = q.get("action")
    if action == "changes":
        stmt = stmt.where(AuditLog.action.notin_(["view", "download"]))
    elif action:
        stmt = stmt.where(AuditLog.action == action[:40])
    day = q.get("day")
    if day:
        try:
            d = date.fromisoformat(day)
            start = datetime.combine(d, datetime.min.time()) - timedelta(hours=3)
            stmt = stmt.where(AuditLog.at >= start, AuditLog.at < start + timedelta(days=1))
        except ValueError:
            pass
    page = max(1, int(q.get("page", "1")) if q.get("page", "1").isdigit() else 1)
    rows = ctx.db.scalars(stmt.limit(100).offset((page - 1) * 100)).all()
    sessions = ctx.db.scalars(select(UserSession).order_by(UserSession.login_at.desc()).limit(40)).all()
    if uid and uid.isdigit():
        sessions = [s for s in sessions if s.user_id == int(uid)]
    users = ctx.db.scalars(select(User).order_by(User.full_name)).all()
    return render(ctx, "admin/activity.html", rows=rows, sessions=sessions, users=users, page=page, q=q,
                  now=datetime.utcnow())


@router.get("/settings")
def settings_page(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    values = {k: setting(ctx.db, k) for k in DEFAULT_SETTINGS}
    return render(ctx, "admin/settings.html", values=values, labels=SETTING_LABELS)


@router.post("/settings")
async def save_settings(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    f = F(await ctx.request.form())
    changes = {}
    for key in DEFAULT_SETTINGS:
        val = f.str(key, True, 200, SETTING_LABELS[key])
        if key == "paye_bands":
            try:
                parse_bands(val)
            except Exception:
                raise FormError("PAYE bands format: 270000:0,520000:8,760000:20,1000000:25,*:30")
        elif key == "sdl_applies":
            if val not in ("0", "1"):
                raise FormError("SDL applies must be 0 or 1.")
        else:
            try:
                float(val)
            except ValueError:
                raise FormError(f"{SETTING_LABELS[key]} must be a number.")
        row = ctx.db.get(Setting, key)
        old = row.value if row else DEFAULT_SETTINGS[key]
        if old != val:
            changes[key] = [old, val]
        if row:
            row.value = val
        else:
            ctx.db.add(Setting(key=key, value=val))
    audit(ctx, "update", "settings", None, "Updated system settings", changes or None)
    ctx.db.commit()
    return redirect("/admin/settings", "Settings saved.")
