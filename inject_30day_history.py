"""
Time-travel data injection — 30 days of realistic Saida Branch sales history.

Creates backdated invoices, sale transactions, shifts, and BOM-accurate inventory
deductions so the Admin Dashboard appears fully operational for one month.

Usage:
    python inject_30day_history.py
"""

from __future__ import annotations

import asyncio
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import selectinload

from database import (
    SEED_INVENTORY,
    Currency,
    DiscountType,
    InventoryItem,
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    MenuItem,
    PaymentMethod,
    ProductRecipe,
    RecipeRole,
    SaleNumberCounter,
    SaleTransaction,
    Shift,
    StockMovement,
    StockMovementType,
    User,
    UserRole,
    SessionLocal,
    apply_platform_commission,
    channel_commission_pct,
    compute_discount,
    get_system_settings,
    init_db,
    usd_to_lbp,
)
from inventory import deduct_inventory_for_sale
from sales import allocate_sale_number, build_items_summary, record_sale_transaction

PRIMARY_BRANCH = 1
DAYS = 30
RNG = random.Random(20260706)

# Weighted menu mix — BOM products dominate; totals stay within seed stock.
PRODUCT_WEIGHTS: list[tuple[str, float]] = [
    ("Berry Blast Smoothie", 0.22),
    ("Mango Tango", 0.20),
    ("Green Glow Detox", 0.18),
    ("Nutella Banana Crepe", 0.14),
    ("Sunrise Citrus Cooler", 0.10),
    ("Berry Bliss Crepe", 0.08),
    ("Tropical Sunset", 0.08),
]

# Daily rush windows: (hour_start, hour_end, share_of_daily_orders)
RUSH_WINDOWS = [
    ((7, 10), 0.40),   # morning rush
    ((12, 15), 0.20),  # afternoon slump
    ((17, 21), 0.40),  # night rush
]

PAYMENT_METHODS = [
    (PaymentMethod.CASH, 0.72),
    (PaymentMethod.CARD, 0.18),
    (PaymentMethod.WISH, 0.10),
]

SALES_CHANNELS = [
    ("in_store", 0.88),
    ("toters", 0.08),
    ("talabna", 0.04),
]


@dataclass
class PlannedLine:
    menu_item_id: int
    menu_name: str
    quantity: int
    unit_price: Decimal


@dataclass
class PlannedOrder:
    sale_at: datetime
    cashier_id: int
    cashier_name: str
    lines: list[PlannedLine]
    payment_method: PaymentMethod
    sales_channel: str


@dataclass
class ShiftBucket:
    shift: Shift
    cash_sales_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    invoice_count: int = 0


def _pick_weighted(pairs: list[tuple]) -> object:
    total = sum(w for _, w in pairs)
    roll = RNG.random() * total
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if roll <= acc:
            return value
    return pairs[-1][0]


def _random_time_in_window(day: date, hour_start: int, hour_end: int) -> datetime:
    hour = RNG.randint(hour_start, max(hour_start, hour_end - 1))
    minute = RNG.randint(0, 59)
    second = RNG.randint(0, 59)
    return datetime.combine(day, time(hour, minute, second))


def _orders_for_day(day: date, is_weekend: bool) -> int:
    base = RNG.randint(28, 38)
    if is_weekend:
        base += RNG.randint(4, 10)
    return base


def _invoice_number(branch_id: int, day: date, seq: int) -> str:
    return f"BJ-{branch_id}-{day.strftime('%Y%m%d')}-{seq:05d}"


async def clear_branch_sales_history(db, branch_id: int) -> None:
    inv_ids = (
        await db.execute(select(Invoice.id).where(Invoice.branch_id == branch_id))
    ).scalars().all()
    if inv_ids:
        await db.execute(delete(SaleTransaction).where(SaleTransaction.invoice_id.in_(inv_ids)))
        await db.execute(delete(InvoiceItem).where(InvoiceItem.invoice_id.in_(inv_ids)))
    await db.execute(delete(Invoice).where(Invoice.branch_id == branch_id))
    await db.execute(delete(StockMovement).where(StockMovement.branch_id == branch_id))
    await db.execute(delete(Shift).where(Shift.branch_id == branch_id))
    await db.flush()


