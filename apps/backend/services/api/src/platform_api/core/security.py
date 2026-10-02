import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pwdlib import PasswordHash

from platform_api.core.config import Settings
from platform_core.errors import UnauthorizedError

_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return _password_hash.verify(password, hashed)


# A precomputed hash used to keep login timing constant when the user doesn't exist.
DUMMY_PASSWORD_HASH = hash_password("timing-safe-dummy-password")


def create_access_token(
    *, user_id: uuid.UUID, tenant_id: uuid.UUID, settings: Settings
) -> tuple[str, int]:
    expires_in = settings.jwt_expires_minutes * 60
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
        "type": "access",
    }
    token = jwt.encode(
        payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, expires_in


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "tid", "exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid token") from exc
    if payload.get("type") != "access":
        raise UnauthorizedError("Invalid token type")
    return payload


def generate_api_key(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


def hash_api_key(raw_key: str) -> str:
    # API keys are high-entropy random strings, so a fast hash is sufficient
    # and allows direct indexed lookup.
    return hashlib.sha256(raw_key.encode()).hexdigest()
