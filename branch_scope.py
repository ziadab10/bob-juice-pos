"""Multi-branch scope helpers — per-user branch assignment and admin filters."""

from __future__ import annotations

from fastapi import HTTPException, status

from config import settings
from database import User, UserRole


def user_branch_id(user: User) -> int | None:
    return getattr(user, "branch_id", None)


def require_operational_branch_id(user: User, *, admin_branch_override: int | None = None) -> int:
    """Branch used for POS sales, shifts, and inventory mutations."""
    if user.role == UserRole.ADMIN and admin_branch_override is not None:
        return admin_branch_override
    bid = user_branch_id(user)
    if bid is not None:
        return int(bid)
    if user.role == UserRole.ADMIN:
        return settings.branch_id
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Cashier is not assigned to a branch — contact administrator",
    )


def resolve_report_branch_filter(user: User, branch_id: int | None) -> int | None:
    """Admin: None = consolidated all branches; int = single branch. Cashier: own branch only."""
    if user.role == UserRole.ADMIN:
        return branch_id
    return require_operational_branch_id(user)


def resolve_inventory_branch_filter(user: User, branch_id: int | None) -> int:
    """Inventory reads/writes — admin may pass branch_id; cashiers locked to assigned branch."""
    if user.role == UserRole.ADMIN:
        if branch_id is not None:
            return branch_id
        return settings.branch_id
    return require_operational_branch_id(user)
