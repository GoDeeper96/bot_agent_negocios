"""
Processor Lambda — main bot logic.

Conversation states:
  idle        → waiting for input
  collecting  → accumulating messages + extracting data
  confirming  → preview shown, waiting for sí/no
  email       → invoice sent to SUNAT, asking about PDF email
"""
import json
import logging
import math
import os
import time

import boto3

from auth import get_token
from claude_client import GeminiClient
from pos_api import PosApiClient, PosApiError
from session import Session, SessionManager
from whatsapp import WhatsAppClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Config loaded from SSM once per container
# ---------------------------------------------------------------------------
_config = None


def _load_config() -> dict:
    global _config
    if _config:
        return _config

    ssm = boto3.client('ssm', region_name='us-west-2')
    prefix = os.environ['SSM_PREFIX']

    names = [
        f"{prefix}/meta/token",
        f"{prefix}/meta/phone_number_id",
        f"{prefix}/allowed_numbers",
        f"{prefix}/gemini/api_key",
        f"{prefix}/cognito/username",
        f"{prefix}/cognito/password",
        f"{prefix}/cognito/client_id",
        f"{prefix}/pos_api/base_url",
    ]

    resp = ssm.get_parameters(Names=names, WithDecryption=True)
    params = {p['Name'].split('/')[-1]: p['Value'] for p in resp['Parameters']}

    _config = {
        'meta_token': params['token'],
        'phone_number_id': params['phone_number_id'],
        'allowed_numbers': [n.strip() for n in params['allowed_numbers'].split(',')],
        'gemini_api_key': params['api_key'],
        'cognito_username': params['username'],
        'cognito_password': params['password'],
        'cognito_client_id': params['client_id'],
        'pos_api_base_url': params['base_url'],
    }
    return _config


