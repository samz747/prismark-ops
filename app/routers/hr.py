from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..deps import Ctx, F, Forbidden, FormError, audit, get_ctx, redirect, render
from ..models import Allowance, PayrollLine, PayrollRun, StaffProfile, User
from ..services import (D0, compute_payroll_line, current_period, docs_for, get_or_404, month_days_label,
                        setting, shift_period, valid_period)

router = APIRouter(prefix="/hr")
ALLOWANCE_KINDS = ["Transport", "Housing", "Meals", "Communication", "Overtime", "Responsibility", "Bonus",
                   "Commission", "Per diem / travel", "Other"]
NON_TAXABLE_DEFAULT = {"Per diem / travel"}


@router.get("")
def staff(ctx: Ctx = Depends(get_ctx)):
    ctx.require("hr", "view")
    rows = ctx.db.scalars(select(StaffProfile).order_by(StaffProfile.is_active.desc(), StaffProfile.full_name)).all()
    runs = ctx.db.scalars(select(PayrollRun).order_by(PayrollRun.period.desc()).limit(12)).all()
    linked = {s.user_id for s in rows if s.user_id}
    users = [u for u in ctx.db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.full_name)) if u.id not in linked]
    return render(ctx, "hr/staff.html", page_title="Staff & payroll", rows=rows, runs=runs, users=users,
                  can_edit=ctx.can("hr", "edit"), period=current_period())


def _fill_staff(s: StaffProfile, f: F):
    s.full_name = f.str("full_name", True, 120, "Full name")
    s.position = f.str("position", max_len=80)
    s.department = f.str("department", max_len=80)
    s.employment_type = f.choice("employment_type", ("permanent", "contract", "casual", "intern"), "permanent")
    s.start_date = f.date("start_date")
    s.basic_salary = f.money("basic_salary", True, label="Basic salary")
    s.tin = f.str("tin", max_len=30)
    s.nssf_number = f.str("nssf_number", max_len=30)
    s.phone = f.str("phone", max_len=40)


@router.post("/staff")
async def add_staff(ctx: Ctx = Depends(get_ctx)):
    ctx.require("hr", "edit")
    f = F(await ctx.request.form())
    s = StaffProfile()
    _fill_staff(s, f)
    uid = f.int("user_id")
    s.user_id = uid if uid and ctx.db.get(User, uid) else None
    ctx.db.add(s)
    ctx.db.flush()
    audit(ctx, "create", "staff", s.id, f"Added staff profile {s.full_name}")
    ctx.db.commit()
    return redirect("/hr", "Staff added.")


