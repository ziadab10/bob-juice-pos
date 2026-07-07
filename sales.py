"""Sales endpoints: shifts, menu, dual-currency immutable invoices, Toters channel."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth import get_current_user, record_audit, require_cashier_or_admin, resolve_client_ip
from branch_scope import require_operational_branch_id
from database import (
    Currency,
    DiscountType,
    GlobalModifier,
    InventoryItem,
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    MenuItem,
    PaymentMethod,
    ProductCategory,
    ProductRecipe,
    RecipeRole,
    SaleNumberCounter,
    SaleTransaction,
    SalesChannel,
    Shift,
    User,
    apply_platform_commission,
    channel_commission_pct,
    compute_discount,
    get_db,
    get_system_settings,
    usd_to_lbp,
)
from inventory import deduct_inventory_for_sale, serialize_line_modifiers
from permissions import require_permission
from sync_service import enqueue_sync
from thermal_print import print_receipt_thermal

logger = logging.getLogger("bob_juice.sales")


async def _queue_thermal_print(inv_dict: dict[str, Any]) -> None:
    """Fire-and-forget thermal print — never block order finalization."""
    try:
        await asyncio.to_thread(print_receipt_thermal, inv_dict)
    except Exception:
        logger.exception("Background thermal print failed for invoice %s", inv_dict.get("invoice_number"))

router = APIRouter(prefix="/api/sales", tags=["Sales"])

SALE_NUMBER_START = 1001


async def allocate_sale_number(db: AsyncSession) -> int:
    result = await db.execute(select(SaleNumberCounter).where(SaleNumberCounter.id == 1))
    counter = result.scalar_one_or_none()
    if not counter:
        counter = SaleNumberCounter(id=1, next_number=SALE_NUMBER_START)
        db.add(counter)
        await db.flush()
    num = int(counter.next_number)
    counter.next_number = num + 1
    await db.flush()
    return num


def build_items_summary(items: list[InvoiceItem]) -> str:
    parts = [f"{item.quantity}x {item.name_snapshot}" for item in items]
    return ", ".join(parts)[:512]


async def record_sale_transaction(
    db: AsyncSession,
    invoice: Invoice,
    user: User,
    items: list[InvoiceItem],
) -> SaleTransaction:
    sale_num = await allocate_sale_number(db)
    txn = SaleTransaction(
        invoice_id=invoice.id,
        sale_number=sale_num,
        branch_id=invoice.branch_id,
        operator_id=user.id,
        operator_name=user.full_name,
        items_summary=build_items_summary(items),
        discount_amount_usd=invoice.discount_amount_usd,
        discount_amount_lbp=invoice.discount_amount_lbp,
        net_total_usd=invoice.net_total_usd,
        net_total_lbp=invoice.net_total_lbp,
        payment_method=invoice.payment_method.value if hasattr(invoice.payment_method, "value") else str(invoice.payment_method),
        sales_channel=invoice.sales_channel.value if hasattr(invoice.sales_channel, "value") else str(invoice.sales_channel),
        invoice_number=invoice.invoice_number,
        finalized_at=invoice.finalized_at,
    )
    db.add(txn)
    await db.flush()
    return txn


class ShiftOpenRequest(BaseModel):
    opening_float_usd: Decimal = Field(default=Decimal("100.00"), ge=0)
    opening_float_lbp: Decimal = Field(default=Decimal("0"), ge=0)


class ShiftCloseRequest(BaseModel):
    counted_cash_usd: Decimal = Field(ge=0)
    counted_cash_lbp: Decimal = Field(ge=0)
    closing_notes: str | None = None


class ShiftOut(BaseModel):
    id: int
    operator_id: int
    opened_at: datetime
    closed_at: datetime | None
    exchange_rate_snapshot: Decimal
    opening_float_usd: Decimal
    opening_float_lbp: Decimal
    expected_cash_usd: Decimal | None
    expected_cash_lbp: Decimal | None
    counted_cash_usd: Decimal | None
    counted_cash_lbp: Decimal | None
    cash_variance_usd: Decimal | None
    cash_variance_lbp: Decimal | None
    total_sales_usd: Decimal | None
    total_sales_lbp: Decimal | None
    invoice_count: int | None
    closing_notes: str | None

    model_config = {"from_attributes": True}


class MenuItemOut(BaseModel):
    id: int
    name: str
    category: str
    category_id: int | None = None
    description: str | None
    unit_price: Decimal
    price_s: Decimal | None = None
    price_m: Decimal | None = None
    price_l: Decimal | None = None
    sizes_enabled: bool = False
    unit_price_lbp: Decimal | None = None
    is_active: bool

    model_config = {"from_attributes": True}


class CategoryTabOut(BaseModel):
    id: int | None
    name: str
    sort_order: int = 0


class GlobalModifierOut(BaseModel):
    id: int
    name: str
    inventory_item_id: int
    inventory_item_name: str
    extra_price_usd: Decimal
    quantity_per_sale: Decimal
    metric_unit: str | None = None
    sort_order: int = 0


class MenuCatalogOut(BaseModel):
    categories: list[CategoryTabOut]
    items: list[MenuItemOut]
    global_modifiers: list[GlobalModifierOut] = Field(default_factory=list)
    exchange_rate: Decimal


class OrderLineExtra(BaseModel):
    inventory_item_id: int
    quantity: Decimal = Field(gt=0)
    extra_price_usd: Decimal = Field(default=Decimal("0"), ge=0)
    name: str | None = None
    global_modifier_id: int | None = None


class OrderLineExclude(BaseModel):
    inventory_item_id: int
    quantity: Decimal = Field(default=Decimal("1"), gt=0)
    name: str | None = None
    global_modifier_id: int | None = None


class OrderLineCreate(BaseModel):
    menu_item_id: int
    quantity: int = Field(ge=1, le=99)
    size: Literal["S", "M", "L"] | None = None
    extras: list[OrderLineExtra] = Field(default_factory=list)
    excludes: list[OrderLineExclude] = Field(default_factory=list)


class OrderCreate(BaseModel):
    payment_method: PaymentMethod
    sales_channel: SalesChannel = SalesChannel.IN_STORE
    settlement_currency: Currency = Currency.USD
    lines: list[OrderLineCreate]
    amount_tendered: Decimal | None = None
    notes: str | None = None
    discount_type: DiscountType = DiscountType.NONE
    discount_value: Decimal = Field(default=Decimal("0"), ge=0)


class InvoiceItemOut(BaseModel):
    id: int
    operator_id: int
    line_timestamp: datetime
    name_snapshot: str
    unit_price_snapshot: Decimal
    quantity: int
    line_total: Decimal
    line_total_lbp: Decimal
    line_modifiers_json: str | None = None

    model_config = {"from_attributes": True}


class InvoiceOut(BaseModel):
    id: int
    invoice_number: str
    sale_number: int | None = None
    shift_id: int
    operator_id: int
    status: str
    payment_method: str
    sales_channel: str
    settlement_currency: str
    subtotal: Decimal
    tax_amount: Decimal
    total: Decimal
    subtotal_usd: Decimal
    total_usd: Decimal
    subtotal_lbp: Decimal
    total_lbp: Decimal
    exchange_rate_snapshot: Decimal
    toters_commission_pct: Decimal
    toters_commission_amount: Decimal
    discount_type: str
    discount_value: Decimal
    discount_amount_usd: Decimal
    discount_amount_lbp: Decimal
    net_total_usd: Decimal
    net_total_lbp: Decimal
    amount_tendered: Decimal | None
    change_given: Decimal | None
    notes: str | None = None
    finalized_at: datetime
    items: list[InvoiceItemOut]
    thermal_print: dict | None = None

    model_config = {"from_attributes": True}


class TransactionOut(BaseModel):
    id: int
    invoice_id: int
    sale_number: int
    invoice_number: str
    items_summary: str
    discount_amount_usd: Decimal
    discount_amount_lbp: Decimal
    net_total_usd: Decimal
    net_total_lbp: Decimal
    payment_method: str
    sales_channel: str
    operator_id: int
    operator_name: str
    finalized_at: datetime

    model_config = {"from_attributes": True}


class MenuModifierCardOut(BaseModel):
    id: int
    kind: str
    display_title: str
    inventory_item_id: int
    inventory_item_name: str
    quantity_per_sale: Decimal | None = None
    extra_price_usd: Decimal = Decimal("0")
    metric_unit: str | None = None


class MenuModifiersOut(BaseModel):
    extras: list[MenuModifierCardOut] = Field(default_factory=list)
    excludes: list[MenuModifierCardOut] = Field(default_factory=list)


class MenuRecipeLineOut(BaseModel):
    inventory_item_id: int
    inventory_item_name: str
    quantity_per_sale: Decimal
    metric_unit: str | None = None
    unit: str | None = None


def _line_display_name(item_name: str, line: OrderLineCreate) -> str:
    if line.size:
        return f"{item_name} ({line.size.upper()})"
    return item_name


def resolve_menu_item_size_price(item: MenuItem, size: str | None) -> Decimal:
    if not item.sizes_enabled or not size:
        return Decimal(str(item.unit_price))
    sz = size.upper()
    if sz not in {"S", "M", "L"}:
        sz = "M"
    fallback = Decimal(str(item.unit_price))
    price_map = {
        "S": item.price_s if item.price_s is not None else fallback,
        "M": item.price_m if item.price_m is not None else fallback,
        "L": item.price_l if item.price_l is not None else fallback,
    }
    return Decimal(str(price_map[sz]))


def _modifier_receipt_lines(line: OrderLineCreate) -> list[str]:
    rows: list[str] = []
    for ex in line.extras:
        label = ex.name or f"#{ex.inventory_item_id}"
        price = Decimal(str(ex.extra_price_usd))
        rows.append(f"+ {label} (${price:.2f})")
    for ex in line.excludes:
        label = ex.name or f"#{ex.inventory_item_id}"
        rows.append(f"- No {label} ($0.00)")
    return rows


def _line_subtotal_usd(unit_price: Decimal, line: OrderLineCreate) -> Decimal:
    base = Decimal(str(unit_price)) * line.quantity
    extra = sum(
        Decimal(str(e.extra_price_usd)) * line.quantity for e in line.extras
    )
    return base + extra


async def get_open_shift(db: AsyncSession, operator_id: int) -> Shift | None:
    result = await db.execute(select(Shift).where(Shift.operator_id == operator_id, Shift.closed_at.is_(None)))
    return result.scalar_one_or_none()


async def require_open_shift(db: AsyncSession, operator_id: int) -> Shift:
    shift = await get_open_shift(db, operator_id)
    if not shift:
        raise HTTPException(status_code=409, detail="No open shift. Open a shift before processing sales.")
    return shift


async def compute_expected_cash(db: AsyncSession, shift: Shift) -> tuple[Decimal, Decimal]:
    result_usd = await db.execute(
        select(func.coalesce(func.sum(Invoice.net_total_usd), 0)).where(
            Invoice.shift_id == shift.id,
            Invoice.status == InvoiceStatus.FINALIZED,
            Invoice.payment_method == PaymentMethod.CASH,
            Invoice.settlement_currency == Currency.USD,
        )
    )
    result_lbp = await db.execute(
        select(func.coalesce(func.sum(Invoice.net_total_lbp), 0)).where(
            Invoice.shift_id == shift.id,
            Invoice.status == InvoiceStatus.FINALIZED,
            Invoice.payment_method == PaymentMethod.CASH,
            Invoice.settlement_currency == Currency.LBP,
        )
    )
    expected_usd = Decimal(str(shift.opening_float_usd)) + Decimal(str(result_usd.scalar_one()))
    expected_lbp = Decimal(str(shift.opening_float_lbp)) + Decimal(str(result_lbp.scalar_one()))
    return expected_usd, expected_lbp


async def generate_invoice_number(db: AsyncSession, branch_id: int) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"BJ-{branch_id}-{ts}-"
    result = await db.execute(select(func.count(Invoice.id)).where(Invoice.invoice_number.like(f"{prefix}%")))
    return f"{prefix}{int(result.scalar_one()) + 1:05d}"


@router.get("/shifts/current", response_model=ShiftOut | None)
async def current_shift(user: Annotated[User, Depends(require_cashier_or_admin)], db: Annotated[AsyncSession, Depends(get_db)]):
    return await get_open_shift(db, user.id)


@router.get("/shifts/{shift_id}/preview")
async def shift_close_preview(
    shift_id: int,
    user: Annotated[User, Depends(require_cashier_or_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Shift).where(Shift.id == shift_id, Shift.operator_id == user.id))
    shift = result.scalar_one_or_none()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")
    if shift.closed_at:
        raise HTTPException(status_code=409, detail="Shift already closed")
    exp_usd, exp_lbp = await compute_expected_cash(db, shift)
    inv_count = await db.execute(
        select(func.count(Invoice.id)).where(Invoice.shift_id == shift.id, Invoice.status == InvoiceStatus.FINALIZED)
    )
    sales = await db.execute(
        select(func.coalesce(func.sum(Invoice.net_total_usd), 0), func.coalesce(func.sum(Invoice.net_total_lbp), 0)).where(
            Invoice.shift_id == shift.id, Invoice.status == InvoiceStatus.FINALIZED
        )
    )
    sales_usd, sales_lbp = sales.one()
    return {
        "shift_id": shift.id,
        "exchange_rate": str(shift.exchange_rate_snapshot),
        "opening_float_usd": str(shift.opening_float_usd),
        "opening_float_lbp": str(shift.opening_float_lbp),
        "expected_cash_usd": str(exp_usd),
        "expected_cash_lbp": str(exp_lbp),
        "total_sales_usd": str(sales_usd),
        "total_sales_lbp": str(sales_lbp),
        "invoice_count": inv_count.scalar_one(),
        "opened_at": shift.opened_at.isoformat(),
    }


@router.post("/shifts/open", response_model=ShiftOut, status_code=status.HTTP_201_CREATED)
async def open_shift(
    payload: ShiftOpenRequest,
    request: Request,
    user: Annotated[User, Depends(require_cashier_or_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if await get_open_shift(db, user.id):
        raise HTTPException(status_code=409, detail="You already have an open shift")
    settings_row = await get_system_settings(db)
    rate = Decimal(str(settings_row.exchange_rate_usd_lbp))
    branch_id = require_operational_branch_id(user)
    shift = Shift(
        branch_id=branch_id,
        operator_id=user.id,
        exchange_rate_snapshot=rate,
        opening_float_usd=payload.opening_float_usd,
        opening_float_lbp=payload.opening_float_lbp,
        opening_float=payload.opening_float_usd,
    )
    db.add(shift)
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="SHIFT_OPEN", entity_type="shift", entity_id=shift.id,
        details={"opening_float_usd": str(payload.opening_float_usd), "opening_float_lbp": str(payload.opening_float_lbp), "rate": str(rate)},
        ip_address=await resolve_client_ip(request),
    )
    return shift


@router.post("/shifts/close", response_model=ShiftOut)
async def close_shift(
    payload: ShiftCloseRequest,
    request: Request,
    user: Annotated[User, Depends(require_cashier_or_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    shift = await require_open_shift(db, user.id)
    exp_usd, exp_lbp = await compute_expected_cash(db, shift)
    sales = await db.execute(
        select(func.coalesce(func.sum(Invoice.net_total_usd), 0), func.coalesce(func.sum(Invoice.net_total_lbp), 0), func.count(Invoice.id)).where(
            Invoice.shift_id == shift.id, Invoice.status == InvoiceStatus.FINALIZED
        )
    )
    sales_usd, sales_lbp, inv_count = sales.one()

    shift.expected_cash_usd = exp_usd
    shift.expected_cash_lbp = exp_lbp
    shift.counted_cash_usd = payload.counted_cash_usd
    shift.counted_cash_lbp = payload.counted_cash_lbp
    shift.cash_variance_usd = payload.counted_cash_usd - exp_usd
    shift.cash_variance_lbp = payload.counted_cash_lbp - exp_lbp
    shift.total_sales_usd = Decimal(str(sales_usd))
    shift.total_sales_lbp = Decimal(str(sales_lbp))
    shift.invoice_count = inv_count
    shift.closing_notes = payload.closing_notes
    shift.closed_at = datetime.utcnow()
    shift.expected_cash = exp_usd
    shift.counted_cash = payload.counted_cash_usd
    shift.cash_variance = shift.cash_variance_usd
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="SHIFT_CLOSE", entity_type="shift", entity_id=shift.id,
        details={"expected_usd": str(exp_usd), "counted_usd": str(payload.counted_cash_usd), "variance_usd": str(shift.cash_variance_usd)},
        ip_address=await resolve_client_ip(request),
    )
    return shift


FALLBACK_CATEGORIES = ["Juice", "Crepe", "Cocktails", "Smoothies"]


@router.get("/menu", response_model=MenuCatalogOut)
async def list_menu(user: Annotated[User, Depends(require_cashier_or_admin)], db: Annotated[AsyncSession, Depends(get_db)]):
    settings_row = await get_system_settings(db)
    rate = Decimal(str(settings_row.exchange_rate_usd_lbp))

    cat_result = await db.execute(
        select(ProductCategory).where(ProductCategory.is_active.is_(True)).order_by(ProductCategory.sort_order, ProductCategory.name)
    )
    db_cats = cat_result.scalars().all()
    if db_cats:
        categories = [CategoryTabOut(id=c.id, name=c.name, sort_order=c.sort_order) for c in db_cats]
    else:
        categories = [CategoryTabOut(id=None, name=n, sort_order=i) for i, n in enumerate(FALLBACK_CATEGORIES, 1)]

    result = await db.execute(select(MenuItem).where(MenuItem.is_active.is_(True)).order_by(MenuItem.category, MenuItem.name))
    items = []
    for row in result.scalars().all():
        items.append(
            MenuItemOut(
                id=row.id,
                name=row.name,
                category=row.category,
                category_id=row.category_id,
                description=row.description,
                unit_price=row.unit_price,
                price_s=row.price_s,
                price_m=row.price_m if row.price_m is not None else row.unit_price,
                price_l=row.price_l,
                sizes_enabled=bool(row.sizes_enabled),
                unit_price_lbp=usd_to_lbp(Decimal(str(row.unit_price)), rate),
                is_active=row.is_active,
            )
        )

    bid = require_operational_branch_id(user)
    mod_result = await db.execute(
        select(GlobalModifier, InventoryItem.name)
        .join(InventoryItem, GlobalModifier.inventory_item_id == InventoryItem.id)
        .where(GlobalModifier.is_active.is_(True), GlobalModifier.branch_id == bid)
        .order_by(GlobalModifier.sort_order, GlobalModifier.name)
    )
    global_modifiers = [
        GlobalModifierOut(
            id=m.id,
            name=m.name,
            inventory_item_id=m.inventory_item_id,
            inventory_item_name=inn,
            extra_price_usd=Decimal(str(m.extra_price_usd or 0)),
            quantity_per_sale=m.quantity_per_sale,
            metric_unit=m.metric_unit,
            sort_order=m.sort_order or 0,
        )
        for m, inn in mod_result.all()
    ]

    return MenuCatalogOut(
        categories=categories,
        items=items,
        global_modifiers=global_modifiers,
        exchange_rate=rate,
    )


@router.get("/menu/{menu_item_id}/modifiers", response_model=MenuModifiersOut)
async def get_menu_item_modifiers(
    menu_item_id: int,
    user: Annotated[User, Depends(require_cashier_or_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """POS modifier cards — always returns a safe empty bundle on missing data or errors."""
    try:
        menu = await db.get(MenuItem, menu_item_id)
        if not menu or not menu.is_active:
            return MenuModifiersOut(extras=[], excludes=[])
        result = await db.execute(
            select(ProductRecipe, InventoryItem.name)
            .join(InventoryItem, ProductRecipe.inventory_item_id == InventoryItem.id)
            .where(
                ProductRecipe.menu_item_id == menu_item_id,
                ProductRecipe.recipe_role.in_([RecipeRole.EXTRA.value, RecipeRole.EXCLUDE.value]),
            )
            .order_by(ProductRecipe.id)
        )
        extras: list[MenuModifierCardOut] = []
        excludes: list[MenuModifierCardOut] = []
        for r, inn in result.all():
            title = (getattr(r, "display_title", None) or inn or "Modifier").strip()
            card = MenuModifierCardOut(
                id=r.id,
                kind=r.recipe_role,
                display_title=title or "Modifier",
                inventory_item_id=r.inventory_item_id,
                inventory_item_name=inn or title,
                quantity_per_sale=r.quantity_per_sale,
                extra_price_usd=Decimal(str(r.extra_price_usd or 0)),
                metric_unit=r.metric_unit,
            )
            if r.recipe_role == RecipeRole.EXTRA.value:
                extras.append(card)
            else:
                excludes.append(card)
        return MenuModifiersOut(extras=extras, excludes=excludes)
    except Exception:
        logger.exception("modifiers lookup failed for menu_item_id=%s", menu_item_id)
        return MenuModifiersOut(extras=[], excludes=[])


@router.get("/menu/{menu_item_id}/recipe", response_model=list[MenuRecipeLineOut])
async def get_menu_item_recipe(
    menu_item_id: int,
    user: Annotated[User, Depends(require_cashier_or_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    menu = await db.get(MenuItem, menu_item_id)
    if not menu or not menu.is_active:
        raise HTTPException(status_code=404, detail="Menu item not found")
    result = await db.execute(
        select(ProductRecipe, InventoryItem.name, InventoryItem.unit)
        .join(InventoryItem, ProductRecipe.inventory_item_id == InventoryItem.id)
        .where(
            ProductRecipe.menu_item_id == menu_item_id,
            ProductRecipe.recipe_role == RecipeRole.BASE.value,
        )
        .order_by(ProductRecipe.id)
    )
    return [
        MenuRecipeLineOut(
            inventory_item_id=r.inventory_item_id,
            inventory_item_name=inn,
            quantity_per_sale=r.quantity_per_sale,
            metric_unit=r.metric_unit,
            unit=unit,
        )
        for r, inn, unit in result.all()
    ]


@router.post("/orders", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
async def finalize_order(
    payload: OrderCreate,
    request: Request,
    user: Annotated[User, Depends(require_cashier_or_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not payload.lines:
        raise HTTPException(status_code=400, detail="Order must contain at least one item")

    shift = await require_open_shift(db, user.id)
    settings_row = await get_system_settings(db)
    rate = Decimal(str(settings_row.exchange_rate_usd_lbp))
    commission_pct = channel_commission_pct(payload.sales_channel, settings_row)
    branch_id = require_operational_branch_id(user)
    if shift.branch_id != branch_id:
        raise HTTPException(status_code=409, detail="Open shift does not belong to your assigned branch")

    now = datetime.utcnow()
    subtotal_usd = Decimal("0.00")
    item_rows: list[InvoiceItem] = []

    for line in payload.lines:
        result = await db.execute(select(MenuItem).where(MenuItem.id == line.menu_item_id, MenuItem.is_active.is_(True)))
        item = result.scalar_one_or_none()
        if not item:
            raise HTTPException(status_code=404, detail=f"Menu item {line.menu_item_id} not found")
        unit_price = resolve_menu_item_size_price(item, line.size if item.sizes_enabled else None)
        line_usd = _line_subtotal_usd(unit_price, line)
        subtotal_usd += line_usd
        extras_payload = [e.model_dump(mode="json") for e in line.extras]
        excludes_payload = [e.model_dump(mode="json") for e in line.excludes]
        item_rows.append(
            InvoiceItem(
                branch_id=branch_id,
                menu_item_id=item.id,
                operator_id=user.id,
                line_timestamp=now,
                name_snapshot=_line_display_name(item.name, line),
                unit_price_snapshot=unit_price,
                quantity=line.quantity,
                line_total=line_usd,
                line_total_lbp=usd_to_lbp(line_usd, rate),
                line_modifiers_json=serialize_line_modifiers(extras_payload, excludes_payload),
            )
        )

    discount_usd, discount_lbp = compute_discount(subtotal_usd, rate, payload.discount_type, payload.discount_value)
    after_discount_usd = max(subtotal_usd - discount_usd, Decimal("0.00"))
    _, commission_amt, net_usd = apply_platform_commission(after_discount_usd, commission_pct)
    subtotal_lbp = usd_to_lbp(subtotal_usd, rate)
    total_lbp = usd_to_lbp(net_usd, rate)

    amount_due = net_usd if payload.settlement_currency == Currency.USD else total_lbp
    change_given = None
    if payload.payment_method == PaymentMethod.CASH:
        if payload.amount_tendered is None or payload.amount_tendered < amount_due:
            raise HTTPException(status_code=400, detail="Insufficient cash tendered")
        change_given = payload.amount_tendered - amount_due

    inv_number = await generate_invoice_number(db, branch_id)
    invoice = Invoice(
        branch_id=branch_id,
        invoice_number=inv_number,
        shift_id=shift.id,
        operator_id=user.id,
        status=InvoiceStatus.FINALIZED,
        payment_method=payload.payment_method,
        sales_channel=payload.sales_channel,
        settlement_currency=payload.settlement_currency,
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
        discount_type=payload.discount_type,
        discount_value=payload.discount_value,
        discount_amount_usd=discount_usd,
        discount_amount_lbp=discount_lbp,
        net_total_usd=net_usd,
        net_total_lbp=total_lbp,
        amount_tendered=payload.amount_tendered,
        change_given=change_given,
        notes=payload.notes,
        finalized_at=now,
        items=item_rows,
    )
    db.add(invoice)
    await db.flush()

    sale_txn = await record_sale_transaction(db, invoice, user, item_rows)

    for line in payload.lines:
        try:
            await deduct_inventory_for_sale(
                db,
                line.menu_item_id,
                line.quantity,
                user.id,
                inv_number,
                branch_id=branch_id,
                extras=[e.model_dump(mode="json") for e in line.extras],
                excludes=[e.model_dump(mode="json") for e in line.excludes],
            )
        except HTTPException:
            raise

    await record_audit(
        db, actor_id=user.id, action="INVOICE_FINALIZE", entity_type="invoice", entity_id=invoice.id,
        details={
            "invoice_number": invoice.invoice_number,
            "net_usd": str(net_usd),
            "channel": payload.sales_channel.value,
            "currency": payload.settlement_currency.value,
            "branch_id": branch_id,
        },
        ip_address=await resolve_client_ip(request),
    )
    result = await db.execute(select(Invoice).options(selectinload(Invoice.items)).where(Invoice.id == invoice.id))
    inv_out = result.scalar_one()
    out = InvoiceOut.model_validate(inv_out)
    out.sale_number = sale_txn.sale_number
    inv_dict = out.model_dump(mode="json")
    await enqueue_sync(
        db,
        entity_type="invoice",
        entity_id=invoice.id,
        payload=inv_dict,
    )
    asyncio.create_task(_queue_thermal_print(inv_dict))
    out.thermal_print = {"printed": None, "reason": "queued", "async": True}
    return out


@router.get("/transactions", response_model=list[TransactionOut])
async def list_transactions(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 300,
):
    if not require_permission(user, "sales_reports"):
        raise HTTPException(status_code=403, detail="Sales reports access not permitted")
    bid = require_operational_branch_id(user)
    stmt = (
        select(SaleTransaction)
        .where(SaleTransaction.branch_id == bid)
        .order_by(SaleTransaction.finalized_at.desc(), SaleTransaction.id.desc())
        .limit(min(limit, 500))
    )
    if q and (term := q.strip()):
        if term.isdigit():
            stmt = stmt.where(SaleTransaction.sale_number == int(term))
        else:
            like = f"%{term}%"
            stmt = stmt.where(SaleTransaction.invoice_number.ilike(like))
    if date_from:
        start = datetime.combine(date_from, datetime.min.time())
        stmt = stmt.where(SaleTransaction.finalized_at >= start)
    if date_to:
        end = datetime.combine(date_to, datetime.max.time())
        stmt = stmt.where(SaleTransaction.finalized_at <= end)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/orders/{invoice_id}/print-thermal")
async def print_order_thermal(
    invoice_id: int,
    user: Annotated[User, Depends(require_cashier_or_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Invoice).options(selectinload(Invoice.items)).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    inv_dict = InvoiceOut.model_validate(invoice).model_dump(mode="json")
    return await asyncio.to_thread(print_receipt_thermal, inv_dict)


@router.get("/orders/{invoice_id}/receipt/pdf")
async def download_receipt_pdf(
    invoice_id: int,
    user: Annotated[User, Depends(require_cashier_or_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from database import Branch

    result = await db.execute(select(Invoice).options(selectinload(Invoice.items)).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    branch = await db.get(Branch, invoice.branch_id)
    inv_dict = InvoiceOut.model_validate(invoice).model_dump(mode="json")
    try:
        from pdf_reports import build_pos_receipt_pdf
        import asyncio

        pdf_bytes = await asyncio.to_thread(
            build_pos_receipt_pdf,
            inv_dict,
            branch_name=branch.name if branch else "BOB JUICE",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}") from exc
    filename = f"bob-juice-receipt-{invoice.invoice_number}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/orders/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: int,
    user: Annotated[User, Depends(require_cashier_or_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Invoice).options(selectinload(Invoice.items)).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice
