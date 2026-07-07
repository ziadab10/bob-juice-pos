"""Catalog CRUD: product categories and menu items (name + category only)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, record_audit, resolve_client_ip
from database import MenuItem, ProductCategory, User, get_db
from permissions import require_permission

router = APIRouter(prefix="/api/catalog", tags=["Catalog"])


class CategoryOut(BaseModel):
    id: int
    name: str
    sort_order: int
    is_active: bool

    model_config = {"from_attributes": True}


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    sort_order: int | None = None
    is_active: bool | None = None


class ProductOut(BaseModel):
    id: int
    name: str
    category: str
    category_id: int | None
    unit_price: Decimal
    price_s: Decimal | None = None
    price_m: Decimal | None = None
    price_l: Decimal | None = None
    sizes_enabled: bool = False
    is_active: bool

    model_config = {"from_attributes": True}


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    category_id: int
    unit_price: Decimal = Field(gt=0)
    sizes_enabled: bool = False
    price_s: Decimal | None = Field(default=None, gt=0)
    price_m: Decimal | None = Field(default=None, gt=0)
    price_l: Decimal | None = Field(default=None, gt=0)
    description: str | None = None


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    category_id: int | None = None
    unit_price: Decimal | None = Field(default=None, gt=0)
    sizes_enabled: bool | None = None
    price_s: Decimal | None = None
    price_m: Decimal | None = None
    price_l: Decimal | None = None
    description: str | None = None
    is_active: bool | None = None


def _apply_product_pricing(item: MenuItem, *, sizes_enabled: bool, unit_price: Decimal, price_s=None, price_m=None, price_l=None) -> None:
    item.sizes_enabled = sizes_enabled
    item.unit_price = unit_price
    if sizes_enabled:
        item.price_s = price_s
        item.price_m = price_m if price_m is not None else unit_price
        item.price_l = price_l
    else:
        item.price_s = None
        item.price_m = None
        item.price_l = None


def _require_catalog(user: User) -> User:
    if not require_permission(user, "catalog"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Catalog management not permitted")
    return user


@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(ProductCategory).order_by(ProductCategory.sort_order, ProductCategory.name))
    return result.scalars().all()


@router.post("/categories", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: CategoryCreate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_catalog(user)
    name = payload.name.strip()
    exists = await db.execute(select(ProductCategory).where(ProductCategory.name == name))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Category already exists")
    cat = ProductCategory(name=name, sort_order=payload.sort_order)
    db.add(cat)
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="CATEGORY_CREATE", entity_type="category", entity_id=cat.id,
        details={"name": name}, ip_address=await resolve_client_ip(request),
    )
    return cat


@router.patch("/categories/{category_id}", response_model=CategoryOut)
async def update_category(
    category_id: int,
    payload: CategoryUpdate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_catalog(user)
    result = await db.execute(select(ProductCategory).where(ProductCategory.id == category_id))
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    if payload.name is not None:
        cat.name = payload.name.strip()
    if payload.sort_order is not None:
        cat.sort_order = payload.sort_order
    if payload.is_active is not None:
        cat.is_active = payload.is_active
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="CATEGORY_UPDATE", entity_type="category", entity_id=cat.id,
        details=payload.model_dump(exclude_none=True), ip_address=await resolve_client_ip(request),
    )
    return cat


@router.delete("/categories/{category_id}")
async def delete_category(
    category_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_catalog(user)
    result = await db.execute(select(ProductCategory).where(ProductCategory.id == category_id))
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    linked = await db.execute(select(MenuItem).where(MenuItem.category_id == category_id, MenuItem.is_active.is_(True)))
    if linked.scalars().first():
        raise HTTPException(status_code=400, detail="Cannot delete category with active products")
    cat.is_active = False
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="CATEGORY_DELETE", entity_type="category", entity_id=cat.id,
        details={"name": cat.name}, ip_address=await resolve_client_ip(request),
    )
    return {"detail": "Category deactivated"}


@router.get("/products", response_model=list[ProductOut])
async def list_products_admin(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_catalog(user)
    result = await db.execute(select(MenuItem).order_by(MenuItem.category, MenuItem.name))
    return result.scalars().all()


@router.post("/products", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_catalog(user)
    name = payload.name.strip()
    cat_row = await db.execute(select(ProductCategory).where(ProductCategory.id == payload.category_id))
    cat = cat_row.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    exists = await db.execute(
        select(MenuItem).where(MenuItem.name == name, MenuItem.category_id == cat.id, MenuItem.is_active.is_(True))
    )
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Product already exists in this category")
    item = MenuItem(
        name=name,
        category=cat.name,
        category_id=cat.id,
        description=payload.description,
        unit_price=payload.unit_price,
        sizes_enabled=False,
    )
    if payload.sizes_enabled:
        if payload.price_s is None or payload.price_m is None or payload.price_l is None:
            raise HTTPException(status_code=400, detail="S, M, and L prices required when multi-size is enabled")
        _apply_product_pricing(
            item,
            sizes_enabled=True,
            unit_price=payload.price_m,
            price_s=payload.price_s,
            price_m=payload.price_m,
            price_l=payload.price_l,
        )
    else:
        _apply_product_pricing(item, sizes_enabled=False, unit_price=payload.unit_price)
    db.add(item)
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="PRODUCT_CREATE", entity_type="menu_item", entity_id=item.id,
        details={"name": item.name, "category": cat.name}, ip_address=await resolve_client_ip(request),
    )
    return item


@router.patch("/products/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: int,
    payload: ProductUpdate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_catalog(user)
    result = await db.execute(select(MenuItem).where(MenuItem.id == product_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Product not found")
    if payload.name is not None:
        item.name = payload.name.strip()
    if payload.category_id is not None:
        cat_row = await db.execute(select(ProductCategory).where(ProductCategory.id == payload.category_id))
        cat = cat_row.scalar_one_or_none()
        if not cat:
            raise HTTPException(status_code=404, detail="Category not found")
        item.category_id = cat.id
        item.category = cat.name
    if payload.sizes_enabled is not None or payload.unit_price is not None or payload.price_s is not None or payload.price_m is not None or payload.price_l is not None:
        enabled = payload.sizes_enabled if payload.sizes_enabled is not None else item.sizes_enabled
        if enabled:
            ps = payload.price_s if payload.price_s is not None else item.price_s
            pm = payload.price_m if payload.price_m is not None else (item.price_m or item.unit_price)
            pl = payload.price_l if payload.price_l is not None else item.price_l
            if ps is None or pm is None or pl is None:
                raise HTTPException(status_code=400, detail="S, M, and L prices required when multi-size is enabled")
            _apply_product_pricing(item, sizes_enabled=True, unit_price=pm, price_s=ps, price_m=pm, price_l=pl)
        else:
            base = payload.unit_price if payload.unit_price is not None else item.unit_price
            _apply_product_pricing(item, sizes_enabled=False, unit_price=base)
    if payload.description is not None:
        item.description = payload.description
    if payload.is_active is not None:
        item.is_active = payload.is_active
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="PRODUCT_UPDATE", entity_type="menu_item", entity_id=item.id,
        details=payload.model_dump(exclude_none=True), ip_address=await resolve_client_ip(request),
    )
    return item


@router.delete("/products/{product_id}")
async def delete_product(
    product_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_catalog(user)
    result = await db.execute(select(MenuItem).where(MenuItem.id == product_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Product not found")
    item.is_active = False
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="PRODUCT_DELETE", entity_type="menu_item", entity_id=item.id,
        details={"name": item.name}, ip_address=await resolve_client_ip(request),
    )
    return {"detail": "Product deactivated"}
