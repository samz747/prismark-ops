from fastapi import APIRouter, Depends
from sqlalchemy import or_, select

from ..deps import Ctx, F, Forbidden, audit, diff, get_ctx, redirect, render, snapshot
from ..models import (Activity, AssignmentHistory, Contact, Customer, CustomerNote, Expense, Invoice, Order, User)
from ..services import (STAGE_LABEL, access, check, customer_scope, customer_value, docs_for, get_or_404,
                        next_number, order_revenue, sales_users, settled_balance)

router = APIRouter(prefix="/customers")

FIELDS = ["company_name", "tin", "vrn", "sector", "address", "city", "country", "phone", "email", "website",
          "source", "status", "payment_terms_days", "attention_note"]
SOURCES = ["Tenda Mpya", "Zabuni", "NeST", "Company website / portal", "Referral", "Direct / walk in", "Social media", "Other"]
STATUSES = ["prospect", "active", "inactive"]
ACTIVITY_KINDS = ["Call", "Visit", "Meeting", "WhatsApp", "Email", "Quotation sent", "Sample delivered", "Follow up", "Other"]


def _fill(c: Customer, f: F):
    c.company_name = f.str("company_name", True, 160, "Company name")
    c.tin = f.str("tin", max_len=30)
    c.vrn = f.str("vrn", max_len=30)
    c.sector = f.str("sector", max_len=80)
    c.address = f.str("address", max_len=200)
    c.city = f.str("city", max_len=80)
    c.country = f.str("country", max_len=60)
    c.phone = f.str("phone", max_len=40)
    c.email = f.str("email", max_len=160)
    c.website = f.str("website", max_len=160)
    c.source = f.choice("source", SOURCES, None, "source")
    c.status = f.choice("status", STATUSES, "prospect", "status")
    c.payment_terms_days = f.int("payment_terms_days", min_v=0) or 30
    c.attention_note = f.text("attention_note")


@router.get("")
def list_customers(ctx: Ctx = Depends(get_ctx)):
    ctx.require("customers")
    q = ctx.request.query_params
    stmt = customer_scope(ctx, select(Customer)).order_by(Customer.company_name)
    if q.get("q"):
        term = f"%{q.get('q')[:60]}%"
        stmt = stmt.where(or_(Customer.company_name.ilike(term), Customer.code.ilike(term), Customer.tin.ilike(term)))
    if q.get("status") in STATUSES:
        stmt = stmt.where(Customer.status == q.get("status"))
    if q.get("rep", "").isdigit() and ctx.level("customers") != "own":
        stmt = stmt.where(Customer.assigned_sales_id == int(q.get("rep")))
    rows = [(c, customer_value(ctx.db, c)) for c in ctx.db.scalars(stmt)]
    if q.get("sort") == "value":
        rows.sort(key=lambda r: r[1]["revenue"], reverse=True)
    totals = {k: sum(r[1][k] for r in rows) for k in ("revenue", "outstanding", "gross_profit")}
    return render(ctx, "customers/list.html", page_title="Customers", rows=rows, q=q, STATUSES=STATUSES,
                  reps=sales_users(ctx.db), totals=totals,
                  can_create=ctx.level("customers") in ("own", "edit"))


@router.get("/new")
def new_customer(ctx: Ctx = Depends(get_ctx)):
    if ctx.level("customers") not in ("own", "edit"):
        raise Forbidden()
    return render(ctx, "customers/form.html", page_title="New customer", c=None, SOURCES=SOURCES, STATUSES=STATUSES,
                  reps=sales_users(ctx.db))


