# Prismark Operations System

Internal operations system for Prismark Company Limited: customers, sourcing orders, documents, invoices, payments, expenses, plans and targets, daily reports, tasks, payroll and tax preparation.

Built with Python (FastAPI), PostgreSQL and server rendered pages. There is no separate frontend app and no public API, which keeps the attack surface small.

## What is inside

| Area | What it does |
|---|---|
| Customers | Company profile, TIN/VRN, contact people, attention notes that pop up whenever the record opens, pinned notes, activity timeline, documents, assignment history. Worth card: revenue, gross profit, paid, pending, outstanding, overdue, acquisition cost, cost to serve, net value, average days to pay, tier A/B/C. |
| Orders & sourcing | Enquiry → Quotation → LPO → Sourcing → Shipping & clearing (international only) → Delivery → Invoiced → Pending confirmation → Paid. A stage can close without its document (logged as "closed without"). The invoice cannot be raised until every earlier document is uploaded, and no payment can be recorded until the tax invoice / EFD receipt is attached. Items, costs (supplier, freight, clearing, duty...), margin per order. TZS or USD with the rate saved. |
| Invoices & payments | Payments recorded by staff stay **Pending confirmation** until the MD confirms receipt. Only confirmed money counts as collected. Ageing buckets, withholding tax. |
| Expenses | Every expense with a receipt, optional link to a customer or order. `"Incentive"`, tender fees, gifts, dinners, travel and marketing linked to a customer count as acquisition cost. Six month trend per category and next month forecast. MD approves. |
| Paper trail check | Every figure is shown everywhere. Any figure without its document gets a red "Proof missing" badge and appears in this list until fixed. |
| Plans & targets | MD uploads the monthly plan document, sets company and personal targets and milestones. Plan vs actual is calculated only from system records (invoices, confirmed payments, LPOs, activities), with an assessment of pace, gap and amount needed per remaining day. |
| Daily reports | Staff submit a daily report. Beside it the system shows what it actually recorded for that person that day. MD reviews and comments. Weekly and monthly summaries per person. |
| Tasks & notice board | Personal to do list, tasks assigned by the MD to one or many people, self planned time slots. Today's plans and assignments for everyone appear on the notice board. |
| Staff & payroll | Staff profiles, allowances with supporting documents, payroll calculation (NSSF 10% + 10%, PAYE monthly bands, SDL 3.5%, WCF 0.5%), MD approval, payslips. |
| Tax & VAT | Output VAT, input VAT (only claimable when a receipt is attached), net VAT, PAYE/SDL/NSSF/WCF, WHT suffered, income tax estimate. CSV schedules for filing on the TRA portal. |
| Profit & loss | Six months of revenue, cost of goods, gross profit, expenses, payroll, net profit and cash collected. |
| Users & access | No self registration. Super admins (MD and backup admin) create users, issue temporary passwords and set access per area: No access, Own records, View all, Edit all. Roles only pre-fill the matrix. |
| Activity log | Every login, failed login, page view, change (with old and new values), upload, download and approval. Sessions with login time, last activity, duration, IP and how they ended. Visible to super admins only. |

Rates (VAT, PAYE bands, NSSF, SDL, WCF, corporate tax, USD rate, tier thresholds) are editable under **Settings** and every change is logged. Check them whenever a new Finance Act takes effect.

## Security built in

These cover the common gaps reported in AI-built apps:

