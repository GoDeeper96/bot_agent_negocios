"""
Bot Simulator API — runs the real bot state machine locally.
Uses real Gemini extraction. Mocks SUNAT, POS API, and email sending.

Run:
    cd simulator
    uvicorn api:app --reload --port 8765
"""
import logging
import os
import sys
from datetime import datetime
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Load Gemini key from ../.env.dev ─────────────────────────────────────────

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GEMINI_KEY = ""
_env_path = os.path.join(_root, ".env.dev")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if "=" in _line and not _line.startswith("#"):
                _k, _, _v = _line.partition("=")
                if _k.strip() == "GEMINI_API_KEY":
                    _GEMINI_KEY = _v.strip()

# Import GeminiClient (no boto3 — safe to import directly)
sys.path.insert(0, os.path.join(_root, "src", "processor"))
from claude_client import GeminiClient  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ─── In-memory session ────────────────────────────────────────────────────────

class Session:
    def __init__(self, sid: str, data: dict):
        self.sid             = sid
        self.state           = data.get("state", "idle")
        self.doc_type        = data.get("doc_type")
        self.messages        = data.get("messages", [])
        self.extracted       = data.get("extracted", {})
        self.last_cot_number = data.get("last_cot_number")
        self.last_email      = data.get("last_email")
        self.last_message_id = data.get("last_message_id")

    def add_message(self, text: str):
        self.messages.append(text)
        if len(self.messages) > 20:
            self.messages = self.messages[-20:]

    def to_dict(self):
        return {
            "state": self.state, "doc_type": self.doc_type,
            "messages": self.messages, "extracted": self.extracted,
            "last_cot_number": self.last_cot_number,
            "last_email": self.last_email,
            "last_message_id": self.last_message_id,
        }


class MemSessions:
    def __init__(self, store: dict, sid: str):
        self._store, self._sid = store, sid

    def save(self, s: Session):
        self._store[self._sid] = s.to_dict()

    def clear(self, _=None):
        self._store.pop(self._sid, None)


# ─── FastAPI ──────────────────────────────────────────────────────────────────

app = FastAPI(title="Bot Simulator")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

_store: dict = {}


class ChatReq(BaseModel):
    session_id: str = "demo"
    text: str


class ChatResp(BaseModel):
    messages: List[str]
    state: str


@app.post("/chat", response_model=ChatResp)
def chat(req: ChatReq):
    session = Session(req.session_id, _store.get(req.session_id, {}))
    out: list = []

    class WA:
        def send_text(self, _, t):
            out.append(t)

    _dispatch(
        req.session_id, req.text,
        session, MemSessions(_store, req.session_id),
        WA(), {"gemini_api_key": _GEMINI_KEY},
    )
    return ChatResp(messages=out, state=session.state)


@app.delete("/chat/{sid}")
def reset_session(sid: str):
    _store.pop(sid, None)
    return {"reset": True}


@app.get("/health")
def health():
    return {"ok": True, "gemini_key_set": bool(_GEMINI_KEY)}


# ─── Bot constants (copied from app.py) ───────────────────────────────────────

_CONFIRM_WORDS = {"si", "sí", "yes", "confirmar", "ok", "dale", "enviar"}
_CANCEL_WORDS  = {"no", "cancelar", "cancel", "nope", "0"}
_MENU_OPTIONS  = {
    "1": "factura", "2": "cotizacion", "3": "guia",
    "5": "orden_compra", "6": "stock",
}
_EXAMPLES_MENU = {"4.1", "4.2", "4.3", "4.4"}

MENU_TEXT = (
    "Hola 👋 ¿Qué deseas crear?\n\n"
    "1️⃣  Factura\n"
    "2️⃣  Cotización\n"
    "3️⃣  Guía de Remisión\n"
    "4️⃣  Ver ejemplos\n"
    "5️⃣  Orden de Compra\n"
    "6️⃣  Movimiento de Stock\n\n"
    "Responde con el número o envía directamente los datos.\n"
    "Escribe *0* en cualquier momento para cancelar."
)

