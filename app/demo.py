"""Demo data for training and previews. Only runs on an empty database."""
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from .db import Base, SessionLocal, engine
from .models import (Activity, Allowance, Contact, Customer, CustomerNote, Expense, Invoice, Milestone,
                     MonthlyPlan, Notice, Order, OrderCost, OrderItem, Payment, Permission, StaffProfile,
                     Target, Task, TaskAssignee, User)
from .permissions import AREAS, template_for_roles
from .security import hash_password

DEMO_PASSWORD = "Prismark2026!"


def _user(db, username, name, roles, title, admin=False):
    u = User(username=username, full_name=name, roles=",".join(roles), job_title=title, is_superadmin=admin,
             password_hash=hash_password(DEMO_PASSWORD), must_change_password=False)
    perms = {a: "edit" for a in AREAS} if admin else template_for_roles(roles)
    u.permissions = [Permission(area=a, level=l) for a, l in perms.items()]
    db.add(u)
    db.flush()
    return u


def seed():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    if db.scalar(select(func.count(User.id))):
        print("Database is not empty; demo data not added.")
        return
    today = date.today()
    md = _user(db, "md", "Sameer", [], "Managing Director", admin=True)
    _user(db, "backup.admin", "Backup Admin", [], "Director", admin=True)
    amina = _user(db, "amina", "Amina Juma", ["sales"], "Sales Executive")
    baraka = _user(db, "baraka", "Baraka Mushi", ["sales"], "Sales Officer")
    neema = _user(db, "neema", "Neema Mollel", ["logistics"], "Logistics Officer")
    joseph = _user(db, "joseph", "Joseph Kweka", ["office_admin"], "Office Admin")
    halima = _user(db, "halima", "Halima Said", ["hr"], "HR Officer")
    _user(db, "director", "Demo Shareholder", ["shareholder"], "Shareholder")

    custs = []
    data = [
        ("Kilimanjaro Cement Works Ltd", "Manufacturing", amina, "active", "Pays late. Ask for 50% advance on new LPOs."),
        ("Bahari Hotels Group", "Hospitality", amina, "active", None),
        ("Msasani Engineering Ltd", "Engineering", baraka, "active", "Procurement head prefers WhatsApp, not email."),
        ("Pwani Agro Processors", "Agriculture", baraka, "active", None),
        ("Mlimani Logistics Co.", "Transport", amina, "prospect", None),
        ("Rufiji Energy Services", "Energy", baraka, "prospect", None),
    ]
    for i, (name, sector, rep, status, note) in enumerate(data, 1):
        c = Customer(code=f"CUS-{i:04d}", company_name=name, sector=sector, tin=f"1{i}0-{i}45-{i}78",
                     vrn=f"40-0{i}1234-K", city="Dar es Salaam", country="Tanzania", phone=f"+255 7{i}4 000 00{i}",
                     email=f"procurement@{name.split()[0].lower()}.co.tz", source=["Tenda Mpya", "Zabuni", "Referral", "Direct"][i % 4],
                     status=status, attention_note=note, assigned_sales_id=rep.id, created_by_id=rep.id,
                     created_at=datetime.utcnow() - timedelta(days=120 - i * 15))
        c.contacts = [Contact(name=["Peter Mrema", "Grace Lyimo", "John Massawe", "Rehema Ali", "Musa Kombo", "Agnes Temba"][i - 1],
                              position="Procurement Officer", phone=f"+255 75{i} 111 22{i}", is_primary=True)]
        db.add(c)
        custs.append(c)
    db.flush()
    db.add(CustomerNote(customer_id=custs[0].id, author_id=amina.id, pinned=True,
                        body="Finance office only processes payments on Thursdays. Send invoice reminders Tuesday."))

    orders_spec = [
        (custs[0], amina, "Industrial bearings and seals", "international", "Guangzhou, China", "paid", 45, "USD", Decimal("2620"),
         [("SKF bearing 6205", 400, 9.5), ("Oil seal kit", 150, 22)], [("Supplier", 2400, "USD"), ("Freight", 1600000, "TZS"), ("Clearing & duty", 2300000, "TZS")]),
        (custs[1], amina, "Kitchen extraction fans", "local", "Kariakoo, Dar es Salaam", "payment_pending", 20, "TZS", Decimal(1),
         [("Extraction fan 450mm", 12, 850000)], [("Supplier", 7200000, "TZS"), ("Transport", 250000, "TZS")]),
        (custs[2], baraka, "Welding machines and electrodes", "international", "Dubai, UAE", "shipping", 10, "USD", Decimal("2640"),
         [("Inverter welding machine 400A", 6, 1450), ("Electrodes E6013 box", 80, 28)], [("Supplier", 8200, "USD")]),
        (custs[3], baraka, "Stainless steel pipes", "local", "Mikocheni, Dar es Salaam", "delivery", 6, "TZS", Decimal(1),
         [("SS pipe 2 inch x 6m", 60, 210000)], [("Supplier", 10200000, "TZS")]),
        (custs[0], amina, "Conveyor belt rubber", "international", "Johannesburg, South Africa", "lpo", 3, "USD", Decimal("2650"),
         [("Conveyor belt 800mm per metre", 120, 95)], []),
        (custs[4], amina, "Forklift spare parts", "local", "Dar es Salaam", "quotation", 2, "TZS", Decimal(1),
         [("Forklift brake pads set", 10, 320000)], []),
    ]
    for n, (c, rep, title, stype, origin, stage, days_ago, cur, rate, items, costs) in enumerate(orders_spec, 1):
        o = Order(ref=f"ORD-{today.year}-{n:04d}", customer_id=c.id, title=title, sales_id=rep.id, logistics_id=neema.id,
                  sourcing_type=stype, origin=origin, supplier_name="Various", stage=stage, currency=cur, exchange_rate=rate,
                  lpo_number=f"LPO/{c.code}/{n:03d}" if stage not in ("enquiry", "quotation") else None,
                  lpo_date=today - timedelta(days=days_ago) if stage not in ("enquiry", "quotation") else None,
                  created_by_id=rep.id, created_at=datetime.utcnow() - timedelta(days=days_ago + 10))
        o.items = [OrderItem(description=d, quantity=Decimal(q), unit="pcs", unit_price=Decimal(str(p))) for d, q, p in items]
        o.costs = [OrderCost(category=cat, amount=Decimal(str(a)), currency=cc, exchange_rate=rate if cc == "USD" else Decimal(1),
                             on_date=today - timedelta(days=days_ago), recorded_by_id=neema.id) for cat, a, cc in costs]
        db.add(o)
        db.flush()
        if stage in ("paid", "payment_pending"):
            sub = sum(Decimal(q) * Decimal(str(p)) for _, q, p in items)
            vat = (sub * Decimal("0.18")).quantize(Decimal("0.01"))
            inv = Invoice(number=f"INV-{today.year}-{n:04d}", order_id=o.id, customer_id=c.id,
                          issue_date=today - timedelta(days=days_ago - 2), due_date=today - timedelta(days=days_ago - 32),
                          currency=cur, exchange_rate=rate, subtotal=sub, vat_amount=vat, total=sub + vat, created_by_id=joseph.id)
            db.add(inv)
            db.flush()
            pay = Payment(invoice_id=inv.id, amount=sub + vat, currency=cur, exchange_rate=rate, paid_on=today - timedelta(days=max(1, days_ago - 20)),
                          method="Bank transfer", reference=f"TT{n}88{n}", recorded_by_id=rep.id,
                          status="confirmed" if stage == "paid" else "pending")
            if stage == "paid":
                pay.confirmed_by_id, pay.confirmed_at = md.id, datetime.utcnow() - timedelta(days=3)
            db.add(pay)

    kinds = ["Call", "Visit", "Meeting", "WhatsApp", "Email"]
    for k in range(18):
        c = custs[k % 6]
        rep = c.assigned_sales
        db.add(Activity(customer_id=c.id, user_id=rep.id, kind=kinds[k % 5], on_date=today - timedelta(days=k * 2),
                        summary=["Discussed upcoming tender for spare parts", "Delivered samples to stores manager",
                                 "Followed up on outstanding invoice", "Presented quotation and lead times"][k % 4],
                        outcome="Positive" if k % 3 else "Needs follow up",
                        next_action="Send revised quotation" if k % 4 == 0 else None,
                        next_action_date=today + timedelta(days=1) if k % 4 == 0 else None))

    period = today.strftime("%Y-%m")
    plan = MonthlyPlan(period=period, title=f"Plan for {today.strftime('%B %Y')}",
                       summary="Grow invoiced sales, collect overdue balances and win two new manufacturing customers.",
                       created_by_id=md.id)
    plan.targets = [Target(metric="revenue", value=Decimal("60000000")), Target(metric="collections", value=Decimal("45000000")),
                    Target(metric="new_customers", value=Decimal(3)), Target(metric="orders_won", value=Decimal(4)),
                    Target(metric="revenue", user_id=amina.id, value=Decimal("35000000")),
                    Target(metric="revenue", user_id=baraka.id, value=Decimal("25000000")),
                    Target(metric="activities", user_id=amina.id, value=Decimal(30)),
                    Target(metric="activities", user_id=baraka.id, value=Decimal(30))]
    plan.milestones = [Milestone(title="Register with two new private sector supplier portals", owner_id=baraka.id,
                                 due_date=today.replace(day=10), done_at=datetime.utcnow() - timedelta(days=5)),
                       Milestone(title="Close Kilimanjaro Cement conveyor belt LPO", owner_id=amina.id, due_date=today.replace(day=20)),
                       Milestone(title="Collect all balances older than 60 days", owner_id=joseph.id, due_date=today.replace(day=28))]
    db.add(plan)

    exp = [("Transport & fuel", 85000, None, joseph), ("Office supplies", 140000, None, joseph),
           ("\"Incentive\"", 300000, custs[0], amina), ("Tender fees", 50000, custs[2], baraka),
           ("Entertainment & dinners", 180000, custs[1], amina), ("Communication & internet", 220000, None, joseph)]
    for k, (cat, amt, cust, who) in enumerate(exp):
        db.add(Expense(on_date=today - timedelta(days=k * 3), category=cat, description=f"{cat} for operations",
                       amount=Decimal(amt), customer_id=cust.id if cust else None, submitted_by_id=who.id,
                       status="approved" if k % 2 == 0 else "pending",
                       approved_by_id=md.id if k % 2 == 0 else None))

    t = Task(kind="assigned", title="Prepare bid for Msasani Engineering tender", priority="high", due_date=today + timedelta(days=2),
             created_by_id=md.id, details="Use updated supplier prices from Dubai.")
    t.assignees = [TaskAssignee(user_id=baraka.id), TaskAssignee(user_id=joseph.id)]
    db.add(t)
    s = Task(kind="schedule", title="Visit Bahari Hotels stores", on_date=today, start_time="10:00", end_time="12:00",
             created_by_id=amina.id)
    s.assignees = [TaskAssignee(user_id=amina.id)]
    db.add(s)
    db.add(Notice(title="Monthly review meeting", body="All staff: monthly plan review on Friday at 9:00 in the office.",
                  pinned=True, posted_by_id=md.id))

    for u, sal in [(amina, 1200000), (baraka, 1100000), (neema, 950000), (joseph, 900000), (halima, 1000000)]:
        sp = StaffProfile(user_id=u.id, full_name=u.full_name, position=u.job_title, basic_salary=Decimal(sal),
                          start_date=today - timedelta(days=400))
        db.add(sp)
        db.flush()
        db.add(Allowance(staff_id=sp.id, period=period, kind="Transport", amount=Decimal(100000), created_by_id=halima.id))
    db.commit()
    print(f"Demo data added. All demo accounts use the password: {DEMO_PASSWORD}")
