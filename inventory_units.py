"""Canonical inventory units — all stock stored in g, ml, or pcs."""

from __future__ import annotations

from decimal import Decimal

# Canonical storage units (never kg / L in the database)
CANONICAL_UNITS = frozenset({"g", "ml", "pcs"})

# Admin may type these; they normalize before persistence
INPUT_UNIT_ALIASES: dict[str, tuple[str, Decimal]] = {
    "g": ("g", Decimal("1")),
    "gram": ("g", Decimal("1")),
    "grams": ("g", Decimal("1")),
    "kg": ("g", Decimal("1000")),
    "kilogram": ("g", Decimal("1000")),
    "kilograms": ("g", Decimal("1000")),
    "kilo": ("g", Decimal("1000")),
    "ml": ("ml", Decimal("1")),
    "milliliter": ("ml", Decimal("1")),
    "milliliters": ("ml", Decimal("1")),
    "millilitre": ("ml", Decimal("1")),
    "l": ("ml", Decimal("1000")),
    "liter": ("ml", Decimal("1000")),
    "liters": ("ml", Decimal("1000")),
    "litre": ("ml", Decimal("1000")),
    "litres": ("ml", Decimal("1000")),
    "pcs": ("pcs", Decimal("1")),
    "pc": ("pcs", Decimal("1")),
    "piece": ("pcs", Decimal("1")),
    "pieces": ("pcs", Decimal("1")),
    "unit": ("pcs", Decimal("1")),
    "units": ("pcs", Decimal("1")),
    "each": ("pcs", Decimal("1")),
    "ea": ("pcs", Decimal("1")),
}


def normalize_unit(raw: str | None, *, default: str = "pcs") -> str:
    key = (raw or default).strip().lower()
    if not key:
        key = default
    if key in CANONICAL_UNITS:
        return key
    mapped = INPUT_UNIT_ALIASES.get(key)
    if mapped:
        return mapped[0]
    raise ValueError(f"Unsupported unit '{raw}'. Use g, kg, ml, L, or pcs.")


def normalize_quantity(qty: Decimal | float | str, unit: str | None) -> tuple[Decimal, str]:
    """Convert input quantity + unit to canonical storage (g, ml, or pcs)."""
    amount = Decimal(str(qty))
    if amount <= 0:
        raise ValueError("Quantity must be greater than zero")
    key = (unit or "pcs").strip().lower()
    if key in CANONICAL_UNITS:
        return amount, key
    mapped = INPUT_UNIT_ALIASES.get(key)
    if not mapped:
        raise ValueError(f"Unsupported unit '{unit}'. Use g, kg, ml, L, or pcs.")
    canonical, factor = mapped
    return (amount * factor).quantize(Decimal("0.001")), canonical


def units_compatible(stored_unit: str | None, incoming_unit: str | None) -> bool:
    try:
        return normalize_unit(stored_unit) == normalize_unit(incoming_unit)
    except ValueError:
        return False


def format_unit_label(unit: str) -> str:
    u = normalize_unit(unit)
    return {"g": "grams (g)", "ml": "milliliters (ml)", "pcs": "pieces (pcs)"}[u]
