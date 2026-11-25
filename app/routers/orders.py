from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select

from ..deps import Ctx, F, Forbidden, FormError, audit, get_ctx, redirect, render
from ..models import Customer, Invoice, Order, OrderCost, OrderItem, StageLog, User
from ..services import (MANUAL_STAGES, STAGE_DOC, STAGE_LABEL, STAGES, access, check, customer_scope, docs_for,
                        get_or_404, missing_before, next_number, next_stage, order_cost, order_doc_types,
                        order_revenue, order_scope, owners, q2, sales_users, setting, setting_dec, staff_users,
                        stages_for)
from ..uploads import DOC_TYPES, save_upload

router = APIRouter(prefix="/orders")
COST_CATEGORIES = ["Supplier", "Freight", "Clearing & duty", "Port & handling", "Insurance", "Transport", "Storage",
                   "Bank charges", "Agent commission", "Other"]


def _can_create(ctx: Ctx) -> bool:
    return ctx.level("orders") in ("own", "edit")


def _parse_items(f: F) -> list[OrderItem]:
    descs, qtys = f.form.getlist("item_desc"), f.form.getlist("item_qty")
    units, prices = f.form.getlist("item_unit"), f.form.getlist("item_price")
    items = []
    for i, d in enumerate(descs):
        d = (d or "").strip()[:250] if isinstance(d, str) else ""
        if not d:
            continue
        try:
            q = Decimal(str(qtys[i]).replace(",", "") or "0")
            p = Decimal(str(prices[i]).replace(",", "") or "0")
        except (InvalidOperation, IndexError):
            raise FormError(f"Item '{d}': quantity and price must be numbers.")
        if q <= 0 or p < 0 or not q.is_finite() or not p.is_finite():
            raise FormError(f"Item '{d}': quantity must be above zero and price cannot be negative.")
        unit = (str(units[i]) if i < len(units) else "pcs").strip()[:20] or "pcs"
        items.append(OrderItem(description=d, quantity=q, unit=unit, unit_price=q2(p)))
    return items


@router.get("")
def list_orders(ctx: Ctx = Depends(get_ctx)):
    ctx.require("orders")
    q = ctx.request.query_params
    stmt = order_scope(ctx, select(Order)).order_by(Order.created_at.desc())
    if q.get("stage") in STAGE_LABEL:
        stmt = stmt.where(Order.stage == q.get("stage"))
    elif q.get("stage") == "open":
        stmt = stmt.where(Order.stage != "paid", Order.is_cancelled.is_(False))
    if q.get("type") in ("local", "international"):
        stmt = stmt.where(Order.sourcing_type == q.get("type"))
    if q.get("rep", "").isdigit():
        stmt = stmt.where(Order.sales_id == int(q.get("rep")))
    if q.get("q"):
        term = f"%{q.get('q')[:60]}%"
        stmt = stmt.join(Customer).where(or_(Order.ref.ilike(term), Order.title.ilike(term), Customer.company_name.ilike(term),
                                             Order.lpo_number.ilike(term)))
    orders = ctx.db.scalars(stmt).all()
    missing = {o.id: len(missing_before(ctx.db, o, o.stage)) for o in orders}
    return render(ctx, "orders/list.html", page_title="Orders & sourcing", orders=orders, q=q, STAGES=STAGES,
                  STAGE_LABEL=STAGE_LABEL, order_revenue=order_revenue, order_cost=order_cost, missing=missing,
                  reps=sales_users(ctx.db), can_create=_can_create(ctx))


@router.get("/new")
def new_order(ctx: Ctx = Depends(get_ctx)):
    if not _can_create(ctx):
        raise Forbidden()
    custs = ctx.db.scalars(customer_scope(ctx, select(Customer).where(Customer.status != "inactive"))
                           .order_by(Customer.company_name)).all() if ctx.can("customers") else []
    if ctx.level("orders") == "edit" and not custs:
        custs = ctx.db.scalars(select(Customer).order_by(Customer.company_name)).all()
    pre = ctx.request.query_params.get("customer", "")
    return render(ctx, "orders/form.html", page_title="New order", o=None, customers=custs,
                  preselect=int(pre) if pre.isdigit() else None, reps=sales_users(ctx.db), staff=staff_users(ctx.db),
                  usd_rate=setting(ctx.db, "usd_rate"))


