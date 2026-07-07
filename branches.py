"""Branch registry — multi-branch management API."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, record_audit, resolve_client_ip
from database import Branch, User, get_db, seed_branch_inventory
from permissions import require_permission

router = APIRouter(prefix="/api/branches", tags=["Branches"])


class BranchOut(BaseModel):
    id: int
    code: str
    name: str
    location: str | None = None
    address: str | None = None
    phone: str | None = None
    is_active: bool
    is_central: bool

    model_config = {"from_attributes": True}


class BranchCreate(BaseModel):
    code: str | None = Field(default=None, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    location: str | None = Field(default=None, max_length=256)
    address: str | None = Field(default=None, max_length=256)
    phone: str | None = Field(default=None, max_length=32)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Branch name must be at least 2 characters")
        return s

    @field_validator("code")
    @classmethod
    def code_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Branch code must be at least 2 characters")
        return s


class BranchUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=2, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    location: str | None = Field(default=None, max_length=256)
    address: str | None = Field(default=None, max_length=256)
    phone: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Branch name must be at least 2 characters")
        return s

    @field_validator("code")
    @classmethod
    def code_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Branch code must be at least 2 characters")
        return s


def _require_settings(user: User) -> None:
    if not require_permission(user, "global_settings"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Settings access not permitted")


def _slug_code(name: str) -> str:
    letters = re.sub(r"[^A-Za-z0-9]", "", name.upper())
    return (letters[:10] or "BRANCH")


async def _unique_code(db: AsyncSession, base: str) -> str:
    code = base[:32]
    exists = await db.execute(select(Branch).where(func.lower(Branch.code) == code.lower()))
    if not exists.scalar_one_or_none():
        return code
    for i in range(2, 100):
        candidate = f"{code[:28]}{i}"
        exists = await db.execute(select(Branch).where(func.lower(Branch.code) == candidate.lower()))
        if not exists.scalar_one_or_none():
            return candidate
    raise HTTPException(status_code=409, detail="Could not allocate unique branch code")


def _branch_out(b: Branch) -> BranchOut:
    return BranchOut(
        id=b.id,
        code=b.code,
        name=b.name,
        location=b.location or b.address,
        address=b.address,
        phone=b.phone,
        is_active=b.is_active,
        is_central=b.is_central,
    )


@router.get("", response_model=list[BranchOut])
async def list_branches(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    active_only: bool = Query(True),
):
    q = select(Branch).order_by(Branch.id)
    if active_only:
        q = q.where(Branch.is_active.is_(True))
    rows = await db.execute(q)
    return [_branch_out(b) for b in rows.scalars().all()]


@router.get("/{branch_id}", response_model=BranchOut)
async def get_branch(
    branch_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    branch = await db.get(Branch, branch_id)
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    return _branch_out(branch)


@router.post("", response_model=BranchOut, status_code=status.HTTP_201_CREATED)
async def create_branch(
    payload: BranchCreate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_settings(user)
    name = payload.name.strip()
    base_code = (payload.code or _slug_code(name)).strip().upper()
    code = await _unique_code(db, base_code)

    branch = Branch(
        code=code,
        name=name,
        location=(payload.location or "").strip() or None,
        address=(payload.address or payload.location or "").strip() or None,
        phone=(payload.phone or "").strip() or None,
        is_active=True,
        is_central=False,
    )
    db.add(branch)
    await db.flush()
    await seed_branch_inventory(db, branch.id)
    await record_audit(
        db,
        actor_id=user.id,
        action="BRANCH_CREATE",
        entity_type="branch",
        entity_id=branch.id,
        details={"code": branch.code, "name": branch.name},
        ip_address=await resolve_client_ip(request),
    )
    return _branch_out(branch)


@router.patch("/{branch_id}", response_model=BranchOut)
async def update_branch(
    branch_id: int,
    payload: BranchUpdate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_settings(user)
    branch = await db.get(Branch, branch_id)
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")

    if payload.code is not None:
        new_code = payload.code.strip().upper()
        clash = await db.execute(
            select(Branch).where(func.lower(Branch.code) == new_code.lower(), Branch.id != branch_id)
        )
        if clash.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Branch code already in use")
        branch.code = new_code
    if payload.name is not None:
        name = payload.name.strip()
        if len(name) < 2:
            raise HTTPException(status_code=422, detail="Branch name must be at least 2 characters")
        branch.name = name
    if payload.location is not None:
        branch.location = payload.location.strip() or None
    if payload.address is not None:
        branch.address = payload.address.strip() or None
    if payload.phone is not None:
        branch.phone = payload.phone.strip() or None
    if payload.is_active is not None:
        branch.is_active = payload.is_active

    await db.flush()
    await record_audit(
        db,
        actor_id=user.id,
        action="BRANCH_UPDATE",
        entity_type="branch",
        entity_id=branch.id,
        details={"code": branch.code, "name": branch.name, "is_active": branch.is_active},
        ip_address=await resolve_client_ip(request),
    )
    return _branch_out(branch)
