"""Hybrid offline sync — queue local writes and push to central cloud when online."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import Branch, SessionLocal, SyncOutbox, SyncStatus

logger = logging.getLogger("bob_juice.sync")

_worker_task: asyncio.Task | None = None


async def enqueue_sync(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: str | int,
    payload: dict[str, Any],
    operation: str = "upsert",
) -> None:
    if settings.is_central_server and not settings.central_sync_url:
        return
    row = SyncOutbox(
        branch_id=settings.branch_id,
        entity_type=entity_type,
        entity_id=str(entity_id),
        operation=operation,
        payload_json=json.dumps(payload, default=str),
        status=SyncStatus.PENDING,
    )
    db.add(row)
    await db.flush()


async def sync_status(db: AsyncSession) -> dict[str, Any]:
    pending = await db.execute(
        select(func.count(SyncOutbox.id)).where(
            SyncOutbox.branch_id == settings.branch_id,
            SyncOutbox.status == SyncStatus.PENDING,
        )
    )
    failed = await db.execute(
        select(func.count(SyncOutbox.id)).where(
            SyncOutbox.branch_id == settings.branch_id,
            SyncOutbox.status == SyncStatus.FAILED,
        )
    )
    branch = await db.get(Branch, settings.branch_id)
    return {
        "branch_id": settings.branch_id,
        "branch_code": settings.branch_code,
        "is_central": settings.is_central_server,
        "central_url": settings.central_sync_url or None,
        "pending_count": pending.scalar_one(),
        "failed_count": failed.scalar_one(),
        "last_sync_at": branch.last_sync_at.isoformat() if branch and branch.last_sync_at else None,
        "worker_running": _worker_task is not None and not _worker_task.done(),
    }


async def _push_batch(batch: list[SyncOutbox]) -> tuple[list[int], str | None]:
    if not settings.central_sync_url:
        return [], "Central sync URL not configured"
    payload = {
        "branch_id": settings.branch_id,
        "branch_code": settings.branch_code,
        "records": [
            {
                "id": row.id,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "operation": row.operation,
                "payload": json.loads(row.payload_json),
                "created_at": row.created_at.isoformat(),
            }
            for row in batch
        ],
    }
    headers = {"Content-Type": "application/json"}
    if settings.sync_api_key:
        headers["X-Sync-Key"] = settings.sync_api_key
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{settings.central_sync_url.rstrip('/')}/api/sync/receive", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data.get("synced_ids", [r.id for r in batch]), None
    except Exception as exc:
        return [], str(exc)


async def run_sync_cycle() -> dict[str, Any]:
    if not settings.central_sync_url:
        return {"skipped": True, "reason": "no central url"}
    async with SessionLocal() as db:
        result = await db.execute(
            select(SyncOutbox)
            .where(SyncOutbox.branch_id == settings.branch_id, SyncOutbox.status == SyncStatus.PENDING)
            .order_by(SyncOutbox.created_at.asc())
            .limit(50)
        )
        batch = list(result.scalars().all())
        if not batch:
            return {"synced": 0}
        synced_ids, err = await _push_batch(batch)
        now = datetime.utcnow()
        if err:
            for row in batch:
                row.retry_count += 1
                row.last_error = err
                if row.retry_count >= 10:
                    row.status = SyncStatus.FAILED
            await db.commit()
            logger.warning("Sync push failed: %s", err)
            return {"synced": 0, "error": err}
        await db.execute(
            update(SyncOutbox)
            .where(SyncOutbox.id.in_(synced_ids))
            .values(status=SyncStatus.SYNCED, synced_at=now, last_error=None)
        )
        branch = await db.get(Branch, settings.branch_id)
        if branch:
            branch.last_sync_at = now
        await db.commit()
        logger.info("Synced %d records to central", len(synced_ids))
        return {"synced": len(synced_ids)}


async def _sync_worker_loop() -> None:
    while True:
        try:
            await run_sync_cycle()
        except Exception:
            logger.exception("Sync worker error")
        await asyncio.sleep(max(settings.sync_interval_seconds, 15))


def start_sync_worker() -> None:
    global _worker_task
    if settings.central_sync_url and (_worker_task is None or _worker_task.done()):
        _worker_task = asyncio.create_task(_sync_worker_loop())
        logger.info("Sync worker started (interval=%ss)", settings.sync_interval_seconds)


def stop_sync_worker() -> None:
    global _worker_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        _worker_task = None


async def apply_inbound_records(db: AsyncSession, records: list[dict[str, Any]], branch_id: int) -> list[int]:
    """Central server merges inbound branch records (idempotent upsert stubs)."""
    synced: list[int] = []
    for rec in records:
        synced.append(rec["id"])
    branch = await db.get(Branch, branch_id)
    if branch:
        branch.last_sync_at = datetime.utcnow()
    await db.flush()
    return synced
