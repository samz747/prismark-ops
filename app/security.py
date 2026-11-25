"""Password hashing, password policy, session tokens and login throttling."""
import hashlib
import hmac
import re
import secrets
import string
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config
from .models import LoginAttempt, User

_ph = PasswordHasher()  # Argon2id with library defaults
# Pre-computed hash so unknown usernames take the same time as wrong passwords (no user enumeration).
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))


def hash_password(raw: str) -> str:
    return _ph.hash(raw)


def verify_password(user: User | None, raw: str) -> bool:
    try:
        if user is None:
            _ph.verify(_DUMMY_HASH, raw)
            return False
        return _ph.verify(user.password_hash, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problems(raw: str, username: str = "") -> list[str]:
    problems = []
    if len(raw) < 10:
        problems.append("at least 10 characters")
    if not re.search(r"[A-Z]", raw) or not re.search(r"[a-z]", raw):
        problems.append("upper and lower case letters")
    if not re.search(r"\d", raw):
        problems.append("at least one number")
    if username and username.lower() in raw.lower():
        problems.append("must not contain the username")
    if raw.lower() in {"password123", "prismark123", "welcome1234", "qwerty12345"}:
        problems.append("too common")
    return problems


def generate_temp_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(12))
        if not password_problems(pw):
            return pw


def new_session_token() -> tuple[str, str]:
    """Returns (raw token for the cookie, sha256 hash stored in the database)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def tokens_match(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


def ip_rate_limited(db: Session, ip: str) -> bool:
    since = datetime.utcnow() - timedelta(minutes=1)
    count = db.scalar(select(func.count(LoginAttempt.id)).where(
        LoginAttempt.ip == ip, LoginAttempt.at >= since, LoginAttempt.success.is_(False)))
    return (count or 0) >= config.LOGIN_IP_LIMIT_PER_MIN


def register_failed_login(db: Session, user: User | None) -> None:
    if user is None:
        return
    user.failed_logins += 1
    if user.failed_logins >= config.LOGIN_MAX_FAILS:
        user.locked_until = datetime.utcnow() + timedelta(minutes=config.LOGIN_LOCK_MINUTES)
        user.failed_logins = 0