_EXAMPLES_SUBMENU = (
    "*4️⃣ Ejemplos de uso*\n\n"
    "4️⃣.1️⃣  Ejemplo Factura\n"
    "4️⃣.2️⃣  Ejemplo Cotización\n"
    "4️⃣.3️⃣  Ejemplo Guía de Remisión\n"
    "4️⃣.4️⃣  Ejemplo Orden de Compra\n\n"
    "Responde con *4.1*, *4.2*, *4.3* o *4.4*"
)

_EXAMPLE_FACTURA = (
    "*📋 Ejemplo de Factura:*\n\n"
    "```\n"
    "Factura para IMPORTACIONES ABC S.A.C.\n"
    "RUC 20512345678\n"
    "correo: compras@importacionesabc.com\n\n"
    "- 50 bolsas arroz 50kg a $18.00 c/u\n"
    "- 20 cajas aceite vegetal a $45.00 c/u\n\n"
    "Precios más IGV\n"
    "```\n\n"
    "_Puedes enviarlo así o con tus propios datos._\n"
    "Escribe *1* para crear una factura."
)

_EXAMPLE_COTIZACION = (
    "*📋 Ejemplo de Cotización:*\n\n"
    "```\n"
    "Cotización para DISTRIBUIDORA NORTE S.R.L.\n"
    "RUC 20601234567\n"
    "Atención: Ing. Carlos Ruiz\n"
    "correo: carlos.ruiz@dnorte.com\n\n"
    "- Harina de trigo 100 sacos 50kg a $22.00\n"
    "- Azúcar rubia 80 bolsas 50kg a $28.00\n\n"
    "Precios más IGV\n"
    "Válido 15 días\n"
    "Pago: Contado (depósito en cuenta)\n"
    "```\n\n"
    "_Puedes enviarlo así o con tus propios datos._\n"
    "Escribe *2* para crear una cotización."
)

_EXAMPLE_GUIA = (
    "*📋 Ejemplo de Guía de Remisión:*\n\n"
    "```\n"
    "Guía de remisión para TRANSPORTES EL RAPIDO S.A.C.\n"
    "RUC 20512345678\n\n"
    "Bienes:\n"
    "- 50 bolsas arroz 50kg c/u\n"
    "- 20 cajas aceite vegetal\n"
    "Peso total: 2500 KG\n\n"
    "Partida: Cal. Los Eucaliptos Mza A Lote 5, Villa El Salvador, Lima\n"
    "Llegada: Av. Industrial 450, Ate, Lima\n"
    "Fecha traslado: 2026-04-28\n\n"
    "Conductor: Juan Perez Quispe\n"
    "DNI: 45678901\n"
    "Licencia: Q45678901\n"
    "Placa: ABC-123\n"
    "```\n\n"
    "_Puedes enviarlo así o con tus propios datos._\n"
    "Escribe *3* para crear una guía de remisión."
)

_EXAMPLE_ORDEN_COMPRA = (
    "*📋 Ejemplo de Orden de Compra:*\n\n"
    "```\n"
    "Orden de compra para CORPORACION INDUSTRIAL LOSARO S.A.C.\n"
    "RUC 20215195539\n"
    "Email: ventas@losaro.pe\n\n"
    "- Código A10000095 - Potasa Caustica (KG)\n"
    "  5,000 KG a $1.12 c/u\n\n"
    "Forma de pago: Contado contra entrega\n"
    "Lugar de entrega: Cal. Los Eucaliptos Mza A Lote 5, Villa El Salvador\n"
    "Tiempo de entrega: 25/02/2026\n"
    "Moneda: USD\n"
    "```\n\n"
    "_Puedes enviarlo así o con tus propios datos._\n"
    "Escribe *5* para crear una orden de compra."
)

_COMPANY_NAME  = "NEGOCIOS MULTIPLES LICHAN S.A.C."
_COMPANY_RUC   = "20607960225"
_MOVEMENT_TYPE_LABELS = {"entry": "Ingreso", "exit": "Salida", "adjustment": "Ajuste"}
_REASON_LABELS = {
    "purchase": "Compra", "return": "Devolución", "initial": "Stock inicial",
    "adjustment": "Ajuste", "sale": "Venta", "damage": "Daño",
    "expired": "Vencido", "correction": "Corrección",
}


