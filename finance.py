"""Finance: system settings, expenses, suppliers & debts (admin-only writes)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import cast, func, or_, select, String
from sqlalchemy.ext.asyncio import AsyncSession
from auth import get_current_user, record_audit, resolve_client_ip
from branch_scope import resolve_inventory_branch_filter, resolve_report_branch_filter
from database import (
    BillInvoiceCounter,
    Customer,
    CustomerDebt,
    Expense,
    ExpenseCategory,
    IntakePaymentStatus,
    InventoryIntake,
    InventoryItem,
    StockMovementType,
    Supplier,
    SupplierDebt,
    SystemSettings,
    User,
    UserRole,
    get_db,
    get_system_settings,
    usd_to_lbp,
)
from inventory import _apply_movement, resolve_inventory_item
from permissions import require_permission
from sync_service import enqueue_sync

router = APIRouter(prefix="/api/finance", tags=["Finance"])

BILL_INVOICE_START = 1001


def bill_display_number(*, invoice_number: int, reference_number: str | None = None) -> str:
    ref = (reference_number or "").strip()
    if ref:
        return ref
    return str(invoice_number)


async def allocate_bill_invoice_number(db: AsyncSession) -> int:
    result = await db.execute(select(BillInvoiceCounter).where(BillInvoiceCounter.id == 1))
    counter = result.scalar_one_or_none()
    if not counter:
        counter = BillInvoiceCounter(id=1, next_number=BILL_INVOICE_START)
        db.add(counter)
        await db.flush()
    num = int(counter.next_number)
    counter.next_number = num + 1
    await db.flush()
    return num


def _bill_search_clause(term: str, debt_model):
    q = term.strip()
    like = f"%{q}%"
    clauses = [
        debt_model.reference_number.ilike(like),
        cast(debt_model.invoice_number, String).ilike(like),
    ]
    if q.isdigit():
        clauses.append(debt_model.invoice_number == int(q))
    return or_(*clauses)


def _require_perm(user: User, permission: str) -> None:
    if not require_permission(user, permission):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission denied: {permission}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SettingsOut(BaseModel):
    exchange_rate_usd_lbp: str
    toters_commission_pct: str
    talabna_commission_pct: str
    markit_commission_pct: str
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class SettingsUpdate(BaseModel):
    exchange_rate_usd_lbp: Decimal = Field(gt=0)
    toters_commission_pct: Decimal = Field(ge=0, le=100)
    talabna_commission_pct: Decimal = Field(ge=0, le=100)
    markit_commission_pct: Decimal = Field(ge=0, le=100)


class ExpenseCreate(BaseModel):
    description: str = Field(min_length=2, max_length=256)
    category: ExpenseCategory
    amount_usd: Decimal = Field(gt=0)
    expense_date: date
    notes: str | None = None


class ExpenseOut(BaseModel):
    id: int
    description: str
    category: str
    amount_usd: Decimal
    amount_lbp: Decimal
    exchange_rate_snapshot: Decimal
    expense_date: date
    recorded_by_id: int
    notes: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SupplierCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    category: str = Field(min_length=2, max_length=64)
    contact_phone: str | None = None
    contact_email: str | None = None
    notes: str | None = None


class SupplierUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=128)
    category: str | None = Field(default=None, min_length=2, max_length=64)
    contact_phone: str | None = None
    contact_email: str | None = None
    notes: str | None = None


class SupplierOut(BaseModel):
    id: int
    name: str
    category: str
    contact_phone: str | None
    contact_email: str | None
    notes: str | None
    is_active: bool
    balance_usd: str = "0.00"
    balance_lbp: str = "0"

    model_config = {"from_attributes": True}


class EntitySuggestOut(BaseModel):
    id: int
    name: str
    category: str | None = None
    contact_phone: str | None = None


class DebtCreate(BaseModel):
    supplier_id: int | None = None
    supplier_name: str | None = Field(default=None, min_length=1, max_length=128)
    supplier_category: str | None = Field(default=None, max_length=64)
    description: str = Field(min_length=2, max_length=256)
    amount_usd: Decimal = Field(gt=0)
    due_date: date | None = None
    is_paid: bool = False
    reference_number: str | None = Field(default=None, max_length=64)


class DebtUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=2, max_length=256)
    amount_usd: Decimal | None = Field(default=None, gt=0)
    due_date: date | None = None
    is_settled: bool | None = None
    reference_number: str | None = Field(default=None, max_length=64)


class DebtOut(BaseModel):
    id: int
    invoice_number: int
    reference_number: str | None = None
    display_number: str = ""
    supplier_id: int
    supplier_name: str = ""
    description: str
    amount_usd: Decimal
    amount_lbp: Decimal
    exchange_rate_snapshot: Decimal
    due_date: date | None
    is_settled: bool
    is_paid: bool = False
    payment_status: str = "Unpaid"
    settled_at: datetime | None
    recorded_by_id: int
    created_at: datetime

    model_config = {"from_attributes": True}


def _debt_out(debt: SupplierDebt, *, supplier_name: str = "") -> DebtOut:
    row = DebtOut.model_validate(debt)
    row.supplier_name = supplier_name
    row.is_paid = bool(debt.is_settled)
    row.payment_status = "Paid" if debt.is_settled else "Unpaid"
    row.display_number = bill_display_number(
        invoice_number=debt.invoice_number,
        reference_number=debt.reference_number,
    )
    return row


class CustomerCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    contact_phone: str | None = None
    notes: str | None = None


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=128)
    contact_phone: str | None = None
    notes: str | None = None


class CustomerOut(BaseModel):
    id: int
    name: str
    contact_phone: str | None
    notes: str | None
    is_active: bool
    balance_usd: str = "0.00"
    balance_lbp: str = "0"

    model_config = {"from_attributes": True}


class CustomerDebtCreate(BaseModel):
    customer_id: int | None = None
    customer_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str = Field(min_length=2, max_length=256)
    amount_usd: Decimal = Field(gt=0)
    due_date: date | None = None
    is_paid: bool = False
    reference_number: str | None = Field(default=None, max_length=64)


class CustomerDebtUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=2, max_length=256)
    amount_usd: Decimal | None = Field(default=None, gt=0)
    due_date: date | None = None
    is_settled: bool | None = None
    reference_number: str | None = Field(default=None, max_length=64)


class CustomerDebtOut(BaseModel):
    id: int
    invoice_number: int
    reference_number: str | None = None
    display_number: str = ""
    customer_id: int
    customer_name: str = ""
    description: str
    amount_usd: Decimal
    amount_lbp: Decimal
    exchange_rate_snapshot: Decimal
    due_date: date | None
    is_settled: bool
    is_paid: bool = False
    payment_status: str = "Unpaid"
    settled_at: datetime | None
    recorded_by_id: int
    created_at: datetime

    model_config = {"from_attributes": True}


def _customer_debt_out(debt: CustomerDebt, *, customer_name: str = "") -> CustomerDebtOut:
    row = CustomerDebtOut.model_validate(debt)
    row.customer_name = customer_name
    row.is_paid = bool(debt.is_settled)
    row.payment_status = "Paid" if debt.is_settled else "Unpaid"
    row.display_number = bill_display_number(
        invoice_number=debt.invoice_number,
        reference_number=debt.reference_number,
    )
    return row


class IntakeCreate(BaseModel):
    supplier_id: int | None = None
    supplier_name: str | None = Field(default=None, min_length=1, max_length=128)
    inventory_item_id: int | None = None
    inventory_item_name: str = Field(min_length=1, max_length=128)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(default="pcs", max_length=64)
    unit_cost_usd: Decimal = Field(gt=0)
    payment_status: IntakePaymentStatus = IntakePaymentStatus.UNPAID
    amount_paid_usd: Decimal = Field(default=Decimal("0"), ge=0)
    intake_date: date
    notes: str | None = None


class IntakeOut(BaseModel):
    id: int
    branch_id: int
    supplier_id: int
    supplier_name: str = ""
    inventory_item_id: int
    inventory_item_name: str = ""
    quantity: Decimal
    unit: str
    unit_cost_usd: Decimal
    total_cost_usd: Decimal
    total_cost_lbp: Decimal
    payment_status: str
    amount_paid_usd: Decimal
    amount_paid_lbp: Decimal
    intake_date: date
    notes: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


async def _supplier_balance(db: AsyncSession, supplier_id: int) -> tuple[Decimal, Decimal]:
    unpaid_usd = await db.execute(
        select(func.coalesce(func.sum(SupplierDebt.amount_usd), 0)).where(
            SupplierDebt.supplier_id == supplier_id,
            SupplierDebt.is_settled.is_(False),
        )
    )
    unpaid_lbp = await db.execute(
        select(func.coalesce(func.sum(SupplierDebt.amount_lbp), 0)).where(
            SupplierDebt.supplier_id == supplier_id,
            SupplierDebt.is_settled.is_(False),
        )
    )
    return Decimal(str(unpaid_usd.scalar_one())), Decimal(str(unpaid_lbp.scalar_one()))


async def _customer_balance(db: AsyncSession, customer_id: int) -> tuple[Decimal, Decimal]:
    unpaid_usd = await db.execute(
        select(func.coalesce(func.sum(CustomerDebt.amount_usd), 0)).where(
            CustomerDebt.customer_id == customer_id,
            CustomerDebt.is_settled.is_(False),
        )
    )
    unpaid_lbp = await db.execute(
        select(func.coalesce(func.sum(CustomerDebt.amount_lbp), 0)).where(
            CustomerDebt.customer_id == customer_id,
            CustomerDebt.is_settled.is_(False),
        )
    )
    return Decimal(str(unpaid_usd.scalar_one())), Decimal(str(unpaid_lbp.scalar_one()))


async def _total_customer_outstanding(db: AsyncSession) -> tuple[Decimal, Decimal]:
    unpaid_usd = await db.execute(
        select(func.coalesce(func.sum(CustomerDebt.amount_usd), 0)).where(
            CustomerDebt.is_settled.is_(False),
        )
    )
    unpaid_lbp = await db.execute(
        select(func.coalesce(func.sum(CustomerDebt.amount_lbp), 0)).where(
            CustomerDebt.is_settled.is_(False),
        )
    )
    return Decimal(str(unpaid_usd.scalar_one())), Decimal(str(unpaid_lbp.scalar_one()))


async def _total_supplier_outstanding(db: AsyncSession) -> tuple[Decimal, Decimal]:
    unpaid_usd = await db.execute(
        select(func.coalesce(func.sum(SupplierDebt.amount_usd), 0)).where(
            SupplierDebt.is_settled.is_(False),
        )
    )
    unpaid_lbp = await db.execute(
        select(func.coalesce(func.sum(SupplierDebt.amount_lbp), 0)).where(
            SupplierDebt.is_settled.is_(False),
        )
    )
    return Decimal(str(unpaid_usd.scalar_one())), Decimal(str(unpaid_lbp.scalar_one()))


# ---------------------------------------------------------------------------
# Settings (read: all auth users | write: admin)
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=SettingsOut)
async def read_settings(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    s = await get_system_settings(db)
    return SettingsOut(
        exchange_rate_usd_lbp=str(s.exchange_rate_usd_lbp),
        toters_commission_pct=str(s.toters_commission_pct),
        talabna_commission_pct=str(s.talabna_commission_pct),
        markit_commission_pct=str(s.markit_commission_pct),
        updated_at=s.updated_at,
    )


@router.patch("/settings", response_model=SettingsOut)
async def update_settings(
    payload: SettingsUpdate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_perm(user, "global_settings")
    s = await get_system_settings(db)
    s.exchange_rate_usd_lbp = payload.exchange_rate_usd_lbp
    s.toters_commission_pct = payload.toters_commission_pct
    s.talabna_commission_pct = payload.talabna_commission_pct
    s.markit_commission_pct = payload.markit_commission_pct
    s.updated_by_id = user.id
    s.updated_at = datetime.utcnow()
    await db.flush()
    await record_audit(
        db,
        actor_id=user.id,
        action="SETTINGS_UPDATE",
        entity_type="system_settings",
        entity_id=1,
        details={
            "exchange_rate_usd_lbp": str(payload.exchange_rate_usd_lbp),
            "toters_commission_pct": str(payload.toters_commission_pct),
            "talabna_commission_pct": str(payload.talabna_commission_pct),
            "markit_commission_pct": str(payload.markit_commission_pct),
        },
        ip_address=await resolve_client_ip(request),
    )
    return SettingsOut(
        exchange_rate_usd_lbp=str(s.exchange_rate_usd_lbp),
        toters_commission_pct=str(s.toters_commission_pct),
        talabna_commission_pct=str(s.talabna_commission_pct),
        markit_commission_pct=str(s.markit_commission_pct),
        updated_at=s.updated_at,
    )


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


@router.get("/expenses", response_model=list[ExpenseOut])
async def list_expenses(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = Query(None),
):
    _require_perm(user, "expenses")
    scope = resolve_report_branch_filter(user, branch_id)
    q = select(Expense).order_by(Expense.expense_date.desc(), Expense.id.desc()).limit(500)
    if scope is not None:
        q = q.where(Expense.branch_id == scope)
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/expenses", response_model=ExpenseOut, status_code=status.HTTP_201_CREATED)
async def create_expense(
    payload: ExpenseCreate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = Query(None),
):
    _require_perm(user, "expenses")
    settings_row = await get_system_settings(db)
    rate = Decimal(str(settings_row.exchange_rate_usd_lbp))
    expense = Expense(
        branch_id=resolve_inventory_branch_filter(user, branch_id),
        description=payload.description.strip(),
        category=payload.category,
        amount_usd=payload.amount_usd,
        amount_lbp=usd_to_lbp(payload.amount_usd, rate),
        exchange_rate_snapshot=rate,
        expense_date=payload.expense_date,
        recorded_by_id=user.id,
        notes=payload.notes,
    )
    db.add(expense)
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="EXPENSE_CREATE", entity_type="expense", entity_id=expense.id,
        details={"amount_usd": str(expense.amount_usd), "category": expense.category.value},
        ip_address=await resolve_client_ip(request),
    )
    return expense


# ---------------------------------------------------------------------------
# Suppliers & debts (admin only)
# ---------------------------------------------------------------------------


@router.get("/suppliers", response_model=list[SupplierOut])
async def list_suppliers(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_perm(user, "suppliers")
    result = await db.execute(select(Supplier).order_by(Supplier.name))
    suppliers = result.scalars().all()
    out = []
    for s in suppliers:
        bal_usd, bal_lbp = await _supplier_balance(db, s.id)
        row = SupplierOut.model_validate(s)
        row.balance_usd = str(bal_usd)
        row.balance_lbp = str(bal_lbp)
        out.append(row)
    return out


@router.get("/suppliers/suggest", response_model=list[EntitySuggestOut])
async def suggest_suppliers(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str = "",
    limit: int = 12,
):
    _require_perm(user, "suppliers")
    stmt = select(Supplier).order_by(Supplier.name)
    needle = q.strip()
    if needle:
        pat = f"%{needle.lower()}%"
        stmt = stmt.where(
            or_(func.lower(Supplier.name).like(pat), func.lower(Supplier.category).like(pat))
        )
    result = await db.execute(stmt.limit(min(max(limit, 1), 30)))
    return [
        EntitySuggestOut(id=s.id, name=s.name, category=s.category, contact_phone=s.contact_phone)
        for s in result.scalars().all()
    ]


@router.post("/suppliers", response_model=SupplierOut, status_code=status.HTTP_201_CREATED)
async def create_supplier(
    payload: SupplierCreate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_perm(user, "suppliers")
    name = payload.name.strip()
    existing = await db.execute(select(Supplier).where(func.lower(Supplier.name) == name.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Supplier already exists")
    supplier = Supplier(
        name=name,
        category=payload.category.strip(),
        contact_phone=payload.contact_phone,
        contact_email=payload.contact_email,
        notes=payload.notes,
    )
    db.add(supplier)
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="SUPPLIER_CREATE", entity_type="supplier", entity_id=supplier.id,
        details={"name": supplier.name}, ip_address=await resolve_client_ip(request),
    )
    row = SupplierOut.model_validate(supplier)
    row.balance_usd = "0.00"
    row.balance_lbp = "0"
    return row


@router.patch("/suppliers/{supplier_id}", response_model=SupplierOut)
async def update_supplier(
    supplier_id: int,
    payload: SupplierUpdate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_perm(user, "suppliers")
    supplier = await db.get(Supplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    if payload.name is not None:
        name = payload.name.strip()
        dup = await db.execute(
            select(Supplier).where(func.lower(Supplier.name) == name.lower(), Supplier.id != supplier_id)
        )
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Supplier name already exists")
        supplier.name = name
    if payload.category is not None:
        supplier.category = payload.category.strip()
    if payload.contact_phone is not None:
        supplier.contact_phone = payload.contact_phone or None
    if payload.contact_email is not None:
        supplier.contact_email = payload.contact_email or None
    if payload.notes is not None:
        supplier.notes = payload.notes
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="SUPPLIER_UPDATE", entity_type="supplier", entity_id=supplier.id,
        details={"name": supplier.name}, ip_address=await resolve_client_ip(request),
    )
    bal_usd, bal_lbp = await _supplier_balance(db, supplier.id)
    row = SupplierOut.model_validate(supplier)
    row.balance_usd = str(bal_usd)
    row.balance_lbp = str(bal_lbp)
    return row


@router.get("/debts", response_model=list[DebtOut])
async def list_all_supplier_debts(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
):
    _require_perm(user, "suppliers")
    stmt = (
        select(SupplierDebt, Supplier.name)
        .join(Supplier, Supplier.id == SupplierDebt.supplier_id)
        .order_by(SupplierDebt.created_at.desc())
        .limit(500)
    )
    if q and (term := q.strip()):
        stmt = stmt.where(_bill_search_clause(term, SupplierDebt))
    result = await db.execute(stmt)
    out = []
    for debt, supplier_name in result.all():
        out.append(_debt_out(debt, supplier_name=supplier_name))
    return out


@router.get("/suppliers/{supplier_id}/debts", response_model=list[DebtOut])
async def list_debts(
    supplier_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
):
    _require_perm(user, "suppliers")
    supplier = await db.get(Supplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    stmt = (
        select(SupplierDebt)
        .where(SupplierDebt.supplier_id == supplier_id)
        .order_by(SupplierDebt.created_at.desc())
    )
    if q and (term := q.strip()):
        stmt = stmt.where(_bill_search_clause(term, SupplierDebt))
    result = await db.execute(stmt)
    return [_debt_out(d, supplier_name=supplier.name) for d in result.scalars().all()]


@router.post("/debts", response_model=DebtOut, status_code=status.HTTP_201_CREATED)
async def create_debt(
    payload: DebtCreate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = Query(None),
):
    _require_perm(user, "suppliers")
    supplier: Supplier | None = None
    if payload.supplier_id:
        supplier = await db.get(Supplier, payload.supplier_id)
    elif payload.supplier_name:
        name = payload.supplier_name.strip()
        result = await db.execute(select(Supplier).where(func.lower(Supplier.name) == name.lower()))
        supplier = result.scalar_one_or_none()
        if not supplier:
            supplier = Supplier(
                name=name,
                category=(payload.supplier_category or "General").strip(),
            )
            db.add(supplier)
            await db.flush()
    if not supplier:
        raise HTTPException(status_code=400, detail="Select an existing supplier or enter a new supplier name")
    settings_row = await get_system_settings(db)
    rate = Decimal(str(settings_row.exchange_rate_usd_lbp))
    inv_num = await allocate_bill_invoice_number(db)
    ref = (payload.reference_number or "").strip() or None
    debt = SupplierDebt(
        branch_id=resolve_inventory_branch_filter(user, branch_id),
        supplier_id=supplier.id,
        invoice_number=inv_num,
        reference_number=ref,
        description=payload.description.strip(),
        amount_usd=payload.amount_usd,
        amount_lbp=usd_to_lbp(payload.amount_usd, rate),
        exchange_rate_snapshot=rate,
        due_date=payload.due_date,
        is_settled=payload.is_paid,
        settled_at=datetime.utcnow() if payload.is_paid else None,
        recorded_by_id=user.id,
    )
    db.add(debt)
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="SUPPLIER_DEBT_CREATE", entity_type="supplier_debt", entity_id=debt.id,
        details={"supplier_id": supplier.id, "supplier_name": supplier.name, "amount_usd": str(payload.amount_usd), "is_paid": payload.is_paid},
        ip_address=await resolve_client_ip(request),
    )
    return _debt_out(debt, supplier_name=supplier.name)


@router.patch("/debts/{debt_id}", response_model=DebtOut)
async def update_debt(
    debt_id: int,
    payload: DebtUpdate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_perm(user, "suppliers")
    debt = await db.get(SupplierDebt, debt_id)
    if not debt:
        raise HTTPException(status_code=404, detail="Debt record not found")
    supplier = await db.get(Supplier, debt.supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    if payload.description is not None:
        debt.description = payload.description.strip()
    if payload.amount_usd is not None:
        settings_row = await get_system_settings(db)
        rate = Decimal(str(settings_row.exchange_rate_usd_lbp))
        debt.amount_usd = payload.amount_usd
        debt.amount_lbp = usd_to_lbp(payload.amount_usd, rate)
        debt.exchange_rate_snapshot = rate
    if payload.due_date is not None:
        debt.due_date = payload.due_date
    if payload.reference_number is not None:
        debt.reference_number = payload.reference_number.strip() or None
    if payload.is_settled is not None:
        debt.is_settled = payload.is_settled
        debt.settled_at = datetime.utcnow() if payload.is_settled else None
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="SUPPLIER_DEBT_UPDATE", entity_type="supplier_debt", entity_id=debt.id,
        details={"supplier_id": debt.supplier_id, "is_settled": debt.is_settled},
        ip_address=await resolve_client_ip(request),
    )
    return _debt_out(debt, supplier_name=supplier.name)


@router.patch("/debts/{debt_id}/settle")
async def settle_debt(
    debt_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_perm(user, "suppliers")
    debt = await db.get(SupplierDebt, debt_id)
    if not debt:
        raise HTTPException(status_code=404, detail="Debt record not found")
    debt.is_settled = True
    debt.settled_at = datetime.utcnow()
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="SUPPLIER_DEBT_SETTLE", entity_type="supplier_debt", entity_id=debt.id,
        details={"supplier_id": debt.supplier_id}, ip_address=await resolve_client_ip(request),
    )
    return {"detail": "Debt marked as settled"}


# ---------------------------------------------------------------------------
# Inventory Intake (Purchases)
# ---------------------------------------------------------------------------


@router.get("/intakes", response_model=list[IntakeOut])
async def list_intakes(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = Query(None),
):
    _require_perm(user, "suppliers")
    bid = resolve_inventory_branch_filter(user, branch_id)
    result = await db.execute(
        select(InventoryIntake, Supplier.name, InventoryItem.name)
        .join(Supplier, Supplier.id == InventoryIntake.supplier_id)
        .join(InventoryItem, InventoryItem.id == InventoryIntake.inventory_item_id)
        .where(InventoryIntake.branch_id == bid)
        .order_by(InventoryIntake.intake_date.desc(), InventoryIntake.id.desc())
        .limit(500)
    )
    out = []
    for intake, sup_name, inv_name in result.all():
        row = IntakeOut.model_validate(intake)
        row.supplier_name = sup_name
        row.inventory_item_name = inv_name
        out.append(row)
    return out


@router.post("/intakes", response_model=IntakeOut, status_code=status.HTTP_201_CREATED)
async def create_intake(
    payload: IntakeCreate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = Query(None),
):
    _require_perm(user, "suppliers")
    supplier: Supplier | None = None
    if payload.supplier_id:
        supplier = await db.get(Supplier, payload.supplier_id)
    elif payload.supplier_name:
        name = payload.supplier_name.strip()
        result = await db.execute(select(Supplier).where(func.lower(Supplier.name) == name.lower()))
        supplier = result.scalar_one_or_none()
        if not supplier:
            supplier = Supplier(name=name, category="General")
            db.add(supplier)
            await db.flush()
    if not supplier:
        raise HTTPException(status_code=400, detail="Supplier required")

    settings_row = await get_system_settings(db)
    rate = Decimal(str(settings_row.exchange_rate_usd_lbp))
    bid = resolve_inventory_branch_filter(user, branch_id)
    from inventory_units import normalize_quantity

    try:
        stock_qty, canon_unit = normalize_quantity(payload.quantity, payload.unit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    inv_item = await resolve_inventory_item(
        db,
        item_id=payload.inventory_item_id,
        name=payload.inventory_item_name,
        unit=canon_unit,
        create_if_missing=True,
        update_unit=False,
        branch_id=bid,
    )
    if inv_item.branch_id != bid:
        inv_item.branch_id = bid

    total_usd = (payload.quantity * payload.unit_cost_usd).quantize(Decimal("0.01"))
    total_lbp = usd_to_lbp(total_usd, rate)
    paid_usd = min(payload.amount_paid_usd, total_usd).quantize(Decimal("0.01"))
    paid_lbp = usd_to_lbp(paid_usd, rate)

    if payload.payment_status == IntakePaymentStatus.PAID:
        paid_usd = total_usd
        paid_lbp = total_lbp
    elif payload.payment_status == IntakePaymentStatus.UNPAID:
        paid_usd = Decimal("0")
        paid_lbp = Decimal("0")

    intake = InventoryIntake(
        branch_id=bid,
        supplier_id=supplier.id,
        inventory_item_id=inv_item.id,
        quantity=payload.quantity,
        unit=payload.unit,
        unit_cost_usd=payload.unit_cost_usd,
        total_cost_usd=total_usd,
        total_cost_lbp=total_lbp,
        payment_status=payload.payment_status,
        amount_paid_usd=paid_usd,
        amount_paid_lbp=paid_lbp,
        exchange_rate_snapshot=rate,
        intake_date=payload.intake_date,
        recorded_by_id=user.id,
        notes=payload.notes,
    )
    db.add(intake)
    await db.flush()

    # Single stock increment — canonical grams/ml/pcs applied to ledger balance.
    await _apply_movement(
        db, inv_item, StockMovementType.IN, stock_qty, user.id,
        reference=f"INTAKE-{intake.id}", notes=f"Purchase from {supplier.name}",
    )

    balance_due = total_usd - paid_usd
    if balance_due > 0:
        inv_num = await allocate_bill_invoice_number(db)
        db.add(
            SupplierDebt(
                branch_id=bid,
                supplier_id=supplier.id,
                invoice_number=inv_num,
                description=f"Inventory intake #{intake.id}: {inv_item.name}",
                amount_usd=balance_due,
                amount_lbp=usd_to_lbp(balance_due, rate),
                exchange_rate_snapshot=rate,
                recorded_by_id=user.id,
            )
        )

    await record_audit(
        db, actor_id=user.id, action="INVENTORY_INTAKE", entity_type="inventory_intake", entity_id=intake.id,
        details={"supplier": supplier.name, "item": inv_item.name, "total_usd": str(total_usd)},
        ip_address=await resolve_client_ip(request),
    )
    await enqueue_sync(db, entity_type="inventory_intake", entity_id=intake.id, payload=IntakeOut.model_validate(intake).model_dump())
    row = IntakeOut.model_validate(intake)
    row.supplier_name = supplier.name
    row.inventory_item_name = inv_item.name
    return row


# ---------------------------------------------------------------------------
# Customer debts (receivables)
# ---------------------------------------------------------------------------


@router.get("/customers", response_model=list[CustomerOut])
async def list_customers(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_perm(user, "suppliers")
    result = await db.execute(select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.name))
    out = []
    for customer in result.scalars().all():
        bal_usd, bal_lbp = await _customer_balance(db, customer.id)
        row = CustomerOut.model_validate(customer)
        row.balance_usd = str(bal_usd)
        row.balance_lbp = str(bal_lbp)
        out.append(row)
    return out


@router.get("/customers/suggest", response_model=list[EntitySuggestOut])
async def suggest_customers(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str = "",
    limit: int = 12,
):
    _require_perm(user, "suppliers")
    stmt = select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.name)
    needle = q.strip()
    if needle:
        pat = f"%{needle.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Customer.name).like(pat),
                func.lower(cast(Customer.contact_phone, String)).like(pat),
            )
        )
    result = await db.execute(stmt.limit(min(max(limit, 1), 30)))
    return [
        EntitySuggestOut(id=c.id, name=c.name, contact_phone=c.contact_phone)
        for c in result.scalars().all()
    ]


@router.post("/customers", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
async def create_customer(
    payload: CustomerCreate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_perm(user, "suppliers")
    name = payload.name.strip()
    existing = await db.execute(select(Customer).where(func.lower(Customer.name) == name.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Customer already exists")
    customer = Customer(name=name, contact_phone=payload.contact_phone, notes=payload.notes)
    db.add(customer)
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="CUSTOMER_CREATE", entity_type="customer", entity_id=customer.id,
        details={"name": customer.name}, ip_address=await resolve_client_ip(request),
    )
    row = CustomerOut.model_validate(customer)
    row.balance_usd = "0.00"
    row.balance_lbp = "0"
    return row


@router.patch("/customers/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_perm(user, "suppliers")
    customer = await db.get(Customer, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if payload.name is not None:
        name = payload.name.strip()
        dup = await db.execute(
            select(Customer).where(func.lower(Customer.name) == name.lower(), Customer.id != customer_id)
        )
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Customer name already exists")
        customer.name = name
    if payload.contact_phone is not None:
        customer.contact_phone = payload.contact_phone or None
    if payload.notes is not None:
        customer.notes = payload.notes
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="CUSTOMER_UPDATE", entity_type="customer", entity_id=customer.id,
        details={"name": customer.name}, ip_address=await resolve_client_ip(request),
    )
    bal_usd, bal_lbp = await _customer_balance(db, customer.id)
    row = CustomerOut.model_validate(customer)
    row.balance_usd = str(bal_usd)
    row.balance_lbp = str(bal_lbp)
    return row


@router.get("/customer-debts", response_model=list[CustomerDebtOut])
async def list_all_customer_debts(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
):
    _require_perm(user, "suppliers")
    stmt = (
        select(CustomerDebt, Customer.name)
        .join(Customer, Customer.id == CustomerDebt.customer_id)
        .order_by(CustomerDebt.created_at.desc())
        .limit(500)
    )
    if q and (term := q.strip()):
        stmt = stmt.where(_bill_search_clause(term, CustomerDebt))
    result = await db.execute(stmt)
    out = []
    for debt, customer_name in result.all():
        out.append(_customer_debt_out(debt, customer_name=customer_name))
    return out


@router.get("/customers/{customer_id}/debts", response_model=list[CustomerDebtOut])
async def list_customer_debts(
    customer_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
):
    _require_perm(user, "suppliers")
    customer = await db.get(Customer, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    stmt = (
        select(CustomerDebt)
        .where(CustomerDebt.customer_id == customer_id)
        .order_by(CustomerDebt.created_at.desc())
    )
    if q and (term := q.strip()):
        stmt = stmt.where(_bill_search_clause(term, CustomerDebt))
    result = await db.execute(stmt)
    return [_customer_debt_out(d, customer_name=customer.name) for d in result.scalars().all()]


@router.post("/customer-debts", response_model=CustomerDebtOut, status_code=status.HTTP_201_CREATED)
async def create_customer_debt(
    payload: CustomerDebtCreate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = Query(None),
):
    _require_perm(user, "suppliers")
    customer: Customer | None = None
    if payload.customer_id:
        customer = await db.get(Customer, payload.customer_id)
    elif payload.customer_name:
        name = payload.customer_name.strip()
        result = await db.execute(select(Customer).where(func.lower(Customer.name) == name.lower()))
        customer = result.scalar_one_or_none()
        if not customer:
            customer = Customer(name=name)
            db.add(customer)
            await db.flush()
    if not customer:
        raise HTTPException(status_code=400, detail="Select an existing customer or enter a new customer name")

    settings_row = await get_system_settings(db)
    rate = Decimal(str(settings_row.exchange_rate_usd_lbp))
    inv_num = await allocate_bill_invoice_number(db)
    ref = (payload.reference_number or "").strip() or None
    debt = CustomerDebt(
        branch_id=resolve_inventory_branch_filter(user, branch_id),
        customer_id=customer.id,
        invoice_number=inv_num,
        reference_number=ref,
        description=payload.description.strip(),
        amount_usd=payload.amount_usd,
        amount_lbp=usd_to_lbp(payload.amount_usd, rate),
        exchange_rate_snapshot=rate,
        due_date=payload.due_date,
        is_settled=payload.is_paid,
        settled_at=datetime.utcnow() if payload.is_paid else None,
        recorded_by_id=user.id,
    )
    db.add(debt)
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="CUSTOMER_DEBT_CREATE", entity_type="customer_debt", entity_id=debt.id,
        details={"customer_id": customer.id, "customer_name": customer.name, "amount_usd": str(payload.amount_usd), "is_paid": payload.is_paid},
        ip_address=await resolve_client_ip(request),
    )
    return _customer_debt_out(debt, customer_name=customer.name)


@router.patch("/customer-debts/{debt_id}", response_model=CustomerDebtOut)
async def update_customer_debt(
    debt_id: int,
    payload: CustomerDebtUpdate,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_perm(user, "suppliers")
    debt = await db.get(CustomerDebt, debt_id)
    if not debt:
        raise HTTPException(status_code=404, detail="Bill not found")
    customer = await db.get(Customer, debt.customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if payload.description is not None:
        debt.description = payload.description.strip()
    if payload.amount_usd is not None:
        settings_row = await get_system_settings(db)
        rate = Decimal(str(settings_row.exchange_rate_usd_lbp))
        debt.amount_usd = payload.amount_usd
        debt.amount_lbp = usd_to_lbp(payload.amount_usd, rate)
        debt.exchange_rate_snapshot = rate
    if payload.due_date is not None:
        debt.due_date = payload.due_date
    if payload.reference_number is not None:
        debt.reference_number = payload.reference_number.strip() or None
    if payload.is_settled is not None:
        debt.is_settled = payload.is_settled
        debt.settled_at = datetime.utcnow() if payload.is_settled else None
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="CUSTOMER_DEBT_UPDATE", entity_type="customer_debt", entity_id=debt.id,
        details={"customer_id": debt.customer_id, "is_settled": debt.is_settled},
        ip_address=await resolve_client_ip(request),
    )
    return _customer_debt_out(debt, customer_name=customer.name)


@router.patch("/customer-debts/{debt_id}/settle")
async def settle_customer_debt(
    debt_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _require_perm(user, "suppliers")
    debt = await db.get(CustomerDebt, debt_id)
    if not debt:
        raise HTTPException(status_code=404, detail="Bill not found")
    debt.is_settled = True
    debt.settled_at = datetime.utcnow()
    await db.flush()
    await record_audit(
        db, actor_id=user.id, action="CUSTOMER_DEBT_SETTLE", entity_type="customer_debt", entity_id=debt.id,
        details={"customer_id": debt.customer_id}, ip_address=await resolve_client_ip(request),
    )
    return {"detail": "Bill marked as paid"}
