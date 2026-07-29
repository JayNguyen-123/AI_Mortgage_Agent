"""
Password hashing + JWT issuance for the internal auth system (loan
officers / admins -- there is no borrower-facing login; borrowers only
ever interact through the voice/chat/SMS agent).

Password hashing uses PBKDF2-HMAC-SHA256 via the standard library
(`hashlib.pbkdf2_hmac`) rather than bcrypt/argon2, specifically so this
module has zero extra dependencies and could be fully unit-tested in the
sandbox this was built in. PBKDF2 at 260k+ iterations is still a
NIST-approved, industry-acceptable choice (OWASP's current minimum
guidance) -- but bcrypt or argon2id are typically preferred for
new production systems if you're free to add the dependency; swapping is
a same-shaped function-body change (`hash_password`/`verify_password`),
not a schema or call-site change, since passwords are stored as an
opaque encoded string.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings

PBKDF2_ITERATIONS = 260_000
_ALGO = "HS256"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algo, iterations_str, salt_b64, hash_b64 = encoded_hash.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False

    iterations = int(iterations_str)
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(hash_b64)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)  # constant-time comparison, avoids timing attacks


def create_access_token(subject: str, role: str, expires_minutes: int = 60 * 12) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGO)


class TokenError(Exception):
    pass


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[_ALGO])
    except jwt.ExpiredSignatureError as e:
        raise TokenError("Session expired -- please log in again.") from e
    except jwt.InvalidTokenError as e:
        raise TokenError("Invalid session token.") from e