# ─── Formatting helpers (copied verbatim from app.py) ─────────────────────────

def _fmt_price(price: float) -> str:
    s = f"{price:.4f}".rstrip("0")
    decimals = len(s) - s.index(".") - 1
    if decimals < 2:
        decimals = 2
    return f"{price:,.{decimals}f}"


def _detect_doc_intent(text_lower: str):
    if any(k in text_lower for k in ("orden de compra", "purchase order")):
        return "orden_compra"
    if any(k in text_lower for k in ("cotizacion", "cotización", "cotizar")):
        return "cotizacion"
    if any(k in text_lower for k in ("guia de remision", "guía de remisión", "guia remision")):
        return "guia"
    if any(k in text_lower for k in ("factura", "facturar")):
        return "factura"
    if "boleta" in text_lower:
        return "boleta"
    return None


def _format_missing(missing: list) -> str:
    labels = {
        "customer_name":  "👤 Nombre del cliente",
        "customer_ruc":   "🔢 RUC del cliente (requerido para factura)",
        "customer_doc":   "🔢 RUC o DNI del cliente",
        "customer_email": "📧 Correo del cliente (para envío de PDF)",
        "items":          "📦 Descripción de productos",
        "quantity":       "🔢 Cantidad del producto",
        "unit_price":     "💰 Precio unitario",
        "doc_type":       "📄 Tipo de documento (¿factura o boleta?)",
        "guia_receiver":  "👤 Nombre/RUC del destinatario",
        "guia_driver":    "🚗 Datos del conductor (nombre, DNI, licencia)",
        "guia_vehicle":   "🔑 Placa del vehículo",
        "guia_addresses": "📍 Dirección de partida y llegada",
        "guia_weight":    "⚖️ Peso total de los bienes (KG)",
        "guia_date":      "📅 Fecha de traslado",
    }
    items = [f"  • {labels.get(f, f)}" for f in missing]
    return "⚠️ *Falta información:*\n" + "\n".join(items) + "\n\nPor favor envíame los datos que faltan."


def _format_partial_preview(extracted: dict) -> str:
    lines = ["📋 *Datos extraídos hasta ahora:*\n"]
    cust = extracted.get("customer", {})
    if cust.get("name"):
        lines.append(f"👤 Cliente: {cust['name']}")
        if cust.get("ruc"):
            lines.append(f"   RUC: {cust['ruc']}")
        if cust.get("email"):
            lines.append(f"   Email: {cust['email']}")
    items = extracted.get("items", [])
    if items:
        lines.append("\n📦 Productos:")
        currency = extracted.get("currency", "USD")
        symbol = "$" if currency == "USD" else "S/."
        for item in items:
            qty   = item.get("quantity", "?")
            unit  = item.get("unit", "")
            price = item.get("unit_price", "?")
            lines.append(f"  • {item.get('description', '?')} — {qty} {unit} x {symbol}{price}")
    doc_type = extracted.get("doc_type", "unknown")
    if doc_type != "unknown":
        type_label = {"factura": "Factura", "boleta": "Boleta", "cotizacion": "Cotización"}
        lines.append(f"\n📄 Documento: {type_label.get(doc_type, doc_type)}")
    if extracted.get("notes"):
        lines.append(f"📝 Obs: {extracted['notes']}")
    return "\n".join(lines)


