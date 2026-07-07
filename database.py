"""
SQLite database setup for BOB JUICE POS.
Tables: users, shifts, menu_items, invoices, invoice_items, audit_logs,
        expenses, suppliers, supplier_debts, system_settings
"""

from __future__ import annotations

import asyncio
import enum
import gc
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import (
    DB_PATH,
    DEFAULT_EXCHANGE_RATE_USD_LBP,
    DEFAULT_MARKIT_COMMISSION_PCT,
    DEFAULT_TALABNA_COMMISSION_PCT,
    DEFAULT_TOTERS_COMMISSION_PCT,
    FRESH_DB_MARKER,
    settings,
)
from security import hash_password

engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False},
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
logger = logging.getLogger("bob_juice.db")


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    CASHIER = "cashier"


class DiscountType(str, enum.Enum):
    NONE = "none"
    PERCENT = "percent"
    FLAT_USD = "flat_usd"
    FLAT_LBP = "flat_lbp"


class StockMovementType(str, enum.Enum):
    IN = "in"
    OUT = "out"
    SALE = "sale"
    ADJUST = "adjust"


class PaymentMethod(str, enum.Enum):
    CASH = "cash"
    CARD = "card"
    MOBILE = "mobile"
    WISH = "wish"


class InvoiceStatus(str, enum.Enum):
    FINALIZED = "finalized"
    ADMIN_VOID = "admin_void"


class Currency(str, enum.Enum):
    USD = "USD"
    LBP = "LBP"


class SalesChannel(str, enum.Enum):
    IN_STORE = "in_store"
    TOTERS = "toters"
    TALABNA = "talabna"
    MARKIT = "markit"


class IntakePaymentStatus(str, enum.Enum):
    PAID = "paid"
    PARTIAL = "partial"
    UNPAID = "unpaid"


class SyncStatus(str, enum.Enum):
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"


class ExpenseCategory(str, enum.Enum):
    SUPPLIES = "supplies"
    UTILITIES = "utilities"
    RENT = "rent"
    SALARIES = "salaries"
    MAINTENANCE = "maintenance"
    MARKETING = "marketing"
    OTHER = "other"


class DebtType(str, enum.Enum):
    PAYABLE = "payable"
    PAYMENT = "payment"


class RecipeRole(str, enum.Enum):
    BASE = "base"
    EXTRA = "extra"
    EXCLUDE = "exclude"
    ADJUSTMENT = "adjustment"


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.CASHIER, index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    permissions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ---------------------------------------------------------------------------
# Global system settings (exchange rate, Toters commission)
# ---------------------------------------------------------------------------


class Branch(Base):
    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    address: Mapped[str | None] = mapped_column(String(256), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_central: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SystemSettings(Base):
    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    exchange_rate_usd_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=DEFAULT_EXCHANGE_RATE_USD_LBP)
    toters_commission_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=DEFAULT_TOTERS_COMMISSION_PCT)
    talabna_commission_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=DEFAULT_TALABNA_COMMISSION_PCT)
    markit_commission_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=DEFAULT_MARKIT_COMMISSION_PCT)
    time_offset_seconds: Mapped[int] = mapped_column(Integer, default=0)
    last_time_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


# ---------------------------------------------------------------------------
# Shift tracking (dual-currency cash control)
# ---------------------------------------------------------------------------


class Shift(Base):
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), default=1, index=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    exchange_rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=DEFAULT_EXCHANGE_RATE_USD_LBP)

    opening_float_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    opening_float_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0), default=Decimal("0"))

    expected_cash_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    expected_cash_lbp: Mapped[Decimal | None] = mapped_column(Numeric(14, 0), nullable=True)
    counted_cash_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    counted_cash_lbp: Mapped[Decimal | None] = mapped_column(Numeric(14, 0), nullable=True)
    cash_variance_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    cash_variance_lbp: Mapped[Decimal | None] = mapped_column(Numeric(14, 0), nullable=True)

    total_sales_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    total_sales_lbp: Mapped[Decimal | None] = mapped_column(Numeric(14, 0), nullable=True)
    invoice_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closing_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Legacy single-currency columns (kept for migration compatibility)
    opening_float: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    expected_cash: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    counted_cash: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    cash_variance: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)


# ---------------------------------------------------------------------------
# Product categories & menu
# ---------------------------------------------------------------------------


class ProductCategory(Base):
    __tablename__ = "product_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    products: Mapped[list["MenuItem"]] = relationship(back_populates="product_category")


class MenuItem(Base):
    __tablename__ = "menu_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("product_categories.id"), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    price_s: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    price_m: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    price_l: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    sizes_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    product_category: Mapped["ProductCategory | None"] = relationship(back_populates="products")
    recipes: Mapped[list["ProductRecipe"]] = relationship(back_populates="menu_item", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), default=1, index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    unit: Mapped[str] = mapped_column(String(64), default="pcs")
    current_stock: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    reorder_level: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    movements: Mapped[list["StockMovement"]] = relationship(back_populates="inventory_item")
    recipes: Mapped[list["ProductRecipe"]] = relationship(back_populates="inventory_item")


class ProductRecipe(Base):
    """Ingredients/units consumed per 1 menu item sold (BOM)."""
    __tablename__ = "product_recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    menu_item_id: Mapped[int] = mapped_column(ForeignKey("menu_items.id"), index=True)
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), index=True)
    quantity_per_sale: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    metric_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)  # g, ml, pcs, kg
    recipe_role: Mapped[str] = mapped_column(String(16), default=RecipeRole.BASE.value, index=True)
    display_title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extra_price_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))

    menu_item: Mapped["MenuItem"] = relationship(back_populates="recipes")
    inventory_item: Mapped["InventoryItem"] = relationship(back_populates="recipes")


class GlobalModifier(Base):
    """Branch-wide POS extras/excludes — not tied to individual menu items."""
    __tablename__ = "global_modifiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), default=1, index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), index=True)
    extra_price_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    quantity_per_sale: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    metric_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    inventory_item: Mapped["InventoryItem"] = relationship()


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), default=1, index=True)
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), index=True)
    movement_type: Mapped[StockMovementType] = mapped_column(Enum(StockMovementType), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    inventory_item: Mapped["InventoryItem"] = relationship(back_populates="movements")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), default=1, index=True)
    invoice_number: Mapped[str] = mapped_column(String(32), index=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id"), index=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[InvoiceStatus] = mapped_column(Enum(InvoiceStatus), default=InvoiceStatus.FINALIZED)
    payment_method: Mapped[PaymentMethod] = mapped_column(Enum(PaymentMethod))
    sales_channel: Mapped[SalesChannel] = mapped_column(Enum(SalesChannel), default=SalesChannel.IN_STORE, index=True)
    settlement_currency: Mapped[Currency] = mapped_column(Enum(Currency), default=Currency.USD)

    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    subtotal_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    subtotal_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0), default=Decimal("0"))
    total_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0), default=Decimal("0"))
    exchange_rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=DEFAULT_EXCHANGE_RATE_USD_LBP)
    toters_commission_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0.00"))
    toters_commission_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    discount_type: Mapped[DiscountType] = mapped_column(Enum(DiscountType), default=DiscountType.NONE)
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    discount_amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    discount_amount_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0), default=Decimal("0"))
    net_total_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    net_total_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0), default=Decimal("0"))

    amount_tendered: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    change_given: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    finalized_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list["InvoiceItem"]] = relationship(back_populates="invoice", cascade="all, delete-orphan")
    sale_transaction: Mapped["SaleTransaction | None"] = relationship(
        back_populates="invoice", uselist=False, cascade="all, delete-orphan"
    )


