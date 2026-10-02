import uuid

import jwt
import pytest

from app.core.config import Settings
from app.core.errors import UnauthorizedError
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("s3cret")
    assert hashed != "s3cret"
    assert verify_password("s3cret", hashed)
    assert not verify_password("wrong", hashed)


def test_access_token_roundtrip(settings: Settings) -> None:
    user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    token, expires_in = create_access_token(user_id=user_id, tenant_id=tenant_id, settings=settings)
    payload = decode_access_token(token, settings)
    assert payload["sub"] == str(user_id)
    assert payload["tid"] == str(tenant_id)
    assert expires_in == settings.jwt_expires_minutes * 60


def test_expired_token_rejected(settings: Settings) -> None:
    settings.jwt_expires_minutes = -1
    token, _ = create_access_token(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), settings=settings)
    with pytest.raises(UnauthorizedError, match="expired"):
        decode_access_token(token, settings)


def test_token_with_wrong_signature_rejected(settings: Settings) -> None:
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "tid": str(uuid.uuid4()), "exp": 9999999999, "type": "access"},
        "another-secret-that-is-long-enough-123",
        algorithm="HS256",
    )
    with pytest.raises(UnauthorizedError):
        decode_access_token(forged, settings)


def test_api_key_format_and_hash() -> None:
    key = generate_api_key("aip_")
    assert key.startswith("aip_")
    assert len(key) > 40
    assert hash_api_key(key) == hash_api_key(key)
    assert hash_api_key(key) != hash_api_key(generate_api_key("aip_"))


def test_production_requires_real_jwt_secret() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(_env_file=None, environment="production")