@router.post("/new")
async def create_customer(ctx: Ctx = Depends(get_ctx)):
    if ctx.level("customers") not in ("own", "edit"):
        raise Forbidden()
    f = F(await ctx.request.form())
    c = Customer(code=next_number(ctx.db, Customer, Customer.code, "CUS", yearly=False), created_by_id=ctx.user.id)
    _fill(c, f)
    rep = f.int("assigned_sales_id") if ctx.level("customers") == "edit" else None
    c.assigned_sales_id = rep if rep and ctx.db.get(User, rep) else ctx.user.id
    if ctx.user.is_superadmin and not rep:
        c.assigned_sales_id = None
    ctx.db.add(c)
    ctx.db.flush()
    name = f.str("contact_name", max_len=120)
    if name:
        ctx.db.add(Contact(customer_id=c.id, name=name, position=f.str("contact_position", max_len=80),
                           phone=f.str("contact_phone", max_len=40), email=f.str("contact_email", max_len=160),
                           is_primary=True))
    ctx.db.add(AssignmentHistory(customer_id=c.id, to_user_id=c.assigned_sales_id, changed_by_id=ctx.user.id,
                                 reason="Customer created"))
    audit(ctx, "create", "customer", c.id, f"Created customer {c.code} {c.company_name}")
    ctx.db.commit()
    return redirect(f"/customers/{c.id}", "Customer created.")


@router.get("/{cid}")
def customer_detail(cid: int, ctx: Ctx = Depends(get_ctx)):
    c = get_or_404(ctx.db, Customer, cid)
    check(ctx, "customers", [c.assigned_sales_id])
    db = ctx.db
    value = customer_value(db, c)
    activities = db.scalars(select(Activity).where(Activity.customer_id == cid).order_by(Activity.on_date.desc(),
                                                                                          Activity.id.desc()).limit(50)).all()
    orders = sorted(c.orders, key=lambda o: o.created_at, reverse=True)
    invoices = db.scalars(select(Invoice).where(Invoice.customer_id == cid).order_by(Invoice.issue_date.desc())).all()
    expenses = db.scalars(select(Expense).where(Expense.customer_id == cid).order_by(Expense.on_date.desc())).all()
    history = db.scalars(select(AssignmentHistory).where(AssignmentHistory.customer_id == cid)
                         .order_by(AssignmentHistory.at.desc())).all()
    tab = ctx.request.query_params.get("tab", "overview")
    return render(ctx, "customers/detail.html", page_title=c.company_name, c=c, v=value, activities=activities,
                  orders=orders, invoices=invoices, expenses=expenses, history=history, tab=tab,
                  docs=docs_for(db, "customer", [cid]).get(cid, []), STAGE_LABEL=STAGE_LABEL,
                  order_revenue=order_revenue, settled_balance=settled_balance, ACTIVITY_KINDS=ACTIVITY_KINDS,
                  can_write=access(ctx, "customers", [c.assigned_sales_id], write=True),
                  reps=sales_users(db))


@router.get("/{cid}/edit")
def edit_customer(cid: int, ctx: Ctx = Depends(get_ctx)):
    c = get_or_404(ctx.db, Customer, cid)
    check(ctx, "customers", [c.assigned_sales_id], write=True)
    return render(ctx, "customers/form.html", page_title=f"Edit {c.company_name}", c=c, SOURCES=SOURCES,
                  STATUSES=STATUSES, reps=sales_users(ctx.db))


@router.post("/{cid}/edit")
async def update_customer(cid: int, ctx: Ctx = Depends(get_ctx)):
    c = get_or_404(ctx.db, Customer, cid)
    check(ctx, "customers", [c.assigned_sales_id], write=True)
    f = F(await ctx.request.form())
    before = snapshot(c, FIELDS)
    _fill(c, f)
    audit(ctx, "update", "customer", c.id, f"Edited customer {c.code}", diff(before, c) or None)
    ctx.db.commit()
    return redirect(f"/customers/{cid}", "Customer saved.")