def _format_full_preview(extracted: dict) -> str:
    doc_type = extracted.get("doc_type", "unknown")
    type_label = {
        "factura":    "🧾 FACTURA",
        "boleta":     "🧾 BOLETA DE VENTA",
        "cotizacion": "📄 COTIZACIÓN",
        "guia":       "🚚 GUÍA DE REMISIÓN",
    }
    header = type_label.get(doc_type, "📄 DOCUMENTO")
    cust = extracted.get("customer", {})
    items = extracted.get("items", [])
    currency = extracted.get("currency", "USD")
    price_includes_igv = extracted.get("price_includes_igv", False)
    symbol = "$" if currency == "USD" else "S/."
    sep = "─────────────────────"
    lines = [
        f"*{header}*", sep,
        f"👤 *{cust.get('name', '-').upper()}*",
    ]
    if cust.get("ruc"):
        lines.append(f"   RUC: {cust['ruc']}")
    if cust.get("contact_person"):
        lines.append(f"   Attn: {cust['contact_person']}")
    if cust.get("email"):
        lines.append(f"   ✉️ {cust['email']}")
    lines += [sep, "📦 *PRODUCTOS*"]
    total_base = 0
    for i, item in enumerate(items, 1):
        qty   = float(item.get("quantity", 0))
        price = float(item.get("unit_price", 0))
        unit  = item.get("unit", "")
        desc  = item.get("description", "?")
        line_total = qty * price
        total_base += line_total
        lines.append(f"\n*{i}. {desc}*")
        if item.get("origin"):
            lines.append(f"   Proc: {item['origin']}")
        if item.get("presentation"):
            lines.append(f"   Pres: {item['presentation']}")
        lines.append(f"   {qty:g} {unit} × {symbol}{_fmt_price(price)} = {symbol}{line_total:,.2f}")
    lines.append(sep)
    if price_includes_igv:
        base_display = round(total_base / 1.18, 2)
        igv   = round(total_base - base_display, 2)
        total = round(total_base, 2)
    else:
        base_display = total_base
        igv   = round(total_base * 0.18, 2)
        total = round(total_base + igv, 2)
    lines.append(f"   Valor venta: {symbol}{base_display:.2f}")
    lines.append(f"   IGV (18%):   {symbol}{igv:.2f}")
    lines.append(f"   *TOTAL:      {symbol}{total:.2f} {currency}*")
    if extracted.get("delivery"):
        lines += [sep, f"🚚 {extracted['delivery']}"]
    if extracted.get("payment_terms"):
        lines.append(f"💳 {extracted['payment_terms']}")
    if extracted.get("notes"):
        lines.append(f"📝 Obs: {extracted['notes']}")
    return "\n".join(lines)


def _format_cotizacion_preview(extracted: dict) -> str:
    cust     = extracted.get("customer", {})
    items    = extracted.get("items", [])
    currency = extracted.get("currency", "PEN")
    inc_igv  = extracted.get("price_includes_igv", False)
    symbol   = "$" if currency == "USD" else "S/."
    contact  = extracted.get("contact_persons") or cust.get("contact_person", "")
    validity = extracted.get("validity_days")
    sep = "─────────────────────"
    lines = [
        "*📄 COTIZACIÓN*",
        f"*{_COMPANY_NAME}*",
        sep,
        f"*Para:* {cust.get('name', '').upper()}",
    ]
    if cust.get("ruc"):
        lines.append(f"*RUC:* {cust['ruc']}")
    if contact:
        lines.append(f"*Attn:* {contact}")
    if cust.get("email"):
        lines.append(f"*Email:* {cust['email']}")
    fecha_line = f"*Fecha:* {datetime.now().strftime('%d/%m/%Y')}"
    if validity:
        fecha_line += f"  |  *Válido:* {validity} días"
    lines += [fecha_line, sep, "*PRODUCTOS*"]
    total_base = 0
    for i, item in enumerate(items, 1):
        qty   = float(item.get("quantity", 0))
        price = float(item.get("unit_price", 0))
        unit  = item.get("unit", "")
        desc  = item.get("description", "?")
        line_total = qty * price
        total_base += line_total
        name_parts = [desc]
        if item.get("origin"):
            name_parts.append(f"Proc. {item['origin']}")
        if item.get("presentation"):
            name_parts.append(item["presentation"])
        lines.append(f"\n*{i}. {' - '.join(name_parts)}*")
        lines.append(f"   {qty:g} {unit}  ×  {symbol}{_fmt_price(price)} = {symbol}{line_total:,.2f}")
    lines.append(sep)
    if inc_igv:
        base = round(total_base / 1.18, 2)
        igv  = round(total_base - base, 2)
        tot  = round(total_base, 2)
    else:
        base = total_base
        igv  = round(total_base * 0.18, 2)
        tot  = round(total_base + igv, 2)
    lines.append(f"Valor venta: {symbol}{base:.2f}")
    lines.append(f"IGV (18%):   {symbol}{igv:.2f}")
    lines.append(f"*TOTAL:      {symbol}{tot:.2f} {currency}*")
    if extracted.get("payment_detail"):
        lines += [sep, f"💳 {extracted['payment_detail']}"]
    if extracted.get("delivery"):
        lines.append(f"🚚 {extracted['delivery']}")
    if extracted.get("notes"):
        lines.append(f"📝 {extracted['notes']}")
    return "\n".join(lines)


def _format_guia_preview(extracted: dict) -> str:
    guia = extracted.get("guia") or {}
    items = extracted.get("items", [])
    sep = "─────────────────────"
    lines = ["*🚚 GUÍA DE REMISIÓN*", sep]
    cust = extracted.get("customer", {})
    if cust.get("name"):
        lines.append(f"*Destinatario:* {cust['name'].upper()}")
    if guia.get("receiver_ruc"):
        lines.append(f"*RUC:* {guia['receiver_ruc']}")
    lines += [
        sep,
        f"*Fecha traslado:* {guia.get('transport_date', 'N/D')}",
        f"*Partida:* {guia.get('departure_address', 'N/D')}",
        f"*Llegada:* {guia.get('arrival_address', 'N/D')}",
        sep,
        "*Conductor:*",
        f"  {guia.get('driver_firstname','')} {guia.get('driver_lastname','')}",
        f"  DNI: {guia.get('driver_dni','N/D')}  Lic: {guia.get('driver_license','N/D')}",
        f"  Placa: {guia.get('vehicle_plate','N/D')}",
        sep,
        "*Bienes:*",
    ]
    for item in items:
        lines.append(f"  • {item.get('description','?')} — {item.get('quantity','?')} {item.get('unit','')}")
    if guia.get("total_weight_kg"):
        lines.append(f"*Peso total:* {guia['total_weight_kg']} KG")
    return "\n".join(lines)


def _format_orden_compra_preview(extracted: dict) -> str:
    cust     = extracted.get("customer", {})
    items    = extracted.get("items", [])
    currency = extracted.get("currency", "USD")
    symbol   = "$" if currency == "USD" else "S/."
    sep = "─────────────────────"
    lines = [
        "*📋 ORDEN DE COMPRA*",
        f"*{_COMPANY_NAME}*",
        sep,
        f"*Proveedor:* {cust.get('name','').upper()}",
    ]
    if cust.get("ruc"):
        lines.append(f"*RUC:* {cust['ruc']}")
    if cust.get("email"):
        lines.append(f"*Email:* {cust['email']}")
    if cust.get("phone"):
        lines.append(f"*Tel:* {cust['phone']}")
    lines += [sep, "*PRODUCTOS*"]
    total = 0
    for i, item in enumerate(items, 1):
        qty   = float(item.get("quantity", 0))
        price = float(item.get("unit_price", 0))
        unit  = item.get("unit", "")
        desc  = item.get("description", "?")
        line_total = qty * price
        total += line_total
        lines.append(f"\n*{i}. {desc}*")
        if item.get("sku"):
            lines.append(f"   Cód: {item['sku']}")
        lines.append(f"   {qty:g} {unit} × {symbol}{_fmt_price(price)} = {symbol}{line_total:,.2f}")
    lines.append(sep)
    lines.append(f"*TOTAL: {symbol}{total:,.2f} {currency}*")
    if extracted.get("payment_detail") or extracted.get("payment_terms"):
        lines.append(f"💳 {extracted.get('payment_detail') or extracted.get('payment_terms')}")
    if extracted.get("delivery"):
        lines.append(f"🚚 {extracted['delivery']}")
    if extracted.get("delivery_date"):
        lines.append(f"📅 Entrega: {extracted['delivery_date']}")
    return "\n".join(lines)


# ─── Dispatch (adapted from app.py) ───────────────────────────────────────────