1. **Every route checks login and permission on the server**, including object level checks (a sales executive opening another person's customer by changing the number in the address gets "Access restricted"). Tested.
2. **Passwords** hashed with Argon2id. Minimum 10 characters with upper and lower case and a number. Temporary passwords must be changed at first login. Changing a password ends the user's other sessions.
3. **Brute force protection**: account locks for 15 minutes after 5 failed attempts; more than 10 failed attempts per minute from one network are refused. Failed logins are logged. The same message is shown for unknown users and wrong passwords.
4. **Sessions** are stored on the server (random token, only its hash in the database), sent in an HttpOnly, Secure, SameSite cookie, expire after 30 minutes idle or 10 hours, and can be ended remotely by the admin.
5. **CSRF tokens** on every form, including the login form.
6. **SQL injection**: all database access goes through SQLAlchemy with bound parameters. No SQL is built from user input.
7. **XSS**: templates escape all output; a strict Content Security Policy blocks inline and outside scripts.
8. **Security headers**: CSP, HSTS, X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy.
9. **File uploads**: whitelist of PDF, JPG, PNG, WEBP, DOCX, XLSX; the file content signature must match the extension; 15 MB limit; random file names; stored outside the web folder; served only after a permission check; SHA-256 fingerprint recorded.
10. **No secrets in code**: everything comes from `.env`, which is excluded from git and Docker. The app refuses to start in production without a SECRET_KEY.
11. **No mass assignment**: every form field is read and validated explicitly.
12. **Open redirect protection** on all "go back" links.
13. **No API docs or debug pages** in production; errors show a generic page and details go to the server log only.
14. **CSV export** neutralises spreadsheet formula injection.
15. **Database not exposed**: PostgreSQL and the app sit on a private Docker network; only Caddy (HTTPS) is public.
16. **Backups**: daily script for the database and documents.
17. **Pinned dependency versions** in `requirements.txt`.

Recommended next step after launch: two factor login (authenticator app) for super admins.

## Run it on your laptop (demo)

```
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python manage.py seed-demo      # demo data, password for all demo users: Prismark2026!
uvicorn app.main:app --reload
```

Open http://localhost:8000 and sign in as `md` (Managing Director), `amina` (sales), `neema` (logistics), `joseph` (office admin), `halima` (HR) or `director` (shareholder). Delete the `data` folder to start again.

Run the tests: `python -m pytest -q`

## Online test version on Render (updates automatically)

The repository includes `render.yaml`. In Render choose **New → Blueprint**, select this GitHub repository and fill in:

* `BOOTSTRAP_ADMIN_USERNAME`, `BOOTSTRAP_ADMIN_PASSWORD` (10+ characters, upper and lower case, a number) and `BOOTSTRAP_ADMIN_NAME`: your first super admin, created only while the database is empty. You must change the password at first login. Delete the password from Render afterwards.
* `SEED_DEMO`: `true` to try the system with demo data instead (all demo users use `Prismark2026!`), otherwise leave empty.

Every push to the main branch redeploys within a few minutes. On the free plan the app sleeps after 15 minutes idle, uploaded documents are lost at each redeploy and the database expires after 30 days. For real use switch the web service to Starter, add a disk mounted at `/data`, set `UPLOAD_DIR=/data/uploads` and upgrade the database.

## Put it online (production)

You need a small Linux server (2 GB RAM is enough for 10 users), Docker installed and a domain or subdomain such as `ops.prismark.co.tz` pointing to the server's IP address.

1. Copy this folder to the server.
2. `cp .env.example .env` and fill it in. Generate the secret with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Use a long random database password and put the same one inside `DATABASE_URL`.
3. `docker compose up -d --build`. Caddy obtains the HTTPS certificate automatically.
4. Create your account: `docker compose exec app python manage.py create-admin`
5. Sign in, then create the backup admin under **Users & access** and tick "Super admin".
6. Create staff accounts. Give each person their temporary password directly.
7. Review **Settings** (rates) and add staff profiles under **Staff & payroll**.
8. Schedule backups: `crontab -e` and add `0 2 * * * /path/to/prismark-ops/scripts/backup.sh`. Copy the `backups` folder off the server regularly.
9. Server hygiene: allow only ports 22, 80 and 443 in the firewall, use SSH keys instead of passwords, and run `apt upgrade` monthly.

Do not run `seed-demo` on the production database.

## Notes

* Tax figures are preparation figures from recorded documents. Filing happens on the TRA portal and should be reviewed by your tax adviser.
* All amounts are stored in their original currency with the exchange rate used, and reported in TZS.
* Times are stored in UTC and shown in East Africa Time.