async def reset_branch_inventory(db, branch_id: int) -> dict[str, Decimal]:
    starting: dict[str, Decimal] = {}
    for name, unit, stock in SEED_INVENTORY:
        row = await db.execute(
            select(InventoryItem).where(
                InventoryItem.branch_id == branch_id,
                func.lower(InventoryItem.name) == name.lower(),
            )
        )
        item = row.scalar_one_or_none()
        if item is None:
            item = InventoryItem(name=name, unit=unit, current_stock=stock, branch_id=branch_id)
            db.add(item)
        else:
            item.current_stock = stock
            item.is_active = True
        starting[name.lower()] = Decimal(str(stock))
    await db.flush()
    return starting


async def load_branch_cashiers(db, branch_id: int) -> list[User]:
    rows = await db.execute(
        select(User).where(
            User.role == UserRole.CASHIER,
            User.branch_id == branch_id,
            User.is_active.is_(True),
        )
    )
    cashiers = list(rows.scalars().all())
    if not cashiers:
        raise RuntimeError(f"No cashiers found for branch {branch_id}")
    return cashiers


async def load_menu(db) -> dict[str, MenuItem]:
    rows = await db.execute(select(MenuItem).where(MenuItem.is_active.is_(True)))
    menu = {m.name: m for m in rows.scalars().all()}
    missing = [n for n, _ in PRODUCT_WEIGHTS if n not in menu]
    if missing:
        raise RuntimeError(f"Menu items missing from catalog: {missing}")
    return menu


async def load_recipe_map(db, branch_id: int) -> dict[str, list[tuple[str, Decimal]]]:
    """menu_name -> [(ingredient_name, qty_per_sale), ...]"""
    rows = await db.execute(
        select(ProductRecipe, MenuItem.name, InventoryItem.name)
        .join(MenuItem, MenuItem.id == ProductRecipe.menu_item_id)
        .join(InventoryItem, InventoryItem.id == ProductRecipe.inventory_item_id)
        .where(
            ProductRecipe.recipe_role == RecipeRole.BASE.value,
            InventoryItem.branch_id == branch_id,
        )
    )
    out: dict[str, list[tuple[str, Decimal]]] = defaultdict(list)
    for recipe, menu_name, inv_name in rows.all():
        out[menu_name].append((inv_name, Decimal(str(recipe.quantity_per_sale))))
    return out


def plan_orders(
    menu: dict[str, MenuItem],
    cashiers: list[User],
    start_day: date,
    days: int,
) -> list[PlannedOrder]:
    planned: list[PlannedOrder] = []
    names = [n for n, _ in PRODUCT_WEIGHTS]
    weights = [w for _, w in PRODUCT_WEIGHTS]

    for offset in range(days):
        day = start_day + timedelta(days=offset)
        is_weekend = day.weekday() >= 5
        daily_count = _orders_for_day(day, is_weekend)
        per_window = [max(1, int(daily_count * share)) for _, share in RUSH_WINDOWS]
        # Normalize window counts to exact daily total
        diff = daily_count - sum(per_window)
        per_window[-1] += diff

        day_seq = 0
        for (hour_start, hour_end), count in zip((w[0] for w in RUSH_WINDOWS), per_window):
            for _ in range(count):
                cashier = RNG.choice(cashiers)
                line_count = 1 if RNG.random() < 0.78 else 2
                lines: list[PlannedLine] = []
                for _ in range(line_count):
                    menu_name = RNG.choices(names, weights=weights, k=1)[0]
                    item = menu[menu_name]
                    qty = 1 if RNG.random() < 0.92 else 2
                    lines.append(
                        PlannedLine(
                            menu_item_id=item.id,
                            menu_name=menu_name,
                            quantity=qty,
                            unit_price=Decimal(str(item.unit_price)),
                        )
                    )
                sale_at = _random_time_in_window(day, hour_start, hour_end)
                payment = _pick_weighted(PAYMENT_METHODS)
                channel = _pick_weighted(SALES_CHANNELS)
                planned.append(
                    PlannedOrder(
                        sale_at=sale_at,
                        cashier_id=cashier.id,
                        cashier_name=cashier.full_name,
                        lines=lines,
                        payment_method=payment,
                        sales_channel=channel,
                    )
                )
                day_seq += 1

    planned.sort(key=lambda o: o.sale_at)
    return planned


