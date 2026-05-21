"""
PDF generator for Ordenes de Compra using fpdf2.
Lichan issues the OC as buyer; the supplier/provider is the recipient.
"""
import os
from datetime import datetime

from fpdf import FPDF


def _fmt_price(price: float) -> str:
    s = f"{price:.4f}".rstrip('0')
    decimals = len(s) - s.index('.') - 1
    if decimals < 2:
        decimals = 2
    return f"{price:,.{decimals}f}"


# ── Spanish number-to-words ────────────────────────────────────────────────
_ONES = [
    '', 'UN', 'DOS', 'TRES', 'CUATRO', 'CINCO', 'SEIS', 'SIETE', 'OCHO', 'NUEVE',
    'DIEZ', 'ONCE', 'DOCE', 'TRECE', 'CATORCE', 'QUINCE', 'DIECISÉIS', 'DIECISIETE',
    'DIECIOCHO', 'DIECINUEVE', 'VEINTE', 'VEINTIUNO', 'VEINTIDÓS', 'VEINTITRÉS',
    'VEINTICUATRO', 'VEINTICINCO', 'VEINTISÉIS', 'VEINTISIETE', 'VEINTIOCHO', 'VEINTINUEVE',
]
_TENS     = ['', '', 'VEINTE', 'TREINTA', 'CUARENTA', 'CINCUENTA',
             'SESENTA', 'SETENTA', 'OCHENTA', 'NOVENTA']
_HUNDREDS = ['', 'CIEN', 'DOSCIENTOS', 'TRESCIENTOS', 'CUATROCIENTOS', 'QUINIENTOS',
             'SEISCIENTOS', 'SETECIENTOS', 'OCHOCIENTOS', 'NOVECIENTOS']


def _int_to_words(n: int) -> str:
    if n == 0:
        return 'CERO'
    if n < 30:
        return _ONES[n]
    if n < 100:
        d, u = divmod(n, 10)
        return _TENS[d] + (' Y ' + _ONES[u] if u else '')
    if n < 1000:
        h, rest = divmod(n, 100)
        base = 'CIEN' if n == 100 else _HUNDREDS[h]
        return base + (' ' + _int_to_words(rest) if rest else '')
    if n < 1_000_000:
        t, rest = divmod(n, 1000)
        prefix = 'MIL' if t == 1 else _int_to_words(t) + ' MIL'
        return prefix + (' ' + _int_to_words(rest) if rest else '')
    m, rest = divmod(n, 1_000_000)
    prefix = 'UN MILLÓN' if m == 1 else _int_to_words(m) + ' MILLONES'
    return prefix + (' ' + _int_to_words(rest) if rest else '')


def _amount_in_words(total: float, currency: str) -> str:
    cents   = round((total % 1) * 100)
    integer = int(total)
    name    = 'Dólares Americanos' if currency == 'USD' else 'Soles'
    return f"{_int_to_words(integer)} CON {cents:02d}/100 {name}"


# ── Constants ──────────────────────────────────────────────────────────────
COMPANY_NAME  = "NEGOCIOS MULTIPLES LICHAN S.A.C."
COMPANY_RUC   = "20607960225"
COMPANY_ADDR  = "AV. GUILLERMO BILLINGHURST NRO. 1089 URB. SAN JUAN ZN. D- SAN JUAN DE MIRAFLORES - LIMA - LIMA"
COMPANY_TEL   = "960113935"
COMPANY_EMAIL = "negocios.lichan@outlook.com"

_DIR        = os.path.dirname(__file__)
LOGO_PATH   = os.path.join(_DIR, "logo_negocios_multiples_lichan.png")
FOOTER_PATH = os.path.join(_DIR, "footer_negocios_multiples_lichan.png")

_RED  = (139, 26, 26)
_GRAY = (100, 100, 100)
_DARK = (40, 40, 40)
_LGRAY = (230, 230, 230)


class _OcPDF(FPDF):
    def __init__(self, oc_number: str):
        super().__init__()
        self.oc_number = oc_number
        self.set_margins(15, 45, 15)
        self.set_auto_page_break(auto=True, margin=28)
        self.add_page()

    def header(self):
        if os.path.exists(LOGO_PATH):
            self.image(LOGO_PATH, x=15, y=8, w=40)
        self.set_xy(60, 8)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*_RED)
        self.cell(0, 6, COMPANY_NAME, ln=True)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_GRAY)
        self.set_x(60)
        self.cell(0, 4.5, f"RUC: {COMPANY_RUC}", ln=True)
        self.set_x(60)
        self.cell(0, 4.5, COMPANY_ADDR, ln=True)
        self.set_x(60)
        self.cell(0, 4.5, f"Telf: {COMPANY_TEL}  |  {COMPANY_EMAIL}", ln=True)
        self.set_draw_color(*_RED)
        self.set_line_width(0.5)
        self.line(15, 40, 195, 40)

    def footer(self):
        if os.path.exists(FOOTER_PATH):
            self.image(FOOTER_PATH, x=15, y=self.h - 22, w=180)
        else:
            self.set_y(-18)
            self.set_draw_color(*_RED)
            self.set_line_width(0.3)
            self.line(15, self.get_y(), 195, self.get_y())
            self.ln(2)
            self.set_font("Helvetica", "I", 7)
            self.set_text_color(150, 150, 150)
            self.cell(0, 5,
                f"{COMPANY_NAME}  |  RUC: {COMPANY_RUC}  |  {COMPANY_ADDR}  |  Tel: {COMPANY_TEL}",
                align="C")


