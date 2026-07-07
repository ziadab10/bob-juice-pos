"""Authentication, JWT sessions, HttpOnly cookies, and role-based access control."""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from branch_scope import user_branch_id
from database import (
    AuditLog,
    User,
    UserRole,
    bootstrap_users_table,
    ensure_master_admin,
    get_db,
)
from security import create_access_token, decode_access_token, verify_password
from session_auth import (
    clear_session_cookies,
    extract_bearer_token,
    portal_cookie_token,
    set_session_cookie,
)

logger = logging.getLogger("bob_juice.auth")

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

# HTML routes gated by portal-scoped HttpOnly session cookies (see main.py middleware).
PROTECTED_HTML_ROUTES: dict[str, str] = {
    "/": "pos",
    "/pos": "pos",
    "/admin/dashboard": "admin",
    "/stock-in": "admin",
    "/stock-out": "admin",
    "/inventory-intake": "admin",
    "/customer-debts": "admin",
    "/supplier-debts": "admin",
}


def html_route_portal(path: str) -> str | None:
    """Return required portal for a protected HTML path, or None if public."""
    return PROTECTED_HTML_ROUTES.get(path)


def login_redirect_url(portal: str) -> str:
    """Portal-aware login URL used by HTML auth middleware."""
    return "/login?portal=admin" if portal == "admin" else "/login"


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    user_id: int
    full_name: str
    username: str
    branch_id: int | None = None
    branch_name: str | None = None


class SessionStatus(BaseModel):
    authenticated: bool
    user_id: int | None = None
    username: str | None = None
    full_name: str | None = None
    role: str | None = None
    access_token: str | None = None
    portal: str | None = None


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


def role_value(role: UserRole | str) -> str:
    """Normalize role for JWT/API — handles enum instances and legacy DB strings."""
    if isinstance(role, UserRole):
        return role.value
    text = str(role).strip().lower()
    if text in {r.value for r in UserRole}:
        return text
    if text == UserRole.ADMIN.name.lower():
        return UserRole.ADMIN.value
    if text == UserRole.CASHIER.name.lower():
        return UserRole.CASHIER.value
    if text == UserRole.MANAGER.name.lower():
        return UserRole.MANAGER.value
    return text


async def _fetch_user_by_username(db: AsyncSession, username: str) -> User | None:
    clean = username.strip()
    if not clean:
        return None
    try:
        result = await db.execute(select(User).where(User.username == clean))
        return result.scalar_one_or_none()
    except Exception:
        logger.exception("User lookup failed for %r", clean)
        return None


async def _user_from_token(db: AsyncSession, raw: str) -> User | None:
    try:
        payload = decode_access_token(raw)
        username = payload.get("sub")
        if not username:
            return None
    except JWTError:
        return None
    user = await _fetch_user_by_username(db, str(username))
    if user is None or not user.is_active:
        return None
    return user


def _portal_from_request(request: Request) -> str | None:
    hint = request.headers.get("X-Bob-Portal", "").lower()
    return hint if hint in ("pos", "admin") else None


async def resolve_authenticated_user(
    request: Request,
    header_token: str | None,
    db: AsyncSession,
    *,
    portal: str | None = None,
) -> User | None:
    """Try header then cookies; skip invalid or cross-portal JWTs."""
    portal = portal or _portal_from_request(request)
    raw = extract_bearer_token(request, header_token, portal=portal)
    if not raw:
        return None
    return await _user_from_token(db, raw)


async def get_current_user(
    request: Request,
    token: Annotated[str | None, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    user = await resolve_authenticated_user(request, token, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required")
    return user


def require_cashier_or_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    return user


async def resolve_client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def record_audit(
    db: AsyncSession,
    *,
    actor_id: int,
    action: str,
    entity_type: str,
    entity_id: str | int,
    details: dict | None = None,
    ip_address: str | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            details=json.dumps(details, default=str) if details else None,
            ip_address=ip_address,
        )
    )
    await db.flush()


@router.get("/session", response_model=SessionStatus)
async def session_status(
    request: Request,
    token: Annotated[str | None, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
    portal: str = Query("pos", pattern="^(pos|admin)$"),
):
    """Portal-scoped session probe — cookie-only; never accepts Authorization header bypass."""
    try:
        raw = extract_bearer_token(request, None, portal=portal)
        if not raw:
            return SessionStatus(authenticated=False, portal=portal)
        user = await _user_from_token(db, raw)
        if not user:
            return SessionStatus(authenticated=False, portal=portal)
        return SessionStatus(
            authenticated=True,
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            role=role_value(user.role),
            access_token=raw,
            portal=portal,
        )
    except Exception:
        logger.exception("Session status check failed")
        return SessionStatus(authenticated=False, portal=portal)


@router.post("/login", response_model=TokenResponse)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    username = (form.username or "").strip()
    password = form.password or ""
    if not username or not password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    try:
        user = await _fetch_user_by_username(db, username)
        if user is None:
            try:
                await ensure_master_admin(db)
                await db.flush()
                user = await _fetch_user_by_username(db, username)
            except Exception:
                logger.exception("Emergency admin bootstrap during login failed")
                try:
                    await bootstrap_users_table()
                    user = await _fetch_user_by_username(db, username)
                except Exception:
                    logger.exception("Full user bootstrap during login failed")

        if user is None or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

        try:
            password_ok = verify_password(password, user.password_hash)
        except Exception:
            logger.exception("Password verification error for %r", username)
            password_ok = False

        if not password_ok:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

        portal = request.headers.get("X-Bob-Portal", "pos").lower()
        if portal not in ("pos", "admin"):
            portal = "pos"

        bid = user_branch_id(user)
        branch_name: str | None = None
        if bid is not None:
            from database import Branch

            branch_row = await db.get(Branch, bid)
            branch_name = branch_row.name if branch_row else None
        if user.role == UserRole.CASHIER and bid is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cashier account is not assigned to a branch",
            )

        token = create_access_token(user.username, role_value(user.role), user.id, portal=portal, branch_id=bid)
        set_session_cookie(response, token, portal=portal)
        return TokenResponse(
            access_token=token,
            role=role_value(user.role),
            user_id=user.id,
            full_name=user.full_name,
            username=user.username,
            branch_id=bid,
            branch_name=branch_name,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Login handler crashed for %r", username)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Login temporarily unavailable — please retry",
        ) from exc


@router.post("/logout")
async def logout(
    response: Response,
    portal: str = Query("pos", pattern="^(pos|admin)$"),
):
    clear_session_cookies(response, portal=portal)
    return {"detail": "Logged out", "portal": portal}


@router.get("/me", response_model=UserOut)
async def get_me(user: Annotated[User, Depends(get_current_user)]):
    return user