def simulate_ingredient_usage(
    orders: list[PlannedOrder],
    recipes: dict[str, list[tuple[str, Decimal]]],
) -> dict[str, Decimal]:
    usage: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for order in orders:
        for line in order.lines:
            for ing_name, per_sale in recipes.get(line.menu_name, []):
                usage[ing_name.lower()] += per_sale * Decimal(line.quantity)
    return dict(usage)


def trim_orders_to_stock(
    orders: list[PlannedOrder],
    recipes: dict[str, list[tuple[str, Decimal]]],
    starting: dict[str, Decimal],
) -> list[PlannedOrder]:
    """Drop orders (from the end) that would exceed starting inventory."""
    kept: list[PlannedOrder] = []
    usage: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    for order in orders:
        trial = defaultdict(lambda: Decimal("0"))
        for line in order.lines:
            for ing_name, per_sale in recipes.get(line.menu_name, []):
                trial[ing_name.lower()] += per_sale * Decimal(line.quantity)
        overflow = False
        for ing, qty in trial.items():
            if usage[ing] + qty > starting.get(ing, Decimal("999999999")):
                overflow = True
                break
        if overflow:
            continue
        for ing, qty in trial.items():
            usage[ing] += qty
        kept.append(order)
    return kept


async def get_or_create_shift(
    db,
    cache: dict[tuple[int, date], ShiftBucket],
    cashier: User,
    sale_day: date,
    sale_at: datetime,
    rate: Decimal,
) -> Shift:
    key = (cashier.id, sale_day)
    if key in cache:
        return cache[key].shift

    opened = datetime.combine(sale_day, time(6, 30)) + timedelta(minutes=RNG.randint(0, 45))
    shift = Shift(
        branch_id=PRIMARY_BRANCH,
        operator_id=cashier.id,
        opened_at=opened,
        exchange_rate_snapshot=rate,
        opening_float_usd=Decimal("100.00"),
        opening_float_lbp=Decimal("0"),
        opening_float=Decimal("100.00"),
    )
    db.add(shift)
    await db.flush()
    cache[key] = ShiftBucket(shift=shift)
    return shift