@router.get("/staff/{sid}")
def staff_detail(sid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require("hr", "view")
    s = get_or_404(ctx.db, StaffProfile, sid)
    lines = ctx.db.scalars(select(PayrollLine).where(PayrollLine.staff_id == sid).join(PayrollRun)
                           .order_by(PayrollRun.period.desc())).all()
    allows = ctx.db.scalars(select(Allowance).where(Allowance.staff_id == sid).order_by(Allowance.period.desc())).all()
    return render(ctx, "hr/staff_detail.html", page_title=s.full_name, s=s, lines=lines, allows=allows,
                  can_edit=ctx.can("hr", "edit"))


@router.post("/staff/{sid}")
async def update_staff(sid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require("hr", "edit")
    s = get_or_404(ctx.db, StaffProfile, sid)
    f = F(await ctx.request.form())
    old_salary = s.basic_salary
    _fill_staff(s, f)
    s.is_active = f.bool("is_active")
    audit(ctx, "update", "staff", sid, f"Edited staff {s.full_name}",
          {"basic_salary": [old_salary, s.basic_salary]} if Decimal(old_salary) != Decimal(s.basic_salary) else None)
    ctx.db.commit()
    return redirect(f"/hr/staff/{sid}", "Saved.")


@router.get("/allowances")
def allowances(ctx: Ctx = Depends(get_ctx)):
    ctx.require("hr", "view")
    period = valid_period(ctx.request.query_params.get("period"))
    rows = ctx.db.scalars(select(Allowance).where(Allowance.period == period).order_by(Allowance.created_at)).all()
    staff_rows = ctx.db.scalars(select(StaffProfile).where(StaffProfile.is_active.is_(True)).order_by(StaffProfile.full_name)).all()
    # history: totals per month for the last six months
    history = []
    for k in range(5, -1, -1):
        p = shift_period(period, -k)
        total = sum((Decimal(a.amount) for a in ctx.db.scalars(select(Allowance).where(Allowance.period == p))), D0)
        history.append((month_days_label(p)[:3], total))
    return render(ctx, "hr/allowances.html", page_title="Allowances", rows=rows, period=period, staff=staff_rows,
                  period_label=month_days_label(period), prev=shift_period(period, -1), next=shift_period(period, 1),
                  KINDS=ALLOWANCE_KINDS, docs=docs_for(ctx.db, "allowance", [a.id for a in rows]), history=history,
                  can_edit=ctx.can("hr", "edit"), total=sum((Decimal(a.amount) for a in rows), D0))


@router.post("/allowances")
async def add_allowance(ctx: Ctx = Depends(get_ctx)):
    ctx.require("hr", "edit")
    form = await ctx.request.form()
    f = F(form)
    period = valid_period(f.str("period", True, 7))
    run = ctx.db.scalar(select(PayrollRun).where(PayrollRun.period == period))
    if run and run.status == "approved":
        raise FormError("Payroll for this month is approved and locked.")
    staff_member = get_or_404(ctx.db, StaffProfile, f.int("staff_id", True, label="Staff") or 0)
    kind = f.choice("kind", ALLOWANCE_KINDS, "Other")
    a = Allowance(staff_id=staff_member.id, period=period, kind=kind, amount=f.money("amount", True, False, "Amount"),
                  taxable=not f.bool("non_taxable"), note=f.str("note", max_len=300), created_by_id=ctx.user.id)
    ctx.db.add(a)
    ctx.db.flush()
    from ..uploads import save_upload
    await save_upload(ctx, form.get("file"), "allowance", a.id, "payslip")
    audit(ctx, "create", "allowance", a.id, f"{kind} allowance TZS {a.amount:,.0f} for {staff_member.full_name} ({period})")
    ctx.db.commit()
    return redirect(f"/hr/allowances?period={period}", "Allowance recorded.")


@router.post("/payroll")
async def generate_payroll(ctx: Ctx = Depends(get_ctx)):
    ctx.require("hr", "edit")
    f = F(await ctx.request.form())
    period = valid_period(f.str("period", True, 7))
    run = ctx.db.scalar(select(PayrollRun).where(PayrollRun.period == period))
    if run and run.status == "approved":
        raise FormError("Payroll for this month is already approved.")
    if not run:
        run = PayrollRun(period=period, created_by_id=ctx.user.id)
        ctx.db.add(run)
    run.lines.clear()
    ctx.db.flush()
    for s in ctx.db.scalars(select(StaffProfile).where(StaffProfile.is_active.is_(True))):
        allows = ctx.db.scalars(select(Allowance).where(Allowance.staff_id == s.id, Allowance.period == period)).all()
        total = sum((Decimal(a.amount) for a in allows), D0)
        taxable = sum((Decimal(a.amount) for a in allows if a.taxable), D0)
        vals = compute_payroll_line(ctx.db, Decimal(s.basic_salary), total, taxable)
        run.lines.append(PayrollLine(staff_id=s.id, **vals))
    audit(ctx, "generate", "payroll", None, f"Generated payroll draft for {period}")
    ctx.db.commit()
    return redirect(f"/hr/payroll/{run.id}", "Payroll draft calculated. The MD approves it.")


@router.get("/payroll/{rid}")
def payroll_run(rid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require("hr", "view")
    run = get_or_404(ctx.db, PayrollRun, rid)
    keys = ["basic", "allowances", "gross", "nssf_employee", "paye", "net", "nssf_employer", "sdl", "wcf"]
    totals = {k: sum((Decimal(getattr(l, k)) for l in run.lines), D0) for k in keys}
    return render(ctx, "hr/payroll.html", page_title=f"Payroll {month_days_label(run.period)}", run=run, totals=totals,
                  sdl_applies=setting(ctx.db, "sdl_applies") == "1", can_edit=ctx.can("hr", "edit"))


@router.post("/payroll/{rid}/approve")
async def approve_payroll(rid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    run = get_or_404(ctx.db, PayrollRun, rid)
    if run.status == "approved":
        raise FormError("Already approved.")
    run.status, run.approved_by_id, run.approved_at = "approved", ctx.user.id, datetime.utcnow()
    audit(ctx, "approve", "payroll", rid, f"Approved payroll {run.period}")
    ctx.db.commit()
    return redirect(f"/hr/payroll/{rid}", "Payroll approved and locked.")


@router.get("/payslips")
def my_payslips(ctx: Ctx = Depends(get_ctx)):
    ctx.require("hr", "own")
    profile = ctx.db.scalar(select(StaffProfile).where(StaffProfile.user_id == ctx.user.id))
    lines = []
    if profile:
        lines = ctx.db.scalars(select(PayrollLine).join(PayrollRun).where(PayrollLine.staff_id == profile.id,
                                                                         PayrollRun.status == "approved")
                               .order_by(PayrollRun.period.desc())).all()
    return render(ctx, "hr/payslips.html", page_title="My payslips", lines=lines, profile=profile)


@router.get("/payslips/{lid}")
def payslip(lid: int, ctx: Ctx = Depends(get_ctx)):
    line = get_or_404(ctx.db, PayrollLine, lid)
    own = line.staff.user_id == ctx.user.id and line.run.status == "approved"
    if not (own or ctx.can("hr", "view")):
        raise Forbidden()
    allows = ctx.db.scalars(select(Allowance).where(Allowance.staff_id == line.staff_id,
                                                    Allowance.period == line.run.period)).all()
    return render(ctx, "hr/payslip.html", page_title=f"Payslip {month_days_label(line.run.period)}", l=line,
                  allows=allows, period_label=month_days_label(line.run.period))
