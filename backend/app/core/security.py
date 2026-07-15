import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet

from app.core.config import settings

_hasher = PasswordHasher()


def hash_password(value: str) -> str:
    return _hasher.hash(value)


def verify_password(value: str, encoded: str) -> bool:
    try:
        return _hasher.verify(encoded, value)
    except VerifyMismatchError:
        return False


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def opaque_token() -> str:
    return secrets.token_urlsafe(48)


def create_jwt(subject: str, kind: str, lifetime: timedelta, jti: str | None = None) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": subject,
            "type": kind,
            "jti": jti or str(uuid4()),
            "iat": now,
            "exp": now + lifetime,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_jwt(value: str, kind: str) -> dict[str, object]:
    payload = jwt.decode(value, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != kind:
        raise jwt.InvalidTokenError("Wrong token type")
    return payload


def fernet() -> Fernet:
    key = settings.private_key_encryption_key
    if not key:
        key = Fernet.generate_key().decode()
    return Fernet(key.encode())
