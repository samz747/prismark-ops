"""Business rules: settings, access scoping, order stages, paper trail checks, metrics and payroll."""
import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .deps import Ctx, Forbidden, NotFound
from .models import (Activity, Allowance, Customer, DailyReport, Document, Expense, Invoice, Order,
                     OrderCost, Payment, Setting, Target, TaskAssignee, User)
from .permissions import LEVEL_RANK

D0 = Decimal(0)

# ------------------------------------------------------------------ settings
DEFAULT_SETTINGS = {
    "vat_rate": "18",
    "usd_rate": "2650",
    "nssf_employee": "10",
    "nssf_employer": "10",
    "sdl_rate": "3.5",
    "sdl_applies": "1",
    "wcf_rate": "0.5",
    "paye_bands": "270000:0,520000:8,760000:20,1000000:25,*:30",
    "corporate_tax": "30",
    "tier_a": "50000000",
    "tier_b": "10000000",
}

SETTING_LABELS = {
    "vat_rate": "VAT rate (%)",
    "usd_rate": "Default USD to TZS rate",
    "nssf_employee": "NSSF employee (%)",
    "nssf_employer": "NSSF employer (%)",
    "sdl_rate": "SDL rate (%)",
    "sdl_applies": "SDL applies (1 = yes, 0 = no; required from 10 employees)",
    "wcf_rate": "WCF rate (%)",
    "paye_bands": "PAYE monthly bands (upper limit:rate, * = above)",
    "corporate_tax": "Corporate income tax (%)",
    "tier_a": "Tier A customer: revenue in last 12 months from (TZS)",
    "tier_b": "Tier B customer: revenue in last 12 months from (TZS)",
}


