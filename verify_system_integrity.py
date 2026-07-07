"""End-to-end audit: units, BOM name matching, extras/excludes deduction math."""

from __future__ import annotations

import asyncio
import sys
import uuid
from decimal import Decimal

from httpx import ASGITransport, AsyncClient

from main import app

ADMIN = {"X-Bob-Portal": "admin"}
DEFAULT_BRANCH_ID = 1


def _ok(r, *codes: int) -> None:
    allowed = codes or (200,)
    assert r.status_code in allowed, r.text


async def login(client: AsyncClient) -> None:
    r = await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "Admin@Bob2026!"},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text


async def stock(client: AsyncClient, name: str) -> Decimal:
    items = (await client.get(f"/api/inventory/items?branch_id={DEFAULT_BRANCH_ID}", headers=ADMIN)).json()
    row = next((i for i in items if i["name"].lower() == name.lower()), None)
    assert row, f"Missing inventory row: {name}"
    return Decimal(str(row["current_stock"]))


async def test_bom_extra_exclude_integrity() -> None:
    tag = uuid.uuid4().hex[:8]
    ashta = f"Ashta Audit {tag}"
    product_name = f"Audit Smoothie {tag}"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await login(client)

        # Stock in 1000 g Ashta
        r = await client.post(
            "/api/inventory/stock-in",
            json={"inventory_item_name": ashta, "quantity": 1000, "unit": "g"},
            headers=ADMIN,
        )
        assert r.status_code == 200, r.text
        assert await stock(client, ashta) == Decimal("1000")

        inv_items = (await client.get(f"/api/inventory/items?branch_id={DEFAULT_BRANCH_ID}", headers=ADMIN)).json()
        ashta_id = next(i["id"] for i in inv_items if i["name"] == ashta)

        # Create catalog product + BOM 30g Ashta per sale
        cat = (await client.get("/api/catalog/categories", headers=ADMIN)).json()[0]
        r = await client.post(
            "/api/catalog/products",
            json={
                "name": product_name,
                "category_id": cat["id"],
                "unit_price": 5.0,
                "sizes_enabled": False,
            },
            headers=ADMIN,
        )
        assert r.status_code in (200, 201), r.text
        product_id = r.json()["id"]

        r = await client.put(
            f"/api/inventory/recipes/product/{product_id}",
            json={"base": [{"inventory_item_id": ashta_id, "quantity_per_sale": 30, "metric_unit": "g"}]},
            headers=ADMIN,
        )
        assert r.status_code == 200, r.text

        # Global extra +30g Ashta
        r = await client.post(
            "/api/inventory/global-modifiers",
            json={
                "name": f"Extra {ashta}",
                "inventory_item_id": ashta_id,
                "extra_price_usd": 1.0,
                "quantity_per_sale": 30,
                "metric_unit": "g",
            },
            headers=ADMIN,
        )
        assert r.status_code == 201, r.text
        mod_id = r.json()["id"]

        shift = (await client.get("/api/sales/shifts/current", headers=ADMIN)).json()
        if not shift:
            r = await client.post(
                "/api/sales/shifts/open",
                json={"opening_float_usd": 100, "opening_float_lbp": 0},
                headers=ADMIN,
            )
            _ok(r, 201)

        # Sale with extra (+30g) => total -60g
        r = await client.post(
            "/api/sales/orders",
            json={
                "payment_method": "cash",
                "sales_channel": "in_store",
                "settlement_currency": "USD",
                "discount_type": "none",
                "discount_value": 0,
                "amount_tendered": 20,
                "lines": [
                    {
                        "menu_item_id": product_id,
                        "quantity": 1,
                        "extras": [
                            {
                                "inventory_item_id": ashta_id,
                                "quantity": 30,
                                "extra_price_usd": 1.0,
                                "name": ashta,
                            }
                        ],
                        "excludes": [],
                    }
                ],
            },
            headers=ADMIN,
        )
        _ok(r, 201)
        assert await stock(client, ashta) == Decimal("940"), "Expected 1000 - 30 base - 30 extra"

        # Sale with exclude => skip base 30g, only extra if added — here exclude only
        r = await client.post(
            "/api/sales/orders",
            json={
                "payment_method": "cash",
                "sales_channel": "in_store",
                "settlement_currency": "USD",
                "discount_type": "none",
                "discount_value": 0,
                "amount_tendered": 20,
                "lines": [
                    {
                        "menu_item_id": product_id,
                        "quantity": 1,
                        "extras": [],
                        "excludes": [
                            {
                                "inventory_item_id": ashta_id,
                                "quantity": 30,
                                "name": ashta,
                            }
                        ],
                    }
                ],
            },
            headers=ADMIN,
        )
        _ok(r, 201)
        assert await stock(client, ashta) == Decimal("940"), "Exclude must skip base BOM deduction"

        # Duplicate name stock-in merges balance (registry)
        r = await client.post(
            "/api/inventory/stock-in",
            json={"inventory_item_name": ashta, "quantity": 500, "unit": "g"},
            headers=ADMIN,
        )
        assert r.status_code == 200, r.text
        assert await stock(client, ashta) == Decimal("1440")

        dup = await client.post(
            "/api/inventory/items",
            json={"name": ashta.upper(), "unit": "g"},
            headers=ADMIN,
        )
        assert dup.status_code == 409, "Duplicate ingredient name must be rejected"

        # Suggest API returns locked row
        sug = (await client.get(f"/api/inventory/items/suggest?q={ashta[:6]}", headers=ADMIN)).json()
        assert any(s["id"] == ashta_id for s in sug)

        # Deactivate modifier cleanup not required for test
        await client.delete(f"/api/inventory/global-modifiers/{mod_id}", headers=ADMIN)


async def test_logo_assets() -> None:
    from logo_assets import load_logo_rgb, logo_pdf_png_bytes, logo_receipt_png_bytes, materialize_circular_logo

    materialize_circular_logo()
    rgb = load_logo_rgb()
    assert rgb is not None, "logo_circular.png missing from static/"
    assert rgb.mode == "RGB"
    pdf = logo_pdf_png_bytes()
    assert pdf and len(pdf) > 100
    web = logo_receipt_png_bytes()
    assert web and len(web) > 100


async def test_supplier_customer_suggest() -> None:
    tag = uuid.uuid4().hex[:8]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await login(client)
        sname = f"Supplier Audit {tag}"
        cname = f"Customer Audit {tag}"
        r = await client.post(
            "/api/finance/suppliers",
            json={"name": sname, "category": "Produce"},
            headers=ADMIN,
        )
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        r = await client.post(
            "/api/finance/customers",
            json={"name": cname, "contact_phone": "123"},
            headers=ADMIN,
        )
        assert r.status_code == 201, r.text
        cid = r.json()["id"]

        sug_s = (await client.get(f"/api/finance/suppliers/suggest?q=Supplier Audit", headers=ADMIN)).json()
        assert any(x["id"] == sid for x in sug_s)
        sug_c = (await client.get(f"/api/finance/customers/suggest?q=Customer Audit", headers=ADMIN)).json()
        assert any(x["id"] == cid for x in sug_c)

        dup = await client.post(
            "/api/finance/suppliers",
            json={"name": sname.lower(), "category": "Other"},
            headers=ADMIN,
        )
        assert dup.status_code == 409, dup.text


async def main() -> int:
    await test_logo_assets()
    await test_bom_extra_exclude_integrity()
    await test_supplier_customer_suggest()
    print("System integrity audit passed (logo, BOM/extras/excludes, registry, suggest APIs).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
