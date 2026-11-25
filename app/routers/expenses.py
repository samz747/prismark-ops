from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..deps import Ctx, F, Forbidden, FormError, audit, get_ctx, redirect, render, safe_next
from ..models import Customer, Expense, Order
from ..services import (ACQUISITION_CATEGORIES, D0, EXPENSE_CATEGORIES, access, check, customer_scope, docs_for,
                        expense_scope, get_or_404, month_bounds, month_days_label, order_scope, owners, setting,
                        shift_period, valid_period)
from ..uploads import save_upload

router = APIRouter(prefix="/expenses")


@router.get("")
def list_expenses(ctx: Ctx = Depends(get_ctx)):
    ctx.require("expenses")
    q = ctx.request.query_params
    period = valid_period(q.get("period"))
    s, e = month_bounds(period)
    stmt = expense_scope(ctx, select(Expense).where(Expense.on_date >= s, Expense.on_date < e)).order_by(Expense.on_date.desc())
    if q.get("category") in EXPENSE_CATEGORIES:
        stmt = stmt.where(Expense.category == q.get("category"))
    if q.get("status") in ("pending", "approved", "rejected"):
        stmt = stmt.where(Expense.status == q.get("status"))
    rows = ctx.db.scalars(stmt).all()
    docs = docs_for(ctx.db, "expense", [x.id for x in rows])

    # six month trend by category with a simple 3 month moving average forecast
    trend_periods = [shift_period(period, -k) for k in range(5, -1, -1)]
    table = defaultdict(lambda: defaultdict(lambda: D0))
    for p in trend_periods:
        ps, pe = month_bounds(p)
        for x in ctx.db.scalars(expense_scope(ctx, select(Expense).where(Expense.on_date >= ps, Expense.on_date < pe,
                                                                         Expense.status != "rejected"))):
            table[x.category][p] += x.amount_tzs
    trend = []
    for cat in sorted(table, key=lambda c: -sum(table[c].values())):
        vals = [table[cat][p] for p in trend_periods]
        forecast = sum(vals[-3:], D0) / 3
        trend.append({"category": cat, "values": vals, "forecast": forecast, "total": sum(vals, D0)})
    month_totals = [sum((t["values"][i] for t in trend), D0) for i in range(6)]
    total_forecast = sum((t["forecast"] for t in trend), D0)

    customers = ctx.db.scalars(customer_scope(ctx, select(Customer)).order_by(Customer.company_name)).all() if ctx.can("customers") else []
    orders = ctx.db.scalars(order_scope(ctx, select(Order).where(Order.stage != "paid")).order_by(Order.created_at.desc())).all() if ctx.can("orders") else []
    return render(ctx, "expenses/list.html", page_title="Expenses", rows=rows, docs=docs, q=q, period=period,
                  period_label=month_days_label(period), prev=shift_period(period, -1), next=shift_period(period, 1),
                  CATEGORIES=EXPENSE_CATEGORIES, ACQ=ACQUISITION_CATEGORIES, trend=trend,
                  trend_labels=[month_days_label(p)[:3] for p in trend_periods], month_totals=month_totals,
                  total_forecast=total_forecast, customers=customers, orders=orders,
                  usd_rate=setting(ctx.db, "usd_rate"), total=sum((x.amount_tzs for x in rows if x.status != "rejected"), D0),
                  can_submit=ctx.level("expenses") in ("own", "edit"))


@router.post("")
async def create_expense(ctx: Ctx = Depends(get_ctx)):
    if ctx.level("expenses") not in ("own", "edit"):
        raise Forbidden()
    form = await ctx.request.form()
    f = F(form)
    cur, rate = f.currency()
    x = Expense(on_date=f.date("on_date") or date.today(), category=f.choice("category", EXPENSE_CATEGORIES, "Other", "category"),
                description=f.str("description", True, 300, "Description"), payee=f.str("payee", max_len=160),
                amount=f.money("amount", True, False, "Amount"), currency=cur, exchange_rate=rate,
                vat_amount=f.money("vat_amount"), submitted_by_id=ctx.user.id, status="pending")
    cid = f.int("customer_id")
    if cid:
        c = get_or_404(ctx.db, Customer, cid)
        if not access(ctx, "customers", [c.assigned_sales_id]) and not ctx.level("expenses") == "edit":
            raise Forbidden()
        x.customer_id = cid
    oid = f.int("order_id")
    if oid:
        o = get_or_404(ctx.db, Order, oid)
        x.order_id = o.id
        x.customer_id = x.customer_id or o.customer_id
    if ctx.is_admin:
        x.status, x.approved_by_id, x.approved_at = "approved", ctx.user.id, datetime.utcnow()
    ctx.db.add(x)
    ctx.db.flush()
    await save_upload(ctx, form.get("file"), "expense", x.id, "receipt")
    audit(ctx, "create", "expense", x.id, f"Expense {x.category}: {cur} {x.amount:,.2f}")
    ctx.db.commit()
    return redirect("/expenses", "Expense recorded." + ("" if ctx.is_admin else " Waiting for MD approval."))


@router.get("/{eid}")
def expense_detail(eid: int, ctx: Ctx = Depends(get_ctx)):
    x = get_or_404(ctx.db, Expense, eid)
    check(ctx, "expenses", owners(x))
    return render(ctx, "expenses/detail.html", page_title=f"Expense EXP-{x.id:04d}", x=x,
                  docs=docs_for(ctx.db, "expense", [eid]).get(eid, []),
                  can_write=access(ctx, "expenses", owners(x), write=True))


@router.post("/{eid}/review")
async def review(eid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    x = get_or_404(ctx.db, Expense, eid)
    f = F(await ctx.request.form())
    decision = f.choice("decision", ("approved", "rejected"), label="decision")
    if not decision:
        raise FormError("Choose approve or reject.")
    old = x.status
    x.status, x.approved_by_id, x.approved_at = decision, ctx.user.id, datetime.utcnow()
    x.review_note = f.str("note", max_len=300)
    audit(ctx, "review", "expense", eid, f"Expense EXP-{eid:04d} {decision}", {"status": [old, decision]})
    ctx.db.commit()
    return redirect(safe_next(f.str("back") or f"/expenses/{eid}"), f"Expense {decision}.")