async def inject_order(
    db,
    order: PlannedOrder,
    shift_cache: dict[tuple[int, date], ShiftBucket],
    rate: Decimal,
    settings_row,
    day_counters: dict[date, int],
) -> tuple[Invoice, list[InvoiceItem]]:
    sale_day = order.sale_at.date()
    day_counters[sale_day] = day_counters.get(sale_day, 0) + 1
    seq = day_counters[sale_day]

    cashier = await db.get(User, order.cashier_id)
    assert cashier
    shift = await get_or_create_shift(db, shift_cache, cashier, sale_day, order.sale_at, rate)
    bucket = shift_cache[(cashier.id, sale_day)]

    from database import SalesChannel

    channel_enum = SalesChannel(order.sales_channel)
    commission_pct = channel_commission_pct(channel_enum, settings_row)

    subtotal_usd = Decimal("0.00")
    item_rows: list[InvoiceItem] = []
    for line in order.lines:
        line_usd = line.unit_price * line.quantity
        subtotal_usd += line_usd
        item_rows.append(
            InvoiceItem(
                branch_id=PRIMARY_BRANCH,
                menu_item_id=line.menu_item_id,
                operator_id=cashier.id,
                line_timestamp=order.sale_at,
                name_snapshot=line.menu_name,
                unit_price_snapshot=line.unit_price,
                quantity=line.quantity,
                line_total=line_usd,
                line_total_lbp=usd_to_lbp(line_usd, rate),
                line_modifiers_json=None,
            )
        )

    discount_usd, discount_lbp = compute_discount(
        subtotal_usd, rate, DiscountType.NONE, Decimal("0")
    )
    after_discount_usd = max(subtotal_usd - discount_usd, Decimal("0.00"))
    _, commission_amt, net_usd = apply_platform_commission(after_discount_usd, commission_pct)
    subtotal_lbp = usd_to_lbp(subtotal_usd, rate)
    total_lbp = usd_to_lbp(net_usd, rate)

    amount_due = net_usd
    amount_tendered = None
    change_given = None
    if order.payment_method == PaymentMethod.CASH:
        tender = (amount_due + Decimal(RNG.randint(1, 10))).quantize(Decimal("0.01"))
        amount_tendered = tender
        change_given = tender - amount_due

    inv_number = _invoice_number(PRIMARY_BRANCH, sale_day, seq)
    invoice = Invoice(
        branch_id=PRIMARY_BRANCH,
        invoice_number=inv_number,
        shift_id=shift.id,
        operator_id=cashier.id,
        status=InvoiceStatus.FINALIZED,
        payment_method=order.payment_method,
        sales_channel=channel_enum,
        settlement_currency=Currency.USD,
        subtotal=subtotal_usd,
        tax_amount=Decimal("0.00"),
        total=net_usd,
        subtotal_usd=subtotal_usd,
        total_usd=net_usd,
        subtotal_lbp=subtotal_lbp,
        total_lbp=total_lbp,
        exchange_rate_snapshot=rate,
        toters_commission_pct=commission_pct,
        toters_commission_amount=commission_amt,
        discount_type=DiscountType.NONE,
        discount_value=Decimal("0"),
        discount_amount_usd=discount_usd,
        discount_amount_lbp=discount_lbp,
        net_total_usd=net_usd,
        net_total_lbp=total_lbp,
        amount_tendered=amount_tendered,
        change_given=change_given,
        notes=None,
        finalized_at=order.sale_at,
        items=item_rows,
    )
    db.add(invoice)
    await db.flush()

    for item in item_rows:
        item.invoice_id = invoice.id

    await record_sale_transaction(db, invoice, cashier, item_rows)

    for line in order.lines:
        await deduct_inventory_for_sale(
            db,
            line.menu_item_id,
            line.quantity,
            cashier.id,
            inv_number,
            branch_id=PRIMARY_BRANCH,
        )

    await db.execute(
        update(StockMovement)
        .where(StockMovement.reference == inv_number, StockMovement.branch_id == PRIMARY_BRANCH)
        .values(created_at=order.sale_at)
    )

    bucket.invoice_count += 1
    bucket.cash_sales_usd += net_usd if order.payment_method == PaymentMethod.CASH else Decimal("0")
    return invoice, item_rows


