"""
PDF generator for cotizaciones using fpdf2.
Produces a formal A4 document with company letterhead (logo + footer banner).
"""
import os
from datetime import datetime

from fpdf import FPDF

COMPANY_NAME  = "NEGOCIOS MULTIPLES LICHAN S.A.C."
COMPANY_RUC   = "20607960225"
COMPANY_ADDR  = "Av. Guillermo Billinghurts 1089-A, San Juan de Miraflores, Lima"
COMPANY_TEL   = "960-113-935"
COMPANY_EMAIL = "negocios.lichan@outlook.com"

_DIR         = os.path.dirname(__file__)
LOGO_PATH    = os.path.join(_DIR, "logo_negocios_multiples_lichan.png")
FOOTER_PATH  = os.path.join(_DIR, "footer_negocios_multiples_lichan.png")

# Brand red
_RED = (200, 30, 30)


class _CotizacionPDF(FPDF):
    def __init__(self, cot_number: str):
        super().__init__()
        self.cot_number = cot_number
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
        self.set_text_color(80, 80, 80)
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
            # Footer banner spans full width at bottom
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


def generate_cotizacion_pdf(extracted: dict, cot_number: str) -> bytes:
    """Return PDF bytes for the given cotización data."""
    date_str  = datetime.now().strftime("%d/%m/%Y")
    validity  = extracted.get("validity_days", 15)
    currency  = extracted.get("currency", "PEN")
    symbol    = "$" if currency == "USD" else "S/."
    inc_igv   = extracted.get("price_includes_igv", False)
    cust      = extracted.get("customer", {})
    items     = extracted.get("items", [])
    contact   = extracted.get("contact_persons") or cust.get("contact_person", "")

    pdf = _CotizacionPDF(cot_number)

    # ── Title ──────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*_RED)
    pdf.cell(0, 9, f"COTIZACIÓN  {cot_number}", align="C", ln=True)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 5, f"Fecha: {date_str}  |  Válido por: {validity} días calendario", align="C", ln=True)
    pdf.ln(4)

    # ── Client block ───────────────────────────────────────
    pdf.set_fill_color(245, 245, 245)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(0, 7, "  DATOS DEL CLIENTE", fill=True, ln=True)
    pdf.ln(1)

    def _row(label, value):
        if not value:
            return
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(32, 6, label, ln=False)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 6, str(value), ln=True)

    _row("Empresa:", cust.get("name", "").upper())
    _row("RUC:", cust.get("ruc"))
    _row("Atención:", contact)
    _row("Email:", cust.get("email"))
    pdf.ln(5)

    # ── Products table ─────────────────────────────────────
    col = [60, 38, 27, 27, 28]   # producto, presentacion, cant, precio, subtotal
    heads = ["PRODUCTO", "PRESENTACIÓN", "CANT.", f"P.UNIT ({symbol})", f"SUBTOTAL ({symbol})"]
    aligns = ["L", "L", "C", "R", "R"]

    pdf.set_fill_color(*_RED)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    for w, h in zip(col, heads):
        pdf.cell(w, 7, h, border=1, fill=True, align="C")
    pdf.ln()

    total_base = 0.0
    shade = False
    pdf.set_text_color(40, 40, 40)
    for item in items:
        qty   = float(item.get("quantity", 0))
        price = float(item.get("unit_price", 0))
        line  = qty * price
        total_base += line

        desc = item.get("description") or "?"
        pres = item.get("presentation") or ""
        unit = item.get("unit") or ""
        orig = item.get("origin") or ""
        if orig:
            desc = f"{desc}\nProc: {orig}"

        pdf.set_fill_color(250, 250, 250) if shade else pdf.set_fill_color(255, 255, 255)
        pdf.set_font("Helvetica", "", 8)

        row_h = 7
        vals  = [desc[:40], pres[:22], f"{qty:g} {unit}", f"{price:,.2f}", f"{line:,.2f}"]
        for w, v, a in zip(col, vals, aligns):
            pdf.cell(w, row_h, v, border=1, fill=shade, align=a)
        pdf.ln()
        shade = not shade

    # ── Totals ─────────────────────────────────────────────
    pdf.ln(3)
    if inc_igv:
        base_display = round(total_base / 1.18, 2)
        igv   = round(total_base - base_display, 2)
        total = total_base
    else:
        base_display = total_base
        igv   = round(total_base * 0.18, 2)
        total = round(total_base + igv, 2)

    right_x = 195 - col[3] - col[4]

    def _total_row(label, value, bold=False, color=(50, 50, 50)):
        pdf.set_x(right_x)
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        pdf.set_text_color(*color)
        pdf.cell(col[3], 6, label, align="R")
        pdf.cell(col[4], 6, value, align="R", ln=True)

    _total_row("SUBTOTAL:", f"{symbol} {base_display:,.2f}")
    _total_row("I.G.V. (18%):", f"{symbol} {igv:,.2f}")
    _total_row("TOTAL A PAGAR:", f"{symbol} {total:,.2f}", bold=True, color=_RED)

    pdf.ln(7)

    # ── Condiciones comerciales ────────────────────────────
    pdf.set_fill_color(245, 245, 245)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(0, 7, "  CONDICIONES COMERCIALES", fill=True, ln=True)
    pdf.ln(1)

    conditions = []
    global_origin = extracted.get("global_origin") or (items[0].get("origin") if items else None) or ""
    if global_origin:
        conditions.append(("Procedencia", global_origin))
    if extracted.get("delivery"):
        conditions.append(("Entrega", extracted["delivery"]))
    payment = extracted.get("payment_detail") or extracted.get("payment_terms")
    if payment:
        conditions.append(("Forma de pago", payment))
    conditions.append(("Validez", f"{validity} días calendario"))
    if extracted.get("notes"):
        conditions.append(("Observaciones", extracted["notes"]))

    for label, value in conditions:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(35, 6, f"  {label}:", ln=False)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 6, str(value), ln=True)

    pdf.ln(10)

    # ── Closing + signature ────────────────────────────────
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, "Quedo atento a su confirmación para generar la orden correspondiente.", ln=True)
    pdf.ln(10)

    pdf.set_font("Helvetica", "", 9)
    pdf.cell(65, 5, "_" * 28, ln=True)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*_RED)
    pdf.cell(65, 5, COMPANY_NAME, ln=True)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(65, 5, f"RUC: {COMPANY_RUC}", ln=True)

    return bytes(pdf.output())