@router.post("/new")
async def create_order(ctx: Ctx = Depends(get_ctx)):
    if not _can_create(ctx):
        raise Forbidden()
    form = await ctx.request.form()
    f = F(form)
    cust = get_or_404(ctx.db, Customer, f.int("customer_id", True, label="Customer") or 0)
    if ctx.level("orders") != "edit" and not access(ctx, "customers", [cust.assigned_sales_id]):
        raise Forbidden("You can only create orders for your own customers.")
    cur, rate = f.currency()
    o = Order(ref=next_number(ctx.db, Order, Order.ref, "ORD"), customer_id=cust.id,
              title=f.str("title", True, 200, "Order title"),
              sourcing_type=f.choice("sourcing_type", ("local", "international"), "local"),
              origin=f.str("origin", max_len=120), supplier_name=f.str("supplier_name", max_len=160),
              tender_ref=f.str("tender_ref", max_len=80), currency=cur, exchange_rate=rate,
              expected_delivery=f.date("expected_delivery"), notes=f.text("notes"), created_by_id=ctx.user.id)
    if ctx.level("orders") == "edit":
        o.sales_id = f.int("sales_id") or cust.assigned_sales_id
    else:
        o.sales_id = ctx.user.id
    lid = f.int("logistics_id")
    o.logistics_id = lid if lid and ctx.db.get(User, lid) else None
    o.items = _parse_items(f)
    if not o.items:
        raise FormError("Add at least one item with a description, quantity and price.")
    ctx.db.add(o)
    ctx.db.flush()
    await save_upload(ctx, form.get("file"), "order", o.id, "rfq")
    if cust.status == "prospect":
        pass
    audit(ctx, "create", "order", o.id, f"Created order {o.ref} for {cust.company_name}")
    ctx.db.commit()
    return redirect(f"/orders/{o.id}", "Order created.")


@router.get("/{oid}")
def order_detail(oid: int, ctx: Ctx = Depends(get_ctx)):
    o = get_or_404(ctx.db, Order, oid)
    if not (access(ctx, "orders", owners(o)) or ctx.can("finance", "view")):
        raise Forbidden()
    db = ctx.db
    keys = stages_for(o)
    cur_idx = keys.index(o.stage) if o.stage in keys else 0
    docs = docs_for(db, "order", [o.id]).get(o.id, [])
    have = order_doc_types(db, o)
    steps = []
    for i, k in enumerate(keys):
        steps.append({"key": k, "label": STAGE_LABEL[k], "state": "done" if i < cur_idx else "current" if i == cur_idx else "todo",
                      "doc": STAGE_DOC.get(k), "has_doc": STAGE_DOC.get(k) in have if STAGE_DOC.get(k) else None})
    cost_docs = docs_for(db, "order_cost", [c.id for c in o.costs])
    revenue = order_revenue(o)
    cost = order_cost(o)
    can_write = access(ctx, "orders", owners(o), write=True)
    nxt = next_stage(o)
    gate_missing = missing_before(db, o, "invoiced")
    return render(ctx, "orders/detail.html", page_title=o.ref, o=o, steps=steps, docs=docs, cost_docs=cost_docs,
                  revenue=revenue, cost=cost, can_write=can_write, nxt=nxt, STAGE_LABEL=STAGE_LABEL,
                  DOC_TYPES=DOC_TYPES, gate_missing=gate_missing, COST_CATEGORIES=COST_CATEGORIES,
                  usd_rate=setting(db, "usd_rate"), vat_rate=setting(db, "vat_rate"),
                  can_invoice=can_write and ctx.can("finance") and o.stage == "delivery" and not o.is_cancelled,
                  MANUAL_STAGES=[k for k in MANUAL_STAGES if k in keys],
                  subtotal=sum((Decimal(i.quantity) * Decimal(i.unit_price) for i in o.items), Decimal(0)),
                  default_due=date.today() + timedelta(days=o.customer.payment_terms_days or 30))


@router.get("/{oid}/edit")
def edit_order(oid: int, ctx: Ctx = Depends(get_ctx)):
    o = get_or_404(ctx.db, Order, oid)
    check(ctx, "orders", owners(o), write=True)
    if o.invoices and not ctx.is_admin:
        raise Forbidden("This order is invoiced. Only the MD can change it now.")
    return render(ctx, "orders/form.html", page_title=f"Edit {o.ref}", o=o, customers=[o.customer], preselect=o.customer_id,
                  reps=sales_users(ctx.db), staff=staff_users(ctx.db), usd_rate=setting(ctx.db, "usd_rate"))


