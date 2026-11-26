"""End to end tests: security controls and the core business rules.

Run with:  python -m pytest -q
"""
import os
import re
import tempfile

TMP = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TMP}/test.db")
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["LOGIN_IP_LIMIT_PER_MIN"] = "1000"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.demo import DEMO_PASSWORD, seed  # noqa: E402
from app.main import app  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


@pytest.fixture(scope="session", autouse=True)
def _db():
    seed()


def login(user: str, password: str = DEMO_PASSWORD) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    r = c.get("/login")
    tok = re.search(r'name="pre_token" value="([^"]+)"', r.text).group(1)
    c.post("/login", data={"username": user, "password": password, "pre_token": tok, "next": "/"})
    return c


def csrf(c: TestClient, page: str = "/") -> str:
    r = c.get(page)
    return re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)


# ------------------------------------------------------------------ security
def test_pages_require_login():
    c = TestClient(app, follow_redirects=False)
    for p in ["/", "/customers", "/orders/1", "/admin/users", "/files/1"]:
        r = c.get(p)
        assert r.status_code == 303 and "/login" in r.headers["location"]


def test_no_self_registration_or_api_docs():
    c = TestClient(app)
    for p in ["/register", "/signup", "/docs", "/openapi.json", "/redoc"]:
        assert c.get(p).status_code == 404


def test_security_headers():
    r = TestClient(app).get("/login")
    h = r.headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"


def test_wrong_password_and_lockout():
    for _ in range(5):
        c = login("baraka", "wrong-password")
        assert "pm_session" not in c.cookies
    # correct password is now refused because the account is locked
    c = login("baraka")
    assert "pm_session" not in c.cookies
    md = login("md")
    uid = 4  # baraka
    md.post(f"/admin/users/{uid}/action", data={"csrf_token": csrf(md), "action": "unlock"})
    assert "pm_session" in login("baraka").cookies


def test_csrf_required():
    c = login("amina")
    r = c.post("/tasks/todo", data={"title": "x"})
    assert r.status_code == 403
    r = c.post("/tasks/todo", data={"title": "x", "csrf_token": "forged"})
    assert r.status_code == 403
    r = c.post("/tasks/todo", data={"title": "x", "csrf_token": csrf(c)})
    assert r.status_code == 303


def test_sales_cannot_see_other_sales_records_or_admin():
    c = login("amina")
    assert c.get("/customers/3").status_code == 403      # Baraka's customer
    assert c.get("/orders/3").status_code == 403         # Baraka's order
    assert c.get("/admin/users").status_code == 403
    assert c.get("/finance/pnl").status_code == 403      # company figures
    assert c.get("/approvals").status_code == 403
    # own records are fine
    assert c.get("/customers/1").status_code == 200
    # list only shows own customers
    body = c.get("/customers").text
    assert "Kilimanjaro" in body and "Msasani" not in body


def test_shareholder_reads_but_cannot_edit():
    c = login("director")
    assert c.get("/customers/1").status_code == 200
    assert c.get("/customers/1/edit").status_code == 403
    r = c.post("/customers/1/notes", data={"body": "x", "csrf_token": csrf(c)})
    assert r.status_code == 403


def test_md_does_not_submit_reports():
    c = login("md")
    r = c.post("/reports", data={"achievements": "x", "csrf_token": csrf(c)})
    assert r.status_code == 403


def test_logout_ends_session():
    c = login("joseph")
    c.post("/logout", data={"csrf_token": csrf(c)})
    assert c.get("/").status_code == 303


def test_new_user_must_change_password():
    md = login("md")
    r = md.post("/admin/users/new", data={"csrf_token": csrf(md), "username": "new.staff", "full_name": "New Staff",
                                          "roles": "sales", "perm_customers": "own", "perm_tasks": "own"})
    temp = re.search(r'class="code">([^<]+)<', r.text).group(1)
    c = login("new.staff", temp)
    r = c.get("/customers")
    assert r.status_code == 303 and r.headers["location"] == "/account/password"
    r = c.post("/account/password", data={"csrf_token": csrf(c, "/account/password"), "current": temp,
                                          "new": "short", "confirm": "short"})
    assert "msg=" in r.headers["location"] and "k=err" in r.headers["location"]
    c.post("/account/password", data={"csrf_token": csrf(c, "/account/password"), "current": temp,
                                      "new": "BetterPass2026", "confirm": "BetterPass2026"})
    assert c.get("/customers").status_code == 200


