"""Password hashing and JWT token utilities (bcrypt 4.0.1)."""

from datetime import datetime, timedelta

from jose import jwt
from passlib.context import CryptContext

from config import settings

# passlib + bcrypt==4.0.1 — production password encryption
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(
    username: str,
    role: str,
    user_id: int,
    *,
    portal: str = "pos",
    branch_id: int | None = None,
) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": username,
        "role": role,
        "uid": user_id,
        "portal": portal,
        "branch_id": branch_id,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
