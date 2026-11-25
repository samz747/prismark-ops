import csv
import io
from decimal import Decimal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select

from ..deps import Ctx, audit, get_ctx, render
from ..models import Expense, Invoice, OrderCost, Payment, PayrollRun
from ..services import (D0, docs_for, month_bounds, month_days_label, setting_dec, shift_period, valid_period)

router = APIRouter(prefix="/tax")


def _data(ctx: Ctx, period: str) -> dict:
    db = ctx.db
    s, e = month_bounds(period)
    invoices = db.scalars(select(Invoice).where(Invoice.issue_date >= s, Invoice.issue_date < e).order_by(Invoice.issue_date)).all()
    idocs = docs_for(db, "invoice", [i.id for i in invoices])
    sales = [{"date": i.issue_date, "number": i.number, "customer": i.customer.company_name, "tin": i.customer.tin or "",
              "vrn": i.customer.vrn or "", "net": Decimal(i.subtotal) * Decimal(i.exchange_rate), "vat": i.vat_tzs,
              "doc": any(d.doc_type == "tax_invoice" for d in idocs.get(i.id, [])), "link": f"/finance/invoices/{i.id}"}
             for i in invoices]
    purchases = []
    costs = db.scalars(select(OrderCost).where(OrderCost.on_date >= s, OrderCost.on_date < e, OrderCost.vat_amount > 0)).all()
    cdocs = docs_for(db, "order_cost", [c.id for c in costs])
    for c in costs:
        purchases.append({"date": c.on_date, "ref": c.order.ref, "supplier": c.supplier or c.category,
                          "net": c.amount_tzs - Decimal(c.vat_amount), "vat": Decimal(c.vat_amount),
                          "doc": bool(cdocs.get(c.id)), "link": f"/orders/{c.order_id}#costs"})
    exps = db.scalars(select(Expense).where(Expense.on_date >= s, Expense.on_date < e, Expense.status == "approved",
                                            Expense.vat_amount > 0)).all()
    edocs = docs_for(db, "expense", [x.id for x in exps])
    for x in exps:
        purchases.append({"date": x.on_date, "ref": f"EXP-{x.id:04d}", "supplier": x.payee or x.category,
                          "net": x.amount_tzs - Decimal(x.vat_amount), "vat": Decimal(x.vat_amount),
                          "doc": bool(edocs.get(x.id)), "link": f"/expenses/{x.id}"})
    purchases.sort(key=lambda r: r["date"])
    output_vat = sum((r["vat"] for r in sales), D0)
    input_vat_all = sum((r["vat"] for r in purchases), D0)
    input_vat_supported = sum((r["vat"] for r in purchases if r["doc"]), D0)
    wht = sum((Decimal(p.withholding_tax or 0) for p in db.scalars(select(Payment).where(
        Payment.status == "confirmed", Payment.paid_on >= s, Payment.paid_on < e))), D0)
    run = db.scalar(select(PayrollRun).where(PayrollRun.period == period))
    payroll = None
    if run:
        payroll = {k: sum((Decimal(getattr(l, k)) for l in run.lines), D0)
                   for k in ("gross", "paye", "nssf_employee", "nssf_employer", "sdl", "wcf")}
        payroll["status"] = run.status
    # corporate tax estimate on the calendar year to date
    year_start = f"{period[:4]}-01"
    ytd_profit = D0
    p = year_start
    while p <= period:
        ps, pe = month_bounds(p)
        rev = sum((Decimal(i.subtotal) * Decimal(i.exchange_rate) for i in db.scalars(
            select(Invoice).where(Invoice.issue_date >= ps, Invoice.issue_date < pe))), D0)
        cogs = sum((c.amount_tzs - Decimal(c.vat_amount or 0) for c in db.scalars(
            select(OrderCost).where(OrderCost.on_date >= ps, OrderCost.on_date < pe))), D0)
        opex = sum((x.amount_tzs - Decimal(x.vat_amount or 0) for x in db.scalars(
            select(Expense).where(Expense.on_date >= ps, Expense.on_date < pe, Expense.status == "approved"))), D0)
        r = db.scalar(select(PayrollRun).where(PayrollRun.period == p, PayrollRun.status == "approved"))
        pay = sum((Decimal(l.gross) + Decimal(l.nssf_employer) + Decimal(l.sdl) + Decimal(l.wcf) for l in r.lines), D0) if r else D0
        ytd_profit += rev - cogs - opex - pay
        p = shift_period(p, 1)
    cit_rate = setting_dec(db, "corporate_tax")
    return {"sales": sales, "purchases": purchases, "output_vat": output_vat, "input_vat_all": input_vat_all,
            "input_vat_supported": input_vat_supported, "net_vat": output_vat - input_vat_supported, "wht": wht,
            "payroll": payroll, "ytd_profit": ytd_profit, "cit_rate": cit_rate,
            "cit_estimate": max(D0, ytd_profit) * cit_rate / 100}


@router.get("")
def tax(ctx: Ctx = Depends(get_ctx)):
    ctx.require("tax", "view")
    period = valid_period(ctx.request.query_params.get("period"))
    return render(ctx, "tax.html", page_title="Tax & VAT", period=period, period_label=month_days_label(period),
                  prev=shift_period(period, -1), next=shift_period(period, 1), d=_data(ctx, period))


def _safe(v) -> str:
    """Neutralise spreadsheet formula injection in exported cells."""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@router.get("/export/{kind}")
def export(kind: str, ctx: Ctx = Depends(get_ctx)):
    ctx.require("tax", "view")
    period = valid_period(ctx.request.query_params.get("period"))
    d = _data(ctx, period)
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "sales":
        w.writerow(["Date", "Invoice", "Customer", "TIN", "VRN", "Net TZS", "VAT TZS", "Tax invoice attached"])
        for r in d["sales"]:
            w.writerow([r["date"], _safe(r["number"]), _safe(r["customer"]), _safe(r["tin"]), _safe(r["vrn"]),
                        f"{r['net']:.2f}", f"{r['vat']:.2f}", "Yes" if r["doc"] else "No"])
    else:
        kind = "purchases"
        w.writerow(["Date", "Reference", "Supplier", "Net TZS", "VAT TZS", "Receipt attached"])
        for r in d["purchases"]:
            w.writerow([r["date"], _safe(r["ref"]), _safe(r["supplier"]), f"{r['net']:.2f}", f"{r['vat']:.2f}",
                        "Yes" if r["doc"] else "No"])
    audit(ctx, "export", "tax", None, f"Exported {kind} VAT schedule for {period}")
    ctx.db.commit()
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="prismark-{kind}-vat-{period}.csv"'})