@router.post("/{cid}/assign")
async def assign(cid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    c = get_or_404(ctx.db, Customer, cid)
    f = F(await ctx.request.form())
    new_id = f.int("user_id")
    if new_id and not ctx.db.get(User, new_id):
        new_id = None
    reason = f.str("reason", True, 300, "Reason")
    old = c.assigned_sales_id
    if old == new_id:
        return redirect(f"/customers/{cid}", "No change.")
    c.assigned_sales_id = new_id
    move_orders = f.bool("move_open_orders")
    if move_orders:
        for o in c.orders:
            if o.stage != "paid" and o.sales_id == old:
                o.sales_id = new_id
    ctx.db.add(AssignmentHistory(customer_id=cid, from_user_id=old, to_user_id=new_id, changed_by_id=ctx.user.id,
                                 reason=reason))
    audit(ctx, "reassign", "customer", cid, f"Reassigned {c.code}: {reason}", {"assigned_sales_id": [old, new_id],
                                                                               "open_orders_moved": [None, move_orders]})
    ctx.db.commit()
    return redirect(f"/customers/{cid}", "Sales representative changed.")


@router.post("/{cid}/contacts")
async def add_contact(cid: int, ctx: Ctx = Depends(get_ctx)):
    c = get_or_404(ctx.db, Customer, cid)
    check(ctx, "customers", [c.assigned_sales_id], write=True)
    f = F(await ctx.request.form())
    ct = Contact(customer_id=cid, name=f.str("name", True, 120, "Name"), position=f.str("position", max_len=80),
                 phone=f.str("phone", max_len=40), email=f.str("email", max_len=160), is_primary=f.bool("is_primary"))
    if ct.is_primary:
        for other in c.contacts:
            other.is_primary = False
    ctx.db.add(ct)
    audit(ctx, "create", "contact", cid, f"Added contact {ct.name} to {c.code}")
    ctx.db.commit()
    return redirect(f"/customers/{cid}", "Contact added.")


@router.post("/{cid}/contacts/{ct_id}/delete")
async def delete_contact(cid: int, ct_id: int, ctx: Ctx = Depends(get_ctx)):
    c = get_or_404(ctx.db, Customer, cid)
    check(ctx, "customers", [c.assigned_sales_id], write=True)
    ct = get_or_404(ctx.db, Contact, ct_id)
    if ct.customer_id != cid:
        raise Forbidden()
    audit(ctx, "delete", "contact", cid, f"Removed contact {ct.name} from {c.code}")
    ctx.db.delete(ct)
    ctx.db.commit()
    return redirect(f"/customers/{cid}", "Contact removed.")


@router.post("/{cid}/notes")
async def add_note(cid: int, ctx: Ctx = Depends(get_ctx)):
    c = get_or_404(ctx.db, Customer, cid)
    check(ctx, "customers", [c.assigned_sales_id], write=True)
    f = F(await ctx.request.form())
    n = CustomerNote(customer_id=cid, author_id=ctx.user.id, body=f.text("body", True, "Note"), pinned=f.bool("pinned"))
    ctx.db.add(n)
    audit(ctx, "create", "note", cid, f"Added note to {c.code}")
    ctx.db.commit()
    return redirect(f"/customers/{cid}?tab=notes", "Note saved.")


@router.post("/{cid}/notes/{nid}/delete")
async def delete_note(cid: int, nid: int, ctx: Ctx = Depends(get_ctx)):
    n = get_or_404(ctx.db, CustomerNote, nid)
    if n.customer_id != cid or not (ctx.is_admin or n.author_id == ctx.user.id):
        raise Forbidden()
    audit(ctx, "delete", "note", cid, "Deleted note", {"body": [n.body, None]})
    ctx.db.delete(n)
    ctx.db.commit()
    return redirect(f"/customers/{cid}?tab=notes", "Note deleted.")


@router.post("/{cid}/activities")
async def log_activity(cid: int, ctx: Ctx = Depends(get_ctx)):
    c = get_or_404(ctx.db, Customer, cid)
    check(ctx, "customers", [c.assigned_sales_id], write=True)
    f = F(await ctx.request.form())
    from datetime import date
    a = Activity(customer_id=cid, user_id=ctx.user.id, kind=f.choice("kind", ACTIVITY_KINDS, "Call", "type"),
                 on_date=f.date("on_date") or date.today(), summary=f.text("summary", True, "Summary"),
                 outcome=f.str("outcome", max_len=300), next_action=f.str("next_action", max_len=300),
                 next_action_date=f.date("next_action_date"))
    ctx.db.add(a)
    audit(ctx, "create", "activity", cid, f"Logged {a.kind} with {c.company_name}")
    ctx.db.commit()
    return redirect(f"/customers/{cid}?tab=activities", "Activity logged.")