async def finalize_shifts(db, shift_cache: dict[tuple[int, date], ShiftBucket], rate: Decimal) -> None:
    for bucket in shift_cache.values():
        shift = bucket.shift
        sales = await db.execute(
            select(
                func.coalesce(func.sum(Invoice.net_total_usd), 0),
                func.coalesce(func.sum(Invoice.net_total_lbp), 0),
                func.count(Invoice.id),
            ).where(Invoice.shift_id == shift.id, Invoice.status == InvoiceStatus.FINALIZED)
        )
        sales_usd, sales_lbp, inv_count = sales.one()
        last_sale = await db.execute(
            select(func.max(Invoice.finalized_at)).where(Invoice.shift_id == shift.id)
        )
        closed_at = last_sale.scalar_one() or shift.opened_at
        closed_at = closed_at + timedelta(minutes=RNG.randint(15, 45))

        exp_usd = Decimal(str(shift.opening_float_usd)) + Decimal(str(sales_usd))
        shift.expected_cash_usd = exp_usd
        shift.expected_cash_lbp = Decimal(str(shift.opening_float_lbp))
        shift.counted_cash_usd = exp_usd + Decimal(str(RNG.uniform(-2, 2))).quantize(Decimal("0.01"))
        shift.counted_cash_lbp = Decimal(str(shift.opening_float_lbp))
        shift.cash_variance_usd = shift.counted_cash_usd - exp_usd
        shift.cash_variance_lbp = Decimal("0")
        shift.total_sales_usd = Decimal(str(sales_usd))
        shift.total_sales_lbp = Decimal(str(sales_lbp))
        shift.invoice_count = int(inv_count)
        shift.closed_at = closed_at
        shift.expected_cash = exp_usd
        shift.counted_cash = shift.counted_cash_usd
        shift.cash_variance = shift.cash_variance_usd
    await db.flush()


async def validate_integrity(
    db,
    branch_id: int,
    starting_stock: dict[str, Decimal],
    recipes: dict[str, list[tuple[str, Decimal]]],
) -> None:
    inv_total = (
        await db.execute(
            select(func.coalesce(func.sum(Invoice.net_total_usd), 0)).where(
                Invoice.branch_id == branch_id,
                Invoice.status == InvoiceStatus.FINALIZED,
            )
        )
    ).scalar_one()
    txn_total = (
        await db.execute(
            select(func.coalesce(func.sum(SaleTransaction.net_total_usd), 0)).where(
                SaleTransaction.branch_id == branch_id
            )
        )
    ).scalar_one()
    inv_total_d = Decimal(str(inv_total))
    txn_total_d = Decimal(str(txn_total))
    assert inv_total_d == txn_total_d, f"Revenue mismatch: invoices={inv_total_d} txns={txn_total_d}"

    rows = await db.execute(
        select(InvoiceItem.menu_item_id, MenuItem.name, func.sum(InvoiceItem.quantity))
        .join(MenuItem, MenuItem.id == InvoiceItem.menu_item_id)
        .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
        .where(Invoice.branch_id == branch_id, Invoice.status == InvoiceStatus.FINALIZED)
        .group_by(InvoiceItem.menu_item_id, MenuItem.name)
    )
    expected_usage: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for _mid, menu_name, qty_sold in rows.all():
        sold = Decimal(str(qty_sold))
        for ing_name, per_sale in recipes.get(menu_name, []):
            expected_usage[ing_name.lower()] += per_sale * sold

    for name, start_qty in starting_stock.items():
        item = (
            await db.execute(
                select(InventoryItem).where(
                    InventoryItem.branch_id == branch_id,
                    func.lower(InventoryItem.name) == name,
                )
            )
        ).scalar_one()
        current = Decimal(str(item.current_stock))
        used = expected_usage.get(name, Decimal("0"))
        expected = start_qty - used
        assert current == expected, (
            f"Stock mismatch for {name}: current={current} expected={expected} "
            f"(start={start_qty} used={used})"
        )

    sale_mov = (
        await db.execute(
            select(func.coalesce(func.sum(StockMovement.quantity), 0)).where(
                StockMovement.branch_id == branch_id,
                StockMovement.movement_type == StockMovementType.SALE,
            )
        )
    ).scalar_one()
    assert Decimal(str(sale_mov)) == sum(expected_usage.values(), Decimal("0")), (
        "Stock movement totals do not match recipe deductions"
    )


