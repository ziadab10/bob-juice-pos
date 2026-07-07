"""
30-day multi-branch concurrency simulation — Saida + Beirut isolation audit.

Usage:
    python simulate_multibranch_30day.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal

os.environ.setdefault("THERMAL_PRINT_ENABLED", "false")

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from database import (
    Branch,
    InventoryItem,
    Invoice,
    MenuItem,
    SessionLocal,
    StockMovement,
    User,
    UserRole,
    init_db,
)
from main import app

SAIDA = 1
BEIRUT = 2
INGREDIENT = "Mango Puree"
PRODUCT = "Mango Tango"
DAYS = 30
ORDERS_PER_BRANCH_PER_DAY = 8

_db_lock = asyncio.Lock()


async def _login(client: AsyncClient, username: str, portal: str = "pos") -> None:
    r = await client.post(
        "/api/auth/login",
        data={"username": username, "password": "Cashier@Bob2026!"},
        headers={"X-Bob-Portal": portal},
    )
    assert r.status_code == 200, f"Login failed for {username}: {r.text}"


async def _open_shift(client: AsyncClient) -> None:
    cur = await client.get("/api/sales/shifts/current", headers={"X-Bob-Portal": "pos"})
    if cur.json():
        return
    r = await client.post(
        "/api/sales/shifts/open",
        json={"opening_float_usd": 100, "opening_float_lbp": 0},
        headers={"X-Bob-Portal": "pos"},
    )
    assert r.status_code in (200, 201), r.text


async def _menu_id(client: AsyncClient, name: str) -> int:
    menu = await client.get("/api/sales/menu", headers={"X-Bob-Portal": "pos"})
    assert menu.status_code == 200, menu.text
    payload = menu.json()
    items = payload.get("items") if isinstance(payload, dict) else payload
    row = next((m for m in items if m["name"] == name), None)
    assert row, f"Menu item {name!r} not found"
    return row["id"]


async def _place_order(client: AsyncClient, menu_id: int) -> None:
    r = await client.post(
        "/api/sales/orders",
        json={
            "lines": [{"menu_item_id": menu_id, "quantity": 1}],
            "payment_method": "cash",
            "settlement_currency": "USD",
            "sales_channel": "in_store",
            "amount_tendered": 20,
        },
        headers={"X-Bob-Portal": "pos"},
    )
    assert r.status_code == 201, r.text


async def _place_order_locked(client: AsyncClient, menu_id: int, lock: asyncio.Lock) -> None:
    async with lock:
        await _place_order(client, menu_id)


async def _branch_client(username: str) -> AsyncClient:
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://sim", follow_redirects=True)
    await _login(client, username)
    await _open_shift(client)
    return client


async def _stock(db, branch_id: int, name: str) -> Decimal:
    row = await db.execute(
        select(InventoryItem).where(
            InventoryItem.branch_id == branch_id,
            func.lower(InventoryItem.name) == name.lower(),
        )
    )
    item = row.scalar_one_or_none()
    assert item, f"{name} missing for branch {branch_id}"
    return Decimal(str(item.current_stock))


async def _branch_sales_total(db, branch_id: int) -> Decimal:
    row = await db.execute(
        select(func.coalesce(func.sum(Invoice.net_total_usd), 0)).where(Invoice.branch_id == branch_id)
    )
    return Decimal(str(row.scalar_one()))


async def _reset_ingredient_stock(db, branch_id: int, name: str, qty: Decimal) -> None:
    row = await db.execute(
        select(InventoryItem).where(
            InventoryItem.branch_id == branch_id,
            func.lower(InventoryItem.name) == name.lower(),
        )
    )
    item = row.scalar_one_or_none()
    if item is None:
        item = InventoryItem(name=name, unit="ml", current_stock=qty, branch_id=branch_id)
        db.add(item)
    else:
        item.current_stock = qty
    await db.flush()


async def run_simulation() -> None:
    print("Initializing database schema + multi-branch seed...")
    await init_db()

    seed_stock = Decimal("200000")
    async with SessionLocal() as db:
        await _reset_ingredient_stock(db, SAIDA, INGREDIENT, seed_stock)
        await _reset_ingredient_stock(db, BEIRUT, INGREDIENT, seed_stock)
        await db.commit()
        saida_start = await _stock(db, SAIDA, INGREDIENT)
        beirut_start = await _stock(db, BEIRUT, INGREDIENT)
        recipe = await db.execute(select(MenuItem).where(MenuItem.name == PRODUCT))
        assert recipe.scalar_one_or_none(), f"Product {PRODUCT} not seeded"

    print(f"Starting stock - Saida {INGREDIENT}: {saida_start}, Beirut: {beirut_start}")

    saida_client = await _branch_client("cashier_saida")
    beirut_client = await _branch_client("cashier_beirut")
    saida_menu = await _menu_id(saida_client, PRODUCT)
    beirut_menu = await _menu_id(beirut_client, PRODUCT)

    total_orders = 0
    for day in range(1, DAYS + 1):
        tasks = []
        for _ in range(ORDERS_PER_BRANCH_PER_DAY):
            tasks.append(_place_order_locked(saida_client, saida_menu, _db_lock))
            tasks.append(_place_order_locked(beirut_client, beirut_menu, _db_lock))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures = [r for r in results if isinstance(r, Exception)]
        if failures:
            raise RuntimeError(f"Day {day} failures ({len(failures)}): {failures[0]}")
        total_orders += len(tasks)
        if day % 10 == 0:
            print(f"  Day {day}/{DAYS} - {total_orders} orders placed")

    await saida_client.aclose()
    await beirut_client.aclose()

    per_sale_ml = Decimal("150")
    expected_saida = saida_start - per_sale_ml * DAYS * ORDERS_PER_BRANCH_PER_DAY
    expected_beirut = beirut_start - per_sale_ml * DAYS * ORDERS_PER_BRANCH_PER_DAY

    async with SessionLocal() as db:
        saida_end = await _stock(db, SAIDA, INGREDIENT)
        beirut_end = await _stock(db, BEIRUT, INGREDIENT)
        saida_sales = await _branch_sales_total(db, SAIDA)
        beirut_sales = await _branch_sales_total(db, BEIRUT)

        cross_saida = await db.execute(
            select(func.count())
            .select_from(StockMovement)
            .join(InventoryItem, InventoryItem.id == StockMovement.inventory_item_id)
            .where(
                StockMovement.branch_id == SAIDA,
                InventoryItem.branch_id == BEIRUT,
            )
        )
        cross_beirut = await db.execute(
            select(func.count())
            .select_from(StockMovement)
            .join(InventoryItem, InventoryItem.id == StockMovement.inventory_item_id)
            .where(
                StockMovement.branch_id == BEIRUT,
                InventoryItem.branch_id == SAIDA,
            )
        )

        cross_saida_count = cross_saida.scalar()
        cross_beirut_count = cross_beirut.scalar()

        branches = await db.execute(select(Branch).order_by(Branch.id))
        users = await db.execute(
            select(User.username, User.branch_id).where(User.role == UserRole.CASHIER)
        )

    print("\n=== MULTI-BRANCH AUDIT ===")
    print(f"Branches: {[(b.id, b.name, b.location) for b in branches.scalars().all()]}")
    print(f"Cashiers: {users.all()}")
    print(f"Total concurrent orders: {total_orders}")
    print(f"Saida {INGREDIENT}: {saida_start} -> {saida_end} (expected {expected_saida})")
    print(f"Beirut {INGREDIENT}: {beirut_start} -> {beirut_end} (expected {expected_beirut})")
    print(f"Saida sales USD total: {saida_sales}")
    print(f"Beirut sales USD total: {beirut_sales}")
    print(f"Cross-branch stock movements (must be 0): Saida->Beirut={cross_saida_count}, Beirut->Saida={cross_beirut_count}")

    assert saida_end == expected_saida, f"Saida stock mismatch: {saida_end} != {expected_saida}"
    assert beirut_end == expected_beirut, f"Beirut stock mismatch: {beirut_end} != {expected_beirut}"
    assert cross_saida_count == 0 and cross_beirut_count == 0, "Cross-branch inventory leakage detected"
    assert saida_sales > 0 and beirut_sales > 0, "Branch sales totals must be positive"
    print("\nAll multi-branch simulation audits PASSED.")


def main() -> int:
    try:
        asyncio.run(run_simulation())
        return 0
    except Exception as exc:
        print(f"\nSIMULATION FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