def setting(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else DEFAULT_SETTINGS[key]


def setting_dec(db: Session, key: str) -> Decimal:
    try:
        return Decimal(setting(db, key))
    except Exception:
        return Decimal(DEFAULT_SETTINGS[key])


def q2(x) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ access scoping
def access(ctx: Ctx, area: str, owner_ids: list[int | None], write: bool = False) -> bool:
    lvl = ctx.level(area)
    if lvl == "edit":
        return True
    if lvl == "view":
        return not write
    if lvl == "own":
        return ctx.user.id in [o for o in owner_ids if o]
    return False


def check(ctx: Ctx, area: str, owner_ids: list[int | None], write: bool = False) -> None:
    if not access(ctx, area, owner_ids, write):
        raise Forbidden()


def get_or_404(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if obj is None:
        raise NotFound()
    return obj


def owners(obj) -> list[int | None]:
    if isinstance(obj, Customer):
        return [obj.assigned_sales_id]
    if isinstance(obj, Order):
        return [obj.sales_id, obj.logistics_id]
    if isinstance(obj, Invoice):
        return [obj.order.sales_id, obj.customer.assigned_sales_id]
    if isinstance(obj, Payment):
        return owners(obj.invoice)
    if isinstance(obj, Expense):
        return [obj.submitted_by_id]
    if isinstance(obj, OrderCost):
        return owners(obj.order)
    return []


def customer_scope(ctx: Ctx, stmt):
    if ctx.level("customers") == "own":
        return stmt.where(Customer.assigned_sales_id == ctx.user.id)
    return stmt


def order_scope(ctx: Ctx, stmt):
    if ctx.level("orders") == "own":
        return stmt.where(or_(Order.sales_id == ctx.user.id, Order.logistics_id == ctx.user.id))
    return stmt


def invoice_scope(ctx: Ctx, stmt):
    if ctx.level("finance") == "own":
        return stmt.join(Order, Invoice.order_id == Order.id).join(Customer, Invoice.customer_id == Customer.id).where(
            or_(Order.sales_id == ctx.user.id, Customer.assigned_sales_id == ctx.user.id))
    return stmt


def expense_scope(ctx: Ctx, stmt):
    if ctx.level("expenses") == "own":
        return stmt.where(Expense.submitted_by_id == ctx.user.id)
    return stmt


def doc_access(ctx: Ctx, doc: Document) -> bool:
    db = ctx.db
    target = {"customer": (Customer, "customers"), "order": (Order, "orders"), "invoice": (Invoice, "finance"),
              "payment": (Payment, "finance"), "expense": (Expense, "expenses"),
              "order_cost": (OrderCost, "orders")}.get(doc.entity)
    if target:
        obj = db.get(target[0], doc.entity_id)
        if obj is None:
            return False
        if access(ctx, target[1], owners(obj)):
            return True
        # Finance staff may open order documents that back an invoice.
        if doc.entity in ("order", "order_cost") and ctx.can("finance", "view"):
            return True
        return False
    if doc.entity == "plan":
        return ctx.can("reports", "own")
    if doc.entity in ("allowance", "payroll", "staff"):
        return ctx.can("hr", "view")
    return ctx.is_admin


def docs_for(db: Session, entity: str, ids) -> dict[int, list[Document]]:
    ids = list(ids)
    out: dict[int, list[Document]] = defaultdict(list)
    if not ids:
        return out
    for d in db.scalars(select(Document).where(Document.entity == entity, Document.entity_id.in_(ids))
                        .order_by(Document.uploaded_at)):
        out[d.entity_id].append(d)
    return out


# ------------------------------------------------------------------ numbering
def next_number(db: Session, model, column, prefix: str, yearly: bool = True) -> str:
    year = date.today().year
    pattern = f"{prefix}-{year}-" if yearly else f"{prefix}-"
    count = db.scalar(select(func.count()).select_from(model).where(column.like(f"{pattern}%"))) or 0
    while True:
        count += 1
        candidate = f"{pattern}{count:04d}"
        if not db.scalar(select(func.count()).select_from(model).where(column == candidate)):
            return candidate


# ------------------------------------------------------------------ order stages
STAGES = [
    ("enquiry", "Enquiry / Tender"),
    ("quotation", "Quotation"),
    ("lpo", "LPO received"),
    ("sourcing", "Sourcing"),
    ("shipping", "Shipping & clearing"),
    ("delivery", "Delivery"),
    ("invoiced", "Invoiced"),
    ("payment_pending", "Pending confirmation"),
    ("paid", "Paid & confirmed"),
]
STAGE_LABEL = dict(STAGES)
STAGE_DOC = {"enquiry": "rfq", "quotation": "quotation", "lpo": "lpo", "sourcing": "supplier_invoice",
             "shipping": "shipping_docs", "delivery": "delivery_note", "invoiced": "tax_invoice"}
MANUAL_STAGES = ["enquiry", "quotation", "lpo", "sourcing", "shipping", "delivery"]


def stages_for(order: Order) -> list[str]:
    keys = [k for k, _ in STAGES]
    if order.sourcing_type == "local":
        keys.remove("shipping")
    return keys


def next_stage(order: Order) -> str | None:
    keys = stages_for(order)
    i = keys.index(order.stage) if order.stage in keys else 0
    return keys[i + 1] if i + 1 < len(keys) else None


def order_doc_types(db: Session, order: Order) -> set[str]:
    types = {d.doc_type for d in db.scalars(select(Document).where(Document.entity == "order",
                                                                    Document.entity_id == order.id))}
    inv_ids = [i.id for i in order.invoices]
    if inv_ids:
        types |= {d.doc_type for d in db.scalars(select(Document).where(Document.entity == "invoice",
                                                                         Document.entity_id.in_(inv_ids)))}
    return types


def missing_before(db: Session, order: Order, stage: str) -> list[str]:
    """Documents missing for every stage before `stage` (stage itself excluded)."""
    have = order_doc_types(db, order)
    keys = stages_for(order)
    upto = keys.index(stage) if stage in keys else len(keys)
    return [STAGE_DOC[k] for k in keys[:upto] if k in STAGE_DOC and STAGE_DOC[k] not in have]


def order_revenue(order: Order) -> Decimal:
    return sum((Decimal(i.quantity) * Decimal(i.unit_price) for i in order.items), D0) * Decimal(order.exchange_rate)


def order_cost(order: Order) -> Decimal:
    return sum((c.amount_tzs - Decimal(c.vat_amount or 0) for c in order.costs), D0)


def sync_order_payment_stage(order: Order) -> None:
    """Move the order between invoiced / pending confirmation / paid based on payments."""
    if order.stage not in ("invoiced", "payment_pending", "paid") or not order.invoices:
        return
    pending = any(p.status == "pending" for inv in order.invoices for p in inv.payments)
    settled = all(settled_balance(inv) <= Decimal("1") for inv in order.invoices)
    if settled and not pending:
        order.stage = "paid"
    elif pending:
        order.stage = "payment_pending"
    else:
        order.stage = "invoiced"


def settled_tzs(inv: Invoice) -> Decimal:
    return sum((p.amount_tzs + Decimal(p.withholding_tax or 0) for p in inv.payments if p.status == "confirmed"), D0)


def settled_balance(inv: Invoice) -> Decimal:
    return inv.total_tzs - settled_tzs(inv)


# ------------------------------------------------------------------ periods
def month_bounds(period: str) -> tuple[date, date]:
    y, m = int(period[:4]), int(period[5:7])
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    return start, end


def current_period() -> str:
    return date.today().strftime("%Y-%m")


def valid_period(p: str | None) -> str:
    try:
        if p and len(p) == 7:
            month_bounds(p)
            return p
    except ValueError:
        pass
    return current_period()


def shift_period(period: str, months: int) -> str:
    y, m = int(period[:4]), int(period[5:7])
    idx = y * 12 + (m - 1) + months
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def week_bounds(d: date) -> tuple[date, date]:
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=7)


# ------------------------------------------------------------------ metrics
METRICS = {
    "revenue": "Sales invoiced (TZS, excl. VAT)",
    "collections": "Payments collected & confirmed (TZS)",
    "gross_profit": "Gross profit (TZS)",
    "new_customers": "New customers",
    "orders_won": "Orders won (LPOs received)",
    "activities": "Customer activities logged",
}
MONEY_METRICS = {"revenue", "collections", "gross_profit"}


def metric_value(db: Session, metric: str, start: date, end: date, user_id: int | None = None) -> Decimal:
    if metric in ("revenue", "gross_profit"):
        stmt = select(Invoice).join(Order, Invoice.order_id == Order.id).where(
            Invoice.issue_date >= start, Invoice.issue_date < end)
        if user_id:
            stmt = stmt.where(Order.sales_id == user_id)
        invoices = db.scalars(stmt).all()
        if metric == "revenue":
            return sum((Decimal(i.subtotal) * Decimal(i.exchange_rate) for i in invoices), D0)
        orders = {i.order_id: i.order for i in invoices}
        return sum((order_revenue(o) - order_cost(o) for o in orders.values()), D0)
    if metric == "collections":
        stmt = (select(Payment).join(Invoice, Payment.invoice_id == Invoice.id).join(Order, Invoice.order_id == Order.id)
                .where(Payment.status == "confirmed", Payment.paid_on >= start, Payment.paid_on < end))
        if user_id:
            stmt = stmt.where(Order.sales_id == user_id)
        return sum((p.amount_tzs + Decimal(p.withholding_tax or 0) for p in db.scalars(stmt)), D0)
    if metric == "new_customers":
        stmt = select(func.count(Customer.id)).where(Customer.created_at >= datetime.combine(start, datetime.min.time()),
                                                     Customer.created_at < datetime.combine(end, datetime.min.time()))
        if user_id:
            stmt = stmt.where(Customer.assigned_sales_id == user_id)
        return Decimal(db.scalar(stmt) or 0)
    if metric == "orders_won":
        stmt = select(func.count(Order.id)).where(Order.lpo_date >= start, Order.lpo_date < end,
                                                  Order.is_cancelled.is_(False))
        if user_id:
            stmt = stmt.where(Order.sales_id == user_id)
        return Decimal(db.scalar(stmt) or 0)
    if metric == "activities":
        stmt = select(func.count(Activity.id)).where(Activity.on_date >= start, Activity.on_date < end)
        if user_id:
            stmt = stmt.where(Activity.user_id == user_id)
        return Decimal(db.scalar(stmt) or 0)
    return D0


def assess(target: Decimal, actual: Decimal, period: str, money: bool) -> dict:
    """Objective plan vs actual assessment computed purely from system records."""
    start, end = month_bounds(period)
    today = date.today()
    total_days = (end - start).days
    if today >= end:
        elapsed = total_days
    elif today < start:
        elapsed = 0
    else:
        elapsed = (today - start).days + 1
    expected_share = Decimal(elapsed) / Decimal(total_days)
    target = Decimal(target or 0)
    pct = (actual / target * 100) if target else D0
    expected = target * expected_share
    gap = actual - expected
    remaining_days = max(0, (end - max(today, start)).days) if today < end else 0
    needed = max(D0, target - actual)
    per_day = (needed / remaining_days) if remaining_days else D0
    if not money:
        per_day = per_day.to_integral_value(rounding="ROUND_CEILING")
    if target and actual >= target:
        status, tone = "Achieved", "good"
    elif not target:
        status, tone = "No target", "muted"
    elif actual >= expected:
        status, tone = "Ahead of pace", "good"
    elif expected and actual >= expected * Decimal("0.9"):
        status, tone = "On track", "ok"
    elif expected and actual >= expected * Decimal("0.6"):
        status, tone = "Behind", "warn"
    else:
        status, tone = "Off track", "bad"

    def fmt(v):
        v = Decimal(v)
        return f"TZS {v:,.0f}" if money else f"{v:,.0f}"

    if not target:
        sentence = "No target set for this measure."
    elif actual >= target:
        sentence = f"Target met: {fmt(actual)} against {fmt(target)} ({pct:.0f}%)."
    else:
        pace_word = "ahead of" if gap >= 0 else "behind"
        sentence = (f"{fmt(actual)} of {fmt(target)} achieved ({pct:.0f}%). With {elapsed} of {total_days} days gone "
                    f"the plan expects {fmt(expected)}, so this is {fmt(abs(gap))} {pace_word} pace.")
        if remaining_days and today < end:
            sentence += f" {fmt(needed)} still needed: about {fmt(per_day)} per remaining day."
        elif today >= end:
            sentence += f" The month closed {fmt(needed)} short of target."
    return {"target": target, "actual": actual, "pct": pct, "expected": expected, "gap": gap,
            "status": status, "tone": tone, "sentence": sentence, "bar": min(100, float(pct)),
            "pace_mark": float(expected_share * 100)}


def plan_assessment(db: Session, plan, user_id: int | None = None) -> list[dict]:
    start, end = month_bounds(plan.period)
    rows = []
    for t in plan.targets:
        if user_id is not None and t.user_id != user_id:
            continue
        actual = metric_value(db, t.metric, start, end, t.user_id)
        a = assess(Decimal(t.value), actual, plan.period, t.metric in MONEY_METRICS)
        a.update({"metric": t.metric, "label": METRICS.get(t.metric, t.metric), "who": t.user, "id": t.id,
                  "money": t.metric in MONEY_METRICS})
        rows.append(a)
    return rows


# ------------------------------------------------------------------ customer value
EXPENSE_CATEGORIES = [
    "Office supplies", "Printing & stationery", "Transport & fuel", "Communication & internet", "Utilities",
    "Rent", "Meals & refreshments", "Repairs & maintenance", "Bank charges", "Licences & permits",
    "Professional fees", "Marketing", "Travel & accommodation", "Tender fees", "\"Incentive\"",
    "Entertainment & dinners", "Gifts", "Other",
]
ACQUISITION_CATEGORIES = {"Tender fees", "\"Incentive\"", "Entertainment & dinners", "Gifts",
                          "Travel & accommodation", "Marketing"}


def customer_value(db: Session, c: Customer) -> dict:
    orders = [o for o in c.orders if not o.is_cancelled]
    invoices = db.scalars(select(Invoice).where(Invoice.customer_id == c.id)).all()
    revenue = sum((Decimal(i.subtotal) * Decimal(i.exchange_rate) for i in invoices), D0)
    invoiced_total = sum((i.total_tzs for i in invoices), D0)
    paid = sum((settled_tzs(i) for i in invoices), D0)
    pending = sum((i.pending_tzs for i in invoices), D0)
    outstanding = invoiced_total - paid
    invoiced_orders = {i.order_id: i.order for i in invoices}
    gp = sum((order_revenue(o) - order_cost(o) for o in invoiced_orders.values()), D0)
    pipeline = sum((order_revenue(o) for o in orders if o.stage not in ("invoiced", "payment_pending", "paid")), D0)
    exps = db.scalars(select(Expense).where(Expense.customer_id == c.id, Expense.status != "rejected")).all()
    cac = sum((e.amount_tzs for e in exps if e.category in ACQUISITION_CATEGORIES), D0)
    serve = sum((e.amount_tzs for e in exps if e.category not in ACQUISITION_CATEGORIES), D0)
    days = []
    overdue = D0
    for i in invoices:
        conf = [p for p in i.payments if p.status == "confirmed"]
        if conf and settled_balance(i) <= 1:
            days.append((max(p.paid_on for p in conf) - i.issue_date).days)
        if settled_balance(i) > 1 and i.due_date < date.today():
            overdue += settled_balance(i)
    year_ago = date.today() - timedelta(days=365)
    rev12 = sum((Decimal(i.subtotal) * Decimal(i.exchange_rate) for i in invoices if i.issue_date >= year_ago), D0)
    tier = "A" if rev12 >= setting_dec(db, "tier_a") else "B" if rev12 >= setting_dec(db, "tier_b") else "C"
    last_act = db.scalar(select(func.max(Activity.on_date)).where(Activity.customer_id == c.id))
    return {"revenue": revenue, "invoiced_total": invoiced_total, "paid": paid, "pending": pending,
            "outstanding": outstanding, "overdue": overdue, "gross_profit": gp, "pipeline": pipeline,
            "cac": cac, "cost_to_serve": serve, "net_value": gp - cac - serve,
            "avg_days_to_pay": (sum(days) / len(days)) if days else None, "orders": len(orders),
            "tier": tier if invoices else "New", "last_activity": last_act,
            "margin": (gp / revenue * 100) if revenue else None}


# ------------------------------------------------------------------ receivables
def ageing(invoices) -> dict:
    buckets = {"Not due": D0, "1-30 days": D0, "31-60 days": D0, "61-90 days": D0, "90+ days": D0}
    today = date.today()
    for inv in invoices:
        bal = settled_balance(inv)
        if bal <= 1:
            continue
        late = (today - inv.due_date).days
        key = ("Not due" if late <= 0 else "1-30 days" if late <= 30 else "31-60 days" if late <= 60
               else "61-90 days" if late <= 90 else "90+ days")
        buckets[key] += bal
    return buckets


# ------------------------------------------------------------------ paper trail
def missing_proofs(ctx: Ctx) -> list[dict]:
    db = ctx.db
    out = []
    if ctx.can("orders", "own"):
        orders = db.scalars(order_scope(ctx, select(Order).where(Order.is_cancelled.is_(False)))).all()
        for o in orders:
            keys = stages_for(o)
            idx = keys.index(o.stage) if o.stage in keys else 0
            have = order_doc_types(db, o)
            for k in keys[:idx]:
                if k in STAGE_DOC and STAGE_DOC[k] not in have and k != "invoiced":
                    out.append({"area": "Order", "ref": o.ref, "what": f"{STAGE_LABEL[k]}: document missing",
                                "link": f"/orders/{o.id}", "who": o.sales.full_name if o.sales else "-"})
        cost_ids = [c.id for o in orders for c in o.costs]
        cdocs = docs_for(db, "order_cost", cost_ids)
        for o in orders:
            for c in o.costs:
                if not cdocs.get(c.id):
                    out.append({"area": "Order cost", "ref": o.ref,
                                "what": f"{c.category} TZS {c.amount_tzs:,.0f}: receipt missing",
                                "link": f"/orders/{o.id}#costs", "who": o.sales.full_name if o.sales else "-"})
    if ctx.can("finance", "own"):
        invs = db.scalars(invoice_scope(ctx, select(Invoice))).all()
        idocs = docs_for(db, "invoice", [i.id for i in invs])
        pdocs = docs_for(db, "payment", [p.id for i in invs for p in i.payments])
        for i in invs:
            if not any(d.doc_type == "tax_invoice" for d in idocs.get(i.id, [])):
                out.append({"area": "Invoice", "ref": i.number, "what": "Tax invoice / EFD receipt missing",
                            "link": f"/finance/invoices/{i.id}", "who": i.order.sales.full_name if i.order.sales else "-"})
            for p in i.payments:
                if p.status != "rejected" and not pdocs.get(p.id):
                    out.append({"area": "Payment", "ref": i.number,
                                "what": f"Payment TZS {p.amount_tzs:,.0f} on {p.paid_on}: proof missing",
                                "link": f"/finance/invoices/{i.id}", "who": p.recorded_by.full_name})
    if ctx.can("expenses", "own"):
        exps = db.scalars(expense_scope(ctx, select(Expense).where(Expense.status != "rejected"))).all()
        edocs = docs_for(db, "expense", [e.id for e in exps])
        for e in exps:
            if not edocs.get(e.id):
                out.append({"area": "Expense", "ref": f"EXP-{e.id:04d}",
                            "what": f"{e.category} TZS {e.amount_tzs:,.0f}: receipt missing",
                            "link": f"/expenses/{e.id}", "who": e.submitted_by.full_name})
    if ctx.can("hr", "view"):
        allows = db.scalars(select(Allowance)).all()
        adocs = docs_for(db, "allowance", [a.id for a in allows])
        for a in allows:
            if not adocs.get(a.id):
                out.append({"area": "Allowance", "ref": a.period,
                            "what": f"{a.kind} TZS {Decimal(a.amount):,.0f} for {a.staff.full_name}: approval missing",
                            "link": f"/hr/allowances?period={a.period}", "who": "-"})
    return out


# ------------------------------------------------------------------ payroll
def parse_bands(text: str) -> list[tuple[Decimal | None, Decimal]]:
    bands = []
    for part in text.split(","):
        upper, rate = part.split(":")
        bands.append((None if upper.strip() == "*" else Decimal(upper.strip()), Decimal(rate.strip())))
    return bands


def paye(taxable: Decimal, bands) -> Decimal:
    tax, lower = D0, D0
    for upper, rate in bands:
        top = taxable if upper is None else min(taxable, upper)
        if top > lower:
            tax += (top - lower) * rate / 100
        if upper is None or taxable <= upper:
            break
        lower = upper
    return q2(tax)


def compute_payroll_line(db: Session, basic: Decimal, allowances: Decimal, taxable_allow: Decimal,
                         other_deductions: Decimal = D0) -> dict:
    gross = basic + allowances
    nssf_e = q2(gross * setting_dec(db, "nssf_employee") / 100)
    taxable = max(D0, basic + taxable_allow - nssf_e)
    tax = paye(taxable, parse_bands(setting(db, "paye_bands")))
    sdl = q2(gross * setting_dec(db, "sdl_rate") / 100) if setting(db, "sdl_applies") == "1" else D0
    return {"basic": q2(basic), "allowances": q2(allowances), "taxable_allowances": q2(taxable_allow),
            "gross": q2(gross), "nssf_employee": nssf_e, "taxable_income": q2(taxable), "paye": tax,
            "other_deductions": q2(other_deductions), "net": q2(gross - nssf_e - tax - other_deductions),
            "nssf_employer": q2(gross * setting_dec(db, "nssf_employer") / 100), "sdl": sdl,
            "wcf": q2(gross * setting_dec(db, "wcf_rate") / 100)}


# ------------------------------------------------------------------ people helpers
def staff_users(db: Session, include_admin: bool = False) -> list[User]:
    stmt = select(User).where(User.is_active.is_(True)).order_by(User.full_name)
    users = db.scalars(stmt).all()
    return [u for u in users if include_admin or not u.is_superadmin]


def sales_users(db: Session) -> list[User]:
    return [u for u in staff_users(db) if "sales" in u.role_list] or staff_users(db)


def open_task_count(db: Session, user_id: int) -> int:
    return db.scalar(select(func.count(TaskAssignee.id)).where(TaskAssignee.user_id == user_id,
                                                                TaskAssignee.status != "done")) or 0


def report_submitted_today(db: Session, user_id: int) -> bool:
    return bool(db.scalar(select(DailyReport.id).where(DailyReport.user_id == user_id,
                                                       DailyReport.on_date == date.today())))


def month_days_label(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{calendar.month_name[m]} {y}"