def test_upload_rejects_fake_file_type():
    c = login("amina")
    tok = csrf(c)
    r = c.post("/files/upload", data={"csrf_token": tok, "entity": "order", "entity_id": "1", "doc_type": "lpo", "back": "/orders/1"},
               files={"file": ("lpo.pdf", b"<script>alert(1)</script>", "application/pdf")})
    assert "k=err" in r.headers["location"]
    r = c.post("/files/upload", data={"csrf_token": tok, "entity": "order", "entity_id": "1", "doc_type": "lpo", "back": "/orders/1"},
               files={"file": ("tool.exe", b"MZ....", "application/octet-stream")})
    assert "k=err" in r.headers["location"]


def test_cannot_attach_to_other_users_order():
    c = login("amina")
    r = c.post("/files/upload", data={"csrf_token": csrf(c), "entity": "order", "entity_id": "3", "doc_type": "lpo", "back": "/"},
               files={"file": ("lpo.pdf", PDF, "application/pdf")})
    assert r.status_code == 403


# ------------------------------------------------------------------ business rules
def test_invoice_locked_until_documents_then_payment_flow():
    c = login("baraka")
    tok = csrf(c)
    # order 4 is local and at delivery with no documents
    r = c.post("/orders/4/invoice", data={"csrf_token": tok})
    assert "Invoice+locked" in r.headers["location"] or "Invoice%20locked" in r.headers["location"]
    for dt in ["rfq", "quotation", "lpo", "supplier_invoice", "delivery_note"]:
        r = c.post("/files/upload", data={"csrf_token": tok, "entity": "order", "entity_id": "4", "doc_type": dt, "back": "/orders/4"},
                   files={"file": (f"{dt}.pdf", PDF, "application/pdf")})
        assert "k=err" not in r.headers["location"]
    r = c.post("/orders/4/invoice", data={"csrf_token": tok})
    assert r.headers["location"].startswith("/finance/invoices/")
    inv_url = r.headers["location"].split("?")[0]
    inv_id = inv_url.rsplit("/", 1)[1]
    # payment blocked until the tax invoice is attached
    r = c.post(f"{inv_url}/payments", data={"csrf_token": tok, "amount": "1000000"})
    assert "k=err" in r.headers["location"]
    c.post("/files/upload", data={"csrf_token": tok, "entity": "invoice", "entity_id": inv_id, "doc_type": "tax_invoice", "back": inv_url},
           files={"file": ("efd.pdf", PDF, "application/pdf")})
    r = c.post(f"{inv_url}/payments", data={"csrf_token": tok, "amount": "14868000"})
    assert "k=err" not in r.headers["location"]
    assert "Pending confirmation" in c.get("/orders/4").text
    # sales cannot confirm; MD can
    from app.db import SessionLocal
    from app.models import Payment
    pid = SessionLocal().query(Payment).order_by(Payment.id.desc()).first().id
    assert c.post(f"/finance/payments/{pid}/review", data={"csrf_token": tok, "decision": "confirmed"}).status_code == 403
    md = login("md")
    md.post(f"/finance/payments/{pid}/review", data={"csrf_token": csrf(md), "decision": "confirmed"})
    assert "Paid &amp; confirmed" in c.get("/orders/4").text


def test_stage_can_close_without_document_but_is_logged():
    c = login("amina")
    r = c.post("/orders/6/advance", data={"csrf_token": csrf(c), "lpo_number": "LPO-77"})
    assert r.status_code == 303
    page = c.get("/orders/6").text
    assert "Closed without" in page


def test_payroll_paye_bands():
    from decimal import Decimal
    from app.services import paye, parse_bands, DEFAULT_SETTINGS
    bands = parse_bands(DEFAULT_SETTINGS["paye_bands"])
    assert paye(Decimal(270000), bands) == 0
    assert paye(Decimal(520000), bands) == Decimal("20000.00")
    assert paye(Decimal(1000000), bands) == Decimal("128000.00")
    assert paye(Decimal(1500000), bands) == Decimal("278000.00")


def test_activity_log_records_actions():
    md = login("md")
    body = md.get("/admin/activity?action=changes").text
    assert "Recorded payment" in body or "Raised invoice" in body


def test_user_page_shows_the_selected_user():
    md = login("md")
    page = md.get("/admin/users/3").text   # Amina
    assert 'value="Amina Juma"' in page
    assert 'value="Sameer"' not in page