def _dispatch(phone, text, session, sessions, wa, config):
    text_lower = text.lower().strip()

    if text_lower in _CANCEL_WORDS and session.state != "idle":
        sessions.clear()
        wa.send_text(phone, "Operación cancelada.\n\n" + MENU_TEXT)
        return

    if text_lower == "4":
        wa.send_text(phone, _EXAMPLES_SUBMENU)
        return
    if text_lower in _EXAMPLES_MENU:
        examples = {
            "4.1": _EXAMPLE_FACTURA, "4.2": _EXAMPLE_COTIZACION,
            "4.3": _EXAMPLE_GUIA, "4.4": _EXAMPLE_ORDEN_COMPRA,
        }
        wa.send_text(phone, examples[text_lower])
        return

    if session.state == "idle":
        if text_lower in _MENU_OPTIONS:
            selected = _MENU_OPTIONS[text_lower]
            if selected == "stock":
                session.state = "stock_collecting"
                sessions.save(session)
                wa.send_text(phone,
                    "📦 *Movimiento de Stock*\n\n"
                    "Envíame los datos del movimiento. Ejemplos:\n"
                    "• _Ingresé 5000 KG de Potasa Caustica, OC-2026-0001_\n"
                    "• _Recibí 1000 KG TRIPOLIFOSFATO DE SODIO, ref factura F001-123_\n\n"
                    "_Escribe *0* para cancelar._"
                )
                return
            session.doc_type = selected
            session.state = "collecting"
            sessions.save(session)
            wa.send_text(phone, f"*{session.doc_type.capitalize()}* seleccionada ✅\nEnvíame los datos del cliente y productos.")
        else:
            detected = _detect_doc_intent(text_lower)
            session.state = "collecting"
            session.add_message(text)
            if detected:
                session.doc_type = detected
                sessions.save(session)
                _handle_collecting(phone, None, session, sessions, wa, config)
            else:
                sessions.save(session)
                wa.send_text(phone, MENU_TEXT)
        return

    if session.state == "collecting":
        if text_lower in _MENU_OPTIONS:
            session.doc_type = _MENU_OPTIONS[text_lower]
            sessions.save(session)
            wa.send_text(phone, f"*{session.doc_type.capitalize()}* seleccionada ✅")
            _handle_collecting(phone, None, session, sessions, wa, config)
        else:
            _handle_collecting(phone, text, session, sessions, wa, config)

    elif session.state == "confirming":
        if text_lower in _CONFIRM_WORDS:
            _handle_submit_mock(phone, session, sessions, wa)
        else:
            _handle_collecting(phone, text, session, sessions, wa, config)

    elif session.state == "email_preview":
        if text_lower in _CONFIRM_WORDS:
            cot = session.last_cot_number or "COT-SIMULADA"
            email = session.last_email or "cliente@email.com"
            wa.send_text(phone,
                f"🎭 *[SIMULADOR]* En el bot real, aquí se generaría el PDF de {cot} "
                f"y se enviaría a *{email}* vía Amazon SES.\n\n✅ Email simulado con éxito."
            )
        else:
            wa.send_text(phone, "Entendido, no se enviará el email. ✅\n\n" + MENU_TEXT)
        sessions.clear()

    elif session.state == "oc_email_preview":
        if text_lower in _CONFIRM_WORDS:
            wa.send_text(phone,
                "🎭 *[SIMULADOR]* En el bot real, aquí se enviaría la Orden de Compra por email.\n\n✅ Email simulado."
            )
        else:
            wa.send_text(phone, "Entendido, no se enviará el email. ✅\n\n" + MENU_TEXT)
        sessions.clear()

    elif session.state == "stock_collecting":
        _handle_stock_collecting(phone, text, session, sessions, wa, config)

    elif session.state == "stock_confirming":
        if text_lower in _CONFIRM_WORDS:
            _handle_stock_submit_mock(phone, session, sessions, wa)
        else:
            sessions.clear()
            wa.send_text(phone, "Movimiento cancelado. ✅\n\n" + MENU_TEXT)


