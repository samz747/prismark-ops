from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..deps import Ctx, F, Forbidden, FormError, audit, get_ctx, redirect, render
from ..models import DailyReport, Expense, Invoice, Order, OrderCost, Payment, PayrollRun
from ..services import (D0, access, ageing, check, current_period, docs_for, get_or_404, invoice_scope,
                        month_bounds, month_days_label, owners, setting, settled_balance, settled_tzs,
                        shift_period, sync_order_payment_stage, valid_period)
from ..uploads import save_upload

router = APIRouter()
METHODS = ["Bank transfer", "Cheque", "Cash", "Mobile money", "Letter of credit", "Other"]


@router.get("/finance")
def invoices(ctx: Ctx = Depends(get_ctx)):
    ctx.require("finance")
    q = ctx.request.query_params
    rows = ctx.db.scalars(invoice_scope(ctx, select(Invoice)).order_by(Invoice.issue_date.desc())).all()
    status = q.get("status", "")
    today = date.today()

    def state(i):
        bal = settled_balance(i)
        if bal <= 1:
            return "paid"
        if i.pending_tzs > 0:
            return "pending"
        return "overdue" if i.due_date < today else "unpaid"

    data = [(i, state(i), settled_balance(i)) for i in rows]
    if status:
        data = [d for d in data if d[1] == status]
    if q.get("q"):
        term = q.get("q").lower()[:60]
        data = [d for d in data if term in d[0].number.lower() or term in d[0].customer.company_name.lower()]
    buckets = ageing(rows)
    pending = ctx.db.scalars(select(Payment).where(Payment.status == "pending")).all()
    pending = [p for p in pending if access(ctx, "finance", owners(p))]
    return render(ctx, "finance/invoices.html", page_title="Invoices & payments", data=data, q=q, buckets=buckets,
                  total_out=sum(buckets.values(), D0), pending=pending)


@router.get("/finance/invoices/{iid}")
def invoice_detail(iid: int, ctx: Ctx = Depends(get_ctx)):
    inv = get_or_404(ctx.db, Invoice, iid)
    check(ctx, "finance", owners(inv))
    docs = docs_for(ctx.db, "invoice", [iid]).get(iid, [])
    pdocs = docs_for(ctx.db, "payment", [p.id for p in inv.payments])
    return render(ctx, "finance/invoice.html", page_title=inv.number, inv=inv, docs=docs, pdocs=pdocs,
                  balance=settled_balance(inv), settled=settled_tzs(inv), METHODS=METHODS,
                  can_write=access(ctx, "finance", owners(inv), write=True),
                  has_tax_invoice=any(d.doc_type == "tax_invoice" for d in docs), usd_rate=setting(ctx.db, "usd_rate"))


@router.post("/finance/invoices/{iid}/payments")
async def record_payment(iid: int, ctx: Ctx = Depends(get_ctx)):
    inv = get_or_404(ctx.db, Invoice, iid)
    check(ctx, "finance", owners(inv), write=True)
    docs = docs_for(ctx.db, "invoice", [iid]).get(iid, [])
    if not any(d.doc_type == "tax_invoice" for d in docs):
        raise FormError("Upload the tax invoice / EFD receipt before recording payment. The invoiced stage cannot close without it.")
    form = await ctx.request.form()
    f = F(form)
    cur, rate = f.currency()
    p = Payment(invoice_id=iid, amount=f.money("amount", True, False, "Amount"), currency=cur, exchange_rate=rate,
                withholding_tax=f.money("withholding_tax"), paid_on=f.date("paid_on") or date.today(),
                method=f.choice("method", METHODS, "Bank transfer", "method"), reference=f.str("reference", max_len=80),
                status="pending", recorded_by_id=ctx.user.id)
    ctx.db.add(p)
    ctx.db.flush()
    await save_upload(ctx, form.get("file"), "payment", p.id, "payment_proof")
    ctx.db.refresh(inv)
    sync_order_payment_stage(inv.order)
    audit(ctx, "create", "payment", p.id, f"Recorded payment {cur} {p.amount:,.2f} on {inv.number}; waiting for MD confirmation")
    ctx.db.commit()
    return redirect(f"/finance/invoices/{iid}", "Payment recorded. It shows as pending until the MD confirms receipt.")


