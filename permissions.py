"""Centralized RBAC permission definitions and resolution."""

from __future__ import annotations

import json

from database import User, UserRole

ALL_PERMISSIONS = [
    "pos",
    "close_shift",
    "sales_reports",
    "inventory",
    "catalog",
    "expenses",
    "suppliers",
    "shift_audits",
    "global_settings",
    "user_management",
    "admin_dashboard",
]

ROLE_DEFAULTS: dict[UserRole, dict[str, bool]] = {
    UserRole.ADMIN: {p: True for p in ALL_PERMISSIONS},
    UserRole.MANAGER: {
        "pos": True,
        "close_shift": True,
        "sales_reports": True,
        "inventory": True,
        "catalog": True,
        "expenses": True,
        "suppliers": True,
        "shift_audits": True,
        "global_settings": False,
        "user_management": False,
        "admin_dashboard": True,
    },
    UserRole.CASHIER: {
        "pos": True,
        "close_shift": True,
        "sales_reports": False,
        "inventory": False,
        "catalog": False,
        "expenses": False,
        "suppliers": False,
        "shift_audits": False,
        "global_settings": False,
        "user_management": False,
        "admin_dashboard": False,
    },
}

ROLE_LABELS = {
    UserRole.ADMIN: "Admin",
    UserRole.MANAGER: "Manager",
    UserRole.CASHIER: "Cashier",
}


def role_display(role: UserRole) -> str:
    return ROLE_LABELS.get(role, role.value.title())


def resolve_permissions(user: User) -> dict[str, bool]:
    base = dict(ROLE_DEFAULTS.get(user.role, ROLE_DEFAULTS[UserRole.CASHIER]))
    if user.permissions_json:
        try:
            overrides = json.loads(user.permissions_json)
            if isinstance(overrides, dict):
                for key, val in overrides.items():
                    if key in ALL_PERMISSIONS and isinstance(val, bool):
                        base[key] = val
        except json.JSONDecodeError:
            pass
    return base


def require_permission(user: User, permission: str) -> bool:
    return resolve_permissions(user).get(permission, False)
