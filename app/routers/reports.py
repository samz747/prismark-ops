from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from ..deps import Ctx, F, Forbidden, FormError, audit, get_ctx, redirect, render
from ..models import (Activity, AuditLog, DailyReport, Milestone, MonthlyPlan, Order, Payment, StageLog, Target,
                      Task, TaskAssignee, User)
from ..services import (METRICS, MONEY_METRICS, current_period, docs_for, get_or_404, metric_value, month_bounds,
                        month_days_label, plan_assessment, shift_period, staff_users, valid_period, week_bounds)

router = APIRouter()


# ------------------------------------------------------------------ plans & targets
@router.get("/plans")
def plans(ctx: Ctx = Depends(get_ctx)):
    ctx.require("reports")
    period = valid_period(ctx.request.query_params.get("period"))
    plan = ctx.db.scalar(select(MonthlyPlan).where(MonthlyPlan.period == period))
    see_all = ctx.can("reports", "view")
    rows, milestones, docs = [], [], []
    if plan:
        rows = plan_assessment(ctx.db, plan, None if see_all else ctx.user.id)
        milestones = [m for m in plan.milestones if see_all or m.owner_id == ctx.user.id]
        docs = docs_for(ctx.db, "plan", [plan.id]).get(plan.id, [])
    company_rows = [r for r in rows if r["who"] is None]
    people = {}
    for r in rows:
        if r["who"] is not None:
            people.setdefault(r["who"].full_name, []).append(r)
    return render(ctx, "reports/plans.html", page_title="Plans & targets", plan=plan, period=period,
                  period_label=month_days_label(period), prev=shift_period(period, -1), next=shift_period(period, 1),
                  company_rows=company_rows, people=people, milestones=milestones, docs=docs, see_all=see_all,
                  METRICS=METRICS, staff=staff_users(ctx.db))


