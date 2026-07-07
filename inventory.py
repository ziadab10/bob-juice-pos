"""Inventory ledger: stock in/out by item name, recipes, automatic sale deduction."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth import get_current_user, record_audit, resolve_client_ip
from branch_scope import require_operational_branch_id, resolve_inventory_branch_filter
from database import (
    GlobalModifier,
    InventoryItem,
    MenuItem,
    ProductRecipe,
    RecipeRole,
    StockMovement,
    StockMovementType,
    User,
    get_db,
)
from inventory_units import CANONICAL_UNITS, normalize_quantity, normalize_unit, units_compatible
from permissions import require_permission

router = APIRouter(prefix="/api/inventory", tags=["Inventory"])


class InventoryItemOut(BaseModel):
    id: int
    name: str
    unit: str
    current_stock: Decimal
    reorder_level: Decimal
    is_active: bool
    unit_label: str | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def attach_unit_label(self):
        try:
            self.unit_label = normalize_unit(self.unit)
        except ValueError:
            self.unit_label = self.unit
        return self


class InventorySuggestOut(BaseModel):
    id: int
    name: str
    unit: str
    current_stock: Decimal


class StockMovementOut(BaseModel):
    id: int
    inventory_item_id: int
    inventory_name: str
    movement_type: str
    quantity: Decimal
    balance_after: Decimal
    reference: str | None
    notes: str | None
    recorded_by_id: int
    created_at: datetime


class InventoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    unit: str = Field(default="pcs", max_length=64)
    current_stock: Decimal = Field(default=Decimal("0"), ge=0)
    reorder_level: Decimal = Field(default=Decimal("0"), ge=0)


class StockInOut(BaseModel):
    inventory_item_id: int | None = None
    inventory_item_name: str | None = Field(default=None, min_length=1, max_length=128)
    quantity: Decimal = Field(gt=0)
    unit: str | None = Field(default=None, max_length=64)
    notes: str | None = None

    @model_validator(mode="after")
    def require_identifier(self):
        if not self.inventory_item_id and not (self.inventory_item_name and self.inventory_item_name.strip()):
            raise ValueError("Provide inventory item name or id")
        return self


class RecipeOut(BaseModel):
    id: int
    menu_item_id: int
    menu_item_name: str
    inventory_item_id: int
    inventory_item_name: str
    quantity_per_sale: Decimal
    metric_unit: str | None = None
    recipe_role: str = RecipeRole.BASE.value
    display_title: str | None = None
    extra_price_usd: Decimal = Decimal("0.00")


class RecipeCreate(BaseModel):
    menu_item_id: int
    inventory_item_id: int
    quantity_per_sale: Decimal = Field(gt=0)
    metric_unit: str | None = Field(default=None, max_length=32)
    recipe_role: str = RecipeRole.BASE.value
    display_title: str | None = Field(default=None, max_length=128)
    extra_price_usd: Decimal = Field(default=Decimal("0"), ge=0)


class RecipeLineIn(BaseModel):
    inventory_item_id: int
    quantity_per_sale: Decimal = Field(gt=0)
    metric_unit: str | None = Field(default=None, max_length=32)


class ExtraModifierIn(BaseModel):
    inventory_item_id: int
    display_title: str = Field(min_length=1, max_length=128)
    quantity_per_sale: Decimal = Field(gt=0)
    extra_price_usd: Decimal = Field(default=Decimal("0"), ge=0)
    metric_unit: str | None = Field(default=None, max_length=32)


class ExcludeModifierIn(BaseModel):
    inventory_item_id: int
    display_title: str = Field(min_length=1, max_length=128)


class ProductRecipesReplace(BaseModel):
    base: list[RecipeLineIn] = Field(default_factory=list)
    # Legacy fields ignored — extras/excludes are global modifiers now
    extras: list[ExtraModifierIn] = Field(default_factory=list)
    excludes: list[ExcludeModifierIn] = Field(default_factory=list)
    lines: list[RecipeLineIn] | None = None


class GlobalModifierOut(BaseModel):
    id: int
    name: str
    inventory_item_id: int
    inventory_item_name: str
    extra_price_usd: Decimal
    quantity_per_sale: Decimal
    metric_unit: str | None = None
    sort_order: int = 0
    is_active: bool = True

    model_config = {"from_attributes": True}


class GlobalModifierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    inventory_item_id: int
    extra_price_usd: Decimal = Field(default=Decimal("0"), ge=0)
    quantity_per_sale: Decimal = Field(gt=0)
    metric_unit: str | None = Field(default=None, max_length=32)
    sort_order: int = 0


class GlobalModifierUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    inventory_item_id: int | None = None
    extra_price_usd: Decimal | None = Field(default=None, ge=0)
    quantity_per_sale: Decimal | None = Field(default=None, gt=0)
    metric_unit: str | None = Field(default=None, max_length=32)
    sort_order: int | None = None
    is_active: bool | None = None


class ProductRecipesBundleOut(BaseModel):
    base: list[RecipeOut] = Field(default_factory=list)
    extras: list[RecipeOut] = Field(default_factory=list)
    excludes: list[RecipeOut] = Field(default_factory=list)


def _recipe_out(
    recipe: ProductRecipe,
    *,
    menu_item_name: str = "",
    inventory_item_name: str = "",
) -> RecipeOut:
    role = recipe.recipe_role or RecipeRole.BASE.value
    title = recipe.display_title
    if not title and role == RecipeRole.BASE.value:
        title = inventory_item_name
    return RecipeOut(
        id=recipe.id,
        menu_item_id=recipe.menu_item_id,
        menu_item_name=menu_item_name,
        inventory_item_id=recipe.inventory_item_id,
        inventory_item_name=inventory_item_name,
        quantity_per_sale=recipe.quantity_per_sale,
        metric_unit=recipe.metric_unit,
        recipe_role=role,
        display_title=title,
        extra_price_usd=Decimal(str(recipe.extra_price_usd or 0)),
    )


def _modifiers_dict(extras: list[dict] | None, excludes: list[dict] | None) -> dict[str, Any]:
    return {"extras": extras or [], "excludes": excludes or []}


def _modifier_rows_json_safe(rows: list[dict] | None) -> list[dict]:
    safe: list[dict] = []
    for row in rows or []:
        cleaned: dict[str, object] = {}
        for key, val in row.items():
            if val is None:
                continue
            if isinstance(val, Decimal):
                cleaned[key] = float(val)
            else:
                cleaned[key] = val
        safe.append(cleaned)
    return safe


def serialize_line_modifiers(extras: list[dict] | None, excludes: list[dict] | None) -> str | None:
    data = {
        "extras": _modifier_rows_json_safe(extras),
        "excludes": _modifier_rows_json_safe(excludes),
    }
    if not data["extras"] and not data["excludes"]:
        return None
    return json.dumps(data, ensure_ascii=False)


def parse_line_modifiers(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {"extras": [], "excludes": []}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {
                "extras": list(data.get("extras") or []),
                "excludes": list(data.get("excludes") or []),
            }
    except json.JSONDecodeError:
        pass
    return {"extras": [], "excludes": []}


def _require_inventory(user: User) -> User:
    if not require_permission(user, "inventory"):
        raise HTTPException(status_code=403, detail="Inventory access not permitted")
    return user


async def resolve_inventory_item(
    db: AsyncSession,
    *,
    item_id: int | None = None,
    name: str | None = None,
    unit: str | None = None,
    create_if_missing: bool = False,
    update_unit: bool = False,
    branch_id: int | None = None,
) -> InventoryItem:
    if item_id:
        result = await db.execute(select(InventoryItem).where(InventoryItem.id == item_id))
        item = result.scalar_one_or_none()
        if not item:
            raise HTTPException(status_code=404, detail="Inventory item not found")
        return item

    clean = (name or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="Inventory item name required")
    if branch_id is None:
        raise HTTPException(status_code=400, detail="branch_id required for inventory resolution")
    bid = branch_id
    result = await db.execute(
        select(InventoryItem).where(
            func.lower(InventoryItem.name) == clean.lower(),
            InventoryItem.branch_id == bid,
            InventoryItem.is_active.is_(True),
        )
    )
    item = result.scalar_one_or_none()
    if item:
        if unit and unit.strip():
            try:
                incoming = normalize_unit(unit)
                stored = normalize_unit(item.unit)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if incoming != stored:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Ingredient '{item.name}' is registered in {stored}. "
                        f"Cannot add stock using {incoming}. Use the locked unit."
                    ),
                )
        return item
    if not create_if_missing:
        raise HTTPException(status_code=404, detail=f"Inventory item '{clean}' not found")
    try:
        default_unit = normalize_unit(unit or "pcs")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    item = InventoryItem(name=clean, unit=default_unit, current_stock=Decimal("0"), branch_id=bid)
    db.add(item)
    await db.flush()
    return item


def _item_out(item: InventoryItem) -> InventoryItemOut:
    return InventoryItemOut(
        id=item.id,
        name=item.name,
        unit=item.unit,
        current_stock=item.current_stock,
        reorder_level=item.reorder_level,
        is_active=item.is_active,
        unit_label=normalize_unit(item.unit) if item.unit else None,
    )


def _normalize_recipe_line(
    quantity: Decimal,
    metric_unit: str | None,
    inv: InventoryItem,
) -> tuple[Decimal, str]:
    try:
        canon_qty, canon_unit = normalize_quantity(quantity, metric_unit or inv.unit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    inv_unit = normalize_unit(inv.unit)
    if canon_unit != inv_unit:
        raise HTTPException(
            status_code=400,
            detail=f"Recipe unit {canon_unit} does not match ingredient '{inv.name}' ({inv_unit})",
        )
    return canon_qty, inv_unit


async def _apply_movement(
    db: AsyncSession,
    item: InventoryItem,
    movement_type: StockMovementType,
    quantity: Decimal,
    user_id: int,
    reference: str | None = None,
    notes: str | None = None,
) -> StockMovement:
    qty = Decimal(str(quantity))
    if movement_type in (StockMovementType.OUT, StockMovementType.SALE):
        if item.current_stock < qty:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient stock for {item.name}: have {item.current_stock}, need {qty}",
            )
        item.current_stock -= qty
    elif movement_type == StockMovementType.ADJUST:
        item.current_stock = qty
    else:
        item.current_stock += qty

    mov = StockMovement(
        branch_id=item.branch_id,
        inventory_item_id=item.id,
        movement_type=movement_type,
        quantity=qty,
        balance_after=item.current_stock,
        reference=reference,
        notes=notes,
        recorded_by_id=user_id,
    )
    db.add(mov)
    await db.flush()
    return mov


async def deduct_inventory_for_sale(
    db: AsyncSession,
    menu_item_id: int,
    qty_sold: int,
    user_id: int,
    invoice_ref: str,
    *,
    branch_id: int,
    extras: list[dict] | None = None,
    excludes: list[dict] | None = None,
) -> None:
    excluded_ids: set[int] = set()
    for excl in excludes or []:
        inv_id = excl.get("inventory_item_id")
        if inv_id is not None:
            excluded_ids.add(int(inv_id))

    recipes = await db.execute(
        select(ProductRecipe)
        .options(selectinload(ProductRecipe.inventory_item))
        .where(
            ProductRecipe.menu_item_id == menu_item_id,
            ProductRecipe.recipe_role == RecipeRole.BASE.value,
        )
    )
    for recipe in recipes.scalars().all():
        if recipe.inventory_item_id in excluded_ids:
            continue
        inv = recipe.inventory_item
        if not inv or not inv.is_active:
            continue
        if inv.branch_id != branch_id:
            continue
        try:
            line_qty, _ = _normalize_recipe_line(
                recipe.quantity_per_sale,
                recipe.metric_unit or inv.unit,
                inv,
            )
        except HTTPException:
            line_qty = recipe.quantity_per_sale
        deduct_qty = line_qty * Decimal(str(qty_sold))
        await _apply_movement(
            db, inv, StockMovementType.SALE, deduct_qty, user_id,
            reference=invoice_ref, notes=f"Recipe deduct x{qty_sold}",
        )

    for extra in extras or []:
        inv_id = extra.get("inventory_item_id")
        if inv_id is None:
            continue
        inv = await db.get(InventoryItem, int(inv_id))
        if not inv or not inv.is_active or inv.branch_id != branch_id:
            continue
        raw_qty = Decimal(str(extra.get("quantity") or extra.get("quantity_per_sale") or 0))
        if raw_qty <= 0:
            continue
        extra_unit = extra.get("metric_unit") or extra.get("unit") or inv.unit
        try:
            line_qty, canon = normalize_quantity(raw_qty, extra_unit)
            if canon != normalize_unit(inv.unit):
                raise HTTPException(
                    status_code=400,
                    detail=f"Extra '{extra.get('name') or inv.name}' unit mismatch with stock ({inv.unit})",
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        deduct_qty = line_qty * Decimal(str(qty_sold))
        label = extra.get("name") or inv.name
        await _apply_movement(
            db, inv, StockMovementType.SALE, deduct_qty, user_id,
            reference=invoice_ref, notes=f"Extra {label} x{qty_sold}",
        )


@router.get("/items", response_model=list[InventoryItemOut])
async def list_items(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
    branch_id: int | None = None,
):
    _require_inventory(user)
    bid = resolve_inventory_branch_filter(user, branch_id)
    stmt = select(InventoryItem).where(InventoryItem.is_active.is_(True), InventoryItem.branch_id == bid)
    if q:
        stmt = stmt.where(InventoryItem.name.ilike(f"%{q.strip()}%"))
    result = await db.execute(stmt.order_by(InventoryItem.name))
    return [_item_out(i) for i in result.scalars().all()]


@router.get("/items/suggest", response_model=list[InventorySuggestOut])
async def suggest_items(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str = "",
    limit: int = 12,
    branch_id: int | None = None,
):
    _require_inventory(user)
    bid = resolve_inventory_branch_filter(user, branch_id)
    stmt = select(InventoryItem).where(InventoryItem.is_active.is_(True), InventoryItem.branch_id == bid)
    clean = q.strip()
    if clean:
        stmt = stmt.where(InventoryItem.name.ilike(f"%{clean}%"))
    result = await db.execute(stmt.order_by(InventoryItem.name).limit(min(max(limit, 1), 25)))
    return [
        InventorySuggestOut(id=i.id, name=i.name, unit=i.unit, current_stock=i.current_stock)
        for i in result.scalars().all()
    ]


@router.post("/items", response_model=InventoryItemOut, status_code=201)
async def create_item(
    payload: InventoryCreate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = None,
):
    _require_inventory(user)
    name = payload.name.strip()
    bid = resolve_inventory_branch_filter(user, branch_id)
    exists = await db.execute(
        select(InventoryItem).where(func.lower(InventoryItem.name) == name.lower(), InventoryItem.branch_id == bid)
    )
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Item name already exists")
    try:
        canon_unit = normalize_unit(payload.unit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    item = InventoryItem(
        name=name, unit=canon_unit, current_stock=Decimal("0"),
        reorder_level=payload.reorder_level, branch_id=bid,
    )
    db.add(item)
    await db.flush()
    if payload.current_stock > 0:
        try:
            canon_qty, _ = normalize_quantity(payload.current_stock, payload.unit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _apply_movement(
            db, item, StockMovementType.IN, canon_qty, user.id, notes="Initial stock",
        )
    await record_audit(
        db, actor_id=user.id, action="INVENTORY_CREATE", entity_type="inventory_item", entity_id=item.id,
        details={"name": item.name, "unit": item.unit}, ip_address=await resolve_client_ip(request),
    )
    return _item_out(item)


@router.get("/ledger", response_model=list[StockMovementOut])
async def ledger(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 100,
):
    _require_inventory(user)
    result = await db.execute(
        select(StockMovement, InventoryItem.name)
        .join(InventoryItem, StockMovement.inventory_item_id == InventoryItem.id)
        .order_by(StockMovement.created_at.desc())
        .limit(min(limit, 500))
    )
    return [
        StockMovementOut(
            id=mov.id,
            inventory_item_id=mov.inventory_item_id,
            inventory_name=inv_name,
            movement_type=mov.movement_type.value,
            quantity=mov.quantity,
            balance_after=mov.balance_after,
            reference=mov.reference,
            notes=mov.notes,
            recorded_by_id=mov.recorded_by_id,
            created_at=mov.created_at,
        )
        for mov, inv_name in result.all()
    ]


@router.post("/stock-in")
async def stock_in(
    payload: StockInOut,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_inventory(user)
    bid = require_operational_branch_id(user)
    try:
        input_unit = payload.unit
        if payload.inventory_item_id:
            pre = await resolve_inventory_item(
                db, item_id=payload.inventory_item_id, create_if_missing=False, branch_id=bid,
            )
            input_unit = input_unit or pre.unit
        elif payload.inventory_item_name and payload.inventory_item_name.strip():
            try:
                pre = await resolve_inventory_item(
                    db, name=payload.inventory_item_name.strip(), create_if_missing=False, branch_id=bid,
                )
                input_unit = input_unit or pre.unit
            except HTTPException:
                pass
        canon_qty, canon_unit = normalize_quantity(payload.quantity, input_unit or "pcs")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    item = await resolve_inventory_item(
        db,
        item_id=payload.inventory_item_id,
        name=payload.inventory_item_name,
        unit=canon_unit,
        create_if_missing=True,
        update_unit=False,
        branch_id=bid,
    )
    mov = await _apply_movement(
        db, item, StockMovementType.IN, canon_qty, user.id,
        notes=payload.notes or "شو فوتت بضاعة",
    )
    await record_audit(
        db, actor_id=user.id, action="STOCK_IN", entity_type="stock_movement", entity_id=mov.id,
        details={"item": item.name, "qty": str(canon_qty), "unit": item.unit},
        ip_address=await resolve_client_ip(request),
    )
    return {"detail": "Stock received", "item": item.name, "balance": str(item.current_stock), "unit": item.unit}


@router.post("/stock-out")
async def stock_out(
    payload: StockInOut,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_inventory(user)
    if not payload.inventory_item_id and not (payload.inventory_item_name and payload.inventory_item_name.strip()):
        raise HTTPException(status_code=400, detail="Provide inventory item name or id")
    bid = require_operational_branch_id(user)
    try:
        item = await resolve_inventory_item(
            db,
            item_id=payload.inventory_item_id,
            name=payload.inventory_item_name,
            create_if_missing=False,
            update_unit=False,
            branch_id=bid,
        )
        input_unit = payload.unit or item.unit
        canon_qty, _ = normalize_quantity(payload.quantity, input_unit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    notes = payload.notes or f"شو ضهرت بضاعة ({canon_qty} {item.unit})"
    mov = await _apply_movement(
        db, item, StockMovementType.OUT, canon_qty, user.id,
        notes=notes,
    )
    await record_audit(
        db, actor_id=user.id, action="STOCK_OUT", entity_type="stock_movement", entity_id=mov.id,
        details={"item": item.name, "qty": str(canon_qty), "unit": item.unit},
        ip_address=await resolve_client_ip(request),
    )
    return {"detail": "Stock removed", "item": item.name, "balance": str(item.current_stock), "unit": item.unit}


@router.get("/recipes", response_model=list[RecipeOut])
async def list_recipes(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_inventory(user)
    result = await db.execute(
        select(ProductRecipe, MenuItem.name, InventoryItem.name)
        .join(MenuItem, ProductRecipe.menu_item_id == MenuItem.id)
        .join(InventoryItem, ProductRecipe.inventory_item_id == InventoryItem.id)
    )
    return [
        _recipe_out(r, menu_item_name=mn, inventory_item_name=inn)
        for r, mn, inn in result.all()
    ]


@router.post("/recipes", response_model=RecipeOut, status_code=201)
async def create_recipe(
    payload: RecipeCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_inventory(user)
    recipe = ProductRecipe(**payload.model_dump())
    db.add(recipe)
    await db.flush()
    menu = await db.get(MenuItem, payload.menu_item_id)
    inv = await db.get(InventoryItem, payload.inventory_item_id)
    return _recipe_out(
        recipe,
        menu_item_name=menu.name if menu else "",
        inventory_item_name=inv.name if inv else "",
    )


@router.get("/recipes/product/{menu_item_id}", response_model=ProductRecipesBundleOut)
async def list_recipes_for_product(
    menu_item_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_inventory(user)
    menu = await db.get(MenuItem, menu_item_id)
    if not menu:
        raise HTTPException(status_code=404, detail="Product not found")
    result = await db.execute(
        select(ProductRecipe, InventoryItem.name)
        .join(InventoryItem, ProductRecipe.inventory_item_id == InventoryItem.id)
        .where(ProductRecipe.menu_item_id == menu_item_id)
        .order_by(ProductRecipe.id)
    )
    base: list[RecipeOut] = []
    extras: list[RecipeOut] = []
    excludes: list[RecipeOut] = []
    for r, inn in result.all():
        row = _recipe_out(r, menu_item_name=menu.name, inventory_item_name=inn)
        role = r.recipe_role or RecipeRole.BASE.value
        if role == RecipeRole.BASE.value:
            base.append(row)
    return ProductRecipesBundleOut(base=base, extras=extras, excludes=excludes)


@router.put("/recipes/product/{menu_item_id}", response_model=ProductRecipesBundleOut)
async def replace_product_recipes(
    menu_item_id: int,
    payload: ProductRecipesReplace,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_inventory(user)
    menu = await db.get(MenuItem, menu_item_id)
    if not menu:
        raise HTTPException(status_code=404, detail="Product not found")
    base_lines = payload.base
    if payload.lines is not None and not payload.base:
        base_lines = payload.lines

    existing = await db.execute(select(ProductRecipe).where(ProductRecipe.menu_item_id == menu_item_id))
    for row in existing.scalars().all():
        await db.delete(row)

    seen_base: set[int] = set()
    for line in base_lines:
        if line.inventory_item_id in seen_base:
            raise HTTPException(status_code=400, detail="Duplicate base ingredient")
        seen_base.add(line.inventory_item_id)
        inv = await db.get(InventoryItem, line.inventory_item_id)
        if not inv:
            raise HTTPException(status_code=404, detail=f"Inventory item {line.inventory_item_id} not found")
        canon_qty, canon_unit = _normalize_recipe_line(line.quantity_per_sale, line.metric_unit, inv)
        db.add(
            ProductRecipe(
                menu_item_id=menu_item_id,
                inventory_item_id=line.inventory_item_id,
                quantity_per_sale=canon_qty,
                metric_unit=canon_unit,
                recipe_role=RecipeRole.BASE.value,
                display_title=None,
                extra_price_usd=Decimal("0"),
            )
        )

    await db.flush()
    return await list_recipes_for_product(menu_item_id, user, db)


@router.delete("/recipes/{recipe_id}")
async def delete_recipe(
    recipe_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_inventory(user)
    recipe = await db.get(ProductRecipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe line not found")
    await db.delete(recipe)
    return {"detail": "Recipe line removed"}


def _global_modifier_out(mod: GlobalModifier, inv_name: str) -> GlobalModifierOut:
    return GlobalModifierOut(
        id=mod.id,
        name=mod.name,
        inventory_item_id=mod.inventory_item_id,
        inventory_item_name=inv_name,
        extra_price_usd=Decimal(str(mod.extra_price_usd or 0)),
        quantity_per_sale=mod.quantity_per_sale,
        metric_unit=mod.metric_unit,
        sort_order=mod.sort_order or 0,
        is_active=mod.is_active,
    )


@router.get("/global-modifiers", response_model=list[GlobalModifierOut])
async def list_global_modifiers(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    active_only: bool = False,
    branch_id: int | None = None,
):
    _require_inventory(user)
    bid = resolve_inventory_branch_filter(user, branch_id)
    stmt = (
        select(GlobalModifier, InventoryItem.name)
        .join(InventoryItem, GlobalModifier.inventory_item_id == InventoryItem.id)
        .where(GlobalModifier.branch_id == bid)
    )
    if active_only:
        stmt = stmt.where(GlobalModifier.is_active.is_(True))
    result = await db.execute(stmt.order_by(GlobalModifier.sort_order, GlobalModifier.name))
    return [_global_modifier_out(m, inn) for m, inn in result.all()]


@router.post("/global-modifiers", response_model=GlobalModifierOut, status_code=201)
async def create_global_modifier(
    payload: GlobalModifierCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_inventory(user)
    inv = await db.get(InventoryItem, payload.inventory_item_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Inventory item not found")
    try:
        canon_qty, canon_unit = _normalize_recipe_line(
            payload.quantity_per_sale, payload.metric_unit, inv,
        )
    except HTTPException:
        raise
    bid = require_operational_branch_id(user)
    mod = GlobalModifier(
        branch_id=bid,
        name=payload.name.strip(),
        inventory_item_id=payload.inventory_item_id,
        extra_price_usd=payload.extra_price_usd,
        quantity_per_sale=canon_qty,
        metric_unit=canon_unit,
        sort_order=payload.sort_order,
        is_active=True,
    )
    db.add(mod)
    await db.flush()
    return _global_modifier_out(mod, inv.name)


@router.put("/global-modifiers/{modifier_id}", response_model=GlobalModifierOut)
async def update_global_modifier(
    modifier_id: int,
    payload: GlobalModifierUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_inventory(user)
    bid = require_operational_branch_id(user)
    mod = await db.get(GlobalModifier, modifier_id)
    if not mod or mod.branch_id != bid:
        raise HTTPException(status_code=404, detail="Modifier not found")
    if payload.name is not None:
        mod.name = payload.name.strip()
    if payload.inventory_item_id is not None:
        inv = await db.get(InventoryItem, payload.inventory_item_id)
        if not inv:
            raise HTTPException(status_code=404, detail="Inventory item not found")
        mod.inventory_item_id = payload.inventory_item_id
    if payload.extra_price_usd is not None:
        mod.extra_price_usd = payload.extra_price_usd
    if payload.quantity_per_sale is not None:
        inv = await db.get(InventoryItem, mod.inventory_item_id)
        if inv:
            mod.quantity_per_sale, mod.metric_unit = _normalize_recipe_line(
                payload.quantity_per_sale, payload.metric_unit or mod.metric_unit, inv,
            )
        else:
            mod.quantity_per_sale = payload.quantity_per_sale
    elif payload.metric_unit is not None:
        inv = await db.get(InventoryItem, mod.inventory_item_id)
        if inv:
            _, mod.metric_unit = _normalize_recipe_line(
                mod.quantity_per_sale, payload.metric_unit, inv,
            )
    if payload.sort_order is not None:
        mod.sort_order = payload.sort_order
    if payload.is_active is not None:
        mod.is_active = payload.is_active
    await db.flush()
    inv = await db.get(InventoryItem, mod.inventory_item_id)
    return _global_modifier_out(mod, inv.name if inv else "")


@router.delete("/global-modifiers/{modifier_id}")
async def delete_global_modifier(
    modifier_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_inventory(user)
    bid = require_operational_branch_id(user)
    mod = await db.get(GlobalModifier, modifier_id)
    if not mod or mod.branch_id != bid:
        raise HTTPException(status_code=404, detail="Modifier not found")
    mod.is_active = False
    await db.flush()
    return {"detail": "Modifier deactivated"}
