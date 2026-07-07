"""System-wide database backup, restore, and clear — admin-only."""

from __future__ import annotations

import asyncio
import gc
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import database as db_module
from auth import get_current_user, record_audit, resolve_authenticated_user, resolve_client_ip
from config import DB_PATH, FRESH_DB_MARKER
from database import (
    User,
    UserRole,
    checkpoint_database_file,
    clear_operational_data,
    release_database_for_file_swap,
    replace_database_file,
)
from finance import _require_perm
from sync_service import start_sync_worker, stop_sync_worker

logger = logging.getLogger("bob_juice.system_data")

router = APIRouter(prefix="/api/system/data", tags=["System Data"])

CLEAR_CONFIRM_PHRASE = "WIPE ALL DATA"
SQLITE_HEADER = b"SQLite format 3\x00"


class ClearDataRequest(BaseModel):
    confirm: bool = False
    confirm_phrase: str = Field(default="", max_length=64)


class DataActionOut(BaseModel):
    status: str
    message: str
    reload_required: bool = False


def _require_system_admin(user: User) -> None:
    _require_perm(user, "global_settings")
    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can manage system data",
        )


def _backup_filename() -> str:
    return f"bob_juice_backup_{datetime.now().strftime('%Y_%m_%d')}.db"


def _remove_wal_sidecars(*, retries: int = 5) -> None:
    """Best-effort WAL/SHM cleanup (Windows may briefly lock files after dispose)."""
    import time

    for path in (Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")):
        if not path.is_file():
            continue
        for attempt in range(retries):
            try:
                path.unlink()
                break
            except PermissionError:
                if attempt + 1 >= retries:
                    logger.warning("Could not remove %s — continuing restore", path)
                else:
                    time.sleep(0.15 * (attempt + 1))
            except OSError as exc:
                logger.warning("Could not remove %s: %s", path, exc)
                break


async def _authenticate_restore_admin(request: Request) -> int:
    """Verify admin in a short-lived session so no connection is held during file swap."""
    auth = request.headers.get("Authorization", "")
    header_token = auth[7:] if auth.startswith("Bearer ") else None
    async with db_module.SessionLocal() as db:
        user = await resolve_authenticated_user(request, header_token, db, portal="admin")
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        _require_system_admin(user)
        actor_id = user.id
        await db.commit()
    return actor_id


async def _write_audit(action: str, request: Request, user: User, details: dict) -> None:
    async with db_module.SessionLocal() as db:
        await record_audit(
            db,
            actor_id=user.id,
            action=action,
            entity_type="database",
            entity_id=0,
            details=details,
            ip_address=await resolve_client_ip(request),
        )
        await db.commit()


async def _write_audit_by_id(action: str, request: Request, actor_id: int, details: dict) -> None:
    async with db_module.SessionLocal() as db:
        await record_audit(
            db,
            actor_id=actor_id,
            action=action,
            entity_type="database",
            entity_id=0,
            details=details,
            ip_address=await resolve_client_ip(request),
        )
        await db.commit()


@router.get("/backup")
async def backup_database(user: Annotated[User, Depends(get_current_user)]):
    _require_system_admin(user)
    if not DB_PATH.is_file():
        raise HTTPException(status_code=404, detail="Database file not found")
    await checkpoint_database_file()
    return FileResponse(
        path=str(DB_PATH.resolve()),
        media_type="application/octet-stream",
        filename=_backup_filename(),
    )


@router.post("/clear", response_model=DataActionOut)
async def clear_database(
    payload: ClearDataRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
):
    _require_system_admin(user)
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    if payload.confirm_phrase.strip().upper() != CLEAR_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail=f'Type confirm_phrase="{CLEAR_CONFIRM_PHRASE}" to proceed',
        )

    try:
        await checkpoint_database_file()
        await clear_operational_data()
        FRESH_DB_MARKER.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
        await _write_audit("SYSTEM_CLEAR_DATA", request, user, {"preserved": "admin_users"})
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Clear data failed")
        raise HTTPException(status_code=500, detail=f"Clear data failed: {exc}") from exc

    return DataActionOut(
        status="cleared",
        message="All products, suppliers, customers, and transaction histories were removed. Master admin login preserved and verified.",
        reload_required=True,
    )


@router.post("/restore", response_model=DataActionOut)
async def restore_database(
    request: Request,
    file: UploadFile = File(...),
):
    """Restore bob_juice.db from upload — disposes engine before overwrite to avoid file locks."""
    actor_id = await _authenticate_restore_admin(request)

    filename = (file.filename or "").strip().lower()
    if not filename.endswith(".db"):
        raise HTTPException(status_code=400, detail="Upload a valid .db SQLite backup file")

    content = await file.read()
    if len(content) < 512:
        raise HTTPException(status_code=400, detail="Backup file is too small")
    if not content.startswith(SQLITE_HEADER):
        raise HTTPException(status_code=400, detail="Invalid SQLite database file")

    stop_sync_worker()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        await release_database_for_file_swap()
        gc.collect()
        await asyncio.sleep(0.15)

        replace_database_file(content, stamp=stamp)
        FRESH_DB_MARKER.unlink(missing_ok=True)

        await db_module.reinitialize_engine()
        await _write_audit_by_id(
            "SYSTEM_RESTORE_DATA",
            request,
            actor_id,
            {"filename": file.filename or "upload.db"},
        )
    except HTTPException:
        raise
    except PermissionError as exc:
        logger.exception("Database restore blocked by file lock")
        try:
            await db_module.reinitialize_engine()
        except Exception:
            logger.exception("Engine reinit after failed restore")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Database restore failed")
        try:
            await db_module.reinitialize_engine()
        except Exception:
            logger.exception("Engine reinit after failed restore")
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}") from exc
    finally:
        start_sync_worker()

    return DataActionOut(
        status="restored",
        message="Database restored from backup. Reloading application state.",
        reload_required=True,
    )