@router.post("/plans")
async def create_plan(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    f = F(await ctx.request.form())
    period = valid_period(f.str("period", True, 7))
    if ctx.db.scalar(select(MonthlyPlan).where(MonthlyPlan.period == period)):
        raise FormError("A plan for this month already exists.")
    plan = MonthlyPlan(period=period, title=f.str("title", True, 200, "Title"), summary=f.text("summary"),
                       created_by_id=ctx.user.id)
    ctx.db.add(plan)
    ctx.db.flush()
    if f.bool("copy_previous"):
        prev = ctx.db.scalar(select(MonthlyPlan).where(MonthlyPlan.period == shift_period(period, -1)))
        if prev:
            for t in prev.targets:
                ctx.db.add(Target(plan_id=plan.id, metric=t.metric, user_id=t.user_id, value=t.value))
    audit(ctx, "create", "plan", plan.id, f"Created plan for {period}")
    ctx.db.commit()
    return redirect(f"/plans?period={period}", "Plan created. Add targets, milestones and upload the plan document.")


@router.post("/plans/{pid}")
async def update_plan(pid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    plan = get_or_404(ctx.db, MonthlyPlan, pid)
    f = F(await ctx.request.form())
    old = plan.summary
    plan.title = f.str("title", True, 200, "Title")
    plan.summary = f.text("summary")
    audit(ctx, "update", "plan", pid, f"Edited plan {plan.period}", {"summary": [old, plan.summary]} if old != plan.summary else None)
    ctx.db.commit()
    return redirect(f"/plans?period={plan.period}", "Plan saved.")


@router.post("/plans/{pid}/targets")
async def add_target(pid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    plan = get_or_404(ctx.db, MonthlyPlan, pid)
    f = F(await ctx.request.form())
    metric = f.choice("metric", METRICS.keys(), label="measure")
    uid = f.int("user_id")
    if uid and not ctx.db.get(User, uid):
        uid = None
    value = f.money("value", True, False, "Target")
    existing = next((t for t in plan.targets if t.metric == metric and t.user_id == uid), None)
    if existing:
        old = existing.value
        existing.value = value
        audit(ctx, "update", "target", existing.id, f"Changed target {METRICS[metric]}", {"value": [old, value]})
    else:
        ctx.db.add(Target(plan_id=pid, metric=metric, user_id=uid, value=value))
        audit(ctx, "create", "target", pid, f"Added target {METRICS[metric]} = {value}")
    ctx.db.commit()
    return redirect(f"/plans?period={plan.period}", "Target saved.")


@router.post("/targets/{tid}/delete")
async def delete_target(tid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    t = get_or_404(ctx.db, Target, tid)
    period = t.plan.period
    audit(ctx, "delete", "target", tid, f"Removed target {METRICS.get(t.metric)}", {"value": [t.value, None]})
    ctx.db.delete(t)
    ctx.db.commit()
    return redirect(f"/plans?period={period}", "Target removed.")


@router.post("/plans/{pid}/milestones")
async def add_milestone(pid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    plan = get_or_404(ctx.db, MonthlyPlan, pid)
    f = F(await ctx.request.form())
    uid = f.int("owner_id")
    ctx.db.add(Milestone(plan_id=pid, title=f.str("title", True, 200, "Milestone"),
                         owner_id=uid if uid and ctx.db.get(User, uid) else None,
                         due_date=f.date("due_date", True, "Due date")))
    audit(ctx, "create", "milestone", pid, f"Added milestone to {plan.period}")
    ctx.db.commit()
    return redirect(f"/plans?period={plan.period}", "Milestone added.")


@router.post("/milestones/{mid}/toggle")
async def toggle_milestone(mid: int, ctx: Ctx = Depends(get_ctx)):
    m = get_or_404(ctx.db, Milestone, mid)
    if not (ctx.is_admin or m.owner_id == ctx.user.id):
        raise Forbidden()
    m.done_at = None if m.done_at else datetime.utcnow()
    audit(ctx, "update", "milestone", mid, f"Milestone '{m.title}' marked {'done' if m.done_at else 'open'}")
    ctx.db.commit()
    return redirect(f"/plans?period={m.plan.period}", "Milestone updated.")


# ------------------------------------------------------------------ daily reports
def system_record(db, user_id: int, day: date) -> dict:
    start = datetime.combine(day, datetime.min.time()) - timedelta(hours=3)
    end = start + timedelta(days=1)
    acts = db.scalars(select(Activity).where(Activity.user_id == user_id, Activity.on_date == day)).all()
    done = db.scalars(select(TaskAssignee).where(TaskAssignee.user_id == user_id, TaskAssignee.done_at >= start,
                                                 TaskAssignee.done_at < end)).all()
    stages = db.scalars(select(StageLog).where(StageLog.user_id == user_id, StageLog.at >= start, StageLog.at < end)).all()
    pays = db.scalars(select(Payment).where(Payment.recorded_by_id == user_id, Payment.recorded_at >= start,
                                            Payment.recorded_at < end)).all()
    changes = db.scalar(select(func.count(AuditLog.id)).where(AuditLog.user_id == user_id, AuditLog.at >= start,
                                                              AuditLog.at < end, AuditLog.action.notin_(["view", "login", "logout", "download"]))) or 0
    return {"activities": acts, "tasks_done": done, "stages": stages, "payments": pays, "changes": changes}


def _workdays(start: date, end: date) -> int:
    return sum(1 for k in range((end - start).days) if (start + timedelta(days=k)).weekday() < 6)


@router.get("/reports")
def reports(ctx: Ctx = Depends(get_ctx)):
    ctx.require("reports")
    q = ctx.request.query_params
    see_all = ctx.can("reports", "view")
    if not see_all:
        mine = ctx.db.scalars(select(DailyReport).where(DailyReport.user_id == ctx.user.id)
                              .order_by(DailyReport.on_date.desc()).limit(30)).all()
        today_report = next((r for r in mine if r.on_date == date.today()), None)
        return render(ctx, "reports/mine.html", page_title="My daily reports", reports=mine, today_report=today_report,
                      record=system_record(ctx.db, ctx.user.id, date.today()))

    view = q.get("view", "summary")
    scope = q.get("scope", "week")
    anchor = date.today()
    try:
        anchor = date.fromisoformat(q.get("date")) if q.get("date") else anchor
    except ValueError:
        pass
    if scope == "month":
        start, end = month_bounds(anchor.strftime("%Y-%m"))
        label = month_days_label(anchor.strftime("%Y-%m"))
        prev_d, next_d = start - timedelta(days=1), end
    else:
        start, end = week_bounds(anchor)
        label = f"Week of {start.strftime('%d %b %Y')}"
        prev_d, next_d = start - timedelta(days=7), end
    people = staff_users(ctx.db)
    summary = []
    for u in people:
        reps = ctx.db.scalar(select(func.count(DailyReport.id)).where(DailyReport.user_id == u.id,
                                                                     DailyReport.on_date >= start, DailyReport.on_date < end)) or 0
        tasks_done = ctx.db.scalar(select(func.count(TaskAssignee.id)).where(
            TaskAssignee.user_id == u.id, TaskAssignee.done_at >= datetime.combine(start, datetime.min.time()),
            TaskAssignee.done_at < datetime.combine(end, datetime.min.time()))) or 0
        summary.append({"user": u, "reports": reps, "workdays": _workdays(start, min(end, date.today() + timedelta(days=1))),
                        "activities": metric_value(ctx.db, "activities", start, end, u.id),
                        "revenue": metric_value(ctx.db, "revenue", start, end, u.id),
                        "collections": metric_value(ctx.db, "collections", start, end, u.id),
                        "new_customers": metric_value(ctx.db, "new_customers", start, end, u.id),
                        "orders_won": metric_value(ctx.db, "orders_won", start, end, u.id), "tasks_done": tasks_done})
    stmt = select(DailyReport).where(DailyReport.on_date >= start, DailyReport.on_date < end).order_by(
        DailyReport.on_date.desc(), DailyReport.submitted_at.desc())
    if q.get("user", "").isdigit():
        stmt = stmt.where(DailyReport.user_id == int(q.get("user")))
    rows = ctx.db.scalars(stmt).all()
    plan = ctx.db.scalar(select(MonthlyPlan).where(MonthlyPlan.period == anchor.strftime("%Y-%m")))
    plan_rows = plan_assessment(ctx.db, plan) if plan else []
    return render(ctx, "reports/team.html", page_title="Staff reports", view=view, scope=scope, label=label,
                  summary=summary, rows=rows, q=q, prev=prev_d.isoformat(), next=next_d.isoformat(), people=people,
                  plan_rows=plan_rows, plan=plan)


@router.post("/reports")
async def submit_report(ctx: Ctx = Depends(get_ctx)):
    ctx.require("reports")
    if ctx.is_admin:
        raise Forbidden("The Managing Director receives reports and does not submit them.")
    f = F(await ctx.request.form())
    day = f.date("on_date") or date.today()
    if day > date.today() or day < date.today() - timedelta(days=3):
        raise FormError("Reports can be submitted for today or the last three days only.")
    r = ctx.db.scalar(select(DailyReport).where(DailyReport.user_id == ctx.user.id, DailyReport.on_date == day))
    if r and r.status == "reviewed":
        raise FormError("This report was already reviewed by the MD and is locked.")
    if not r:
        r = DailyReport(user_id=ctx.user.id, on_date=day)
        ctx.db.add(r)
    r.achievements = f.text("achievements", True, "What you achieved")
    r.challenges = f.text("challenges")
    r.plan_next = f.text("plan_next")
    r.submitted_at = datetime.utcnow()
    ctx.db.flush()
    audit(ctx, "submit", "daily_report", r.id, f"Daily report for {day}")
    ctx.db.commit()
    return redirect("/reports", "Report submitted.")


@router.get("/reports/{rid}")
def report_detail(rid: int, ctx: Ctx = Depends(get_ctx)):
    r = get_or_404(ctx.db, DailyReport, rid)
    if not (ctx.can("reports", "view") or r.user_id == ctx.user.id):
        raise Forbidden()
    return render(ctx, "reports/detail.html", page_title=f"Daily report · {r.user.full_name}", r=r,
                  record=system_record(ctx.db, r.user_id, r.on_date))


@router.post("/reports/{rid}/review")
async def review_report(rid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    r = get_or_404(ctx.db, DailyReport, rid)
    f = F(await ctx.request.form())
    r.md_comment = f.text("md_comment")
    r.status = "reviewed"
    r.reviewed_by_id = ctx.user.id
    audit(ctx, "review", "daily_report", rid, f"Reviewed report of {r.user.full_name} for {r.on_date}")
    ctx.db.commit()
    return redirect("/reports?view=reports", "Report reviewed.")