@router.post("/finance/payments/{pid}/review")
async def review_payment(pid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    p = get_or_404(ctx.db, Payment, pid)
    f = F(await ctx.request.form())
    decision = f.choice("decision", ("confirmed", "rejected"), label="decision")
    if not decision or p.status != "pending":
        raise FormError("This payment was already reviewed.")
    p.status = decision
    p.confirmed_by_id = ctx.user.id
    p.confirmed_at = datetime.utcnow()
    p.review_note = f.str("note", max_len=300)
    ctx.db.flush()
    sync_order_payment_stage(p.invoice.order)
    audit(ctx, "review", "payment", pid, f"Payment on {p.invoice.number} {decision}: {p.currency} {p.amount:,.2f}")
    ctx.db.commit()
    return redirect(f.str("back") if (f.str("back") or "").startswith("/") else "/approvals", f"Payment {decision}.")


@router.get("/approvals")
def approvals(ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    db = ctx.db
    payments = db.scalars(select(Payment).where(Payment.status == "pending").order_by(Payment.recorded_at)).all()
    expenses = db.scalars(select(Expense).where(Expense.status == "pending").order_by(Expense.submitted_at)).all()
    runs = db.scalars(select(PayrollRun).where(PayrollRun.status == "draft")).all()
    reports = db.scalars(select(DailyReport).where(DailyReport.status == "submitted")
                         .order_by(DailyReport.on_date.desc()).limit(20)).all()
    pdocs = docs_for(db, "payment", [p.id for p in payments])
    edocs = docs_for(db, "expense", [e.id for e in expenses])
    return render(ctx, "finance/approvals.html", page_title="Approvals", payments=payments, expenses=expenses,
                  runs=runs, reports=reports, pdocs=pdocs, edocs=edocs)


@router.get("/finance/pnl")
def pnl(ctx: Ctx = Depends(get_ctx)):
    ctx.require("finance", "view")
    db = ctx.db
    end_period = valid_period(ctx.request.query_params.get("period"))
    months = []
    for k in range(5, -1, -1):
        p = shift_period(end_period, -k)
        s, e = month_bounds(p)
        invs = db.scalars(select(Invoice).where(Invoice.issue_date >= s, Invoice.issue_date < e)).all()
        revenue = sum((Decimal(i.subtotal) * Decimal(i.exchange_rate) for i in invs), D0)
        costs = db.scalars(select(OrderCost).where(OrderCost.on_date >= s, OrderCost.on_date < e)).all()
        cogs = sum((c.amount_tzs - Decimal(c.vat_amount or 0) for c in costs), D0)
        exps = db.scalars(select(Expense).where(Expense.on_date >= s, Expense.on_date < e, Expense.status == "approved")).all()
        opex = sum((e_.amount_tzs - Decimal(e_.vat_amount or 0) for e_ in exps), D0)
        run = db.scalar(select(PayrollRun).where(PayrollRun.period == p, PayrollRun.status == "approved"))
        payroll = sum((Decimal(l.gross) + Decimal(l.nssf_employer) + Decimal(l.sdl) + Decimal(l.wcf) for l in run.lines), D0) if run else D0
        collected = sum((pay.amount_tzs + Decimal(pay.withholding_tax or 0) for pay in db.scalars(
            select(Payment).where(Payment.status == "confirmed", Payment.paid_on >= s, Payment.paid_on < e))), D0)
        gross = revenue - cogs
        months.append({"period": p, "label": month_days_label(p), "revenue": revenue, "cogs": cogs, "gross": gross,
                       "opex": opex, "payroll": payroll, "net": gross - opex - payroll, "collected": collected})
    totals = {k: sum((m[k] for m in months), D0) for k in ("revenue", "cogs", "gross", "opex", "payroll", "net", "collected")}
    return render(ctx, "finance/pnl.html", page_title="Profit & loss", months=months, totals=totals, period=end_period,
                  prev=shift_period(end_period, -6), next=shift_period(end_period, 6),
                  show_next=end_period < current_period())