@router.post("/{oid}/edit")
async def update_order(oid: int, ctx: Ctx = Depends(get_ctx)):
    o = get_or_404(ctx.db, Order, oid)
    check(ctx, "orders", owners(o), write=True)
    if o.invoices and not ctx.is_admin:
        raise Forbidden("This order is invoiced. Only the MD can change it now.")
    f = F(await ctx.request.form())
    before_items = [(i.description, str(i.quantity), str(i.unit_price)) for i in o.items]
    before = {"title": o.title, "sourcing_type": o.sourcing_type, "origin": o.origin, "supplier_name": o.supplier_name,
              "currency": o.currency, "exchange_rate": o.exchange_rate, "sales_id": o.sales_id, "logistics_id": o.logistics_id}
    o.title = f.str("title", True, 200, "Order title")
    o.sourcing_type = f.choice("sourcing_type", ("local", "international"), "local")
    o.origin = f.str("origin", max_len=120)
    o.supplier_name = f.str("supplier_name", max_len=160)
    o.tender_ref = f.str("tender_ref", max_len=80)
    o.currency, o.exchange_rate = f.currency()
    o.expected_delivery = f.date("expected_delivery")
    o.notes = f.text("notes")
    if ctx.level("orders") == "edit":
        o.sales_id = f.int("sales_id") or o.sales_id
    lid = f.int("logistics_id")
    o.logistics_id = lid if lid and ctx.db.get(User, lid) else None
    if o.stage == "shipping" and o.sourcing_type == "local":
        o.stage = "delivery"
    items = _parse_items(f)
    if not items:
        raise FormError("Add at least one item.")
    o.items.clear()
    ctx.db.flush()
    o.items.extend(items)
    changes = {k: [v, getattr(o, k)] for k, v in before.items() if str(v) != str(getattr(o, k))}
    after_items = [(i.description, str(i.quantity), str(i.unit_price)) for i in o.items]
    if before_items != after_items:
        changes["items"] = [before_items, after_items]
    audit(ctx, "update", "order", o.id, f"Edited order {o.ref}", changes or None)
    ctx.db.commit()
    return redirect(f"/orders/{oid}", "Order saved.")


@router.post("/{oid}/advance")
async def advance(oid: int, ctx: Ctx = Depends(get_ctx)):
    o = get_or_404(ctx.db, Order, oid)
    check(ctx, "orders", owners(o), write=True)
    if o.is_cancelled:
        raise FormError("This order is cancelled.")
    form = await ctx.request.form()
    f = F(form)
    if o.stage not in MANUAL_STAGES:
        raise FormError("This stage moves automatically with invoices and payments.")
    nxt = next_stage(o)
    if nxt == "invoiced" or nxt is None:
        raise FormError("Raise the invoice to close the delivery stage.")
    if STAGE_DOC.get(o.stage):
        await save_upload(ctx, form.get("file"), "order", o.id, STAGE_DOC[o.stage])
    if o.stage == "quotation":
        o.lpo_number = f.str("lpo_number", max_len=60) or o.lpo_number
        o.lpo_date = f.date("lpo_date") or date.today()
        if o.customer.status == "prospect":
            o.customer.status = "active"
    ctx.db.flush()
    missing = [m for m in missing_before(ctx.db, o, nxt)]
    ctx.db.add(StageLog(order_id=o.id, from_stage=o.stage, to_stage=nxt, user_id=ctx.user.id,
                        note=f.str("note", max_len=300),
                        missing_docs=", ".join(DOC_TYPES[m] for m in missing) or None))
    audit(ctx, "stage", "order", o.id, f"{o.ref}: {STAGE_LABEL[o.stage]} closed, now {STAGE_LABEL[nxt]}"
          + (f" (missing: {', '.join(DOC_TYPES[m] for m in missing)})" if missing else ""))
    o.stage = nxt
    ctx.db.commit()
    msg = f"Moved to {STAGE_LABEL[nxt]}."
    if missing:
        msg += " Documents still missing; the invoice stays locked until they are uploaded."
    return redirect(f"/orders/{oid}", msg, "err" if missing else "ok")