class SaleTransaction(Base):
    """POS sale ledger — one row per finalized invoice with short system ID."""
    __tablename__ = "sale_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), unique=True, index=True)
    sale_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), default=1, index=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    operator_name: Mapped[str] = mapped_column(String(128))
    items_summary: Mapped[str] = mapped_column(String(512))
    discount_amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    discount_amount_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0), default=Decimal("0"))
    net_total_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    net_total_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0))
    payment_method: Mapped[str] = mapped_column(String(16))
    sales_channel: Mapped[str] = mapped_column(String(32), default="in_store")
    invoice_number: Mapped[str] = mapped_column(String(32))
    finalized_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    invoice: Mapped["Invoice"] = relationship(back_populates="sale_transaction")


class SaleNumberCounter(Base):
    """Sequential short sale IDs for POS transactions (from 1001)."""
    __tablename__ = "sale_number_counter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    next_number: Mapped[int] = mapped_column(Integer, default=1001)


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), default=1, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    menu_item_id: Mapped[int] = mapped_column(ForeignKey("menu_items.id"))
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    line_timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    name_snapshot: Mapped[str] = mapped_column(String(128))
    unit_price_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    quantity: Mapped[int] = mapped_column(Integer)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    line_total_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0), default=Decimal("0"))
    line_modifiers_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    invoice: Mapped["Invoice"] = relationship(back_populates="items")


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), default=1, index=True)
    description: Mapped[str] = mapped_column(String(256))
    category: Mapped[ExpenseCategory] = mapped_column(Enum(ExpenseCategory), index=True)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    amount_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0))
    exchange_rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    expense_date: Mapped[date] = mapped_column(Date, index=True)
    recorded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


# ---------------------------------------------------------------------------
# Suppliers & Debts
# ---------------------------------------------------------------------------


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    debts: Mapped[list["SupplierDebt"]] = relationship(back_populates="supplier", cascade="all, delete-orphan")


class SupplierDebt(Base):
    __tablename__ = "supplier_debts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    reference_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), default=1, index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    description: Mapped[str] = mapped_column(String(256))
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    amount_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0))
    exchange_rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_settled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recorded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    supplier: Mapped["Supplier"] = relationship(back_populates="debts")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    debts: Mapped[list["CustomerDebt"]] = relationship(back_populates="customer", cascade="all, delete-orphan")


class CustomerDebt(Base):
    __tablename__ = "customer_debts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    reference_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), default=1, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    description: Mapped[str] = mapped_column(String(256))
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    amount_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0))
    exchange_rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_settled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recorded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    customer: Mapped["Customer"] = relationship(back_populates="debts")


class BillInvoiceCounter(Base):
    """Unified sequential bill invoice numbers (customer + supplier debts)."""
    __tablename__ = "bill_invoice_counter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    next_number: Mapped[int] = mapped_column(Integer, default=1001)


class InventoryIntake(Base):
    """Purchase log: raw goods received from suppliers with payment tracking."""
    __tablename__ = "inventory_intakes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), default=1, index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit: Mapped[str] = mapped_column(String(64), default="pcs")
    unit_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    total_cost_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0))
    payment_status: Mapped[IntakePaymentStatus] = mapped_column(Enum(IntakePaymentStatus), default=IntakePaymentStatus.UNPAID, index=True)
    amount_paid_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount_paid_lbp: Mapped[Decimal] = mapped_column(Numeric(14, 0), default=Decimal("0"))
    exchange_rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    intake_date: Mapped[date] = mapped_column(Date, index=True)
    recorded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    supplier: Mapped["Supplier"] = relationship()
    inventory_item: Mapped["InventoryItem"] = relationship()


class SyncOutbox(Base):
    """Hybrid offline queue — records pending upload to central cloud DB."""
    __tablename__ = "sync_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    operation: Mapped[str] = mapped_column(String(16), default="upsert")
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[SyncStatus] = mapped_column(Enum(SyncStatus), default=SyncStatus.PENDING, index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


# ---------------------------------------------------------------------------
# Session & helpers
# ---------------------------------------------------------------------------


async def get_db():
    """Yield a request-scoped session; commit on success so every write hits disk immediately."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def checkpoint_database_file() -> None:
    """Flush WAL into the main SQLite file before backup or restore."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA wal_checkpoint(FULL)")


async def release_database_for_file_swap() -> None:
    """Checkpoint WAL, dispose engine, and wait for OS file locks to clear (Windows-safe)."""
    try:
        await checkpoint_database_file()
    except Exception as exc:
        logger.warning("Checkpoint before database file swap failed: %s", exc)
    await engine.dispose()
    gc.collect()
    await asyncio.sleep(0.35)


def replace_database_file(content: bytes, *, stamp: str | None = None) -> None:
    """Overwrite bob_juice.db after the engine is disposed — retries on PermissionError."""
    import shutil
    import time

    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    staging = DB_PATH.with_suffix(".db.restore")
    staging.write_bytes(content)
    wal_paths = [Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")]
    for path in wal_paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    last_exc: PermissionError | None = None
    for attempt in range(12):
        try:
            if DB_PATH.is_file():
                pre_restore = DB_PATH.with_name(f"bob_juice.pre_restore_{stamp}.db")
                if not pre_restore.is_file():
                    shutil.copy2(DB_PATH, pre_restore)
                DB_PATH.unlink()
            staging.replace(DB_PATH)
            for path in wal_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            return
        except PermissionError as exc:
            last_exc = exc
            gc.collect()
            time.sleep(0.25 * (attempt + 1))
    raise PermissionError(
        f"Could not replace {DB_PATH} — database file is still locked. Stop other BOB JUICE processes and retry."
    ) from last_exc


async def reinitialize_engine() -> None:
    """Dispose pooled connections and re-run schema init (after restore)."""
    global engine, SessionLocal
    await engine.dispose()
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        connect_args={"check_same_thread": False},
    )
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    await init_db()


MASTER_ADMIN_USERNAME = "admin"
MASTER_ADMIN_PASSWORD = "Admin@Bob2026!"
DEFAULT_CASHIER_USERNAME = "cashier1"
DEFAULT_CASHIER_PASSWORD = "Cashier@Bob2026!"

SEED_BRANCHES = (
    (1, "SAIDA", "Saida Branch", "Saida, Lebanon"),
    (2, "BEIRUT", "Beirut Branch", "Beirut, Lebanon"),
)

SEED_BRANCH_CASHIERS = (
    ("cashier_saida", "Cashier@Bob2026!", "Saida Cashier", 1),
    ("cashier_beirut", "Cashier@Bob2026!", "Beirut Cashier", 2),
)

CLEAR_TABLES_ORDER = (
    "invoice_items",
    "invoices",
    "sale_transactions",
    "stock_movements",
    "product_recipes",
    "global_modifiers",
    "inventory_intakes",
    "supplier_debts",
    "customer_debts",
    "expenses",
    "sync_outbox",
    "audit_logs",
    "shifts",
    "menu_items",
    "product_categories",
    "inventory_items",
    "suppliers",
    "customers",
    "sale_number_counter",
    "bill_invoice_counter",
)


async def ensure_master_admin(db: AsyncSession) -> User:
    """Guarantee the master admin account exists — safe on startup and after data clears."""
    try:
        result = await db.execute(select(User).where(User.username == MASTER_ADMIN_USERNAME))
        admin = result.scalar_one_or_none()
        if admin is None:
            admin = User(
                username=MASTER_ADMIN_USERNAME,
                password_hash=hash_password(MASTER_ADMIN_PASSWORD),
                full_name="System Administrator",
                role=UserRole.ADMIN,
                branch_id=None,
                is_active=True,
            )
            db.add(admin)
            await db.flush()
            logger.warning("Seeded master admin user %r", MASTER_ADMIN_USERNAME)
        else:
            repaired = False
            if admin.role != UserRole.ADMIN:
                admin.role = UserRole.ADMIN
                repaired = True
            if not admin.is_active:
                admin.is_active = True
                repaired = True
            if repaired:
                await db.flush()
                logger.warning("Repaired master admin account %r", MASTER_ADMIN_USERNAME)
        return admin
    except Exception as exc:
        logger.exception("Failed to ensure master admin user")
        raise RuntimeError("Master admin bootstrap failed") from exc


async def bootstrap_users_table() -> User:
    """Create users table if needed and guarantee master admin — never raises silently."""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.tables["users"].create, checkfirst=True)
    except Exception as exc:
        logger.warning("Users table create check: %s", exc)
    async with SessionLocal() as db:
        admin = await ensure_master_admin(db)
        await get_system_settings(db)
        await db.commit()
        return admin


async def ensure_seed_branches(db: AsyncSession) -> dict[str, int]:
    """Ensure Saida + Beirut branches exist; return code→id map."""
    code_map: dict[str, int] = {}
    for bid, code, name, location in SEED_BRANCHES:
        row = await db.get(Branch, bid)
        if row is None:
            row = await db.execute(select(Branch).where(Branch.code == code))
            row = row.scalar_one_or_none()
        if row is None:
            row = Branch(id=bid, code=code, name=name, location=location, address=location, is_central=(bid == 1))
            db.add(row)
            await db.flush()
        else:
            row.name = name
            row.location = location
            if not row.address:
                row.address = location
            row.is_active = True
        code_map[code] = row.id
    return code_map


async def ensure_branch_cashiers(db: AsyncSession) -> None:
    for username, password, full_name, branch_id in SEED_BRANCH_CASHIERS:
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if user is None:
            db.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    full_name=full_name,
                    role=UserRole.CASHIER,
                    branch_id=branch_id,
                    is_active=True,
                )
            )
        else:
            user.branch_id = branch_id
            user.role = UserRole.CASHIER
            user.is_active = True
    await db.flush()


async def ensure_default_cashier(db: AsyncSession) -> None:
    """Legacy single-cashier seed — also provisions multi-branch cashiers."""
    await ensure_branch_cashiers(db)
    count = await db.scalar(select(func.count()).select_from(User)) or 0
    if count > 3:
        return
    result = await db.execute(select(User).where(User.username == DEFAULT_CASHIER_USERNAME))
    if result.scalar_one_or_none() is None:
        db.add(
            User(
                username=DEFAULT_CASHIER_USERNAME,
                password_hash=hash_password(DEFAULT_CASHIER_PASSWORD),
                full_name="Alex Rivera",
                role=UserRole.CASHIER,
                branch_id=1,
                is_active=True,
            )
        )
        await db.flush()


async def clear_operational_data() -> None:
    """Delete all business data; preserve master admin, branch row, and settings shell."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in CLEAR_TABLES_ORDER:
            try:
                await conn.execute(text(f"DELETE FROM {table}"))
            except Exception as exc:
                logger.warning("Clear skip %s: %s", table, exc)
        # Never delete the master admin by username — role strings vary across SQLite migrations.
        await conn.execute(
            text("DELETE FROM users WHERE username != :username"),
            {"username": MASTER_ADMIN_USERNAME},
        )
        await conn.execute(
            text(
                """
                UPDATE system_settings SET
                    exchange_rate_usd_lbp = :rate,
                    toters_commission_pct = :toters,
                    talabna_commission_pct = :talabna,
                    markit_commission_pct = :markit,
                    updated_at = :updated_at
                WHERE id = 1
                """
            ),
            {
                "rate": str(DEFAULT_EXCHANGE_RATE_USD_LBP),
                "toters": str(DEFAULT_TOTERS_COMMISSION_PCT),
                "talabna": str(DEFAULT_TALABNA_COMMISSION_PCT),
                "markit": str(DEFAULT_MARKIT_COMMISSION_PCT),
                "updated_at": now,
            },
        )
        try:
            await conn.execute(
                text(
                    "DELETE FROM sqlite_sequence WHERE name NOT IN ('users', 'branches', 'system_settings')"
                )
            )
        except Exception:
            pass
        await conn.execute(text("PRAGMA foreign_keys=ON"))

    await bootstrap_users_table()


async def get_system_settings(db: AsyncSession) -> SystemSettings:
    result = await db.execute(select(SystemSettings).where(SystemSettings.id == 1))
    row = result.scalar_one_or_none()
    if not row:
        row = SystemSettings(id=1)
        db.add(row)
        await db.flush()
    return row


def usd_to_lbp(amount_usd: Decimal, rate: Decimal) -> Decimal:
    return (amount_usd * rate).quantize(Decimal("1"))


def lbp_to_usd(amount_lbp: Decimal, rate: Decimal) -> Decimal:
    if rate == 0:
        return Decimal("0.00")
    return (amount_lbp / rate).quantize(Decimal("0.01"))


def apply_platform_commission(gross_usd: Decimal, commission_pct: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Returns (gross, commission, net) after platform cut."""
    commission = (gross_usd * commission_pct / Decimal("100")).quantize(Decimal("0.01"))
    net = gross_usd - commission
    return gross_usd, commission, net


def apply_toters_commission(gross_usd: Decimal, commission_pct: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    return apply_platform_commission(gross_usd, commission_pct)


def channel_commission_pct(channel: SalesChannel, settings_row: SystemSettings) -> Decimal:
    if channel == SalesChannel.TOTERS:
        return Decimal(str(settings_row.toters_commission_pct))
    if channel == SalesChannel.TALABNA:
        return Decimal(str(settings_row.talabna_commission_pct))
    if channel == SalesChannel.MARKIT:
        return Decimal(str(settings_row.markit_commission_pct))
    return Decimal("0")


def current_branch_id() -> int:
    return settings.branch_id


def compute_discount(
    subtotal_usd: Decimal,
    rate: Decimal,
    discount_type: "DiscountType",
    discount_value: Decimal,
) -> tuple[Decimal, Decimal]:
    """Returns (discount_usd, discount_lbp)."""
    from database import DiscountType  # avoid circular at module level

    if discount_type == DiscountType.NONE or discount_value <= 0:
        return Decimal("0.00"), Decimal("0")
    if discount_type == DiscountType.PERCENT:
        pct = min(discount_value, Decimal("100"))
        usd = (subtotal_usd * pct / Decimal("100")).quantize(Decimal("0.01"))
    elif discount_type == DiscountType.FLAT_USD:
        usd = min(discount_value, subtotal_usd).quantize(Decimal("0.01"))
    elif discount_type == DiscountType.FLAT_LBP:
        lbp_val = min(discount_value, usd_to_lbp(subtotal_usd, rate))
        usd = lbp_to_usd(lbp_val, rate)
    else:
        usd = Decimal("0.00")
    lbp = usd_to_lbp(usd, rate)
    return usd, lbp


SEED_INVENTORY = [
    ("Fresh Oranges", "g", Decimal("50000")),
    ("Mango Puree", "ml", Decimal("20000")),
    ("Crepe Batter Mix", "g", Decimal("15000")),
    ("Berry Mix", "g", Decimal("10000")),
]

SEED_RECIPES = [
    ("Green Glow Detox", "Fresh Oranges", Decimal("250"), "g"),
    ("Mango Tango", "Mango Puree", Decimal("150"), "ml"),
    ("Nutella Banana Crepe", "Crepe Batter Mix", Decimal("100"), "g"),
    ("Berry Blast Smoothie", "Berry Mix", Decimal("120"), "g"),
]


SEED_CATEGORIES = [
    ("Juice", 1),
    ("Crepe", 2),
    ("Cocktails", 3),
    ("Smoothies", 4),
]

SEED_MENU = [
    ("Sunrise Citrus Cooler", "Cocktails", 8.50),
    ("Tropical Sunset", "Cocktails", 9.00),
    ("Green Glow Detox", "Juice", 6.50),
    ("Mango Tango", "Juice", 7.00),
    ("Nutella Banana Crepe", "Crepe", 7.50),
    ("Berry Bliss Crepe", "Crepe", 8.00),
    ("Berry Blast Smoothie", "Smoothies", 7.50),
]

SEED_SUPPLIERS = [
    ("Tropical Fruit Co.", "Produce", "+961 1 234 567"),
    ("Fresh Sip Beverages", "Syrups & Mixers", "+961 1 987 654"),
]


MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP",
    "ALTER TABLE shifts ADD COLUMN exchange_rate_snapshot NUMERIC(14,2) DEFAULT 89500",
    "ALTER TABLE shifts ADD COLUMN opening_float_usd NUMERIC(12,2) DEFAULT 0",
    "ALTER TABLE shifts ADD COLUMN opening_float_lbp NUMERIC(14,0) DEFAULT 0",
    "ALTER TABLE shifts ADD COLUMN expected_cash_usd NUMERIC(12,2)",
    "ALTER TABLE shifts ADD COLUMN expected_cash_lbp NUMERIC(14,0)",
    "ALTER TABLE shifts ADD COLUMN counted_cash_usd NUMERIC(12,2)",
    "ALTER TABLE shifts ADD COLUMN counted_cash_lbp NUMERIC(14,0)",
    "ALTER TABLE shifts ADD COLUMN cash_variance_usd NUMERIC(12,2)",
    "ALTER TABLE shifts ADD COLUMN cash_variance_lbp NUMERIC(14,0)",
    "ALTER TABLE shifts ADD COLUMN total_sales_usd NUMERIC(12,2)",
    "ALTER TABLE shifts ADD COLUMN total_sales_lbp NUMERIC(14,0)",
    "ALTER TABLE shifts ADD COLUMN invoice_count INTEGER",
    "ALTER TABLE invoices ADD COLUMN sales_channel VARCHAR(20) DEFAULT 'in_store'",
    "ALTER TABLE invoices ADD COLUMN settlement_currency VARCHAR(3) DEFAULT 'USD'",
    "ALTER TABLE invoices ADD COLUMN subtotal_usd NUMERIC(12,2) DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN total_usd NUMERIC(12,2) DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN subtotal_lbp NUMERIC(14,0) DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN total_lbp NUMERIC(14,0) DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN exchange_rate_snapshot NUMERIC(14,2) DEFAULT 89500",
    "ALTER TABLE invoices ADD COLUMN toters_commission_pct NUMERIC(5,2) DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN toters_commission_amount NUMERIC(12,2) DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN net_total_usd NUMERIC(12,2) DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN net_total_lbp NUMERIC(14,0) DEFAULT 0",
    "ALTER TABLE invoice_items ADD COLUMN line_total_lbp NUMERIC(14,0) DEFAULT 0",
    "ALTER TABLE menu_items ADD COLUMN category_id INTEGER REFERENCES product_categories(id)",
    "ALTER TABLE users ADD COLUMN permissions_json TEXT",
    "ALTER TABLE invoices ADD COLUMN discount_type VARCHAR(16) DEFAULT 'none'",
    "ALTER TABLE invoices ADD COLUMN discount_value NUMERIC(12,2) DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN discount_amount_usd NUMERIC(12,2) DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN discount_amount_lbp NUMERIC(14,0) DEFAULT 0",
    "ALTER TABLE system_settings ADD COLUMN talabna_commission_pct NUMERIC(5,2) DEFAULT 15",
    "ALTER TABLE system_settings ADD COLUMN markit_commission_pct NUMERIC(5,2) DEFAULT 18",
    "ALTER TABLE product_recipes ADD COLUMN metric_unit VARCHAR(32)",
    "ALTER TABLE shifts ADD COLUMN branch_id INTEGER DEFAULT 1 REFERENCES branches(id)",
    "ALTER TABLE invoices ADD COLUMN branch_id INTEGER DEFAULT 1 REFERENCES branches(id)",
    "ALTER TABLE invoice_items ADD COLUMN branch_id INTEGER DEFAULT 1 REFERENCES branches(id)",
    "ALTER TABLE inventory_items ADD COLUMN branch_id INTEGER DEFAULT 1 REFERENCES branches(id)",
    "ALTER TABLE stock_movements ADD COLUMN branch_id INTEGER DEFAULT 1 REFERENCES branches(id)",
    "ALTER TABLE expenses ADD COLUMN branch_id INTEGER DEFAULT 1 REFERENCES branches(id)",
    "ALTER TABLE supplier_debts ADD COLUMN branch_id INTEGER DEFAULT 1 REFERENCES branches(id)",
    "ALTER TABLE system_settings ADD COLUMN time_offset_seconds INTEGER DEFAULT 0",
    "ALTER TABLE system_settings ADD COLUMN last_time_sync_at DATETIME",
]


async def ensure_catalog_seed(db: AsyncSession) -> None:
    """Categories + menu products — idempotent."""
    cat_map: dict[str, int] = {}
    for name, order in SEED_CATEGORIES:
        exists = await db.execute(select(ProductCategory).where(ProductCategory.name == name))
        cat = exists.scalar_one_or_none()
        if not cat:
            cat = ProductCategory(name=name, sort_order=order)
            db.add(cat)
            await db.flush()
        cat_map[name] = cat.id

    for name, category, price in SEED_MENU:
        exists = await db.execute(
            select(MenuItem).where(MenuItem.name == name, MenuItem.category == category)
        )
        if not exists.scalar_one_or_none():
            db.add(
                MenuItem(
                    name=name,
                    category=category,
                    category_id=cat_map.get(category),
                    unit_price=Decimal(str(price)),
                )
            )
    await db.flush()


async def ensure_branch_inventory_seed(db: AsyncSession) -> None:
    """Per-branch raw ingredient stock + BOM links — idempotent."""
    branch_ids = [b[0] for b in SEED_BRANCHES]
    inv_map: dict[str, int] = {}
    for bid in branch_ids:
        for name, unit, stock in SEED_INVENTORY:
            exists = await db.execute(
                select(InventoryItem).where(InventoryItem.name == name, InventoryItem.branch_id == bid)
            )
            row = exists.scalar_one_or_none()
            if not row:
                row = InventoryItem(name=name, unit=unit, current_stock=stock, branch_id=bid)
                db.add(row)
                await db.flush()
            inv_map[f"{bid}:{name}"] = row.id

    menu_by_name: dict[str, int] = {}
    menu_rows = await db.execute(select(MenuItem))
    for m in menu_rows.scalars().all():
        menu_by_name[m.name] = m.id

    for menu_name, inv_name, qty, metric in SEED_RECIPES:
        mid = menu_by_name.get(menu_name)
        if not mid:
            continue
        for bid in branch_ids:
            iid = inv_map.get(f"{bid}:{inv_name}")
            if not iid:
                continue
            exists = await db.execute(
                select(ProductRecipe).where(ProductRecipe.menu_item_id == mid, ProductRecipe.inventory_item_id == iid)
            )
            if not exists.scalar_one_or_none():
                db.add(
                    ProductRecipe(
                        menu_item_id=mid,
                        inventory_item_id=iid,
                        quantity_per_sale=qty,
                        metric_unit=metric,
                        recipe_role=RecipeRole.BASE.value,
                    )
                )
    await db.flush()


async def seed_branch_inventory(db: AsyncSession, branch_id: int) -> None:
    """Seed default ingredient stock + BOM links for a single branch."""
    inv_map: dict[str, int] = {}
    for name, unit, stock in SEED_INVENTORY:
        exists = await db.execute(
            select(InventoryItem).where(InventoryItem.name == name, InventoryItem.branch_id == branch_id)
        )
        row = exists.scalar_one_or_none()
        if not row:
            row = InventoryItem(name=name, unit=unit, current_stock=stock, branch_id=branch_id)
            db.add(row)
            await db.flush()
        inv_map[name] = row.id

    menu_rows = await db.execute(select(MenuItem))
    menu_by_name = {m.name: m.id for m in menu_rows.scalars().all()}

    for menu_name, inv_name, qty, metric in SEED_RECIPES:
        mid = menu_by_name.get(menu_name)
        iid = inv_map.get(inv_name)
        if not mid or not iid:
            continue
        exists = await db.execute(
            select(ProductRecipe).where(ProductRecipe.menu_item_id == mid, ProductRecipe.inventory_item_id == iid)
        )
        if not exists.scalar_one_or_none():
            db.add(
                ProductRecipe(
                    menu_item_id=mid,
                    inventory_item_id=iid,
                    quantity_per_sale=qty,
                    metric_unit=metric,
                    recipe_role=RecipeRole.BASE.value,
                )
            )
    await db.flush()


async def seed_database() -> None:
    try:
        async with SessionLocal() as db:
            await ensure_seed_branches(db)

            user_count_before = await db.scalar(select(func.count()).select_from(User)) or 0
            await ensure_master_admin(db)
            await ensure_branch_cashiers(db)
            if user_count_before == 0:
                await ensure_default_cashier(db)

            await get_system_settings(db)
            await ensure_catalog_seed(db)
            await ensure_branch_inventory_seed(db)

            if FRESH_DB_MARKER.exists():
                await db.commit()
                return

            cat_map: dict[str, int] = {}
            for name, order in SEED_CATEGORIES:
                exists = await db.execute(select(ProductCategory).where(ProductCategory.name == name))
                cat = exists.scalar_one_or_none()
                if not cat:
                    cat = ProductCategory(name=name, sort_order=order)
                    db.add(cat)
                    await db.flush()
                cat_map[name] = cat.id

            for name, category, price in SEED_MENU:
                exists = await db.execute(
                    select(MenuItem).where(MenuItem.name == name, MenuItem.category == category)
                )
                if not exists.scalar_one_or_none():
                    db.add(
                        MenuItem(
                            name=name,
                            category=category,
                            category_id=cat_map.get(category),
                            unit_price=Decimal(str(price)),
                        )
                    )

            # Link existing menu items to categories by name
            items = await db.execute(select(MenuItem).where(MenuItem.category_id.is_(None)))
            for item in items.scalars().all():
                cat_row = await db.execute(select(ProductCategory).where(ProductCategory.name == item.category))
                cat = cat_row.scalar_one_or_none()
                if cat:
                    item.category_id = cat.id

            for name, category, phone in SEED_SUPPLIERS:
                exists = await db.execute(select(Supplier).where(Supplier.name == name))
                if not exists.scalar_one_or_none():
                    db.add(Supplier(name=name, category=category, contact_phone=phone))

            inv_map: dict[str, int] = {}
            branch_ids = [b[0] for b in SEED_BRANCHES]
            for bid in branch_ids:
                for name, unit, stock in SEED_INVENTORY:
                    exists = await db.execute(
                        select(InventoryItem).where(InventoryItem.name == name, InventoryItem.branch_id == bid)
                    )
                    row = exists.scalar_one_or_none()
                    if not row:
                        row = InventoryItem(name=name, unit=unit, current_stock=stock, branch_id=bid)
                        db.add(row)
                        await db.flush()
                    key = f"{bid}:{name}"
                    inv_map[key] = row.id

            menu_by_name: dict[str, int] = {}
            menu_rows = await db.execute(select(MenuItem))
            for m in menu_rows.scalars().all():
                menu_by_name[m.name] = m.id

            for menu_name, inv_name, qty, metric in SEED_RECIPES:
                mid = menu_by_name.get(menu_name)
                if not mid:
                    continue
                for bid in branch_ids:
                    iid = inv_map.get(f"{bid}:{inv_name}")
                    if not iid:
                        continue
                    exists = await db.execute(
                        select(ProductRecipe).where(ProductRecipe.menu_item_id == mid, ProductRecipe.inventory_item_id == iid)
                    )
                    if not exists.scalar_one_or_none():
                        db.add(
                            ProductRecipe(
                                menu_item_id=mid,
                                inventory_item_id=iid,
                                quantity_per_sale=qty,
                                metric_unit=metric,
                                recipe_role=RecipeRole.BASE.value,
                            )
                        )

            await db.commit()
    except Exception as exc:
        logger.exception("Operational seed failed — recovering master admin only: %s", exc)
        await bootstrap_users_table()


async def _migrate_legacy_schema(conn) -> None:
    """Rebuild legacy tables that still carry removed SKU columns."""
    async def column_exists(table: str, col: str) -> bool:
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        return col in [row[1] for row in result.fetchall()]

    if await column_exists("menu_items", "sku"):
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS menu_items_new (
                id INTEGER PRIMARY KEY,
                name VARCHAR(128) NOT NULL,
                category VARCHAR(64) NOT NULL,
                category_id INTEGER REFERENCES product_categories(id),
                description TEXT,
                unit_price NUMERIC(12,2) NOT NULL,
                is_active BOOLEAN DEFAULT 1
            )
            """
        )
        await conn.exec_driver_sql(
            """
            INSERT OR IGNORE INTO menu_items_new (id, name, category, category_id, description, unit_price, is_active)
            SELECT id, name, category, category_id, description, unit_price, is_active FROM menu_items
            """
        )
        await conn.exec_driver_sql("DROP TABLE menu_items")
        await conn.exec_driver_sql("ALTER TABLE menu_items_new RENAME TO menu_items")
        await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_menu_items_name ON menu_items (name)")
        await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_menu_items_category ON menu_items (category)")

    if await column_exists("inventory_items", "sku"):
        await conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS inventory_items_new (
                id INTEGER PRIMARY KEY,
                name VARCHAR(128) NOT NULL UNIQUE,
                unit VARCHAR(64) DEFAULT 'pcs',
                current_stock NUMERIC(12,3) DEFAULT 0,
                reorder_level NUMERIC(12,3) DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                created_at DATETIME
            )
            """
        )
        await conn.exec_driver_sql(
            """
            INSERT OR IGNORE INTO inventory_items_new (id, name, unit, current_stock, reorder_level, is_active, created_at)
            SELECT id, name, unit, current_stock, reorder_level, is_active, created_at FROM inventory_items
            """
        )
        await conn.exec_driver_sql("DROP TABLE inventory_items")
        await conn.exec_driver_sql("ALTER TABLE inventory_items_new RENAME TO inventory_items")

    if await column_exists("invoice_items", "sku_snapshot"):
        await conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS invoice_items_new (
                id INTEGER PRIMARY KEY,
                invoice_id INTEGER NOT NULL REFERENCES invoices(id),
                menu_item_id INTEGER NOT NULL REFERENCES menu_items(id),
                operator_id INTEGER NOT NULL REFERENCES users(id),
                line_timestamp DATETIME,
                name_snapshot VARCHAR(128) NOT NULL,
                unit_price_snapshot NUMERIC(12,2) NOT NULL,
                quantity INTEGER NOT NULL,
                line_total NUMERIC(12,2) NOT NULL,
                line_total_lbp NUMERIC(14,0) DEFAULT 0
            )
            """
        )
        await conn.exec_driver_sql(
            """
            INSERT OR IGNORE INTO invoice_items_new
            (id, invoice_id, menu_item_id, operator_id, line_timestamp, name_snapshot,
             unit_price_snapshot, quantity, line_total, line_total_lbp)
            SELECT id, invoice_id, menu_item_id, operator_id, line_timestamp, name_snapshot,
                   unit_price_snapshot, quantity, line_total, line_total_lbp FROM invoice_items
            """
        )
        await conn.exec_driver_sql("DROP TABLE invoice_items")
        await conn.exec_driver_sql("ALTER TABLE invoice_items_new RENAME TO invoice_items")
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    async def table_has_column(table: str, col: str) -> bool:
        return await column_exists(table, col)

    if await table_has_column("customer_debts", "debt_type"):
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS customer_debts_new (
                id INTEGER PRIMARY KEY,
                branch_id INTEGER DEFAULT 1 REFERENCES branches(id),
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                description VARCHAR(256) NOT NULL,
                amount_usd NUMERIC(12,2) NOT NULL,
                amount_lbp NUMERIC(14,0) NOT NULL,
                exchange_rate_snapshot NUMERIC(14,2) NOT NULL,
                due_date DATE,
                is_settled BOOLEAN DEFAULT 0,
                settled_at DATETIME,
                recorded_by_id INTEGER NOT NULL REFERENCES users(id),
                created_at DATETIME
            )
            """
        )
        await conn.exec_driver_sql(
            """
            INSERT INTO customer_debts_new
            (id, branch_id, customer_id, description, amount_usd, amount_lbp, exchange_rate_snapshot,
             due_date, is_settled, settled_at, recorded_by_id, created_at)
            SELECT id, branch_id, customer_id, description, amount_usd, amount_lbp, exchange_rate_snapshot,
                   due_date,
                   CASE WHEN debt_type = 'payment' OR is_settled = 1 THEN 1 ELSE 0 END,
                   settled_at, recorded_by_id, created_at
            FROM customer_debts
            """
        )
        await conn.exec_driver_sql("DROP TABLE customer_debts")
        await conn.exec_driver_sql("ALTER TABLE customer_debts_new RENAME TO customer_debts")
        await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_debts_customer_id ON customer_debts (customer_id)")
        await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_debts_is_settled ON customer_debts (is_settled)")

    if await table_has_column("supplier_debts", "debt_type"):
        await conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS supplier_debts_new (
                id INTEGER PRIMARY KEY,
                branch_id INTEGER DEFAULT 1 REFERENCES branches(id),
                supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                description VARCHAR(256) NOT NULL,
                amount_usd NUMERIC(12,2) NOT NULL,
                amount_lbp NUMERIC(14,0) NOT NULL,
                exchange_rate_snapshot NUMERIC(14,2) NOT NULL,
                due_date DATE,
                is_settled BOOLEAN DEFAULT 0,
                settled_at DATETIME,
                recorded_by_id INTEGER NOT NULL REFERENCES users(id),
                created_at DATETIME
            )
            """
        )
        await conn.exec_driver_sql(
            """
            INSERT INTO supplier_debts_new
            (id, branch_id, supplier_id, description, amount_usd, amount_lbp, exchange_rate_snapshot,
             due_date, is_settled, settled_at, recorded_by_id, created_at)
            SELECT id, branch_id, supplier_id, description, amount_usd, amount_lbp, exchange_rate_snapshot,
                   due_date,
                   CASE WHEN debt_type = 'payment' OR is_settled = 1 THEN 1 ELSE 0 END,
                   settled_at, recorded_by_id, created_at
            FROM supplier_debts
            """
        )
        await conn.exec_driver_sql("DROP TABLE supplier_debts")
        await conn.exec_driver_sql("ALTER TABLE supplier_debts_new RENAME TO supplier_debts")
        await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_supplier_debts_supplier_id ON supplier_debts (supplier_id)")
        await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_supplier_debts_is_settled ON supplier_debts (is_settled)")
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")


async def _column_exists(conn, table: str, column: str) -> bool:
    result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
    return column in [row[1] for row in result.fetchall()]


async def _table_exists(conn, table: str) -> bool:
    result = await conn.exec_driver_sql(
        f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{table}'"
    )
    return result.fetchone() is not None


# Columns added after v3 — applied idempotently (no silent failure)
SCHEMA_COLUMN_PATCHES: list[tuple[str, str, str]] = [
    ("system_settings", "talabna_commission_pct", "ALTER TABLE system_settings ADD COLUMN talabna_commission_pct NUMERIC(5,2) DEFAULT 15"),
    ("system_settings", "markit_commission_pct", "ALTER TABLE system_settings ADD COLUMN markit_commission_pct NUMERIC(5,2) DEFAULT 18"),
    ("product_recipes", "metric_unit", "ALTER TABLE product_recipes ADD COLUMN metric_unit VARCHAR(32)"),
    ("product_recipes", "recipe_role", "ALTER TABLE product_recipes ADD COLUMN recipe_role VARCHAR(16) DEFAULT 'base'"),
    ("product_recipes", "display_title", "ALTER TABLE product_recipes ADD COLUMN display_title VARCHAR(128)"),
    ("product_recipes", "extra_price_usd", "ALTER TABLE product_recipes ADD COLUMN extra_price_usd NUMERIC(12,2) DEFAULT 0"),
    ("shifts", "branch_id", "ALTER TABLE shifts ADD COLUMN branch_id INTEGER DEFAULT 1"),
    ("invoices", "branch_id", "ALTER TABLE invoices ADD COLUMN branch_id INTEGER DEFAULT 1"),
    ("invoice_items", "branch_id", "ALTER TABLE invoice_items ADD COLUMN branch_id INTEGER DEFAULT 1"),
    ("invoice_items", "line_modifiers_json", "ALTER TABLE invoice_items ADD COLUMN line_modifiers_json TEXT"),
    ("inventory_items", "branch_id", "ALTER TABLE inventory_items ADD COLUMN branch_id INTEGER DEFAULT 1"),
    ("stock_movements", "branch_id", "ALTER TABLE stock_movements ADD COLUMN branch_id INTEGER DEFAULT 1"),
    ("expenses", "branch_id", "ALTER TABLE expenses ADD COLUMN branch_id INTEGER DEFAULT 1"),
    ("supplier_debts", "branch_id", "ALTER TABLE supplier_debts ADD COLUMN branch_id INTEGER DEFAULT 1"),
    ("customer_debts", "invoice_number", "ALTER TABLE customer_debts ADD COLUMN invoice_number INTEGER"),
    ("customer_debts", "reference_number", "ALTER TABLE customer_debts ADD COLUMN reference_number VARCHAR(64)"),
    ("supplier_debts", "invoice_number", "ALTER TABLE supplier_debts ADD COLUMN invoice_number INTEGER"),
    ("supplier_debts", "reference_number", "ALTER TABLE supplier_debts ADD COLUMN reference_number VARCHAR(64)"),
    ("menu_items", "price_s", "ALTER TABLE menu_items ADD COLUMN price_s NUMERIC(12,2)"),
    ("menu_items", "price_m", "ALTER TABLE menu_items ADD COLUMN price_m NUMERIC(12,2)"),
    ("menu_items", "price_l", "ALTER TABLE menu_items ADD COLUMN price_l NUMERIC(12,2)"),
    ("menu_items", "sizes_enabled", "ALTER TABLE menu_items ADD COLUMN sizes_enabled BOOLEAN DEFAULT 0"),
    ("users", "branch_id", "ALTER TABLE users ADD COLUMN branch_id INTEGER REFERENCES branches(id)"),
    ("branches", "location", "ALTER TABLE branches ADD COLUMN location VARCHAR(256)"),
]


async def _apply_schema_column_patches(conn) -> None:
    for table, column, ddl in SCHEMA_COLUMN_PATCHES:
        if not await _table_exists(conn, table):
            continue
        if not await _column_exists(conn, table, column):
            await conn.exec_driver_sql(ddl)


async def _backfill_bill_invoice_numbers(conn) -> None:
    """Assign unified sequential invoice_number starting at 1001 for legacy debts."""
    if not await _table_exists(conn, "customer_debts") or not await _table_exists(conn, "supplier_debts"):
        return
    if not await _column_exists(conn, "customer_debts", "invoice_number"):
        return

    cust_rows = await conn.exec_driver_sql(
        "SELECT id, created_at FROM customer_debts WHERE invoice_number IS NULL ORDER BY created_at, id"
    )
    sup_rows = await conn.exec_driver_sql(
        "SELECT id, created_at FROM supplier_debts WHERE invoice_number IS NULL ORDER BY created_at, id"
    )
    pending: list[tuple[str, int, object]] = []
    for row in cust_rows.fetchall():
        pending.append(("customer_debts", row[0], row[1]))
    for row in sup_rows.fetchall():
        pending.append(("supplier_debts", row[0], row[1]))
    if not pending:
        max_cust = await conn.exec_driver_sql("SELECT MAX(invoice_number) FROM customer_debts")
        max_sup = await conn.exec_driver_sql("SELECT MAX(invoice_number) FROM supplier_debts")
        mx = max(max_cust.scalar() or 0, max_sup.scalar() or 0)
        if mx >= 1000:
            await conn.exec_driver_sql(
                "INSERT OR IGNORE INTO bill_invoice_counter (id, next_number) VALUES (1, ?)",
                (mx + 1,),
            )
            await conn.exec_driver_sql(
                "UPDATE bill_invoice_counter SET next_number = ? WHERE id = 1 AND next_number < ?",
                (mx + 1, mx + 1),
            )
        return

    pending.sort(key=lambda x: (x[2] or "", x[1]))
    next_num = 1001
    for table, row_id, _ in pending:
        await conn.exec_driver_sql(
            f"UPDATE {table} SET invoice_number = ? WHERE id = ?",
            (next_num, row_id),
        )
        next_num += 1
    await conn.exec_driver_sql(
        "INSERT OR IGNORE INTO bill_invoice_counter (id, next_number) VALUES (1, ?)",
        (next_num,),
    )
    await conn.exec_driver_sql(
        "UPDATE bill_invoice_counter SET next_number = ? WHERE id = 1",
        (next_num,),
    )
    await conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_customer_debts_invoice_number ON customer_debts (invoice_number)"
    )
    await conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_supplier_debts_invoice_number ON supplier_debts (invoice_number)"
    )


async def _backfill_sale_transactions(conn) -> None:
    if not await _table_exists(conn, "sale_transactions"):
        return
    if not await _table_exists(conn, "invoices"):
        return

    max_row = await conn.exec_driver_sql("SELECT MAX(sale_number) FROM sale_transactions")
    mx = int(max_row.scalar() or 0)
    next_num = max(mx + 1, 1001) if mx >= 1001 else 1001

    inv_rows = await conn.exec_driver_sql(
        """
        SELECT i.id, i.branch_id, i.operator_id, u.full_name, i.finalized_at,
               i.discount_amount_usd, i.discount_amount_lbp, i.net_total_usd, i.net_total_lbp,
               i.payment_method, i.sales_channel, i.invoice_number
        FROM invoices i
        JOIN users u ON u.id = i.operator_id
        LEFT JOIN sale_transactions t ON t.invoice_id = i.id
        WHERE t.id IS NULL AND i.status IN ('finalized', 'FINALIZED')
        ORDER BY i.finalized_at, i.id
        """
    )
    rows = inv_rows.fetchall()
    for row in rows:
        inv_id = row[0]
        items = await conn.exec_driver_sql(
            "SELECT quantity, name_snapshot FROM invoice_items WHERE invoice_id = ? ORDER BY id",
            (inv_id,),
        )
        parts = [f"{r[0]}x {r[1]}" for r in items.fetchall()]
        summary = ", ".join(parts)[:512]
        await conn.exec_driver_sql(
            """
            INSERT INTO sale_transactions
            (invoice_id, sale_number, branch_id, operator_id, operator_name, items_summary,
             discount_amount_usd, discount_amount_lbp, net_total_usd, net_total_lbp,
             payment_method, sales_channel, invoice_number, finalized_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inv_id, next_num, row[1], row[2], row[3], summary,
                row[5], row[6], row[7], row[8],
                str(row[9]), str(row[10]), row[11], row[4],
            ),
        )
        next_num += 1
    await conn.exec_driver_sql(
        "INSERT OR IGNORE INTO sale_number_counter (id, next_number) VALUES (1, ?)",
        (next_num,),
    )
    await conn.exec_driver_sql(
        "UPDATE sale_number_counter SET next_number = ? WHERE id = 1 AND next_number < ?",
        (next_num, next_num),
    )


def reset_local_database_files() -> list[str]:
    """Delete bob_juice.db and WAL sidecars. Returns deleted paths."""
    deleted: list[str] = []
    for path in (DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")):
        if path.exists():
            path.unlink()
            deleted.append(str(path.resolve()))
    return deleted


async def _backfill_menu_item_sizes(conn) -> None:
    if not await _table_exists(conn, "menu_items"):
        return
    if not await _column_exists(conn, "menu_items", "price_m"):
        return
    await conn.exec_driver_sql(
        "UPDATE menu_items SET price_m = unit_price WHERE price_m IS NULL"
    )
    if await _column_exists(conn, "menu_items", "sizes_enabled"):
        await conn.exec_driver_sql(
            """UPDATE menu_items SET sizes_enabled = 1
               WHERE price_s IS NOT NULL OR price_l IS NOT NULL
                  OR (price_m IS NOT NULL AND price_m != unit_price)"""
        )


async def _normalize_inventory_units_data(conn) -> None:
    """One-time migration: kg/L → g/ml canonical storage."""
    if not await _table_exists(conn, "inventory_items"):
        return
    from inventory_units import normalize_quantity, normalize_unit

    rows = await conn.exec_driver_sql("SELECT id, unit, current_stock FROM inventory_items")
    for row_id, unit, stock in rows.fetchall():
        if unit is None:
            continue
        try:
            if normalize_unit(unit) == normalize_unit(str(unit)):
                if str(unit).lower() in ("g", "ml", "pcs"):
                    continue
            new_qty, new_unit = normalize_quantity(stock or 0, unit)
            await conn.exec_driver_sql(
                "UPDATE inventory_items SET unit = ?, current_stock = ? WHERE id = ?",
                (new_unit, str(new_qty), row_id),
            )
        except (ValueError, Exception):
            continue

    if await _table_exists(conn, "product_recipes"):
        recipe_rows = await conn.exec_driver_sql(
            "SELECT id, quantity_per_sale, metric_unit, inventory_item_id FROM product_recipes"
        )
        for rid, qty, metric, inv_id in recipe_rows.fetchall():
            inv_row = await conn.exec_driver_sql(
                "SELECT unit FROM inventory_items WHERE id = ?", (inv_id,)
            )
            inv_unit = inv_row.scalar()
            if inv_unit is None:
                continue
            try:
                src_unit = metric or inv_unit
                new_qty, new_unit = normalize_quantity(qty or 0, src_unit)
                if normalize_unit(inv_unit) != new_unit:
                    new_unit = normalize_unit(inv_unit)
                await conn.exec_driver_sql(
                    "UPDATE product_recipes SET quantity_per_sale = ?, metric_unit = ? WHERE id = ?",
                    (str(new_qty), new_unit, rid),
                )
            except (ValueError, Exception):
                continue

    if await _table_exists(conn, "global_modifiers"):
        mod_rows = await conn.exec_driver_sql(
            "SELECT id, quantity_per_sale, metric_unit, inventory_item_id FROM global_modifiers"
        )
        for mid, qty, metric, inv_id in mod_rows.fetchall():
            inv_row = await conn.exec_driver_sql(
                "SELECT unit FROM inventory_items WHERE id = ?", (inv_id,)
            )
            inv_unit = inv_row.scalar()
            if inv_unit is None:
                continue
            try:
                src_unit = metric or inv_unit
                new_qty, new_unit = normalize_quantity(qty or 0, src_unit)
                if normalize_unit(inv_unit) != new_unit:
                    new_unit = normalize_unit(inv_unit)
                await conn.exec_driver_sql(
                    "UPDATE global_modifiers SET quantity_per_sale = ?, metric_unit = ? WHERE id = ?",
                    (str(new_qty), new_unit, mid),
                )
            except (ValueError, Exception):
                continue


async def init_db() -> None:
    if settings.reset_db_on_startup:
        removed = reset_local_database_files()
        if removed:
            logger.warning("BOB_RESET_DB=1 — deleted old database: %s", ", ".join(removed))
        else:
            logger.warning("BOB_RESET_DB=1 — no database file found at %s", DB_PATH.resolve())

    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
        await conn.exec_driver_sql("PRAGMA synchronous=FULL;")
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON;")
        await conn.run_sync(Base.metadata.create_all)
        for sql in MIGRATIONS:
            try:
                await conn.exec_driver_sql(sql)
            except Exception:
                pass
        await _apply_schema_column_patches(conn)
        await _backfill_menu_item_sizes(conn)
        await _normalize_inventory_units_data(conn)
        await _backfill_bill_invoice_numbers(conn)
        await _backfill_sale_transactions(conn)
        await _migrate_legacy_schema(conn)
    try:
        await seed_database()
    except Exception:
        logger.exception("Seed step failed — continuing with master admin bootstrap")

    await bootstrap_users_table()