def _handle_collecting(phone, text, session, sessions, wa, config):
    if text:
        session.add_message(text)
    session.state = "collecting"

    gemini = GeminiClient(config["gemini_api_key"])
    try:
        extracted = gemini.extract_invoice_data(session.messages, doc_type=session.doc_type)
    except Exception as e:
        logger.error(f"Gemini extraction failed: {e}")
        wa.send_text(phone, "No pude entender el mensaje. ¿Puedes darme más detalles?")
        sessions.save(session)
        return

    if session.doc_type:
        extracted["doc_type"] = session.doc_type

    session.extracted = extracted
    missing = list(extracted.get("missing_fields", []))

    if session.doc_type and "doc_type" in missing:
        missing = [f for f in missing if f != "doc_type"]

    doc_now = extracted.get("doc_type")
    if doc_now in ("cotizacion", "boleta", "guia", "orden_compra"):
        missing = [f for f in missing if f != "customer_ruc"]
    if doc_now in ("cotizacion", "boleta"):
        cust = extracted.get("customer", {})
        if not cust.get("ruc") and not cust.get("dni"):
            missing = [f for f in missing if f != "customer_ruc"]
            if "customer_doc" not in missing:
                missing.append("customer_doc")
    if doc_now == "orden_compra":
        missing = [f for f in missing if f not in ("customer_ruc", "customer_doc", "customer_email")]
    missing = [f for f in missing if f != "customer_email"]
    if doc_now != "guia":
        missing = [f for f in missing if not f.startswith("guia_")]

    if missing:
        preview = _format_partial_preview(extracted)
        missing_msg = _format_missing(missing)
        wa.send_text(phone, f"{preview}\n\n{missing_msg}\n\n_Escribe *0* para cancelar._")
        sessions.save(session)
    else:
        session.state = "confirming"
        if doc_now == "cotizacion":
            preview = _format_cotizacion_preview(extracted)
            confirm_text = "¿Confirmar datos? Responde *sí* o *no*\n_Escribe *0* para cancelar._"
        elif doc_now == "guia":
            preview = _format_guia_preview(extracted)
            confirm_text = "¿Confirmar y emitir guía? Responde *sí* o *no*\n_Escribe *0* para cancelar._"
        elif doc_now == "orden_compra":
            preview = _format_orden_compra_preview(extracted)
            confirm_text = "¿Confirmar datos? Responde *sí* o *no*\n_Escribe *0* para cancelar._"
        else:
            preview = _format_full_preview(extracted)
            confirm_text = "¿Confirmar y enviar? Responde *sí* o *no*\n_Escribe *0* para cancelar._"
        wa.send_text(phone, f"{preview}\n\n{confirm_text}")
        sessions.save(session)


def _handle_submit_mock(phone, session, sessions, wa):
    """Mock submission — shows what would happen in production."""
    doc_type = session.doc_type or session.extracted.get("doc_type", "factura")
    doc_labels = {
        "factura": ("Factura", "F001-00000099"),
        "boleta":  ("Boleta", "B001-00000099"),
        "cotizacion": ("Cotización", "COT-2026-099"),
        "guia": ("Guía de Remisión", "T001-00000099"),
        "orden_compra": ("Orden de Compra", "OC-2026-099"),
    }
    label, number = doc_labels.get(doc_type, ("Documento", "DOC-001"))

    if doc_type == "cotizacion":
        session.last_cot_number = number
        session.last_email = session.extracted.get("customer", {}).get("email")
        customer_email = session.last_email
        if customer_email:
            session.state = "email_preview"
            sessions.save(session)
            wa.send_text(phone,
                f"✅ *{label} {number}* confirmada.\n\n"
                f"🎭 *[SIMULADOR]* En el bot real, se generaría el PDF y se mostraría el preview del email.\n\n"
                f"¿Enviar email a *{customer_email}*? Responde *sí* o *no*"
            )
            return
        else:
            wa.send_text(phone,
                f"✅ *{label} {number}* confirmada.\n\n"
                "🎭 *[SIMULADOR]* No hay email del cliente — en el bot real se completaría aquí."
            )
    elif doc_type == "orden_compra":
        customer_email = session.extracted.get("customer", {}).get("email")
        if customer_email:
            session.state = "oc_email_preview"
            sessions.save(session)
            wa.send_text(phone,
                f"✅ *{label} {number}* confirmada.\n\n"
                f"🎭 *[SIMULADOR]* En el bot real, se generaría el PDF de la OC.\n\n"
                f"¿Enviar email a *{customer_email}*? Responde *sí* o *no*"
            )
            return
        else:
            wa.send_text(phone,
                f"✅ *{label} {number}* confirmada.\n\n"
                "🎭 *[SIMULADOR]* En el bot real, se generaría y enviaría la OC en PDF."
            )
    else:
        wa.send_text(phone,
            f"🎭 *[SIMULADOR]* En el bot real, aquí se enviaría la {label} a SUNAT via apisunat.\n\n"
            f"✅ {label} *{number}* procesada correctamente (simulada)\n"
            "📄 PDF estaría disponible en el enlace de apisunat."
        )

    sessions.clear()
    wa.send_text(phone, "\n\n" + MENU_TEXT)


