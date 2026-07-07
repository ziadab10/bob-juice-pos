"""System time synchronization — align database timestamps with client PC clock."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, record_audit, resolve_client_ip
from database import SessionLocal, User, get_db, get_system_settings
from permissions import require_permission

logger = logging.getLogger("bob_juice.time_sync")

router = APIRouter(prefix="/api/system", tags=["System"])

# (table, datetime columns) shifted on sync
_DATETIME_COLUMNS: list[tuple[str, list[str]]] = [
    ("invoices", ["finalized_at", "voided_at"]),
    ("invoice_items", ["line_timestamp"]),
    ("sale_transactions", ["finalized_at"]),
    ("shifts", ["opened_at", "closed_at"]),
    ("stock_movements", ["created_at"]),
    ("expenses", ["created_at"]),
    ("supplier_debts", ["created_at", "settled_at"]),
    ("customer_debts", ["created_at", "settled_at"]),
    ("inventory_intakes", ["created_at"]),
    ("inventory_items", ["created_at"]),
    ("audit_logs", ["created_at"]),
    ("users", ["created_at", "updated_at"]),
    ("branches", ["created_at", "last_sync_at"]),
    ("suppliers", ["created_at"]),
    ("customers", ["created_at"]),
    ("product_categories", ["created_at"]),
    ("global_modifiers", ["created_at"]),
    ("system_settings", ["updated_at", "last_time_sync_at"]),
]


class TimeSyncRequest(BaseModel):
    client_time: str = Field(description="ISO-8601 datetime from the admin PC browser")


class TimeSyncOut(BaseModel):
    server_time_utc: str
    client_time: str
    offset_seconds: int
    tables_updated: int
    columns_updated: int
    message: str
    status: str = "completed"


class TimeStatusOut(BaseModel):
    server_time_utc: str
    last_time_sync_at: str | None
    time_offset_seconds: int


def _parse_client_time(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1]
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid client_time — use ISO-8601") from exc


def _require_settings_perm(user: User) -> None:
    if not require_permission(user, "global_settings"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Settings access not permitted")


async def _shift_column(db: AsyncSession, table: str, column: str, modifier: str) -> int:
    try:
        result = await db.execute(
            text(
                f"UPDATE {table} SET {column} = datetime({column}, :modifier) "
                f"WHERE {column} IS NOT NULL AND TRIM({column}) != ''"
            ),
            {"modifier": modifier},
        )
        return result.rowcount or 0
    except Exception:
        logger.debug("Skip time shift for %s.%s", table, column, exc_info=True)
        return 0


async def apply_time_offset(db: AsyncSession, offset_seconds: int) -> tuple[int, int]:
    if offset_seconds == 0:
        return 0, 0
    sign = "+" if offset_seconds >= 0 else "-"
    modifier = f"{sign}{abs(offset_seconds)} seconds"
    tables = 0
    columns = 0
    for table, cols in _DATETIME_COLUMNS:
        for col in cols:
            count = await _shift_column(db, table, col, modifier)
            if count:
                tables += 1
                columns += 1
    return tables, columns


async def _run_time_sync_background(
    offset_seconds: int,
    client_dt: datetime,
    user_id: int,
    client_time_str: str,
    ip_address: str | None,
) -> None:
    """Heavy timestamp updates run off the request thread so orders stay instant."""
    async with SessionLocal() as db:
        try:
            tables_updated, columns_updated = 0, 0
            if abs(offset_seconds) >= 2:
                tables_updated, columns_updated = await apply_time_offset(db, offset_seconds)

            settings_row = await get_system_settings(db)
            settings_row.time_offset_seconds = int(settings_row.time_offset_seconds or 0) + offset_seconds
            settings_row.last_time_sync_at = client_dt
            settings_row.updated_by_id = user_id
            settings_row.updated_at = client_dt

            await db.execute(
                text("UPDATE branches SET last_sync_at = :ts WHERE last_sync_at IS NOT NULL"),
                {"ts": client_dt.isoformat(sep=" ", timespec="seconds")},
            )

            await record_audit(
                db,
                actor_id=user_id,
                action="TIME_SYNC",
                entity_type="system_settings",
                entity_id=1,
                details={
                    "offset_seconds": offset_seconds,
                    "client_time": client_time_str,
                    "tables_updated": tables_updated,
                    "columns_updated": columns_updated,
                    "async": True,
                },
                ip_address=ip_address,
            )
            await db.commit()
            logger.info(
                "Time sync complete: offset=%ss tables=%s columns=%s",
                offset_seconds,
                tables_updated,
                columns_updated,
            )
        except Exception:
            logger.exception("Background time sync failed")
            await db.rollback()


@router.get("/time-status", response_model=TimeStatusOut)
async def time_status(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_settings_perm(user)
    s = await get_system_settings(db)
    return TimeStatusOut(
        server_time_utc=datetime.utcnow().isoformat(),
        last_time_sync_at=s.last_time_sync_at.isoformat() if s.last_time_sync_at else None,
        time_offset_seconds=int(s.time_offset_seconds or 0),
    )


@router.post("/sync-time", response_model=TimeSyncOut)
async def sync_time(
    payload: TimeSyncRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(get_current_user)],
):
    _require_settings_perm(user)
    client_dt = _parse_client_time(payload.client_time)
    server_now = datetime.utcnow()
    offset_seconds = int((client_dt - server_now).total_seconds())
    ip_address = await resolve_client_ip(request)

    background_tasks.add_task(
        _run_time_sync_background,
        offset_seconds,
        client_dt,
        user.id,
        payload.client_time,
        ip_address,
    )

    if abs(offset_seconds) < 2:
        msg = "System time is already aligned with this PC."
    else:
        msg = (
            f"Time sync started in the background ({offset_seconds:+d}s). "
            "Sales and orders will not be delayed."
        )

    return TimeSyncOut(
        server_time_utc=server_now.isoformat(),
        client_time=client_dt.isoformat(),
        offset_seconds=offset_seconds,
        tables_updated=0,
        columns_updated=0,
        message=msg,
        status="processing",
    )
