"""Profile settings and admin user/role management."""

from __future__ import annotations

import json
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, record_audit, require_admin, resolve_client_ip, TokenResponse
from database import User, UserRole, get_db
from permissions import ALL_PERMISSIONS, resolve_permissions, role_display
from security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/users", tags=["Users & Profile"])

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    is_active: bool
    permissions: dict[str, bool] | None = None

    model_config = {"from_attributes": True}


def user_to_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        role=user.role.value,
        is_active=user.is_active,
        permissions=resolve_permissions(user),
    )


class PermissionsOut(BaseModel):
    role: str
    role_label: str
    permissions: dict[str, bool]


class ChangeUsernameRequest(BaseModel):
    new_username: str = Field(min_length=3, max_length=32)

    @field_validator("new_username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        v = v.strip().lower()
        if not USERNAME_PATTERN.match(v):
            raise ValueError("Username must be 3–32 characters: letters, numbers, dots, hyphens, underscores")
        return v


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_strength(cls, v: str) -> str:
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one number")
        return v

    @model_validator(mode="after")
    def passwords_match(self):
        if self.new_password != self.confirm_password:
            raise ValueError("New password and confirmation do not match")
        return self


class UpdateProfileRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=128)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=128)
    role: UserRole
    permissions: dict[str, bool] | None = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        v = v.strip().lower()
        if not USERNAME_PATTERN.match(v):
            raise ValueError("Username must be 3–32 characters: letters, numbers, dots, hyphens, underscores")
        return v


class AdminUpdateUserRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=128)
    role: UserRole | None = None
    is_active: bool | None = None
    new_password: str | None = Field(default=None, min_length=8, max_length=128)
    permissions: dict[str, bool] | None = None


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------


def _serialize_permissions(overrides: dict[str, bool] | None) -> str | None:
    if not overrides:
        return None
    clean = {k: bool(v) for k, v in overrides.items() if k in ALL_PERMISSIONS}
    return json.dumps(clean) if clean else None


def get_permissions(user: User) -> dict[str, bool]:
    return resolve_permissions(user)


# ---------------------------------------------------------------------------
# Profile routes (any authenticated user)
# ---------------------------------------------------------------------------


@router.get("/permissions", response_model=PermissionsOut)
async def my_permissions(user: Annotated[User, Depends(get_current_user)]):
    return PermissionsOut(
        role=user.role.value,
        role_label=role_display(user.role),
        permissions=get_permissions(user),
    )


@router.patch("/profile/full-name", response_model=UserOut)
async def update_full_name(
    payload: UpdateProfileRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user.full_name = payload.full_name.strip()
    await db.flush()
    await record_audit(
        db,
        actor_id=user.id,
        action="PROFILE_UPDATE_NAME",
        entity_type="user",
        entity_id=user.id,
        details={"full_name": user.full_name},
        ip_address=await resolve_client_ip(request),
    )
    return user_to_out(user)


@router.patch("/profile/username", response_model=TokenResponse)
async def change_username(
    payload: ChangeUsernameRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if payload.new_username == user.username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New username is the same as current")
    existing = await db.execute(select(User).where(User.username == payload.new_username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")
    old_username = user.username
    user.username = payload.new_username
    await db.flush()
    await record_audit(
        db,
        actor_id=user.id,
        action="PROFILE_CHANGE_USERNAME",
        entity_type="user",
        entity_id=user.id,
        details={"old_username": old_username, "new_username": user.username},
        ip_address=await resolve_client_ip(request),
    )
    token = create_access_token(user.username, user.role.value, user.id)
    return TokenResponse(
        access_token=token,
        role=user.role.value,
        user_id=user.id,
        full_name=user.full_name,
        username=user.username,
    )


@router.patch("/profile/password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must differ from current password")
    user.password_hash = hash_password(payload.new_password)
    await db.flush()
    await record_audit(
        db,
        actor_id=user.id,
        action="PROFILE_CHANGE_PASSWORD",
        entity_type="user",
        entity_id=user.id,
        details={"username": user.username},
        ip_address=await resolve_client_ip(request),
    )
    return {"detail": "Password updated successfully"}


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------


@router.get("/admin/list", response_model=list[UserOut])
async def admin_list_users(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).order_by(User.role, User.username))
    return [user_to_out(u) for u in result.scalars().all()]


@router.post("/admin/create", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def admin_create_user(
    payload: CreateUserRequest,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    existing = await db.execute(select(User).where(User.username == payload.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
    new_user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name.strip(),
        role=payload.role,
        permissions_json=_serialize_permissions(payload.permissions),
    )
    db.add(new_user)
    await db.flush()
    await record_audit(
        db,
        actor_id=admin.id,
        action="USER_CREATE",
        entity_type="user",
        entity_id=new_user.id,
        details={"username": new_user.username, "role": new_user.role.value},
        ip_address=await resolve_client_ip(request),
    )
    return user_to_out(new_user)


@router.patch("/admin/{user_id}", response_model=UserOut)
async def admin_update_user(
    user_id: int,
    payload: AdminUpdateUserRequest,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == admin.id and payload.is_active is False:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot deactivate your own account")
    if target.id == admin.id and payload.role == UserRole.CASHIER:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot demote your own admin account")

    changes: dict = {}
    if payload.full_name is not None:
        target.full_name = payload.full_name.strip()
        changes["full_name"] = target.full_name
    if payload.role is not None:
        target.role = payload.role
        changes["role"] = payload.role.value
    if payload.is_active is not None:
        target.is_active = payload.is_active
        changes["is_active"] = payload.is_active
    if payload.permissions is not None:
        target.permissions_json = _serialize_permissions(payload.permissions)
        changes["permissions"] = payload.permissions
    if payload.new_password:
        target.password_hash = hash_password(payload.new_password)
        changes["password_reset"] = True

    await db.flush()
    await record_audit(
        db,
        actor_id=admin.id,
        action="USER_UPDATE",
        entity_type="user",
        entity_id=target.id,
        details=changes,
        ip_address=await resolve_client_ip(request),
    )
    return user_to_out(target)


@router.post("/admin/{user_id}/reset-password")
async def admin_reset_password(
    user_id: int,
    payload: AdminResetPasswordRequest,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    target.password_hash = hash_password(payload.new_password)
    await db.flush()
    await record_audit(
        db,
        actor_id=admin.id,
        action="USER_PASSWORD_RESET",
        entity_type="user",
        entity_id=target.id,
        details={"username": target.username},
        ip_address=await resolve_client_ip(request),
    )
    return {"detail": f"Password reset for {target.username}"}
