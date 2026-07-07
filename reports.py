"""Dynamic accounting engine and admin management endpoints."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth import get_current_user, record_audit, require_admin, resolve_client_ip
from branch_scope import resolve_inventory_branch_filter, resolve_report_branch_filter
from database import AuditLog, Branch, Customer, CustomerDebt, Expense, InventoryItem, Invoice, InvoiceItem, InvoiceStatus, MenuItem, PaymentMethod, SalesChannel, Shift, StockMovement, StockMovementType, Supplier, SupplierDebt, User, UserRole, get_db, get_system_settings
from permissions import require_permission
from sales import InvoiceOut
from sync_service import sync_status

router = APIRouter(prefix="/api/reports", tags=["Reports & Admin"])


async def _render_pdf(build_fn, *args, **kwargs) -> bytes:
    """Run synchronous fpdf2 work off the async event loop."""
    import asyncio
    return await asyncio.to_thread(build_fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DashboardOut(BaseModel):
    period: str
    start: str
    end: str
    invoice_count: int
    subtotal: str
    gross_total_usd: str
    tax_amount: str
    total: str
    total_lbp: str
    total_commission_usd: str
    cash_total: str
    wish_total: str
    card_total: str
    mobile_total: str
    payment_breakdown: list[dict[str, Any]]
    channel_breakdown: list[dict[str, Any]]
    operator_breakdown: list[dict[str, Any]]
    category_breakdown: list[dict[str, Any]]
    peak_sales_hours: list[dict[str, Any]]
    top_selling_products: list[dict[str, Any]]
    recent_invoices: list[dict[str, Any]]
    shift_summary: list[dict[str, Any]]

    model_config = {"extra": "ignore"}


class AuditLogOut(BaseModel):
    id: int
    actor_id: int
    action: str
    entity_type: str
    entity_id: str
    details: str | None
    ip_address: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


class AdminVoidRequest(BaseModel):
    reason: str = Field(min_length=5)


# ---------------------------------------------------------------------------
# Accounting engine
# ---------------------------------------------------------------------------

PAYMENT_METHOD_LABELS: dict[PaymentMethod, str] = {
    PaymentMethod.CASH: "Cash",
    PaymentMethod.WISH: "Wish",
    PaymentMethod.CARD: "Card",
    PaymentMethod.MOBILE: "Mobile",
}


def period_bounds(period: str, anchor: datetime | None = None) -> tuple[datetime, datetime]:
    now = anchor or datetime.utcnow()
    if period == "daily":
        start = datetime(now.year, now.month, now.day)
        end = start + timedelta(days=1)
    elif period == "weekly":
        start = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
        end = start + timedelta(days=7)
    elif period == "monthly":
        start = datetime(now.year, now.month, 1)
        end = datetime(now.year + 1, 1, 1) if now.month == 12 else datetime(now.year, now.month + 1, 1)
    elif period == "annual":
        start = datetime(now.year, 1, 1)
        end = datetime(now.year + 1, 1, 1)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid period")
    return start, end


async def build_report(
    db: AsyncSession,
    period: str,
    operator_id: int | None = None,
    branch_id: int | None = None,
    anchor: datetime | None = None,
) -> dict[str, Any]:
    start, end = period_bounds(period, anchor)
    inv_filters = [
        Invoice.finalized_at >= start,
        Invoice.finalized_at < end,
        Invoice.status == InvoiceStatus.FINALIZED,
    ]
    if branch_id is not None:
        inv_filters.append(Invoice.branch_id == branch_id)
    if operator_id is not None:
        inv_filters.append(Invoice.operator_id == operator_id)

    totals = await db.execute(
        select(
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.subtotal_usd), 0),
            func.coalesce(func.sum(Invoice.discount_amount_usd), 0),
            func.coalesce(func.sum(Invoice.net_total_usd), 0),
            func.coalesce(func.sum(Invoice.net_total_lbp), 0),
        ).where(*inv_filters)
    )
    count, subtotal_usd, discount_usd, net_total_usd, net_total_lbp = totals.one()
    subtotal_usd = Decimal(str(subtotal_usd))
    discount_usd = Decimal(str(discount_usd))
    net_total_usd = Decimal(str(net_total_usd))
    net_total_lbp = Decimal(str(net_total_lbp))
    gross_usd = subtotal_usd - discount_usd

    commission_total = await db.execute(
        select(func.coalesce(func.sum(Invoice.toters_commission_amount), 0)).where(*inv_filters)
    )
    total_commission = Decimal(str(commission_total.scalar_one()))

    channel_rows = await db.execute(
        select(
            Invoice.sales_channel,
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.subtotal_usd - Invoice.discount_amount_usd), 0),
            func.coalesce(func.sum(Invoice.toters_commission_amount), 0),
            func.coalesce(func.sum(Invoice.net_total_usd), 0),
        )
        .where(*inv_filters)
        .group_by(Invoice.sales_channel)
    )
    channel_labels = {
        SalesChannel.IN_STORE: "In-Store / Cashier",
        SalesChannel.TOTERS: "Toters",
        SalesChannel.TALABNA: "Talabna",
        SalesChannel.MARKIT: "Markit",
    }
    channel_breakdown = [
        {
            "channel": ch.value,
            "channel_label": channel_labels.get(ch, ch.value),
            "order_count": ic,
            "gross_usd": str(gross),
            "commission_usd": str(comm),
            "net_usd": str(net),
        }
        for ch, ic, gross, comm, net in channel_rows.all()
    ]

    payment_rows = await db.execute(
        select(Invoice.payment_method, func.coalesce(func.sum(Invoice.net_total_usd), 0))
        .where(*inv_filters)
        .group_by(Invoice.payment_method)
    )
    payments: dict[str, Decimal] = {pm.value: Decimal("0") for pm in PaymentMethod}
    for pm, amt in payment_rows.all():
        key = pm.value if isinstance(pm, PaymentMethod) else str(pm)
        payments[key] = Decimal(str(amt))

    payment_breakdown = [
        {
            "method": pm.value,
            "label": PAYMENT_METHOD_LABELS[pm],
            "total_usd": str(payments.get(pm.value, Decimal("0"))),
        }
        for pm in PaymentMethod
    ]

    op_rows = await db.execute(
        select(User.id, User.full_name, func.count(Invoice.id), func.coalesce(func.sum(Invoice.net_total_usd), 0))
        .join(User, User.id == Invoice.operator_id)
        .where(*inv_filters)
        .group_by(User.id, User.full_name)
        .order_by(func.sum(Invoice.net_total_usd).desc())
    )
    operator_breakdown = [
        {"operator_id": oid, "operator_name": name, "invoice_count": ic, "total": str(tot)}
        for oid, name, ic, tot in op_rows.all()
    ]

    cat_rows = await db.execute(
        select(
            MenuItem.category,
            func.sum(InvoiceItem.quantity),
            func.coalesce(func.sum(InvoiceItem.line_total), 0),
        )
        .join(InvoiceItem, InvoiceItem.menu_item_id == MenuItem.id)
        .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
        .where(*inv_filters)
        .group_by(MenuItem.category)
        .order_by(func.sum(InvoiceItem.line_total).desc())
    )
    category_breakdown = [
        {"category": cat, "quantity": int(qty), "revenue": str(rev)}
        for cat, qty, rev in cat_rows.all()
    ]

    peak_rows = await db.execute(
        select(
            func.strftime("%H", Invoice.finalized_at),
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.net_total_usd), 0),
        )
        .where(*inv_filters)
        .group_by(func.strftime("%H", Invoice.finalized_at))
        .order_by(func.count(Invoice.id).desc())
    )
    peak_sales_hours = [
        {
            "hour": int(h),
            "invoice_count": ic,
            "revenue_usd": str(rev),
        }
        for h, ic, rev in peak_rows.all()
        if h is not None
    ]

    top_prod_rows = await db.execute(
        select(
            InvoiceItem.name_snapshot,
            func.coalesce(func.sum(InvoiceItem.quantity), 0),
            func.coalesce(func.sum(InvoiceItem.line_total), 0),
        )
        .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
        .where(*inv_filters)
        .group_by(InvoiceItem.name_snapshot)
        .order_by(func.sum(InvoiceItem.quantity).desc())
        .limit(5)
    )
    top_selling_products = [
        {
            "product_name": name,
            "quantity_sold": int(qty),
            "revenue_usd": str(rev),
        }
        for name, qty, rev in top_prod_rows.all()
    ]

    recent = await db.execute(
        select(Invoice).where(*inv_filters).order_by(Invoice.finalized_at.desc()).limit(15)
    )
    recent_invoices = [
        {
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "operator_id": inv.operator_id,
            "total": str(inv.net_total_usd),
            "payment_method": inv.payment_method.value,
            "finalized_at": inv.finalized_at.isoformat(),
        }
        for inv in recent.scalars().all()
    ]

    shift_filters = [Shift.opened_at >= start, Shift.opened_at < end]
    if branch_id is not None:
        shift_filters.append(Shift.branch_id == branch_id)
    if operator_id is not None:
        shift_filters.append(Shift.operator_id == operator_id)

    shift_rows = await db.execute(
        select(Shift, User.full_name)
        .join(User, User.id == Shift.operator_id)
        .where(*shift_filters)
        .order_by(Shift.opened_at.desc())
        .limit(50)
    )
    shift_summary = []
    for shift, op_name in shift_rows.all():
        inv_total = await db.execute(
            select(func.coalesce(func.sum(Invoice.net_total_usd), 0)).where(
                Invoice.shift_id == shift.id,
                Invoice.status == InvoiceStatus.FINALIZED,
            )
        )
        shift_summary.append(
            {
                "shift_id": shift.id,
                "operator_id": shift.operator_id,
                "operator_name": op_name,
                "opened_at": shift.opened_at.isoformat(),
                "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
                "opening_float_usd": str(shift.opening_float_usd),
                "opening_float_lbp": str(shift.opening_float_lbp),
                "expected_cash_usd": str(shift.expected_cash_usd) if shift.expected_cash_usd is not None else None,
                "expected_cash_lbp": str(shift.expected_cash_lbp) if shift.expected_cash_lbp is not None else None,
                "counted_cash_usd": str(shift.counted_cash_usd) if shift.counted_cash_usd is not None else None,
                "counted_cash_lbp": str(shift.counted_cash_lbp) if shift.counted_cash_lbp is not None else None,
                "cash_variance_usd": str(shift.cash_variance_usd) if shift.cash_variance_usd is not None else None,
                "cash_variance_lbp": str(shift.cash_variance_lbp) if shift.cash_variance_lbp is not None else None,
                "opening_float": str(shift.opening_float_usd),
                "expected_cash": str(shift.expected_cash_usd) if shift.expected_cash_usd is not None else None,
                "counted_cash": str(shift.counted_cash_usd) if shift.counted_cash_usd is not None else None,
                "cash_variance": str(shift.cash_variance_usd) if shift.cash_variance_usd is not None else None,
                "shift_sales_total": str(inv_total.scalar_one()),
                "is_open": shift.closed_at is None,
            }
        )

    return {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "invoice_count": count,
        "subtotal": str(subtotal_usd),
        "gross_total_usd": str(gross_usd),
        "tax_amount": "0.00",
        "total": str(net_total_usd),
        "total_lbp": str(net_total_lbp),
        "total_commission_usd": str(total_commission),
        "channel_breakdown": channel_breakdown,
        "payment_breakdown": payment_breakdown,
        "cash_total": str(payments.get(PaymentMethod.CASH.value, Decimal("0"))),
        "wish_total": str(payments.get(PaymentMethod.WISH.value, Decimal("0"))),
        "card_total": str(payments.get(PaymentMethod.CARD.value, Decimal("0"))),
        "mobile_total": str(payments.get(PaymentMethod.MOBILE.value, Decimal("0"))),
        "operator_breakdown": operator_breakdown,
        "category_breakdown": category_breakdown,
        "peak_sales_hours": peak_sales_hours,
        "top_selling_products": top_selling_products,
        "recent_invoices": recent_invoices,
        "shift_summary": shift_summary,
    }


# ---------------------------------------------------------------------------
# Report routes
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period: str = Query("daily", pattern="^(daily|weekly|monthly|annual)$"),
    operator_id: int | None = None,
    branch_id: int | None = None,
):
    """Admin-only sales dashboard — branch_id omitted = consolidated all branches."""
    if user.role == UserRole.CASHIER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied — Admin Authorization Required",
        )
    scope = resolve_report_branch_filter(user, branch_id)
    return await build_report(db, period, operator_id, branch_id=scope)


async def _expenses_for_period(
    db: AsyncSession, period: str, operator_id: int | None = None, branch_id: int | None = None
) -> tuple[Decimal, Decimal]:
    start, end = period_bounds(period)
    start_d, end_d = start.date(), end.date()
    filters = [Expense.expense_date >= start_d, Expense.expense_date < end_d]
    if branch_id is not None:
        filters.append(Expense.branch_id == branch_id)
    result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount_usd), 0), func.coalesce(func.sum(Expense.amount_lbp), 0)).where(*filters)
    )
    usd, lbp = result.one()
    return Decimal(str(usd)), Decimal(str(lbp))


async def _inventory_consumption(db: AsyncSession, period: str, branch_id: int | None = None) -> list[dict]:
    start, end = period_bounds(period)
    filters = [
        StockMovement.movement_type == StockMovementType.SALE,
        StockMovement.created_at >= start,
        StockMovement.created_at < end,
    ]
    if branch_id is not None:
        filters.append(StockMovement.branch_id == branch_id)
    rows = await db.execute(
        select(InventoryItem.name, InventoryItem.unit, func.sum(StockMovement.quantity), func.max(StockMovement.balance_after))
        .join(InventoryItem, InventoryItem.id == StockMovement.inventory_item_id)
        .where(*filters)
        .group_by(InventoryItem.name, InventoryItem.unit)
        .order_by(func.sum(StockMovement.quantity).desc())
        .limit(25)
    )
    return [
        {"item_name": name, "unit": unit, "consumed": str(qty), "balance_after": str(bal)}
        for name, unit, qty, bal in rows.all()
    ]


async def _supplier_debt_summary(db: AsyncSession) -> list[dict]:
    from finance import _supplier_balance

    result = await db.execute(select(Supplier).where(Supplier.is_active.is_(True)).order_by(Supplier.name))
    out = []
    for s in result.scalars().all():
        bal_usd, bal_lbp = await _supplier_balance(db, s.id)
        if bal_usd > 0:
            out.append({"supplier_name": s.name, "category": s.category, "balance_usd": str(bal_usd), "balance_lbp": str(bal_lbp)})
    return out


async def _branch_for_export(
    db: AsyncSession,
    user: User,
    branch_id: int | None,
    *,
    inventory: bool = False,
) -> tuple[int | None, Branch | None]:
    if inventory:
        bid = resolve_inventory_branch_filter(user, branch_id)
        branch = await db.get(Branch, bid)
        return bid, branch
    scope = resolve_report_branch_filter(user, branch_id)
    if scope is None:
        return None, None
    branch = await db.get(Branch, scope)
    return scope, branch


@router.get("/export/pdf")
async def export_pdf_report(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period: str = Query("daily", pattern="^(daily|weekly|monthly|annual)$"),
    operator_id: int | None = None,
    branch_id: int | None = None,
):
    if not require_permission(user, "sales_reports"):
        raise HTTPException(status_code=403, detail="Sales reports access not permitted")
    scope = resolve_report_branch_filter(user, branch_id)
    try:
        from pdf_reports import build_sales_pdf

        report = await build_report(db, period, operator_id, branch_id=scope)
        exp_usd, exp_lbp = await _expenses_for_period(db, period, operator_id, branch_id=scope)
        settings = await get_system_settings(db)
        rate = Decimal(str(settings.exchange_rate_usd_lbp))
        sync = await sync_status(db)
        consumption = await _inventory_consumption(db, period, branch_id=scope)
        debts = await _supplier_debt_summary(db)
        if scope is not None:
            branch = await db.get(Branch, scope)
        else:
            branch = None
        branch_name = branch.name if branch else "BOB JUICE — All Branches"
        pdf_bytes = await _render_pdf(
            build_sales_pdf,
            report, exp_usd, exp_lbp, rate, user.full_name,
            channel_breakdown=report.get("channel_breakdown"),
            sync_status=sync,
            inventory_consumption=consumption,
            supplier_debts=debts,
            branch_name=branch_name,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}") from exc
    filename = f"bob-juice-report-{period}-{datetime.utcnow().strftime('%Y%m%d')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/inventory/pdf")
async def export_inventory_pdf(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = None,
):
    if not require_permission(user, "inventory"):
        raise HTTPException(status_code=403, detail="Inventory access not permitted")
    bid, branch = await _branch_for_export(db, user, branch_id, inventory=True)
    items_result = await db.execute(
        select(InventoryItem)
        .where(InventoryItem.is_active.is_(True), InventoryItem.branch_id == bid)
        .order_by(InventoryItem.name)
    )
    items = [
        {
            "name": i.name,
            "unit": i.unit,
            "current_stock": str(i.current_stock),
            "reorder_level": str(i.reorder_level),
        }
        for i in items_result.scalars().all()
    ]
    mov_result = await db.execute(
        select(StockMovement, InventoryItem.name)
        .join(InventoryItem, StockMovement.inventory_item_id == InventoryItem.id)
        .where(StockMovement.branch_id == bid)
        .order_by(StockMovement.created_at.desc())
        .limit(50)
    )
    movements = [
        {
            "created_at": mov.created_at.isoformat() if mov.created_at else "",
            "inventory_name": inv_name,
            "movement_type": mov.movement_type.value,
            "quantity": str(mov.quantity),
            "balance_after": str(mov.balance_after),
            "notes": mov.notes,
            "reference": mov.reference,
        }
        for mov, inv_name in mov_result.all()
    ]
    try:
        from pdf_reports import build_inventory_pdf

        pdf_bytes = await _render_pdf(
            build_inventory_pdf,
            items,
            movements,
            user.full_name,
            branch_name=branch.name if branch else "BOB JUICE",
            branch_code=branch.code if branch else "-",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}") from exc
    filename = f"bob-juice-inventory-{datetime.utcnow().strftime('%Y%m%d')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/expenses/pdf")
async def export_expenses_pdf(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period: str = Query("monthly", pattern="^(daily|weekly|monthly|annual|all)$"),
    branch_id: int | None = None,
):
    if not require_permission(user, "expenses"):
        raise HTTPException(status_code=403, detail="Expenses access not permitted")
    settings = await get_system_settings(db)
    rate = Decimal(str(settings.exchange_rate_usd_lbp))
    scope, branch = await _branch_for_export(db, user, branch_id)
    branch_label = branch.name if branch else "BOB JUICE — All Branches"
    start_s, end_s = "", ""
    if period == "all":
        q = select(Expense).order_by(Expense.expense_date.desc(), Expense.id.desc()).limit(500)
        if scope is not None:
            q = q.where(Expense.branch_id == scope)
        result = await db.execute(q)
        period_label = "All Time"
    else:
        start, end = period_bounds(period)
        start_s, end_s = start.date().isoformat(), end.date().isoformat()
        period_label = period.title()
        filters = [Expense.expense_date >= start.date(), Expense.expense_date < end.date()]
        if scope is not None:
            filters.append(Expense.branch_id == scope)
        result = await db.execute(
            select(Expense)
            .where(*filters)
            .order_by(Expense.expense_date.desc(), Expense.id.desc())
        )
    expenses = [
        {
            "expense_date": str(e.expense_date),
            "description": e.description,
            "category": e.category.value if hasattr(e.category, "value") else str(e.category),
            "amount_usd": str(e.amount_usd),
            "amount_lbp": str(e.amount_lbp),
            "notes": e.notes,
        }
        for e in result.scalars().all()
    ]
    try:
        from pdf_reports import build_expenses_pdf

        pdf_bytes = await _render_pdf(
            build_expenses_pdf,
            expenses,
            user.full_name,
            period_label=period_label,
            start=start_s,
            end=end_s,
            exchange_rate=rate,
            branch_name=branch_label,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}") from exc
    filename = f"bob-juice-expenses-{period}-{datetime.utcnow().strftime('%Y%m%d')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/customer-debts/pdf")
async def export_customer_debts_pdf(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = None,
):
    if not require_permission(user, "suppliers"):
        raise HTTPException(status_code=403, detail="Customer debts access not permitted")
    from finance import _customer_balance

    settings = await get_system_settings(db)
    rate = Decimal(str(settings.exchange_rate_usd_lbp))
    _, branch = await _branch_for_export(db, user, branch_id)
    branch_label = branch.name if branch else "BOB JUICE — All Branches"
    cust_result = await db.execute(select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.name))
    customers = []
    for c in cust_result.scalars().all():
        bal_usd, bal_lbp = await _customer_balance(db, c.id)
        customers.append({
            "name": c.name,
            "contact_phone": c.contact_phone,
            "balance_usd": str(bal_usd),
            "balance_lbp": str(bal_lbp),
        })
    debt_result = await db.execute(
        select(CustomerDebt, Customer.name)
        .join(Customer, Customer.id == CustomerDebt.customer_id)
        .order_by(CustomerDebt.created_at.desc())
    )
    entries = [
        {
            "created_at": debt.created_at.isoformat() if debt.created_at else "",
            "customer_name": name,
            "description": debt.description,
            "amount_usd": str(debt.amount_usd),
            "amount_lbp": str(debt.amount_lbp),
            "is_settled": debt.is_settled,
            "is_paid": debt.is_settled,
            "invoice_number": debt.invoice_number,
            "reference_number": debt.reference_number,
        }
        for debt, name in debt_result.all()
    ]
    try:
        from pdf_reports import build_customer_debts_pdf

        pdf_bytes = await _render_pdf(
            build_customer_debts_pdf,
            customers,
            entries,
            user.full_name,
            exchange_rate=rate,
            branch_name=branch_label,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}") from exc
    filename = f"bob-juice-customer-debts-{datetime.utcnow().strftime('%Y%m%d')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/customer-debt/{debt_id}/pdf")
async def export_single_customer_debt_pdf(
    debt_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = None,
):
    if not require_permission(user, "suppliers"):
        raise HTTPException(status_code=403, detail="Customer debts access not permitted")
    result = await db.execute(
        select(CustomerDebt, Customer.name, Customer.contact_phone)
        .join(Customer, Customer.id == CustomerDebt.customer_id)
        .where(CustomerDebt.id == debt_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Bill not found")
    debt, customer_name, phone = row
    _, branch = await _branch_for_export(db, user, branch_id)
    branch_label = branch.name if branch else "BOB JUICE — All Branches"
    bill = {
        "id": debt.id,
        "invoice_id": debt.invoice_number,
        "invoice_number": debt.invoice_number,
        "reference_number": debt.reference_number,
        "display_number": debt.reference_number or str(debt.invoice_number),
        "customer_name": customer_name,
        "contact_phone": phone,
        "description": debt.description,
        "amount_usd": str(debt.amount_usd),
        "amount_lbp": str(debt.amount_lbp),
        "created_at": debt.created_at.isoformat() if debt.created_at else "",
        "is_settled": debt.is_settled,
        "is_paid": debt.is_settled,
    }
    try:
        from pdf_reports import build_single_customer_bill_pdf

        pdf_bytes = await _render_pdf(
            build_single_customer_bill_pdf,
            bill,
            generated_by=user.full_name,
            branch_name=branch_label,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}") from exc
    filename = f"bob-juice-customer-bill-{debt_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/supplier-debts/pdf")
async def export_supplier_debts_pdf(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = None,
):
    if not require_permission(user, "suppliers"):
        raise HTTPException(status_code=403, detail="Supplier debts access not permitted")
    from finance import _supplier_balance

    settings = await get_system_settings(db)
    rate = Decimal(str(settings.exchange_rate_usd_lbp))
    _, branch = await _branch_for_export(db, user, branch_id)
    branch_label = branch.name if branch else "BOB JUICE — All Branches"
    sup_result = await db.execute(select(Supplier).where(Supplier.is_active.is_(True)).order_by(Supplier.name))
    suppliers = []
    for s in sup_result.scalars().all():
        bal_usd, bal_lbp = await _supplier_balance(db, s.id)
        suppliers.append({
            "name": s.name,
            "category": s.category,
            "contact_phone": s.contact_phone,
            "balance_usd": str(bal_usd),
            "balance_lbp": str(bal_lbp),
        })
    debt_result = await db.execute(
        select(SupplierDebt, Supplier.name)
        .join(Supplier, Supplier.id == SupplierDebt.supplier_id)
        .order_by(SupplierDebt.created_at.desc())
    )
    entries = [
        {
            "created_at": debt.created_at.isoformat() if debt.created_at else "",
            "supplier_name": name,
            "description": debt.description,
            "amount_usd": str(debt.amount_usd),
            "amount_lbp": str(debt.amount_lbp),
            "is_settled": debt.is_settled,
            "is_paid": debt.is_settled,
            "invoice_number": debt.invoice_number,
            "reference_number": debt.reference_number,
        }
        for debt, name in debt_result.all()
    ]
    try:
        from pdf_reports import build_supplier_debts_pdf

        pdf_bytes = await _render_pdf(
            build_supplier_debts_pdf,
            suppliers,
            entries,
            user.full_name,
            exchange_rate=rate,
            branch_name=branch_label,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}") from exc
    filename = f"bob-juice-supplier-debts-{datetime.utcnow().strftime('%Y%m%d')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/supplier-debt/{debt_id}/pdf")
async def export_single_supplier_debt_pdf(
    debt_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    branch_id: int | None = None,
):
    if not require_permission(user, "suppliers"):
        raise HTTPException(status_code=403, detail="Supplier debts access not permitted")
    result = await db.execute(
        select(SupplierDebt, Supplier.name, Supplier.category, Supplier.contact_phone)
        .join(Supplier, Supplier.id == SupplierDebt.supplier_id)
        .where(SupplierDebt.id == debt_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Bill not found")
    debt, supplier_name, category, phone = row
    _, branch = await _branch_for_export(db, user, branch_id)
    branch_label = branch.name if branch else "BOB JUICE — All Branches"
    bill = {
        "id": debt.id,
        "invoice_id": debt.invoice_number,
        "invoice_number": debt.invoice_number,
        "reference_number": debt.reference_number,
        "display_number": debt.reference_number or str(debt.invoice_number),
        "supplier_name": supplier_name,
        "category": category,
        "contact_phone": phone,
        "description": debt.description,
        "amount_usd": str(debt.amount_usd),
        "amount_lbp": str(debt.amount_lbp),
        "created_at": debt.created_at.isoformat() if debt.created_at else "",
        "is_settled": debt.is_settled,
        "is_paid": debt.is_settled,
    }
    try:
        from pdf_reports import build_single_supplier_bill_pdf

        pdf_bytes = await _render_pdf(
            build_single_supplier_bill_pdf,
            bill,
            generated_by=user.full_name,
            branch_name=branch_label,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}") from exc
    filename = f"bob-juice-supplier-bill-{debt_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/sales")
async def sales_report(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period: str = Query("daily", pattern="^(daily|weekly|monthly|annual)$"),
    operator_id: int | None = None,
):
    return await build_report(db, period, operator_id)


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------


@router.get("/admin/audit-logs", response_model=list[AuditLogOut])
async def list_audit_logs(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(300, ge=1, le=1000),
):
    result = await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
    return result.scalars().all()


@router.post("/admin/invoices/{invoice_id}/void", response_model=InvoiceOut)
async def void_invoice(
    invoice_id: int,
    payload: AdminVoidRequest,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Invoice).options(selectinload(Invoice.items)).where(Invoice.id == invoice_id)
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    if invoice.status == InvoiceStatus.ADMIN_VOID:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invoice already voided")
    invoice.status = InvoiceStatus.ADMIN_VOID
    invoice.voided_at = datetime.utcnow()
    invoice.voided_by_id = admin.id
    invoice.void_reason = payload.reason
    await db.flush()
    await record_audit(
        db,
        actor_id=admin.id,
        action="INVOICE_ADMIN_VOID",
        entity_type="invoice",
        entity_id=invoice.id,
        details={"invoice_number": invoice.invoice_number, "reason": payload.reason},
        ip_address=await resolve_client_ip(request),
    )
    return invoice
