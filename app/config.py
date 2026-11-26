"""Application settings. Every secret comes from environment variables, never from code."""
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


ENV = os.getenv("APP_ENV", "development")
IS_PROD = ENV == "production"

SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    if IS_PROD:
        raise RuntimeError("SECRET_KEY must be set in production (see .env.example).")
    SECRET_KEY = secrets.token_urlsafe(48)  # dev only: changes on every restart

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'prismark.db'}")
# Hosts such as Render give "postgres://" or "postgresql://" without a driver. SQLAlchemy 2.1 would then
# pick the psycopg 3 driver, which is not installed, so name the installed psycopg2 driver explicitly.
for _prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(_prefix):
        DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len(_prefix):]
        break

# First super admin, for hosts without shell access. Used only while the database has no users.
BOOTSTRAP_ADMIN_USERNAME = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "").strip().lower()
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
BOOTSTRAP_ADMIN_NAME = os.getenv("BOOTSTRAP_ADMIN_NAME", "Managing Director")
SEED_DEMO = os.getenv("SEED_DEMO", "").strip().lower() in {"1", "true", "yes"}
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "data" / "uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "15"))
SESSION_IDLE_MINUTES = int(os.getenv("SESSION_IDLE_MINUTES", "30"))
SESSION_MAX_HOURS = int(os.getenv("SESSION_MAX_HOURS", "10"))
COOKIE_SECURE = _bool("COOKIE_SECURE", IS_PROD)

LOGIN_MAX_FAILS = int(os.getenv("LOGIN_MAX_FAILS", "5"))
LOGIN_LOCK_MINUTES = int(os.getenv("LOGIN_LOCK_MINUTES", "15"))
LOGIN_IP_LIMIT_PER_MIN = int(os.getenv("LOGIN_IP_LIMIT_PER_MIN", "10"))

TRUSTED_HOSTS = [h.strip() for h in os.getenv("TRUSTED_HOSTS", "*").split(",") if h.strip()]

COMPANY_NAME = "Prismark Company Limited"
