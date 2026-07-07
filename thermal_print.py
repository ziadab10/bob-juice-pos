"""Direct thermal receipt printing — bypasses browser print preview."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from config import settings
from inventory import parse_line_modifiers
from logo_assets import logo_thermal_escpos_bytes

logger = logging.getLogger("bob_juice.print")

SHOP_NAME = "BOB JUICE"
SHOP_TAGLINE = "Fresh · Natural · Bold"
SHOP_CONTACT = "70 613 481"
CLOSING_MESSAGE = "Thank you for visiting BOB JUICE!"
CLOSING_SUBMESSAGE = "Enjoy your drink!"


def _fmt_usd(val) -> str:
    return f"${Decimal(str(val)):,.2f}"


def _fmt_lbp(val) -> str:
    return f"{int(Decimal(str(val))):,} LBP"


def _resolve_printer_name() -> str:
    configured = settings.thermal_printer_name.strip()
    if configured:
        return configured
    try:
        import win32print

        return win32print.GetDefaultPrinter()
    except Exception:
        return ""


def _escpos_logo_bytes() -> bytes | None:
    """Optional raster logo for ESC/POS printers (alpha-safe, no black matte)."""
    return logo_thermal_escpos_bytes()


def _build_escpos_payload(lines: list[str]) -> bytes:
    ESC, GS = b"\x1b", b"\x1d"
    out = bytearray()
    out += ESC + b"@"

    logo = _escpos_logo_bytes()
    if logo:
        out += logo

    out += ESC + b"a" + b"\x01"
    out += ESC + b"E" + b"\x01"
    out += f"{SHOP_NAME}\n".encode("cp437", errors="replace")
    out += ESC + b"E" + b"\x00"
    out += f"{SHOP_TAGLINE}\n".encode("cp437", errors="replace")
    out += f"Tel: {SHOP_CONTACT}\n".encode("cp437", errors="replace")
    out += ESC + b"a" + b"\x00"
    out += b"========================\n"

    for line in lines:
        out += line.encode("cp437", errors="replace") + b"\n"

    out += b"\n"
    out += ESC + b"a" + b"\x01"
    out += CLOSING_MESSAGE.encode("cp437", errors="replace") + b"\n"
    out += CLOSING_SUBMESSAGE.encode("cp437", errors="replace") + b"\n"
    out += ESC + b"a" + b"\x00"
    out += b"\n\n\n"
    out += GS + b"V" + b"\x00"
    return bytes(out)


def _fmt_receipt_datetime(val) -> str:
    if val is None:
        return ""
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    s = str(val).strip()
    if not s:
        return ""
    try:
        normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
        return datetime.fromisoformat(normalized).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s[:19] if len(s) >= 19 else s


def _escpos_receipt_lines(inv: dict[str, Any]) -> list[str]:
    channel = (inv.get("sales_channel") or "in_store").replace("_", " ").title()
    lines = [
        inv.get("invoice_number", ""),
        _fmt_receipt_datetime(inv.get("finalized_at")),
        f"Channel: {channel}",
        "------------------------",
    ]
    for item in inv.get("items") or []:
        qty = item.get("quantity", 1)
        lines.append(f"{qty}x {item.get('name_snapshot', '')}")
        mods = parse_line_modifiers(item.get("line_modifiers_json"))
        for ex in mods.get("extras") or []:
            label = ex.get("name") or "Extra"
            price = Decimal(str(ex.get("extra_price_usd", 0)))
            lines.append(f"  + {label} ({_fmt_usd(price)})")
        for ex in mods.get("excludes") or []:
            label = ex.get("name") or "Item"
            lines.append(f"  - No {label} ($0.00)")
        lines.append(f"  Line: {_fmt_usd(item.get('line_total', 0))}")
    lines.append("------------------------")
    lines.append(f"Subtotal: {_fmt_usd(inv.get('subtotal_usd', 0))}")
    disc = Decimal(str(inv.get("discount_amount_usd", 0)))
    if disc > 0:
        lines.append(f"Discount: -{_fmt_usd(disc)}")
    comm = Decimal(str(inv.get("toters_commission_amount", 0)))
    if comm > 0:
        pct = inv.get("toters_commission_pct", 0)
        lines.append(f"Platform ({pct}%): -{_fmt_usd(comm)}")
    lines.append(f"TOTAL: {_fmt_usd(inv.get('net_total_usd', 0))}")
    lines.append(f"       {_fmt_lbp(inv.get('net_total_lbp', 0))}")
    if inv.get("change_given") is not None and Decimal(str(inv["change_given"])) > 0:
        lines.append(f"Change: {_fmt_usd(inv['change_given'])}")
    note = (inv.get("notes") or "").strip()
    if note:
        lines.append("------------------------")
        lines.append("Note:")
        lines.append(note)
    return lines


def print_receipt_thermal(inv: dict[str, Any]) -> dict[str, Any]:
    if not settings.thermal_print_enabled:
        return {"printed": False, "reason": "thermal printing disabled"}

    lines = _escpos_receipt_lines(inv)
    raw = _build_escpos_payload(lines)
    printer_name = _resolve_printer_name()

    if not printer_name:
        preview = "\n".join([SHOP_NAME, SHOP_TAGLINE, f"Tel: {SHOP_CONTACT}", *lines, "", CLOSING_MESSAGE, CLOSING_SUBMESSAGE])
        return {"printed": False, "reason": "no printer configured", "preview": preview}

    try:
        import win32print

        h = win32print.OpenPrinter(printer_name)
        try:
            win32print.StartDocPrinter(h, 1, ("BOB JUICE Receipt", None, "RAW"))
            win32print.StartPagePrinter(h)
            win32print.WritePrinter(h, raw)
            win32print.EndPagePrinter(h)
            win32print.EndDocPrinter(h)
        finally:
            win32print.ClosePrinter(h)
        logger.info("Thermal receipt sent to %s", printer_name)
        return {"printed": True, "printer": printer_name}
    except ImportError:
        preview = "\n".join([SHOP_NAME, *lines, "", CLOSING_MESSAGE])
        return {"printed": False, "reason": "pywin32 not installed", "preview": preview}
    except Exception as exc:
        logger.error("Thermal print failed: %s", exc)
        preview = "\n".join([SHOP_NAME, *lines, "", CLOSING_MESSAGE])
        return {"printed": False, "reason": str(exc), "preview": preview}