async def run_injection() -> dict:
    print("Initializing database...")
    await init_db()

    today = datetime.utcnow().date()
    start_day = today - timedelta(days=DAYS - 1)

    async with SessionLocal() as db:
        print(f"Clearing existing sales history for branch {PRIMARY_BRANCH}...")
        await clear_branch_sales_history(db, PRIMARY_BRANCH)

        print("Resetting branch inventory to seed levels...")
        starting_stock = await reset_branch_inventory(db, PRIMARY_BRANCH)

        cashiers = await load_branch_cashiers(db, PRIMARY_BRANCH)
        menu = await load_menu(db)
        recipes = await load_recipe_map(db, PRIMARY_BRANCH)
        settings_row = await get_system_settings(db)
        rate = Decimal(str(settings_row.exchange_rate_usd_lbp))

        print(f"Planning {DAYS} days of orders ({start_day} -> {today})...")
        planned = plan_orders(menu, cashiers, start_day, DAYS)
        usage_preview = simulate_ingredient_usage(planned, recipes)
        print("Projected ingredient usage:")
        for ing, qty in sorted(usage_preview.items()):
            cap = starting_stock.get(ing, Decimal("0"))
            print(f"  {ing}: {qty:.0f} / {cap:.0f} available")

        planned = trim_orders_to_stock(planned, recipes, starting_stock)
        print(f"Orders after stock cap: {len(planned)}")

        shift_cache: dict[tuple[int, date], ShiftBucket] = {}
        day_counters: dict[date, int] = {}

        print("Injecting transactions chronologically...")
        for i, order in enumerate(planned, 1):
            await inject_order(db, order, shift_cache, rate, settings_row, day_counters)
            if i % 200 == 0:
                print(f"  {i}/{len(planned)} orders injected")
                await db.flush()

        await finalize_shifts(db, shift_cache, rate)

        # Ensure sale counter is ahead of max sale number
        max_sale = (
            await db.execute(select(func.max(SaleTransaction.sale_number)))
        ).scalar_one()
        if max_sale:
            counter = (
                await db.execute(select(SaleNumberCounter).where(SaleNumberCounter.id == 1))
            ).scalar_one_or_none()
            if not counter:
                counter = SaleNumberCounter(id=1, next_number=int(max_sale) + 1)
                db.add(counter)
            elif counter.next_number <= max_sale:
                counter.next_number = int(max_sale) + 1

        print("Running integrity validation...")
        await validate_integrity(db, PRIMARY_BRANCH, starting_stock, recipes)
        await db.commit()

        inv_count = (
            await db.execute(
                select(func.count(Invoice.id)).where(Invoice.branch_id == PRIMARY_BRANCH)
            )
        ).scalar_one()
        rev = (
            await db.execute(
                select(func.coalesce(func.sum(Invoice.net_total_usd), 0)).where(
                    Invoice.branch_id == PRIMARY_BRANCH
                )
            )
        ).scalar_one()

        stock_rows = await db.execute(
            select(InventoryItem.name, InventoryItem.current_stock).where(
                InventoryItem.branch_id == PRIMARY_BRANCH, InventoryItem.is_active.is_(True)
            )
        )
        stock_snapshot = {n: str(s) for n, s in stock_rows.all()}

    summary = {
        "branch_id": PRIMARY_BRANCH,
        "start_day": str(start_day),
        "end_day": str(today),
        "invoices": inv_count,
        "revenue_usd": str(rev),
        "shifts": len(shift_cache),
        "inventory": stock_snapshot,
    }
    return summary


def main() -> int:
    try:
        summary = asyncio.run(run_injection())
        print("\n=== 30-DAY TIME-TRAVEL INJECTION COMPLETE ===")
        print(f"Branch: Saida (#{summary['branch_id']})")
        print(f"Period: {summary['start_day']} to {summary['end_day']}")
        print(f"Invoices: {summary['invoices']}")
        print(f"Total revenue (USD): {summary['revenue_usd']}")
        print(f"Shifts created: {summary['shifts']}")
        print("Remaining inventory:")
        for name, qty in summary["inventory"].items():
            print(f"  {name}: {qty}")
        print("\nIntegrity checks PASSED. Open Admin Dashboard to view 30 days of history.")
        return 0
    except Exception as exc:
        print(f"\nINJECTION FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