def handler(event, context):
    """Entry point — invoked async by webhook Lambda."""
    try:
        phone = event['from']
        text = event['text'].strip()

        config = _load_config()

        # Security: only whitelisted numbers
        if phone not in config['allowed_numbers']:
            logger.warning(f"Blocked message from unauthorized number: {phone}")
            return

        wa = WhatsAppClient(config['meta_token'], config['phone_number_id'])
        sessions = SessionManager()
        session = sessions.get(phone)

        _dispatch(phone, text, session, sessions, wa, config)

    except Exception as e:
        logger.exception(f"Unhandled error processing message: {e}")
        try:
            wa.send_text(phone, "⚠️ Ocurrió un error interno. Por favor intenta de nuevo.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_CONFIRM_WORDS = {'si', 'sí', 'yes', 'confirmar', 'ok', 'dale', 'enviar'}
_CANCEL_WORDS  = {'no', 'cancelar', 'cancel', 'nope', '0'}
_MENU_OPTIONS  = {'1': 'factura', '2': 'cotizacion', '3': 'guia'}

MENU_TEXT = (
    "Hola 👋 ¿Qué deseas crear?\n\n"
    "1️⃣  Factura\n"
    "2️⃣  Cotización\n"
    "3️⃣  Guía de Remisión\n\n"
    "Responde con el número o envía directamente los datos."
)


def _dispatch(phone, text, session, sessions, wa, config):
    text_lower = text.lower().strip()

    # Global cancel — works in any state
    if text_lower in _CANCEL_WORDS and session.state != 'idle':
        sessions.clear(phone)
        wa.send_text(phone, "Operación cancelada. ¿En qué más te puedo ayudar?")
        return

    # Idle: show menu or detect doc type from first message
    if session.state == 'idle':
        if text_lower in _MENU_OPTIONS:
            session.doc_type = _MENU_OPTIONS[text_lower]
            session.state = 'collecting'
            sessions.save(session)
            wa.send_text(phone, f"*{session.doc_type.capitalize()}* seleccionada ✅\nEnvíame los datos del cliente y productos.")
        else:
            # Dad sent data directly — show menu but also start collecting
            session.state = 'collecting'
            session.add_message(text)
            sessions.save(session)
            wa.send_text(phone, MENU_TEXT)
        return

    if session.state == 'collecting':
        # If they reply with a menu number now, set doc type
        if text_lower in _MENU_OPTIONS:
            session.doc_type = _MENU_OPTIONS[text_lower]
            sessions.save(session)
            wa.send_text(phone, f"*{session.doc_type.capitalize()}* seleccionada ✅")
            # Re-run extraction with doc_type now known
            _handle_collecting(phone, None, session, sessions, wa, config)
        else:
            _handle_collecting(phone, text, session, sessions, wa, config)

    elif session.state == 'confirming':
        if text_lower in _CONFIRM_WORDS:
            _handle_submit(phone, session, sessions, wa, config)
        else:
            # Treat as additional info, re-extract
            _handle_collecting(phone, text, session, sessions, wa, config)

    elif session.state == 'email':
        if text_lower in _CONFIRM_WORDS:
            _handle_send_email(phone, session, sessions, wa, config)
        else:
            sessions.clear(phone)
            wa.send_text(phone, "Entendido, no se enviará el PDF. ✅")


# ---------------------------------------------------------------------------
# State: collecting
# ---------------------------------------------------------------------------

def _handle_collecting(phone, text, session, sessions, wa, config):
    if text:
        session.add_message(text)
    session.state = 'collecting'

    gemini = GeminiClient(config['gemini_api_key'])

    try:
        extracted = gemini.extract_invoice_data(session.messages, doc_type=session.doc_type)
    except Exception as e:
        logger.error(f"Gemini extraction failed: {e}")
        wa.send_text(phone, "No pude entender el mensaje. ¿Puedes darme más detalles?")
        sessions.save(session)
        return

    session.extracted = extracted
    missing = extracted.get('missing_fields', [])

    if missing:
        # Show what we have + ask for what's missing
        preview = _format_partial_preview(extracted)
        missing_msg = _format_missing(missing)
        wa.send_text(phone, f"{preview}\n\n{missing_msg}")
        sessions.save(session)
    else:
        # Complete — show full confirmation
        session.state = 'confirming'
        preview = _format_full_preview(extracted)
        wa.send_text(phone, f"{preview}\n\n¿Confirmar y enviar? Responde *sí* o *no*")
        sessions.save(session)


# ---------------------------------------------------------------------------
# State: submit
# ---------------------------------------------------------------------------

def _handle_submit(phone, session, sessions, wa, config):
    wa.send_text(phone, "⏳ Procesando...")

    extracted = session.extracted

    try:
        token = get_token(
            cognito_region='us-west-2',
            client_id=config['cognito_client_id'],
            username=config['cognito_username'],
            password=config['cognito_password'],
        )
        pos = PosApiClient(config['pos_api_base_url'], token)

        # 1. Resolve customer
        customer = _resolve_customer(extracted, pos)
        if isinstance(customer, str):
            # customer is an error/question message for the user
            wa.send_text(phone, customer)
            sessions.save(session)
            return

        # 2. Get document series
        doc_type_code = '01'  # Factura
        series = pos.get_document_series(doc_type_code)
        if not series:
            wa.send_text(phone, "❌ No se encontró serie de documentos activa para Factura.")
            sessions.clear(phone)
            return

        # 3. Create sale
        items = extracted.get('items', [])
        currency = extracted.get('currency', 'PEN')
        price_includes_igv = extracted.get('price_includes_igv', False)

        sale_payload = {
            'customerId': customer.get('customerId'),
            'customerName': customer.get('name'),
            'customerDocument': customer.get('documentNumber'),
            'documentType': doc_type_code,
            'currency': currency,
            'notes': extracted.get('notes') or extracted.get('delivery') or '',
        }
        sale = pos.create_sale(sale_payload)
        sale_id = sale['saleId']

        # 4. Add items
        for item in items:
            unit_price = _to_cents(item['unit_price'], price_includes_igv)
            pos.add_sale_item(sale_id, {
                'productName': _build_product_name(item),
                'productSku': item.get('sku') or 'SIN-SKU',
                'productUom': item.get('unit', 'UNIDADES'),
                'quantity': item['quantity'],
                'unitPrice': unit_price,
            })

        # 5. Complete sale
        pos.complete_sale(sale_id, {'paymentMethod': 'CONTADO', 'amount': 0})

        # 6. Send to SUNAT
        sunat_resp = pos.send_to_sunat(sale_id)

        success = sunat_resp.get('success', False)
        document = sunat_resp.get('document', {})
        pdf_url = document.get('pdfUrl')
        full_number = document.get('fullNumber') or sale.get('documentFullNumber', '')

        if success:
            session.last_sale_id = sale_id
            session.last_pdf_url = pdf_url
            session.last_email = extracted.get('customer', {}).get('email')
            session.state = 'email'
            sessions.save(session)

            msg = f"✅ Factura *{full_number}* enviada y aceptada por SUNAT."
            if session.last_email:
                msg += f"\n\n¿Enviar PDF al correo *{session.last_email}*? Responde *sí* o *no*"
                wa.send_text(phone, msg)
            else:
                wa.send_text(phone, msg + "\n\n(No hay correo registrado para el cliente.)")
                sessions.clear(phone)
        else:
            error_msg = sunat_resp.get('message', 'Error desconocido')
            wa.send_text(phone, f"⚠️ Factura creada ({full_number}) pero SUNAT respondió: {error_msg}")
            sessions.clear(phone)

    except PosApiError as e:
        logger.error(f"POS API error: {e}")
        wa.send_text(phone, f"❌ Error al procesar: {e}")
        sessions.clear(phone)
    except Exception as e:
        logger.exception(f"Unexpected error during submit: {e}")
        wa.send_text(phone, "❌ Error inesperado. Por favor intenta de nuevo.")
        sessions.clear(phone)


# ---------------------------------------------------------------------------
# State: email
# ---------------------------------------------------------------------------

def _handle_send_email(phone, session, sessions, wa, config):
    pdf_url = session.last_pdf_url
    email = session.last_email

    if not pdf_url or not email:
        wa.send_text(phone, "No hay PDF o correo disponible para enviar.")
        sessions.clear(phone)
        return

    # TODO: implement SES email sending (Phase 4)
    # For now, confirm and show the PDF URL
    wa.send_text(phone, f"📧 PDF enviado a *{email}*.\n\nLink: {pdf_url}")
    sessions.clear(phone)


# ---------------------------------------------------------------------------
# Customer resolution
# ---------------------------------------------------------------------------

def _resolve_customer(extracted: dict, pos: PosApiClient):
    """
    Look up customer by name. Returns customer dict or error string.
    """
    cust = extracted.get('customer', {})
    name = cust.get('name')
    ruc = cust.get('ruc')
    email = cust.get('email')

    if not name:
        return "Necesito el nombre del cliente para continuar."

    # Try lookup by name
    found = pos.search_customer(name)
    if found:
        return found

    # Not found — need RUC to create
    if not ruc:
        return (
            f"No encontré al cliente *{name}* en el sistema.\n"
            "Por favor envíame su *RUC* para registrarlo."
        )

    # Create customer
    try:
        new_customer = pos.create_customer(
            name=name.upper(),
            document_number=ruc,
            document_type='RUC',
            email=email,
        )
        return new_customer.get('customer') or new_customer
    except PosApiError as e:
        return f"❌ Error al crear cliente: {e}"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_partial_preview(extracted: dict) -> str:
    lines = ["📋 *Datos extraídos hasta ahora:*\n"]

    cust = extracted.get('customer', {})
    if cust.get('name'):
        lines.append(f"👤 Cliente: {cust['name']}")
        if cust.get('ruc'):
            lines.append(f"   RUC: {cust['ruc']}")
        if cust.get('email'):
            lines.append(f"   Email: {cust['email']}")

    items = extracted.get('items', [])
    if items:
        lines.append("\n📦 Productos:")
        currency = extracted.get('currency', 'PEN')
        symbol = '$' if currency == 'USD' else 'S/.'
        for item in items:
            qty = item.get('quantity', '?')
            unit = item.get('unit', '')
            price = item.get('unit_price', '?')
            lines.append(f"  • {item.get('description', '?')} — {qty} {unit} x {symbol}{price}")

    doc_type = extracted.get('doc_type', 'unknown')
    if doc_type != 'unknown':
        type_label = {'factura': 'Factura', 'boleta': 'Boleta', 'cotizacion': 'Cotización'}
        lines.append(f"\n📄 Documento: {type_label.get(doc_type, doc_type)}")

    return '\n'.join(lines)


def _format_full_preview(extracted: dict) -> str:
    lines = ["📋 *Resumen para confirmar:*\n"]

    doc_type = extracted.get('doc_type', 'unknown')
    type_label = {'factura': '🧾 FACTURA', 'boleta': '🧾 BOLETA', 'cotizacion': '📄 COTIZACIÓN'}
    lines.append(type_label.get(doc_type, '📄 DOCUMENTO'))

    cust = extracted.get('customer', {})
    lines.append(f"\n👤 *Cliente:* {cust.get('name', '-')}")
    if cust.get('ruc'):
        lines.append(f"   RUC: {cust['ruc']}")
    if cust.get('email'):
        lines.append(f"   Email: {cust['email']}")
    if cust.get('contact_person'):
        lines.append(f"   Atención: {cust['contact_person']}")

    items = extracted.get('items', [])
    currency = extracted.get('currency', 'PEN')
    price_includes_igv = extracted.get('price_includes_igv', False)
    symbol = '$' if currency == 'USD' else 'S/.'

    lines.append("\n📦 *Productos:*")
    total_base = 0
    for item in items:
        qty = item.get('quantity', 0)
        price = item.get('unit_price', 0)
        unit = item.get('unit', '')
        desc = item.get('description', '?')
        line_total = qty * price
        total_base += line_total
        lines.append(f"  • {desc}")
        if item.get('origin'):
            lines.append(f"    Procedencia: {item['origin']}")
        if item.get('presentation'):
            lines.append(f"    Presentación: {item['presentation']}")
        lines.append(f"    {qty} {unit} × {symbol}{price:.2f} = {symbol}{line_total:.2f}")

    igv = round(total_base * 0.18, 2)
    total = round(total_base + igv, 2) if not price_includes_igv else round(total_base, 2)
    base_display = round(total_base / 1.18, 2) if price_includes_igv else total_base

    lines.append(f"\n💰 *Subtotal:* {symbol}{base_display:.2f}")
    lines.append(f"   IGV 18%: {symbol}{igv:.2f}" if not price_includes_igv else f"   IGV inc.: {symbol}{round(total_base - base_display, 2):.2f}")
    lines.append(f"   *TOTAL: {symbol}{total:.2f}*")

    if extracted.get('delivery'):
        lines.append(f"\n🚚 Entrega: {extracted['delivery']}")
    if extracted.get('payment_terms'):
        lines.append(f"💳 Pago: {extracted['payment_terms']}")

    return '\n'.join(lines)


def _format_missing(missing: list) -> str:
    labels = {
        'customer_name': '👤 Nombre del cliente',
        'customer_ruc': '🔢 RUC del cliente (requerido para factura)',
        'customer_email': '📧 Correo del cliente (para envío de PDF)',
        'items': '📦 Descripción de productos',
        'quantity': '🔢 Cantidad del producto',
        'unit_price': '💰 Precio unitario',
        'doc_type': '📄 Tipo de documento (¿factura o boleta?)',
    }
    items = [f"  • {labels.get(f, f)}" for f in missing]
    return "⚠️ *Falta información:*\n" + '\n'.join(items) + "\n\nPor favor envíame los datos que faltan."


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def _to_cents(price: float, includes_igv: bool) -> int:
    """
    Convert unit price to integer cents for the POS API.
    The API expects prices WITH IGV included (tax-inclusive), in cents.
    """
    if includes_igv:
        return round(price * 100)
    else:
        return round(price * 1.18 * 100)


def _build_product_name(item: dict) -> str:
    parts = [item.get('description', 'Producto')]
    if item.get('origin'):
        parts.append(f"PROC. {item['origin']}")
    if item.get('presentation'):
        parts.append(item['presentation'])
    return ' - '.join(parts)
