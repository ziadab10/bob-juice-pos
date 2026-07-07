"""Branded PDF reports via fpdf2 — compact Ministry-of-Finance audit layout."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from io import BytesIO
from typing import TYPE_CHECKING, Any

from inventory import parse_line_modifiers
from logo_assets import logo_pdf_png_bytes

if TYPE_CHECKING:
    from fpdf import FPDF

SHOP_CONTACT = "70 613 481"

_DARK = (17, 24, 39)
_GRAY = (75, 85, 99)
_MUTED = (107, 114, 128)
_BORDER = (203, 213, 225)
_HEADER_BG = (241, 245, 249)
_STRIPE = (248, 250, 252)
_PDF_LOGO_MM = 16  # square — keeps circular PNG from stretching
_RECEIPT_LOGO_MM = 21.0  # ~80px at 96dpi — prominent circular badge on POS receipts

_PDF_CLASS: type | None = None


def _embed_pdf_logo(pdf: "FPDF", *, x: float | None = None, y: float | None = None, size_mm: float = _PDF_LOGO_MM) -> bool:
    """Embed square circular logo badge (equal width/height prevents stretch)."""
    logo = logo_pdf_png_bytes()
    if not logo:
        return False
    try:
        pos_x = x if x is not None else (pdf.w - size_mm) / 2
        pos_y = y if y is not None else pdf.get_y()
        pdf.image(BytesIO(logo), x=pos_x, y=pos_y, w=size_mm, h=size_mm, type="PNG")
        return True
    except Exception:
        return False


def _safe(text) -> str:
    if text is None:
        return ""
    return str(text).encode("latin-1", "replace").decode("latin-1")


def _fmt_usd(val) -> str:
    return f"${Decimal(str(val)):,.2f}"


def _fmt_lbp(val) -> str:
    return f"{int(Decimal(str(val))):,} LBP"


def _fmt_lbp_from_usd(usd, rate) -> str:
    return _fmt_lbp(Decimal(str(usd)) * Decimal(str(rate)))


def _fmt_datetime(val) -> str:
    """Format datetime or ISO string for PDF rows — never slice datetime objects."""
    if val is None:
        return ""
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(val, "strftime"):
        try:
            return val.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    s = str(val).strip()
    if not s:
        return ""
    try:
        normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
        parsed = datetime.fromisoformat(normalized)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s[:19] if len(s) >= 19 else s


def _fmt_date(val) -> str:
    """Date-only label for ledger tables."""
    dt = _fmt_datetime(val)
    return dt[:10] if len(dt) >= 10 else dt


def _pdf_class() -> type:
    global _PDF_CLASS
    if _PDF_CLASS is not None:
        return _PDF_CLASS

    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise RuntimeError("fpdf2 is required for PDF export. Install with: pip install fpdf2") from exc

    class BobReportPDF(FPDF):
        def __init__(self, branch_name: str, report_title: str):
            super().__init__(orientation="P", unit="mm", format="A4")
            self.branch_name = branch_name
            self.report_title = report_title
            self.set_auto_page_break(auto=True, margin=12)
            self.set_margins(10, 22, 10)

        def header(self) -> None:
            logo_size = 11.0
            if _embed_pdf_logo(self, x=self.l_margin, y=4, size_mm=logo_size):
                tx = self.l_margin + logo_size + 3
            else:
                tx = self.l_margin
            self.set_xy(tx, 4)
            self.set_font("Helvetica", "B", 10)
            self.set_text_color(*_DARK)
            self.cell(0, 4, _safe(self.branch_name), new_x="LMARGIN", new_y="NEXT")
            self.set_x(tx)
            self.set_font("Helvetica", "", 7)
            self.set_text_color(*_MUTED)
            self.cell(0, 3.5, _safe(f"Tel: {SHOP_CONTACT}"), new_x="LMARGIN", new_y="NEXT")
            self.set_x(tx)
            self.set_font("Helvetica", "B", 8)
            self.set_text_color(*_GRAY)
            self.cell(0, 4, _safe(self.report_title), new_x="LMARGIN", new_y="NEXT")
            y = 17.5
            self.set_draw_color(*_BORDER)
            self.set_line_width(0.2)
            self.line(self.l_margin, y, self.w - self.r_margin, y)
            self.set_y(y + 2)

        def footer(self) -> None:
            self.set_y(-10)
            self.set_draw_color(*_BORDER)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.set_font("Helvetica", "I", 6)
            self.set_text_color(*_MUTED)
            self.cell(
                0,
                6,
                _safe(f"BOB JUICE — Internal Audit Copy  |  {SHOP_CONTACT}  |  Page {self.page_no()}"),
                align="C",
            )

        def meta_block(self, lines: list[str]) -> None:
            self.set_font("Helvetica", "", 6.5)
            self.set_text_color(*_MUTED)
            self.set_fill_color(*_HEADER_BG)
            self.set_draw_color(*_BORDER)
            y0 = self.get_y()
            self.rect(self.l_margin, y0, self.w - self.l_margin - self.r_margin, 4.5 * len(lines) + 2, style="DF")
            self.set_xy(self.l_margin + 2, y0 + 1.5)
            for line in lines:
                self.cell(0, 4, _safe(line), new_x="LMARGIN", new_y="NEXT")
                self.set_x(self.l_margin + 2)
            self.set_y(y0 + 4.5 * len(lines) + 3)

        def section_title(self, title: str) -> None:
            self.ln(0.5)
            self.set_font("Helvetica", "B", 7.5)
            self.set_text_color(*_DARK)
            self.cell(0, 4, _safe(title.upper()), new_x="LMARGIN", new_y="NEXT")
            self.ln(0.5)

        def summary_strip(self, items: list[tuple[str, str]]) -> None:
            if not items:
                return
            w = (self.w - self.l_margin - self.r_margin) / len(items)
            y0 = self.get_y()
            h = 9
            for i, (label, value) in enumerate(items):
                x = self.l_margin + i * w
                self.set_xy(x, y0)
                self.set_fill_color(*_HEADER_BG)
                self.set_draw_color(*_BORDER)
                self.rect(x, y0, w, h, style="DF")
                self.set_xy(x + 1.5, y0 + 1)
                self.set_font("Helvetica", "", 6)
                self.set_text_color(*_MUTED)
                self.cell(w - 3, 3, _safe(label.upper()))
                self.set_xy(x + 1.5, y0 + 4.5)
                self.set_font("Helvetica", "B", 8)
                self.set_text_color(*_DARK)
                self.cell(w - 3, 4, _safe(value))
            self.set_y(y0 + h + 1.5)

        def table(
            self,
            headers: list[str],
            rows: list[list[str]],
            col_widths: list[float] | None = None,
            right_cols: set[int] | None = None,
        ) -> None:
            right_cols = right_cols or set()
            usable = self.w - self.l_margin - self.r_margin
            if not col_widths:
                col_widths = [usable / len(headers)] * len(headers)
            row_h = 4.2
            head_h = 4.8

            def draw_row(cells: list[str], *, header: bool = False, stripe: bool = False) -> None:
                if self.get_y() + (head_h if header else row_h) > self.page_break_trigger:
                    self.add_page()
                h = head_h if header else row_h
                if header:
                    self.set_font("Helvetica", "B", 6.5)
                    self.set_fill_color(*_DARK)
                    self.set_text_color(255, 255, 255)
                else:
                    self.set_font("Helvetica", "", 6.5)
                    self.set_text_color(*_DARK)
                    self.set_fill_color(*(_STRIPE if stripe else (255, 255, 255)))
                x = self.l_margin
                y = self.get_y()
                for i, cell in enumerate(cells):
                    align = "R" if i in right_cols else "L"
                    self.set_xy(x, y)
                    self.cell(col_widths[i], h, _safe(cell)[:64], border=1, align=align, fill=True)
                    x += col_widths[i]
                self.set_y(y + h)

            draw_row(headers, header=True)
            for ri, row in enumerate(rows):
                draw_row(row, stripe=ri % 2 == 1)
            self.ln(1)

        def to_bytes(self) -> bytes:
            out = self.output(dest="S")
            if isinstance(out, bytes):
                return out
            if isinstance(out, bytearray):
                return bytes(out)
            return out.encode("latin-1")

    _PDF_CLASS = BobReportPDF
    return _PDF_CLASS


def _new_pdf(branch_name: str, report_title: str) -> Any:
    return _pdf_class()(branch_name, report_title)


def _stamp() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def build_sales_pdf(
    report: dict,
    expenses_total_usd: Decimal,
    expenses_total_lbp: Decimal,
    exchange_rate: Decimal,
    generated_by: str,
    *,
    channel_breakdown: list[dict] | None = None,
    sync_status: dict | None = None,
    inventory_consumption: list[dict] | None = None,
    supplier_debts: list[dict] | None = None,
    branch_name: str = "BOB JUICE",
) -> bytes:
    period = report.get("period", "daily").title()
    start = _fmt_date(report.get("start"))
    end = _fmt_date(report.get("end"))
    gross = Decimal(str(report.get("gross_total_usd", report.get("subtotal", "0"))))
    net_sales = Decimal(str(report.get("total", "0")))
    net_profit = net_sales - expenses_total_usd

    pdf = _new_pdf(branch_name, f"Sales & Performance Audit — {period}")
    pdf.add_page()
    pdf.meta_block([
        f"Reporting period: {start} to {end}",
        f"Generated: {_stamp()}  |  Prepared by: {generated_by}  |  FX: 1 USD = {int(exchange_rate):,} LBP",
        "Document classification: Internal financial audit — Ministry of Finance compliance format",
    ])
    pdf.summary_strip([
        ("Gross Sales", _fmt_usd(gross)),
        ("Net Revenue", _fmt_usd(net_sales)),
        ("Net Profit", _fmt_usd(net_profit)),
        ("Invoices", str(report.get("invoice_count", 0))),
    ])
    pdf.section_title("Revenue & Expense Summary")
    pdf.table(
        ["Description", "USD", "LBP"],
        [
            ["Gross Sales", _fmt_usd(gross), _fmt_lbp_from_usd(gross, exchange_rate)],
            ["Net Revenue", _fmt_usd(net_sales), _fmt_lbp_from_usd(net_sales, exchange_rate)],
            ["Operating Expenses", _fmt_usd(expenses_total_usd), _fmt_lbp(expenses_total_lbp)],
            ["Platform Commissions", f"-{_fmt_usd(report.get('total_commission_usd', 0))}", "—"],
            ["Net Profit (after expenses)", _fmt_usd(net_profit), _fmt_lbp_from_usd(net_profit, exchange_rate)],
        ],
        col_widths=[78, 54, 54],
        right_cols={1, 2},
    )

    pdf.section_title("Payment Methods")
    pay_rows = (report.get("payment_breakdown") or [])[:8]
    if not pay_rows:
        pay_rows = [
            {"label": "Cash", "total_usd": report.get("cash_total", 0)},
            {"label": "Wish", "total_usd": report.get("wish_total", 0)},
            {"label": "Card", "total_usd": report.get("card_total", 0)},
            {"label": "Mobile", "total_usd": report.get("mobile_total", 0)},
        ]
    pdf.table(
        ["Method", "USD", "LBP"],
        [
            [p.get("label", ""), _fmt_usd(p.get("total_usd", 0)), _fmt_lbp_from_usd(p.get("total_usd", 0), exchange_rate)]
            for p in pay_rows if Decimal(str(p.get("total_usd", 0))) > 0
        ] or [["No payment activity", "—", "—"]],
        col_widths=[78, 54, 54],
        right_cols={1, 2},
    )

    pdf.section_title("Delivery Platform Breakdown")
    ch_rows = [
        [
            ch.get("channel_label", ch.get("channel", "")),
            str(ch.get("order_count", 0)),
            _fmt_usd(ch.get("gross_usd", 0)),
            f"-{_fmt_usd(ch.get('commission_usd', 0))}",
            _fmt_usd(ch.get("net_usd", 0)),
        ]
        for ch in (channel_breakdown or [])
    ] or [["No delivery platform sales", "0", "$0.00", "$0.00", "$0.00"]]
    pdf.table(
        ["Channel", "Ord", "Gross USD", "Comm USD", "Net USD"],
        ch_rows,
        col_widths=[48, 16, 32, 32, 58],
        right_cols={1, 2, 3, 4},
    )

    sync = sync_status or {}
    pdf.section_title("Branch Sync Status")
    pdf.table(
        ["Branch", "Pending", "Failed", "Last Sync"],
        [[sync.get("branch_code", "—"), str(sync.get("pending_count", 0)), str(sync.get("failed_count", 0)), _fmt_datetime(sync.get("last_sync_at")) or "Never"]],
        col_widths=[46, 30, 30, 80],
        right_cols={1, 2},
    )

    pdf.section_title("Inventory Consumption (Period)")
    inv_rows = [
        [row.get("item_name", ""), f"{row.get('consumed', 0)} {row.get('unit', '')}", str(row.get("balance_after", "—"))]
        for row in (inventory_consumption or [])
    ] or [["No consumption recorded", "—", "—"]]
    pdf.table(["Ingredient", "Consumed", "Balance"], inv_rows, col_widths=[80, 53, 53], right_cols={1, 2})

    pdf.section_title("Outstanding Supplier Balances")
    debt_rows = [
        [d.get("supplier_name", ""), d.get("category", ""), _fmt_usd(d.get("balance_usd", 0)), _fmt_lbp(d.get("balance_lbp", 0))]
        for d in (supplier_debts or [])
    ] or [["No outstanding supplier balances", "—", "$0.00", "0 LBP"]]
    pdf.table(["Supplier", "Category", "USD", "LBP"], debt_rows, col_widths=[52, 40, 47, 47], right_cols={2, 3})

    return pdf.to_bytes()


def build_inventory_pdf(
    items: list[dict],
    recent_movements: list[dict],
    generated_by: str,
    *,
    branch_name: str = "BOB JUICE",
    branch_code: str = "-",
) -> bytes:
    total_skus = len(items)
    low_stock = sum(
        1 for i in items if Decimal(str(i.get("current_stock", 0))) <= Decimal(str(i.get("reorder_level", 0)))
    )
    total_units = sum(Decimal(str(i.get("current_stock", 0))) for i in items)

    pdf = _new_pdf(branch_name, "Inventory Status Audit")
    pdf.add_page()
    pdf.meta_block([
        f"Branch code: {branch_code}  |  Generated: {_stamp()}  |  Prepared by: {generated_by}",
        "Stock ledger extract — quantities per SKU with recent movement audit trail",
    ])
    pdf.summary_strip([
        ("Active SKUs", str(total_skus)),
        ("Low Stock", str(low_stock)),
        ("Units On Hand", f"{total_units:.3f}"),
    ])

    pdf.section_title("Current Stock Register")
    item_rows = []
    for i in items:
        stock = Decimal(str(i.get("current_stock", 0)))
        reorder = Decimal(str(i.get("reorder_level", 0)))
        status = "REORDER" if stock <= reorder and reorder > 0 else "OK"
        item_rows.append([i.get("name", ""), i.get("unit", "pcs"), f"{stock:.3f}", f"{reorder:.3f}", status])
    pdf.table(
        ["#", "Item", "Unit", "On Hand", "Reorder", "Status"],
        [[str(n + 1), *row] for n, row in enumerate(item_rows)] or [["—", "No inventory items", "—", "—", "—", "—"]],
        col_widths=[8, 58, 18, 28, 28, 26],
        right_cols={3, 4},
    )

    pdf.section_title("Recent Stock Movements")
    mov_rows = [
        [
            (m.get("created_at") or "")[:16].replace("T", " "),
            m.get("inventory_name", ""),
            (m.get("movement_type") or "").upper(),
            str(m.get("quantity", "")),
            str(m.get("balance_after", "")),
            (m.get("notes") or m.get("reference") or "—")[:40],
        ]
        for m in recent_movements[:60]
    ]
    pdf.table(
        ["Timestamp", "Item", "Type", "Qty", "Balance", "Reference"],
        mov_rows or [["—", "No movements", "—", "—", "—", "—"]],
        col_widths=[30, 40, 16, 20, 22, 58],
        right_cols={3, 4},
    )
    return pdf.to_bytes()


def build_expenses_pdf(
    expenses: list[dict],
    generated_by: str,
    *,
    period_label: str = "All Time",
    start: str = "",
    end: str = "",
    exchange_rate: Decimal = Decimal("89500"),
    branch_name: str = "BOB JUICE",
) -> bytes:
    total_usd = sum(Decimal(str(e.get("amount_usd", 0))) for e in expenses)
    total_lbp = sum(Decimal(str(e.get("amount_lbp", 0))) for e in expenses)
    by_category: dict[str, Decimal] = {}
    for e in expenses:
        cat = e.get("category", "other")
        by_category[cat] = by_category.get(cat, Decimal("0")) + Decimal(str(e.get("amount_usd", 0)))

    period_meta = f"{start} to {end}" if start and end else period_label

    pdf = _new_pdf(branch_name, f"Expenses Audit — {period_label.title()}")
    pdf.add_page()
    pdf.meta_block([
        f"Period: {period_meta}  |  Generated: {_stamp()}  |  By: {generated_by}",
        f"Exchange rate: 1 USD = {int(exchange_rate):,} LBP  |  Entries: {len(expenses)}",
    ])
    pdf.summary_strip([
        ("Total USD", _fmt_usd(total_usd)),
        ("Total LBP", _fmt_lbp(total_lbp)),
        ("Categories", str(len(by_category))),
    ])

    pdf.section_title("Category Summary")
    cat_rows = [
        [cat.replace("_", " ").title(), _fmt_usd(amt), _fmt_lbp_from_usd(amt, exchange_rate)]
        for cat, amt in sorted(by_category.items(), key=lambda x: -x[1])
    ]
    pdf.table(
        ["Category", "USD", "LBP"],
        cat_rows or [["No expenses", "$0.00", "0 LBP"]],
        col_widths=[78, 54, 54],
        right_cols={1, 2},
    )

    pdf.section_title("Expense Ledger")
    exp_rows = [
        [
            str(e.get("expense_date", "")),
            e.get("description", "")[:36],
            str(e.get("category", "")).replace("_", " ").title(),
            _fmt_usd(e.get("amount_usd", 0)),
            _fmt_lbp(e.get("amount_lbp", 0)),
            (e.get("notes") or "—")[:28],
        ]
        for e in expenses
    ]
    pdf.table(
        ["Date", "Description", "Category", "USD", "LBP", "Notes"],
        exp_rows or [["—", "No expenses recorded", "—", "$0.00", "0 LBP", "—"]],
        col_widths=[22, 44, 26, 26, 30, 38],
        right_cols={3, 4},
    )
    return pdf.to_bytes()


def build_customer_debts_pdf(
    customers: list[dict],
    entries: list[dict],
    generated_by: str,
    *,
    exchange_rate: Decimal = Decimal("89500"),
    branch_name: str = "BOB JUICE",
) -> bytes:
    total_receivable = sum(Decimal(str(c.get("balance_usd", 0))) for c in customers if Decimal(str(c.get("balance_usd", 0))) > 0)
    open_entries = sum(1 for e in entries if not e.get("is_settled"))

    pdf = _new_pdf(branch_name, "Customer Debts Audit (Receivables)")
    pdf.add_page()
    pdf.meta_block([
        f"Generated: {_stamp()}  |  Prepared by: {generated_by}",
        f"FX snapshot: 1 USD = {int(exchange_rate):,} LBP  |  Open entries: {open_entries}",
    ])
    pdf.summary_strip([
        ("Customers", str(len(customers))),
        ("Total Receivable", _fmt_usd(total_receivable)),
        ("Open Entries", str(open_entries)),
    ])

    pdf.section_title("Customer Balance Register")
    bal_rows = [
        [
            c.get("name", ""),
            c.get("contact_phone") or "—",
            _fmt_usd(c.get("balance_usd", 0)),
            _fmt_lbp(c.get("balance_lbp", 0)),
        ]
        for c in customers
    ]
    pdf.table(
        ["Customer", "Phone", "Balance USD", "Balance LBP"],
        bal_rows or [["No customers on record", "—", "$0.00", "0 LBP"]],
        col_widths=[62, 38, 43, 43],
        right_cols={2, 3},
    )

    pdf.section_title("Debt Transaction Ledger")
    entry_rows = [
        [
            _fmt_date(e.get("created_at")),
            e.get("customer_name", ""),
            e.get("description", "")[:32],
            _fmt_usd(e.get("amount_usd", 0)),
            "Paid" if e.get("is_settled") or e.get("is_paid") else "Unpaid",
        ]
        for e in entries
    ]
    pdf.table(
        ["Date", "Customer", "Description", "Amount USD", "Status"],
        entry_rows or [["—", "No debt entries", "—", "$0.00", "—"]],
        col_widths=[22, 38, 58, 34, 34],
        right_cols={3},
    )
    return pdf.to_bytes()


def build_supplier_debts_pdf(
    suppliers: list[dict],
    entries: list[dict],
    generated_by: str,
    *,
    exchange_rate: Decimal = Decimal("89500"),
    branch_name: str = "BOB JUICE",
) -> bytes:
    total_payable = sum(Decimal(str(s.get("balance_usd", 0))) for s in suppliers if Decimal(str(s.get("balance_usd", 0))) > 0)
    open_entries = sum(1 for e in entries if not e.get("is_settled"))

    pdf = _new_pdf(branch_name, "Supplier Debts Audit (Payables)")
    pdf.add_page()
    pdf.meta_block([
        f"Generated: {_stamp()}  |  Prepared by: {generated_by}",
        f"FX snapshot: 1 USD = {int(exchange_rate):,} LBP  |  Open entries: {open_entries}",
    ])
    pdf.summary_strip([
        ("Suppliers", str(len(suppliers))),
        ("Total Payable", _fmt_usd(total_payable)),
        ("Open Entries", str(open_entries)),
    ])

    pdf.section_title("Supplier Balance Register")
    bal_rows = [
        [
            s.get("name", ""),
            s.get("category", ""),
            s.get("contact_phone") or "—",
            _fmt_usd(s.get("balance_usd", 0)),
            _fmt_lbp(s.get("balance_lbp", 0)),
        ]
        for s in suppliers
    ]
    pdf.table(
        ["Supplier", "Category", "Phone", "Balance USD", "Balance LBP"],
        bal_rows or [["No suppliers on record", "—", "—", "$0.00", "0 LBP"]],
        col_widths=[44, 32, 30, 40, 40],
        right_cols={3, 4},
    )

    pdf.section_title("Debt Transaction Ledger")
    entry_rows = [
        [
            _fmt_date(e.get("created_at")),
            e.get("supplier_name", ""),
            e.get("description", "")[:32],
            _fmt_usd(e.get("amount_usd", 0)),
            "Paid" if e.get("is_settled") or e.get("is_paid") else "Unpaid",
        ]
        for e in entries
    ]
    pdf.table(
        ["Date", "Supplier", "Description", "Amount USD", "Status"],
        entry_rows or [["—", "No debt entries", "—", "$0.00", "—"]],
        col_widths=[22, 38, 58, 34, 34],
        right_cols={3},
    )
    return pdf.to_bytes()


def build_single_customer_bill_pdf(
    bill: dict,
    *,
    generated_by: str,
    branch_name: str = "BOB JUICE",
) -> bytes:
    invoice_id = bill.get("display_number") or bill.get("invoice_number") or bill.get("invoice_id") or bill.get("id")
    status = "Paid" if bill.get("is_paid") or bill.get("is_settled") else "Unpaid"
    pdf = _new_pdf(branch_name, f"Customer Bill #{invoice_id}")
    pdf.add_page()
    pdf.meta_block([
        f"Invoice #{invoice_id}  |  Customer: {bill.get('customer_name', '—')}",
        f"Generated: {_stamp()}  |  By: {generated_by}  |  Status: {status}",
    ])
    pdf.table(
        ["Field", "Value"],
        [
            ["Invoice #", str(invoice_id)],
            ["System ID", str(bill.get("invoice_number", invoice_id))],
            ["Reference #", bill.get("reference_number") or "—"],
            ["Customer", bill.get("customer_name", "—")],
            ["Phone", bill.get("contact_phone") or "—"],
            ["Date", _fmt_date(bill.get("created_at"))],
            ["Description", bill.get("description", "—")],
            ["Amount USD", _fmt_usd(bill.get("amount_usd", 0))],
            ["Amount LBP", _fmt_lbp(bill.get("amount_lbp", 0))],
            ["Payment Status", status],
        ],
        col_widths=[52, 134],
        right_cols=set(),
    )
    return pdf.to_bytes()


def build_single_supplier_bill_pdf(
    bill: dict,
    *,
    generated_by: str,
    branch_name: str = "BOB JUICE",
) -> bytes:
    invoice_id = bill.get("display_number") or bill.get("invoice_number") or bill.get("invoice_id") or bill.get("id")
    status = "Paid" if bill.get("is_paid") or bill.get("is_settled") else "Unpaid"
    pdf = _new_pdf(branch_name, f"Supplier Bill #{invoice_id}")
    pdf.add_page()
    pdf.meta_block([
        f"Invoice #{invoice_id}  |  Supplier: {bill.get('supplier_name', '—')}",
        f"Generated: {_stamp()}  |  By: {generated_by}  |  Status: {status}",
    ])
    pdf.table(
        ["Field", "Value"],
        [
            ["Invoice #", str(invoice_id)],
            ["System ID", str(bill.get("invoice_number", invoice_id))],
            ["Reference #", bill.get("reference_number") or "—"],
            ["Supplier", bill.get("supplier_name", "—")],
            ["Category", bill.get("category") or "—"],
            ["Phone", bill.get("contact_phone") or "—"],
            ["Date", _fmt_date(bill.get("created_at"))],
            ["Description", bill.get("description", "—")],
            ["Amount USD", _fmt_usd(bill.get("amount_usd", 0))],
            ["Amount LBP", _fmt_lbp(bill.get("amount_lbp", 0))],
            ["Payment Status", status],
        ],
        col_widths=[52, 134],
        right_cols=set(),
    )
    return pdf.to_bytes()


def build_pos_receipt_pdf(inv: dict[str, Any], *, branch_name: str = "BOB JUICE") -> bytes:
    """Compact POS sale receipt with circular logo, line items, and invoice note at bottom."""
    pdf = _new_pdf(branch_name, "Sale Receipt")
    pdf.add_page()
    logo_y = pdf.get_y()
    logo_x = (pdf.w - _RECEIPT_LOGO_MM) / 2
    if _embed_pdf_logo(pdf, x=logo_x, y=logo_y, size_mm=_RECEIPT_LOGO_MM):
        pdf.set_y(logo_y + _RECEIPT_LOGO_MM + 3)
    channel = (inv.get("sales_channel") or "in_store").replace("_", " ").title()
    pdf.meta_block([
        f"Invoice: {inv.get('invoice_number', '')}",
        f"Date: {_fmt_datetime(inv.get('finalized_at'))}  |  Channel: {channel}",
        f"Tel: {SHOP_CONTACT}",
    ])
    rows = []
    for item in inv.get("items") or []:
        qty = item.get("quantity", 1)
        rows.append([f"{qty}×", _safe(item.get("name_snapshot", ""))[:48], _fmt_usd(item.get("line_total", 0))])
        mods = parse_line_modifiers(item.get("line_modifiers_json"))
        for ex in mods.get("extras") or []:
            label = ex.get("name") or "Extra"
            price = Decimal(str(ex.get("extra_price_usd", 0)))
            rows.append(["", f"  + {label} ({_fmt_usd(price)})", ""])
        for ex in mods.get("excludes") or []:
            label = ex.get("name") or "Item"
            rows.append(["", f"  - No {label} ($0.00)", ""])
    pdf.table(
        ["Qty", "Item", "Total"],
        rows or [["—", "No items", "$0.00"]],
        col_widths=[14, 118, 54],
        right_cols={2},
    )
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_GRAY)
    pdf.cell(0, 5, _safe(f"Subtotal: {_fmt_usd(inv.get('subtotal_usd', 0))}  |  {_fmt_lbp(inv.get('subtotal_lbp', 0))}"), ln=1)
    disc = Decimal(str(inv.get("discount_amount_usd", 0)))
    if disc > 0:
        pdf.cell(0, 5, _safe(f"Discount: -{_fmt_usd(disc)}"), ln=1)
    comm = Decimal(str(inv.get("toters_commission_amount", 0)))
    if comm > 0:
        pct = inv.get("toters_commission_pct", 0)
        pdf.cell(0, 5, _safe(f"Platform ({pct}%): -{_fmt_usd(comm)}"), ln=1)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*_DARK)
    pdf.cell(0, 6, _safe(f"TOTAL: {_fmt_usd(inv.get('net_total_usd', 0))}  |  {_fmt_lbp(inv.get('net_total_lbp', 0))}"), ln=1)
    if inv.get("change_given") is not None and Decimal(str(inv["change_given"])) > 0:
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, _safe(f"Change: {_fmt_usd(inv['change_given'])}"), ln=1)
    note = (inv.get("notes") or "").strip()
    if note:
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, _safe("Invoice Note:"), ln=1)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, _safe(note))
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(*_MUTED)
    pdf.cell(0, 5, _safe("Thank you for choosing BOB JUICE!"), align="C", ln=1)
    return pdf.to_bytes()