def generate_orden_compra_pdf(extracted: dict, oc_number: str) -> bytes:
    """Return PDF bytes for the given Orden de Compra data."""
    date_str  = datetime.now().strftime("%d / %m / %Y")
    currency  = extracted.get("currency", "USD")
    symbol    = "$" if currency == "USD" else "S/."
    inc_igv   = extracted.get("price_includes_igv", False)
    supplier  = extracted.get("customer", {})
    items     = extracted.get("items", [])

    pdf = _OcPDF(oc_number)

    # ── Title ──────────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*_RED)
    pdf.cell(0, 10, "ORDEN DE COMPRA", align="C", ln=True)
    pdf.ln(2)

    # ── Date + OC Number row ───────────────────────────────────────────────
    pdf.set_draw_color(60, 60, 60)
    pdf.set_line_width(0.3)
    pdf.set_fill_color(*_LGRAY)

    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*_DARK)
    pdf.cell(25, 8, "FECHA", border=1, fill=True, align="C")
    pdf.set_font("Helvetica", "", 8.5)
    pdf.cell(45, 8, date_str, border=1, align="C")
    pdf.cell(15, 8, "", border=0)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.cell(42, 8, "No. DE ORDEN :", border=1, fill=True, align="C")
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*_RED)
    pdf.cell(0, 8, f"  {oc_number}", border=1, ln=True)
    pdf.set_text_color(*_DARK)
    pdf.ln(4)

    # ── Info block helper ──────────────────────────────────────────────────
    _LABEL_W = 40
    _VALUE_W = 140   # 180 - 40
    _ROW_H   = 6.5
    _X0      = 15    # left margin

    def _info_row(label: str, value: str):
        if not value:
            return
        text = f":  {value}"
        pdf.set_font("Helvetica", "", 8.5)

        if pdf.get_string_width(text) <= _VALUE_W:
            # Single line — fast path
            pdf.set_font("Helvetica", "B", 8.5)
            pdf.set_text_color(50, 50, 50)
            pdf.cell(_LABEL_W, _ROW_H, f"  {label}", border="LR", ln=False)
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(*_DARK)
            pdf.cell(_VALUE_W, _ROW_H, text, border="R", ln=True)
        else:
            # Multi-line: draw value first to measure height, then draw label
            y0 = pdf.get_y()
            pdf.set_xy(_X0 + _LABEL_W, y0)
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(*_DARK)
            pdf.multi_cell(_VALUE_W, _ROW_H, text, border="R", align="L")
            y1 = pdf.get_y()

            n_lines = max(1, round((y1 - y0) / _ROW_H))
            pdf.set_xy(_X0, y0)
            pdf.set_font("Helvetica", "B", 8.5)
            pdf.set_text_color(50, 50, 50)
            for i in range(n_lines):
                pdf.cell(_LABEL_W, _ROW_H, f"  {label}" if i == 0 else "", border="LR", ln=True)
                pdf.set_x(_X0)

            pdf.set_xy(_X0, y1)

    # ── FACTURACIÓN block (Lichan as buyer) ────────────────────────────────
    pdf.set_fill_color(*_LGRAY)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*_DARK)
    pdf.cell(0, 7, "  FACTURACIÓN", border=1, fill=True, ln=True)

    _info_row("FACTURACION", COMPANY_NAME)
    _info_row("RUC", COMPANY_RUC)
    _info_row("DIRECCION", COMPANY_ADDR)
    payment = extracted.get("payment_detail") or extracted.get("payment_terms") or ""
    _info_row("FORMA DE PAGO", payment.upper() if payment else "")
    delivery = extracted.get("delivery") or ""
    _info_row("LUGAR DE ENTREGA", delivery.upper() if delivery else "")
    delivery_date = extracted.get("delivery_date") or ""
    _info_row("TIEMPO DE ENTREGA", delivery_date)

    # Close box
    pdf.set_draw_color(60, 60, 60)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(4)

    # ── PROVEEDOR block (Supplier) ─────────────────────────────────────────
    pdf.set_fill_color(*_LGRAY)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*_DARK)
    pdf.cell(0, 7, "  PROVEEDOR", border=1, fill=True, ln=True)

    _info_row("RAZON SOCIAL", (supplier.get("name") or "").upper())
    _info_row("DIRECCION", (supplier.get("address") or "").upper())
    _info_row("RUC", supplier.get("ruc") or "")
    contact = supplier.get("contact_person") or extracted.get("contact_persons") or ""
    _info_row("CONTACTO", contact)
    _info_row("TELEFONO", supplier.get("phone") or "")
    _info_row("EMAIL", supplier.get("email") or "")

    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(5)

    # ── Items table ────────────────────────────────────────────────────────
    # Widths: ITE=10, CODIG=22, CANT=18, UND=14, DESC=72, PRECIO=24, TOTAL=20 → 180
    col     = [10, 22, 18, 14, 72, 24, 20]
    heads   = ["ITE", "CODIG", "CANT.", "UND.", "DESCRIPCIÓN", f"P.UNIT ({symbol})", f"TOTAL ({symbol})"]
    aligns  = ["C", "C", "C", "C", "L", "R", "R"]

    pdf.set_fill_color(*_RED)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 7.5)
    for w, h, a in zip(col, heads, aligns):
        pdf.cell(w, 7, h, border=1, fill=True, align=a)
    pdf.ln()

    total_base = 0.0
    shade = False
    pdf.set_text_color(*_DARK)
    for idx, item in enumerate(items, 1):
        qty   = float(item.get("quantity", 0))
        price = float(item.get("unit_price", 0))
        line  = qty * price
        total_base += line

        desc  = item.get("description") or "?"
        sku   = item.get("sku") or ""
        unit  = item.get("unit") or ""

        pdf.set_fill_color(250, 250, 250) if shade else pdf.set_fill_color(255, 255, 255)
        pdf.set_font("Helvetica", "", 8)
        vals = [str(idx).zfill(3), sku[:10], f"{qty:g}", unit, desc[:55],
                _fmt_price(price), f"{line:,.2f}"]
        for w, v, a in zip(col, vals, aligns):
            pdf.cell(w, 7, v, border=1, fill=shade, align=a)
        pdf.ln()
        shade = not shade

    # ── Totals ─────────────────────────────────────────────────────────────
    if inc_igv:
        base_display = round(total_base / 1.18, 2)
        igv   = round(total_base - base_display, 2)
        total = total_base
    else:
        base_display = total_base
        igv   = round(total_base * 0.18, 2)
        total = round(total_base + igv, 2)

    left_w  = sum(col[:5])   # 136
    right_w = col[5]          # 24
    last_w  = col[6]          # 20

    words_line = _amount_in_words(total, currency)

    # Son + IGV row
    pdf.set_font("Helvetica", "I", 7.5)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(left_w, 6, f"Son :  {words_line[:70]}", border="LB", ln=False)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*_DARK)
    pdf.cell(right_w, 6, "IGV", border=1, align="R")
    pdf.cell(last_w,  6, f"{igv:,.2f}", border=1, align="R", ln=True)

    # TOTAL row
    pdf.cell(left_w, 7, "", border="LB", ln=False)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*_RED)
    pdf.cell(right_w, 7, "TOTAL", border=1, align="R")
    pdf.cell(last_w,  7, f"{total:,.2f}", border=1, align="R", ln=True)

    # Close left column bottom border
    pdf.set_text_color(*_DARK)
    pdf.cell(left_w, 0, "", border="B", ln=True)

    pdf.ln(5)

    # ── Observación ────────────────────────────────────────────────────────
    notes = extracted.get("notes") or ""
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*_DARK)
    pdf.cell(0, 6, "Observación:", ln=True)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(60, 60, 60)
    if notes:
        pdf.multi_cell(0, 5, notes)
    else:
        pdf.ln(8)

    pdf.ln(18)

    # ── Signatures ─────────────────────────────────────────────────────────
    pdf.set_draw_color(60, 60, 60)
    pdf.set_line_width(0.3)
    sig_w = 80

    pdf.cell(sig_w, 0, "", border="T")
    pdf.cell(20, 0, "")
    pdf.cell(sig_w, 0, "", border="T", ln=True)
    pdf.ln(1)

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_DARK)
    supplier_name = (supplier.get("name") or "PROVEEDOR").upper()
    pdf.cell(sig_w, 5, COMPANY_NAME, align="C")
    pdf.cell(20, 5, "")
    pdf.cell(sig_w, 5, supplier_name, align="C", ln=True)

    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*_GRAY)
    pdf.cell(sig_w, 5, f"RUC: {COMPANY_RUC}", align="C")
    pdf.cell(20, 5, "")
    pdf.cell(sig_w, 5, f"RUC: {supplier.get('ruc') or ''}", align="C", ln=True)

    return bytes(pdf.output())
