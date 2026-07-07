"""Sync API routes for hybrid multi-branch deployment."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from config import settings
from database import User, get_db
from permissions import require_permission
from sync_service import apply_inbound_records, run_sync_cycle, sync_status

router = APIRouter(prefix="/api/sync", tags=["Sync"])


class InboundSyncPayload(BaseModel):
    branch_id: int
    branch_code: str
    records: list[dict[str, Any]]


@router.get("/status")
async def get_sync_status(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not require_permission(user, "sales_reports") and user.role.value == "cashier":
        raise HTTPException(status_code=403, detail="Access denied")
    return await sync_status(db)


@router.post("/push")
async def manual_sync_push(
    user: Annotated[User, Depends(get_current_user)],
):
    if not require_permission(user, "global_settings"):
        raise HTTPException(status_code=403, detail="Admin required")
    return await run_sync_cycle()


@router.post("/receive")
async def receive_sync_batch(
    payload: InboundSyncPayload,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_sync_key: str | None = Header(default=None),
):
    if not settings.is_central_server:
        raise HTTPException(status_code=400, detail="This node is not configured as central server")
    if settings.sync_api_key and x_sync_key != settings.sync_api_key:
        raise HTTPException(status_code=401, detail="Invalid sync API key")
    synced_ids = await apply_inbound_records(db, payload.records, payload.branch_id)
    return {"synced_ids": synced_ids, "count": len(synced_ids)}
