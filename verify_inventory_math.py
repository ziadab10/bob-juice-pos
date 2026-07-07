"""Validate inventory stock-in, intake, and stock-out math against the live DB layer."""

from __future__ import annotations

import asyncio
import sys
import uuid
from decimal import Decimal

from httpx import ASGITransport, AsyncClient

from main import app

ADMIN_HEADERS = {"X-Bob-Portal": "admin"}
DEFAULT_BRANCH_ID = 1


async def login_admin(client: AsyncClient) -> None:
    res = await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "Admin@Bob2026!"},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200, res.text


async def item_stock(client: AsyncClient, name: str) -> Decimal:
    items = (
        await client.get(f"/api/inventory/items?branch_id={DEFAULT_BRANCH_ID}", headers=ADMIN_HEADERS)
    ).json()
    match = next((i for i in items if i["name"].lower() == name.lower()), None)
    assert match, f"Item {name!r} not found"
    return Decimal(str(match["current_stock"]))


async def test_stock_in_out_and_intake_no_duplication() -> None:
    tag = uuid.uuid4().hex[:8]
    item_name = f"Test Mango {tag}"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await login_admin(client)

        # General stock-in (+10)
        r = await client.post(
            "/api/inventory/stock-in",
            json={
                "inventory_item_name": item_name,
                "quantity": 10,
                "unit": "kg",
                "notes": "validation stock-in",
            },
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200, r.text
        assert await item_stock(client, item_name) == Decimal("10000")

        # Purchase intake (+5 kg → +5000 g)
        r = await client.post(
            f"/api/finance/intakes?branch_id={DEFAULT_BRANCH_ID}",
            json={
                "supplier_name": f"Supplier {tag}",
                "inventory_item_name": item_name,
                "quantity": 5,
                "unit": "kg",
                "unit_cost_usd": 2.5,
                "payment_status": "paid",
                "amount_paid_usd": 12.5,
                "intake_date": "2026-07-02",
            },
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text
        assert await item_stock(client, item_name) == Decimal("15000")

        # Stock out (-4000 g)
        r = await client.post(
            "/api/inventory/stock-out",
            json={"inventory_item_name": item_name, "quantity": 4000, "unit": "g", "notes": "validation stock-out"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200, r.text
        assert await item_stock(client, item_name) == Decimal("11000")

        # Insufficient stock rejected
        r = await client.post(
            "/api/inventory/stock-out",
            json={"inventory_item_name": item_name, "quantity": 99999, "unit": "g"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400, r.text
        assert await item_stock(client, item_name) == Decimal("11000")


async def main() -> int:
    await test_stock_in_out_and_intake_no_duplication()
    print("Inventory math validation passed (stock-in + intake + stock-out).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
