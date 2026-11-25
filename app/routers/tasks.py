import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select

from ..deps import Ctx, F, Forbidden, FormError, audit, get_ctx, redirect, render
from ..models import Customer, Notice, Task, TaskAssignee, User
from ..services import customer_scope, get_or_404, staff_users

router = APIRouter()
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PRIORITIES = ["low", "normal", "high", "urgent"]


def _can_assign(ctx: Ctx) -> bool:
    return ctx.is_admin or ctx.can("tasks", "edit")


def _time(f: F, name: str, required: bool = False):
    v = f.str(name, required, 5, name.replace("_", " "))
    if v and not TIME_RE.match(v):
        raise FormError("Times must be in HH:MM format.")
    return v


@router.get("/tasks")
def tasks(ctx: Ctx = Depends(get_ctx)):
    db = ctx.db
    q = ctx.request.query_params
    day = date.today()
    try:
        day = date.fromisoformat(q.get("day")) if q.get("day") else day
    except ValueError:
        pass
    mine = db.scalars(select(TaskAssignee).join(Task).where(TaskAssignee.user_id == ctx.user.id)
                      .order_by(TaskAssignee.status, Task.due_date)).all()
    schedule = sorted([a for a in mine if a.task.kind == "schedule" and a.task.on_date == day],
                      key=lambda a: a.task.start_time or "")
    assigned = [a for a in mine if a.task.kind == "assigned" and (a.status != "done" or (a.done_at and a.done_at > datetime.utcnow() - timedelta(days=7)))]
    todos = [a for a in mine if a.task.kind == "todo" and (a.status != "done" or (a.done_at and a.done_at > datetime.utcnow() - timedelta(days=3)))]
    by_me = []
    if _can_assign(ctx):
        by_me = db.scalars(select(Task).where(Task.kind == "assigned", Task.created_by_id == ctx.user.id)
                           .order_by(Task.created_at.desc()).limit(40)).all()
    custs = db.scalars(customer_scope(ctx, select(Customer)).order_by(Customer.company_name)).all() if ctx.can("customers") else []
    return render(ctx, "tasks/index.html", page_title="Tasks & schedule", schedule=schedule, assigned=assigned,
                  todos=todos, by_me=by_me, day=day, prev=(day - timedelta(days=1)).isoformat(),
                  next=(day + timedelta(days=1)).isoformat(), can_assign=_can_assign(ctx), staff=staff_users(db),
                  PRIORITIES=PRIORITIES, customers=custs)


@router.post("/tasks/todo")
async def add_todo(ctx: Ctx = Depends(get_ctx)):
    f = F(await ctx.request.form())
    t = Task(kind="todo", title=f.str("title", True, 200, "Task"), priority=f.choice("priority", PRIORITIES, "normal"),
             due_date=f.date("due_date"), created_by_id=ctx.user.id)
    t.assignees = [TaskAssignee(user_id=ctx.user.id)]
    ctx.db.add(t)
    ctx.db.commit()
    return redirect("/tasks", "Added to your list.")


@router.post("/tasks/schedule")
async def add_schedule(ctx: Ctx = Depends(get_ctx)):
    f = F(await ctx.request.form())
    start, end = _time(f, "start_time", True), _time(f, "end_time", True)
    if end <= start:
        raise FormError("End time must be after start time.")
    day = f.date("on_date") or date.today()
    if day < date.today() - timedelta(days=1):
        raise FormError("You can only plan today or future days.")
    cid = f.int("customer_id")
    t = Task(kind="schedule", title=f.str("title", True, 200, "Activity"), details=f.text("details"), on_date=day,
             start_time=start, end_time=end, customer_id=cid if cid and ctx.db.get(Customer, cid) else None,
             created_by_id=ctx.user.id)
    t.assignees = [TaskAssignee(user_id=ctx.user.id)]
    ctx.db.add(t)
    ctx.db.flush()
    audit(ctx, "create", "schedule", t.id, f"Planned '{t.title}' on {day} {start}-{end}")
    ctx.db.commit()
    return redirect(f"/tasks?day={day.isoformat()}", "Added to your schedule and the notice board.")


