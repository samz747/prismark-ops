"""Database models for the Prismark Operations System."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

Money = Numeric(18, 2)
Rate = Numeric(14, 4)


def now() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------- users & access
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    job_title: Mapped[str | None] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    roles: Mapped[str] = mapped_column(String(200), default="")  # comma separated role keys
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)  # MD and backup admin
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    permissions: Mapped[list["Permission"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def role_list(self) -> list[str]:
        return [r for r in self.roles.split(",") if r]

    @property
    def initials(self) -> str:
        parts = self.full_name.split()
        return "".join(p[0] for p in parts[:2]).upper() or "?"


class Permission(Base):
    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("user_id", "area"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    area: Mapped[str] = mapped_column(String(30))
    level: Mapped[str] = mapped_column(String(10), default="none")  # none | own | view | edit
    user: Mapped[User] = relationship(back_populates="permissions")


class UserSession(Base):
    """Server side session. Lets the admin see login time, duration and end sessions remotely."""
    __tablename__ = "user_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    login_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    end_reason: Mapped[str | None] = mapped_column(String(30))  # logout | idle | expired | revoked
    user: Mapped[User] = relationship()

    @property
    def duration_minutes(self) -> int:
        end = self.ended_at or self.last_seen_at
        return max(0, int((end - self.login_at).total_seconds() // 60))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("user_sessions.id"))
    action: Mapped[str] = mapped_column(String(40))  # login, login_failed, view, create, update, delete...
    entity: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(String(300))
    changes: Mapped[str | None] = mapped_column(Text)  # JSON {field: [old, new]}
    ip: Mapped[str | None] = mapped_column(String(64))
    user: Mapped[User | None] = relationship()


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    ip: Mapped[str] = mapped_column(String(64), index=True)
    username: Mapped[str] = mapped_column(String(80))
    success: Mapped[bool] = mapped_column(Boolean, default=False)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# ---------------------------------------------------------------- documents (paper trail)
class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String(30), index=True)  # customer, order, order_cost, invoice, payment, expense, ...
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    doc_type: Mapped[str] = mapped_column(String(30))
    original_name: Mapped[str] = mapped_column(String(200))
    stored_name: Mapped[str] = mapped_column(String(80), unique=True)
    mime: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    uploaded_by: Mapped[User] = relationship()


# ---------------------------------------------------------------- customers
class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    company_name: Mapped[str] = mapped_column(String(160), index=True)
    tin: Mapped[str | None] = mapped_column(String(30))
    vrn: Mapped[str | None] = mapped_column(String(30))
    sector: Mapped[str | None] = mapped_column(String(80))
    address: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(80), default="Dar es Salaam")
    country: Mapped[str | None] = mapped_column(String(60), default="Tanzania")
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(160))
    website: Mapped[str | None] = mapped_column(String(160))
    source: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="prospect")  # prospect | active | inactive
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    attention_note: Mapped[str | None] = mapped_column(Text)  # shown as an alert whenever the record opens
    assigned_sales_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    assigned_sales: Mapped[User | None] = relationship(foreign_keys=[assigned_sales_id])
    contacts: Mapped[list["Contact"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    notes: Mapped[list["CustomerNote"]] = relationship(back_populates="customer", cascade="all, delete-orphan",
                                                      order_by="desc(CustomerNote.created_at)")
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[str | None] = mapped_column(String(80))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(160))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    customer: Mapped[Customer] = relationship(back_populates="contacts")


class CustomerNote(Base):
    __tablename__ = "customer_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    customer: Mapped[Customer] = relationship(back_populates="notes")
    author: Mapped[User] = relationship()


class AssignmentHistory(Base):
    __tablename__ = "assignment_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    from_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    to_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    changed_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(String(300))
    at: Mapped[datetime] = mapped_column(DateTime, default=now)
    from_user: Mapped[User | None] = relationship(foreign_keys=[from_user_id])
    to_user: Mapped[User | None] = relationship(foreign_keys=[to_user_id])
    changed_by: Mapped[User] = relationship(foreign_keys=[changed_by_id])


class Activity(Base):
    """Interaction between a staff member and a customer (call, visit, meeting...)."""
    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    on_date: Mapped[date] = mapped_column(Date, index=True)
    summary: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(300))
    next_action: Mapped[str | None] = mapped_column(String(300))
    next_action_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    customer: Mapped[Customer] = relationship()
    user: Mapped[User] = relationship()


# ---------------------------------------------------------------- orders
class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(30), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    sales_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    logistics_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    sourcing_type: Mapped[str] = mapped_column(String(15), default="local")  # local | international
    origin: Mapped[str | None] = mapped_column(String(120))
    supplier_name: Mapped[str | None] = mapped_column(String(160))
    tender_ref: Mapped[str | None] = mapped_column(String(80))
    lpo_number: Mapped[str | None] = mapped_column(String(60))
    stage: Mapped[str] = mapped_column(String(20), default="enquiry")
    currency: Mapped[str] = mapped_column(String(3), default="TZS")
    exchange_rate: Mapped[Decimal] = mapped_column(Rate, default=1)
    expected_delivery: Mapped[date | None] = mapped_column(Date)
    lpo_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    customer: Mapped[Customer] = relationship(back_populates="orders")
    sales: Mapped[User | None] = relationship(foreign_keys=[sales_id])
    logistics: Mapped[User | None] = relationship(foreign_keys=[logistics_id])
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    costs: Mapped[list["OrderCost"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    stage_logs: Mapped[list["StageLog"]] = relationship(back_populates="order", cascade="all, delete-orphan",
                                                       order_by="StageLog.at")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    description: Mapped[str] = mapped_column(String(250))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit: Mapped[str] = mapped_column(String(20), default="pcs")
    unit_price: Mapped[Decimal] = mapped_column(Money)  # selling price, order currency
    order: Mapped[Order] = relationship(back_populates="items")


class OrderCost(Base):
    """Costs of fulfilling an order: supplier, freight, clearing, duty, transport..."""
    __tablename__ = "order_costs"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(30))
    description: Mapped[str | None] = mapped_column(String(250))
    supplier: Mapped[str | None] = mapped_column(String(160))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="TZS")
    exchange_rate: Mapped[Decimal] = mapped_column(Rate, default=1)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=0)  # input VAT (TZS) if claimable
    on_date: Mapped[date] = mapped_column(Date)
    recorded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    order: Mapped[Order] = relationship(back_populates="costs")

    @property
    def amount_tzs(self) -> Decimal:
        return Decimal(self.amount) * Decimal(self.exchange_rate)


class StageLog(Base):
    __tablename__ = "stage_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    from_stage: Mapped[str] = mapped_column(String(20))
    to_stage: Mapped[str] = mapped_column(String(20))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(String(300))
    missing_docs: Mapped[str | None] = mapped_column(String(300))
    at: Mapped[datetime] = mapped_column(DateTime, default=now)
    order: Mapped[Order] = relationship(back_populates="stage_logs")
    user: Mapped[User] = relationship()


# ---------------------------------------------------------------- invoices & payments
class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(40), unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    issue_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), default="TZS")
    exchange_rate: Mapped[Decimal] = mapped_column(Rate, default=1)
    subtotal: Mapped[Decimal] = mapped_column(Money)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=18)
    vat_amount: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    order: Mapped[Order] = relationship(back_populates="invoices")
    customer: Mapped[Customer] = relationship()
    payments: Mapped[list["Payment"]] = relationship(back_populates="invoice", cascade="all, delete-orphan")

    @property
    def total_tzs(self) -> Decimal:
        return Decimal(self.total) * Decimal(self.exchange_rate)

    @property
    def vat_tzs(self) -> Decimal:
        return Decimal(self.vat_amount) * Decimal(self.exchange_rate)

    @property
    def confirmed_tzs(self) -> Decimal:
        return sum((p.amount_tzs for p in self.payments if p.status == "confirmed"), Decimal(0))

    @property
    def pending_tzs(self) -> Decimal:
        return sum((p.amount_tzs for p in self.payments if p.status == "pending"), Decimal(0))

    @property
    def balance_tzs(self) -> Decimal:
        return self.total_tzs - self.confirmed_tzs


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="TZS")
    exchange_rate: Mapped[Decimal] = mapped_column(Rate, default=1)
    withholding_tax: Mapped[Decimal] = mapped_column(Money, default=0)  # TZS withheld by customer
    paid_on: Mapped[date] = mapped_column(Date)
    method: Mapped[str] = mapped_column(String(30))
    reference: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | confirmed | rejected
    recorded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_note: Mapped[str | None] = mapped_column(String(300))
    invoice: Mapped[Invoice] = relationship(back_populates="payments")
    recorded_by: Mapped[User] = relationship(foreign_keys=[recorded_by_id])
    confirmed_by: Mapped[User | None] = relationship(foreign_keys=[confirmed_by_id])

    @property
    def amount_tzs(self) -> Decimal:
        return Decimal(self.amount) * Decimal(self.exchange_rate)


# ---------------------------------------------------------------- expenses
class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    on_date: Mapped[date] = mapped_column(Date, index=True)
    category: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(String(300))
    payee: Mapped[str | None] = mapped_column(String(160))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="TZS")
    exchange_rate: Mapped[Decimal] = mapped_column(Rate, default=1)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=0)  # claimable input VAT in TZS
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))  # cost to win/serve customer
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | approved | rejected
    submitted_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_note: Mapped[str | None] = mapped_column(String(300))
    customer: Mapped[Customer | None] = relationship()
    order: Mapped[Order | None] = relationship()
    submitted_by: Mapped[User] = relationship(foreign_keys=[submitted_by_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])

    @property
    def amount_tzs(self) -> Decimal:
        return Decimal(self.amount) * Decimal(self.exchange_rate)


# ---------------------------------------------------------------- plans, targets, reports
class MonthlyPlan(Base):
    __tablename__ = "monthly_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), unique=True)  # YYYY-MM
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    targets: Mapped[list["Target"]] = relationship(back_populates="plan", cascade="all, delete-orphan")
    milestones: Mapped[list["Milestone"]] = relationship(back_populates="plan", cascade="all, delete-orphan",
                                                         order_by="Milestone.due_date")


class Target(Base):
    __tablename__ = "targets"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("monthly_plans.id", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(String(30))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # None = whole company
    value: Mapped[Decimal] = mapped_column(Money)
    plan: Mapped[MonthlyPlan] = relationship(back_populates="targets")
    user: Mapped[User | None] = relationship()


class Milestone(Base):
    __tablename__ = "milestones"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("monthly_plans.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    due_date: Mapped[date] = mapped_column(Date)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    plan: Mapped[MonthlyPlan] = relationship(back_populates="milestones")
    owner: Mapped[User | None] = relationship()


class DailyReport(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (UniqueConstraint("user_id", "on_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    on_date: Mapped[date] = mapped_column(Date, index=True)
    achievements: Mapped[str] = mapped_column(Text)
    challenges: Mapped[str | None] = mapped_column(Text)
    plan_next: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="submitted")  # submitted | reviewed
    md_comment: Mapped[str | None] = mapped_column(Text)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    user: Mapped[User] = relationship(foreign_keys=[user_id])


# ---------------------------------------------------------------- tasks & notice board
class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))  # todo | assigned | schedule
    title: Mapped[str] = mapped_column(String(200))
    details: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(10), default="normal")  # low | normal | high | urgent
    on_date: Mapped[date | None] = mapped_column(Date, index=True)
    start_time: Mapped[str | None] = mapped_column(String(5))
    end_time: Mapped[str | None] = mapped_column(String(5))
    due_date: Mapped[date | None] = mapped_column(Date)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_by: Mapped[User] = relationship()
    customer: Mapped[Customer | None] = relationship()
    assignees: Mapped[list["TaskAssignee"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class TaskAssignee(Base):
    __tablename__ = "task_assignees"
    __table_args__ = (UniqueConstraint("task_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | in_progress | done
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    task: Mapped[Task] = relationship(back_populates="assignees")
    user: Mapped[User] = relationship()


class Notice(Base):
    __tablename__ = "notices"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_on: Mapped[date | None] = mapped_column(Date)
    posted_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    posted_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    posted_by: Mapped[User] = relationship()


# ---------------------------------------------------------------- HR & payroll
class StaffProfile(Base):
    __tablename__ = "staff_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), unique=True)
    full_name: Mapped[str] = mapped_column(String(120))
    position: Mapped[str | None] = mapped_column(String(80))
    department: Mapped[str | None] = mapped_column(String(80))
    employment_type: Mapped[str] = mapped_column(String(20), default="permanent")
    start_date: Mapped[date | None] = mapped_column(Date)
    basic_salary: Mapped[Decimal] = mapped_column(Money, default=0)
    tin: Mapped[str | None] = mapped_column(String(30))
    nssf_number: Mapped[str | None] = mapped_column(String(30))
    phone: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    user: Mapped[User | None] = relationship()


class Allowance(Base):
    __tablename__ = "allowances"
    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff_profiles.id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(7), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Money)
    taxable: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(300))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    staff: Mapped[StaffProfile] = relationship()


class PayrollRun(Base):
    __tablename__ = "payroll_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), unique=True)
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft | approved
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    lines: Mapped[list["PayrollLine"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class PayrollLine(Base):
    __tablename__ = "payroll_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id", ondelete="CASCADE"))
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff_profiles.id"))
    basic: Mapped[Decimal] = mapped_column(Money)
    allowances: Mapped[Decimal] = mapped_column(Money)
    taxable_allowances: Mapped[Decimal] = mapped_column(Money)
    gross: Mapped[Decimal] = mapped_column(Money)
    nssf_employee: Mapped[Decimal] = mapped_column(Money)
    taxable_income: Mapped[Decimal] = mapped_column(Money)
    paye: Mapped[Decimal] = mapped_column(Money)
    other_deductions: Mapped[Decimal] = mapped_column(Money, default=0)
    net: Mapped[Decimal] = mapped_column(Money)
    nssf_employer: Mapped[Decimal] = mapped_column(Money)
    sdl: Mapped[Decimal] = mapped_column(Money)
    wcf: Mapped[Decimal] = mapped_column(Money)
    run: Mapped[PayrollRun] = relationship(back_populates="lines")
    staff: Mapped[StaffProfile] = relationship()