@router.post("/{oid}/stage")
async def set_stage(oid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    o = get_or_404(ctx.db, Order, oid)
    f = F(await ctx.request.form())
    target = f.choice("stage", [k for k in MANUAL_STAGES if k in stages_for(o)], label="stage")
    if not target or o.invoices:
        raise FormError("Invoiced orders cannot be moved back. Cancel the invoice first.")
    ctx.db.add(StageLog(order_id=o.id, from_stage=o.stage, to_stage=target, user_id=ctx.user.id,
                        note=f.str("note", True, 300, "Reason")))
    audit(ctx, "stage", "order", o.id, f"MD moved {o.ref} from {STAGE_LABEL[o.stage]} to {STAGE_LABEL[target]}")
    o.stage = target
    ctx.db.commit()
    return redirect(f"/orders/{oid}", "Stage changed.")


@router.post("/{oid}/cancel")
async def cancel(oid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    o = get_or_404(ctx.db, Order, oid)
    f = F(await ctx.request.form())
    o.is_cancelled = not o.is_cancelled
    audit(ctx, "cancel" if o.is_cancelled else "restore", "order", o.id,
          f"{'Cancelled' if o.is_cancelled else 'Restored'} {o.ref}: {f.str('note', max_len=300) or ''}")
    ctx.db.commit()
    return redirect(f"/orders/{oid}", "Order cancelled." if o.is_cancelled else "Order restored.")


@router.post("/{oid}/costs")
async def add_cost(oid: int, ctx: Ctx = Depends(get_ctx)):
    o = get_or_404(ctx.db, Order, oid)
    check(ctx, "orders", owners(o), write=True)
    form = await ctx.request.form()
    f = F(form)
    cur, rate = f.currency()
    c = OrderCost(order_id=o.id, category=f.choice("category", COST_CATEGORIES, "Other", "category"),
                  description=f.str("description", max_len=250), supplier=f.str("supplier", max_len=160),
                  amount=f.money("amount", True, False, "Amount"), currency=cur, exchange_rate=rate,
                  vat_amount=f.money("vat_amount"), on_date=f.date("on_date") or date.today(), recorded_by_id=ctx.user.id)
    ctx.db.add(c)
    ctx.db.flush()
    await save_upload(ctx, form.get("file"), "order_cost", c.id, "receipt")
    audit(ctx, "create", "order_cost", c.id, f"{o.ref}: added {c.category} cost {cur} {c.amount:,.2f}")
    ctx.db.commit()
    return redirect(f"/orders/{oid}#costs", "Cost recorded.")


@router.post("/{oid}/costs/{cid}/delete")
async def delete_cost(oid: int, cid: int, ctx: Ctx = Depends(get_ctx)):
    o = get_or_404(ctx.db, Order, oid)
    if not (ctx.is_admin or ctx.level("orders") == "edit"):
        raise Forbidden()
    c = get_or_404(ctx.db, OrderCost, cid)
    if c.order_id != oid:
        raise Forbidden()
    audit(ctx, "delete", "order_cost", cid, f"{o.ref}: removed {c.category} cost",
          {"amount": [f"{c.currency} {c.amount}", None]})
    ctx.db.delete(c)
    ctx.db.commit()
    return redirect(f"/orders/{oid}#costs", "Cost removed.")


@router.post("/{oid}/invoice")
async def raise_invoice(oid: int, ctx: Ctx = Depends(get_ctx)):
    o = get_or_404(ctx.db, Order, oid)
    check(ctx, "orders", owners(o), write=True)
    if not ctx.can("finance"):
        raise Forbidden("You need access to invoices to raise one.")
    if o.stage != "delivery" or o.is_cancelled:
        raise FormError("An invoice can only be raised once the order is at the delivery stage.")
    missing = missing_before(ctx.db, o, "invoiced")
    if missing:
        raise FormError("Invoice locked. Upload these first: " + ", ".join(DOC_TYPES[m] for m in missing) + ".")
    form = await ctx.request.form()
    f = F(form)
    subtotal = sum((Decimal(i.quantity) * Decimal(i.unit_price) for i in o.items), Decimal(0))
    vat_rate = Decimal(0) if f.bool("vat_exempt") else setting_dec(ctx.db, "vat_rate")
    vat = q2(subtotal * vat_rate / 100)
    number = f.str("number", max_len=40) or next_number(ctx.db, Invoice, Invoice.number, "INV")
    if ctx.db.scalar(select(Invoice.id).where(Invoice.number == number)):
        raise FormError("That invoice number already exists.")
    issue = f.date("issue_date") or date.today()
    inv = Invoice(number=number, order_id=o.id, customer_id=o.customer_id, issue_date=issue,
                  due_date=f.date("due_date") or issue + timedelta(days=o.customer.payment_terms_days or 30),
                  currency=o.currency, exchange_rate=o.exchange_rate, subtotal=q2(subtotal), vat_rate=vat_rate,
                  vat_amount=vat, total=q2(subtotal) + vat, created_by_id=ctx.user.id)
    ctx.db.add(inv)
    ctx.db.flush()
    await save_upload(ctx, form.get("file"), "invoice", inv.id, "tax_invoice")
    ctx.db.add(StageLog(order_id=o.id, from_stage=o.stage, to_stage="invoiced", user_id=ctx.user.id,
                        note=f"Invoice {number} raised"))
    o.stage = "invoiced"
    audit(ctx, "create", "invoice", inv.id, f"Raised invoice {number} for {o.ref}: {o.currency} {inv.total:,.2f}")
    ctx.db.commit()
    return redirect(f"/finance/invoices/{inv.id}", "Invoice raised.")