@router.post("/tasks/assign")
async def assign(ctx: Ctx = Depends(get_ctx)):
    if not _can_assign(ctx):
        raise Forbidden("Only the MD or users allowed to assign tasks can do this.")
    f = F(await ctx.request.form())
    ids = {int(v) for v in f.list("assignees") if v.isdigit()}
    users = [u for u in (ctx.db.get(User, i) for i in ids) if u and u.is_active]
    if not users:
        raise FormError("Choose at least one person.")
    t = Task(kind="assigned", title=f.str("title", True, 200, "Task"), details=f.text("details"),
             priority=f.choice("priority", PRIORITIES, "normal"), due_date=f.date("due_date"),
             on_date=f.date("on_date"), start_time=_time(f, "start_time"), end_time=_time(f, "end_time"),
             created_by_id=ctx.user.id)
    t.assignees = [TaskAssignee(user_id=u.id) for u in users]
    ctx.db.add(t)
    ctx.db.flush()
    audit(ctx, "assign", "task", t.id, f"Assigned '{t.title}' to {', '.join(u.full_name for u in users)}")
    ctx.db.commit()
    return redirect("/tasks", "Task assigned.")


@router.post("/tasks/status/{aid}")
async def set_status(aid: int, ctx: Ctx = Depends(get_ctx)):
    a = get_or_404(ctx.db, TaskAssignee, aid)
    if a.user_id != ctx.user.id and not ctx.is_admin:
        raise Forbidden()
    f = F(await ctx.request.form())
    status = f.choice("status", ("open", "in_progress", "done"), "done")
    a.status = status
    a.done_at = datetime.utcnow() if status == "done" else None
    if a.task.kind != "todo":
        audit(ctx, "update", "task", a.task_id, f"'{a.task.title}' marked {status.replace('_', ' ')}")
    ctx.db.commit()
    back = f.str("back") or "/tasks"
    return redirect(back if back.startswith("/") and not back.startswith("//") else "/tasks", "Task updated.")


@router.post("/tasks/{tid}/delete")
async def delete_task(tid: int, ctx: Ctx = Depends(get_ctx)):
    t = get_or_404(ctx.db, Task, tid)
    if not (ctx.is_admin or t.created_by_id == ctx.user.id):
        raise Forbidden()
    if t.kind != "todo":
        audit(ctx, "delete", "task", tid, f"Deleted task '{t.title}'")
    ctx.db.delete(t)
    ctx.db.commit()
    return redirect("/tasks", "Task removed.")


@router.get("/notices")
def notices(ctx: Ctx = Depends(get_ctx)):
    db = ctx.db
    today = date.today()
    items = db.scalars(select(Notice).where(or_(Notice.expires_on.is_(None), Notice.expires_on >= today))
                       .order_by(Notice.pinned.desc(), Notice.posted_at.desc())).all()
    todays = db.scalars(select(Task).where(Task.kind.in_(("schedule", "assigned")),
                                           or_(Task.on_date == today, Task.due_date == today))).all()
    board = defaultdict(list)
    for t in todays:
        for a in t.assignees:
            board[a.user].append((t, a))
    open_assigned = db.scalars(select(TaskAssignee).join(Task).where(Task.kind == "assigned", TaskAssignee.status != "done")).all()
    for a in open_assigned:
        if a.task not in [t for t, _ in board[a.user]]:
            board[a.user].append((a.task, a))
    board = sorted(((u, sorted(v, key=lambda x: x[0].start_time or "99")) for u, v in board.items()),
                   key=lambda x: x[0].full_name)
    return render(ctx, "tasks/notices.html", page_title="Notice board", notices=items, board=board,
                  can_post=_can_assign(ctx))


@router.post("/notices")
async def post_notice(ctx: Ctx = Depends(get_ctx)):
    if not _can_assign(ctx):
        raise Forbidden()
    f = F(await ctx.request.form())
    n = Notice(title=f.str("title", True, 200, "Title"), body=f.text("body", True, "Message"), pinned=f.bool("pinned"),
               expires_on=f.date("expires_on"), posted_by_id=ctx.user.id)
    ctx.db.add(n)
    ctx.db.flush()
    audit(ctx, "create", "notice", n.id, f"Posted notice '{n.title}'")
    ctx.db.commit()
    return redirect("/notices", "Notice posted.")


@router.post("/notices/{nid}/delete")
async def delete_notice(nid: int, ctx: Ctx = Depends(get_ctx)):
    n = get_or_404(ctx.db, Notice, nid)
    if not (ctx.is_admin or n.posted_by_id == ctx.user.id):
        raise Forbidden()
    audit(ctx, "delete", "notice", nid, f"Removed notice '{n.title}'")
    ctx.db.delete(n)
    ctx.db.commit()
    return redirect("/notices", "Notice removed.")