def _handle_stock_collecting(phone, text, session, sessions, wa, config):
    session.add_message(text)
    session.state = "stock_collecting"
    gemini = GeminiClient(config["gemini_api_key"])
    try:
        extracted = gemini.extract_stock_movement(session.messages)
    except Exception as e:
        logger.error(f"Gemini stock extraction failed: {e}")
        wa.send_text(phone, "No pude entender el movimiento. ¿Puedes darme más detalles?")
        sessions.save(session)
        return

    session.extracted = extracted
    missing = extracted.get("missing_fields", [])

    if missing:
        lines = ["⚠️ *Falta información:*"]
        if "product_name" in missing:
            lines.append("  • 📦 Nombre o SKU del producto")
        if "quantity" in missing:
            lines.append("  • 🔢 Cantidad")
        lines.append("\nPor favor envíame los datos que faltan.\n_Escribe *0* para cancelar._")
        wa.send_text(phone, "\n".join(lines))
        sessions.save(session)
        return

    session.state = "stock_confirming"
    sessions.save(session)

    mv_type = extracted.get("movement_type", "entry")
    reason  = extracted.get("reason", "purchase")
    product = extracted.get("product_name") or extracted.get("sku", "?")
    qty     = extracted.get("quantity", 0)
    unit    = extracted.get("unit", "KG")
    ref     = extracted.get("reference")
    notes   = extracted.get("notes")
    sku     = extracted.get("sku")

    sep   = "─────────────────────"
    emoji = "📥" if mv_type == "entry" else ("📤" if mv_type == "exit" else "🔄")
    lines = [
        f"*{emoji} {_MOVEMENT_TYPE_LABELS.get(mv_type, mv_type).upper()} DE STOCK*",
        sep,
        f"📦 *{product.upper()}*",
    ]
    if sku:
        lines.append(f"   SKU: {sku}")
    lines.append(f"🔢 Cantidad: *{qty:g} {unit}*")
    lines.append(f"📋 Motivo: {_REASON_LABELS.get(reason, reason)}")
    if ref:
        lines.append(f"🔖 Referencia: {ref}")
    if notes:
        lines.append(f"📝 Obs: {notes}")
    lines += [sep, "¿Confirmar movimiento? Responde *sí* o *no*\n_Escribe *0* para cancelar._"]
    wa.send_text(phone, "\n".join(lines))


def _handle_stock_submit_mock(phone, session, sessions, wa):
    extracted = session.extracted
    product   = extracted.get("product_name") or extracted.get("sku", "?")
    qty       = extracted.get("quantity", 0)
    unit      = extracted.get("unit", "KG")
    mv_type   = extracted.get("movement_type", "entry")
    emoji     = "📥" if mv_type != "exit" else "📤"
    action    = _MOVEMENT_TYPE_LABELS.get(mv_type, mv_type)
    wa.send_text(phone,
        f"🎭 *[SIMULADOR]* En el bot real, se registraría el movimiento en el inventario.\n\n"
        f"✅ {emoji} *{action}* simulada correctamente.\n"
        f"📦 {product}\n"
        f"🔢 {qty:g} {unit}"
    )
    sessions.clear()
    wa.send_text(phone, "\n\n" + MENU_TEXT)
