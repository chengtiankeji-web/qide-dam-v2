"""JWT + password + API key crypto.

API Key format: dam_<env>_<32-hex-random>
Stored as SHA-256 hash; raw value only shown once at issuance.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ----- password -----

def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


# ----- JWT -----

def create_access_token(
    subject: str,
    *,
    extra_claims: dict[str, Any] | None = None,
    expires_minutes: int | None = None,
) -> str:
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=expires_minutes or settings.JWT_EXPIRE_MINUTES)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises JWTError on bad signature / expired."""
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


def safe_decode(token: str) -> dict[str, Any] | None:
    try:
        return decode_access_token(token)
    except JWTError:
        return None


# ----- API Key -----

API_KEY_PREFIX = "dam"


def generate_api_key(env_tag: str = "live") -> tuple[str, str, str]:
    """Returns (raw_key, prefix_for_display, sha256_hash).

    Raw key is shown to user once. Hash is stored. Prefix is shown later
    in the UI as `dam_live_a1b2c3...` so users can identify which key.
    """
    raw_random = secrets.token_hex(32)  # 64 chars
    raw_key = f"{API_KEY_PREFIX}_{env_tag}_{raw_random}"
    display_prefix = f"{API_KEY_PREFIX}_{env_tag}_{raw_random[:8]}"
    digest = hashlib.sha256(raw_key.encode()).hexdigest()
    return raw_key, display_prefix, digest


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()
