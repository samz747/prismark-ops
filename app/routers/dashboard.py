from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from ..deps import Ctx, get_ctx, render
from ..models import (Activity, Customer, DailyReport, Expense, Invoice, MonthlyPlan, Order, Payment, PayrollRun,
                      Task, TaskAssignee, User)
from ..services import (D0, METRICS, MONEY_METRICS, STAGE_LABEL, assess, customer_scope, invoice_scope,
                        metric_value, missing_proofs, month_bounds, order_revenue, order_scope, sales_users,
                        settled_balance, shift_period, current_period, month_days_label)

router = APIRouter()


def approvals_count(db) -> int:
    return ((db.scalar(select(func.count(Payment.id)).where(Payment.status == "pending")) or 0)
            + (db.scalar(select(func.count(Expense.id)).where(Expense.status == "pending")) or 0)
            + (db.scalar(select(func.count(PayrollRun.id)).where(PayrollRun.status == "draft")) or 0))


@router.get("/")
def dashboard(ctx: Ctx = Depends(get_ctx)):
    db = ctx.db
    company = ctx.can("dashboard", "view")
    uid = None if company else ctx.user.id
    period = current_period()
    start, end = month_bounds(period)

    revenue = metric_value(db, "revenue", start, end, uid)
    collections = metric_value(db, "collections", start, end, uid)
    gp = metric_value(db, "gross_profit", start, end, uid)

    invoices = db.scalars(invoice_scope(ctx, select(Invoice))).all() if ctx.can("finance") else []
    if not company:
        invoices = [i for i in invoices if i.order.sales_id == ctx.user.id or i.customer.assigned_sales_id == ctx.user.id]
    outstanding = sum((max(D0, settled_balance(i)) for i in invoices), D0)
    overdue = sum((max(D0, settled_balance(i)) for i in invoices if i.due_date < date.today()), D0)

    orders = db.scalars(order_scope(ctx, select(Order).where(Order.is_cancelled.is_(False)))).all() if ctx.can("orders") else []
    if not company:
        orders = [o for o in orders if ctx.user.id in (o.sales_id, o.logistics_id)]
    open_orders = [o for o in orders if o.stage not in ("paid",)]
    pipeline = sum((order_revenue(o) for o in open_orders if o.stage not in ("invoiced", "payment_pending")), D0)
    stage_counts = {}
    for o in open_orders:
        stage_counts[o.stage] = stage_counts.get(o.stage, 0) + 1

    # revenue target for the month
    plan = db.scalar(select(MonthlyPlan).where(MonthlyPlan.period == period))
    target = None
    if plan:
        for t in plan.targets:
            if t.metric == "revenue" and t.user_id == uid:
                target = assess(Decimal(t.value), revenue, period, True)
    milestones = [m for m in (plan.milestones if plan else []) if company or m.owner_id == ctx.user.id]

    # six month trend
    trend = []
    for k in range(5, -1, -1):
        p = shift_period(period, -k)
        s, e = month_bounds(p)
        trend.append({"label": month_days_label(p)[:3], "value": metric_value(db, "revenue", s, e, uid),
                      "coll": metric_value(db, "collections", s, e, uid)})
    peak = max([t["value"] for t in trend] + [t["coll"] for t in trend] + [Decimal(1)])

    # sales leaderboard (company view)
    leaders = []
    if company:
        for su in sales_users(db):
            leaders.append({"user": su, "revenue": metric_value(db, "revenue", start, end, su.id),
                            "collections": metric_value(db, "collections", start, end, su.id),
                            "activities": metric_value(db, "activities", start, end, su.id)})
        leaders.sort(key=lambda r: r["revenue"], reverse=True)

    # follow ups and quiet customers
    follow_ups, quiet = [], []
    if ctx.can("customers"):
        cust_stmt = customer_scope(ctx, select(Customer).where(Customer.status != "inactive"))
        if not company:
            cust_stmt = cust_stmt.where(Customer.assigned_sales_id == ctx.user.id)
        custs = db.scalars(cust_stmt).all()
        ids = [c.id for c in custs]
        if ids:
            follow_ups = db.scalars(select(Activity).where(Activity.customer_id.in_(ids),
                                                           Activity.next_action_date.isnot(None),
                                                           Activity.next_action_date <= date.today() + timedelta(days=3))
                                    .order_by(Activity.next_action_date).limit(8)).all()
            last = dict(db.execute(select(Activity.customer_id, func.max(Activity.on_date))
                                   .where(Activity.customer_id.in_(ids)).group_by(Activity.customer_id)).all())
            cutoff = date.today() - timedelta(days=30)
            quiet = sorted([c for c in custs if (last.get(c.id) or date(2000, 1, 1)) < cutoff],
                           key=lambda c: last.get(c.id) or date(2000, 1, 1))[:6]
            quiet = [(c, last.get(c.id)) for c in quiet]

    my_tasks = db.scalars(select(TaskAssignee).join(Task).where(
        TaskAssignee.user_id == ctx.user.id, TaskAssignee.status != "done",
        ((Task.on_date == date.today()) | (Task.due_date <= date.today() + timedelta(days=2)) | (Task.kind == "assigned"))
    ).limit(8)).all()

    proofs = missing_proofs(ctx)
    pending_reports = 0
    if ctx.is_admin:
        pending_reports = db.scalar(select(func.count(DailyReport.id)).where(DailyReport.status == "submitted")) or 0

    from datetime import datetime
    hour = (datetime.utcnow() + timedelta(hours=3)).hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
    return render(ctx, "dashboard.html", page_title="Dashboard", company=company, greeting=greeting, period=period,
                  period_label=month_days_label(period), revenue=revenue, collections=collections, gp=gp,
                  outstanding=outstanding, overdue=overdue, pipeline=pipeline, open_orders=len(open_orders),
                  stage_counts=stage_counts, STAGE_LABEL=STAGE_LABEL, target=target, milestones=milestones,
                  trend=trend, peak=peak, leaders=leaders, follow_ups=follow_ups, quiet=quiet, my_tasks=my_tasks,
                  proofs=proofs, approvals=approvals_count(db) if ctx.is_admin else 0, pending_reports=pending_reports)


@router.get("/proofs")
def proofs(ctx: Ctx = Depends(get_ctx)):
    rows = missing_proofs(ctx)
    return render(ctx, "proofs.html", page_title="Paper trail check", rows=rows)
