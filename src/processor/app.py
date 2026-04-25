"""
Processor Lambda — main bot logic.

Conversation states:
  idle          → waiting for input
  collecting    → accumulating messages + extracting data
  confirming    → preview shown, waiting for sí/no
  email_preview → cotización confirmed, email preview shown, waiting for sí/no
  email         → invoice sent to SUNAT, asking about PDF email
"""
import json
import logging
import math
import os
import time
from datetime import datetime

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
        f"{prefix}/sunat/persona_id",
        f"{prefix}/sunat/persona_token",
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
        'sunat_persona_id': params.get('persona_id', ''),
        'sunat_persona_token': params.get('persona_token', ''),
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
    "Responde con el número o envía directamente los datos.\n"
    "Escribe *0* en cualquier momento para cancelar."
)


def _dispatch(phone, text, session, sessions, wa, config):
    text_lower = text.lower().strip()

    # Global cancel / back to menu — works in any state
    if text_lower in _CANCEL_WORDS and session.state != 'idle':
        sessions.clear(phone)
        wa.send_text(phone, "Operación cancelada.\n\n" + MENU_TEXT)
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

    elif session.state == 'email_preview':
        if text_lower in _CONFIRM_WORDS:
            _handle_send_cotizacion_email(phone, session, sessions, wa, config)
        else:
            sessions.clear(phone)
            wa.send_text(phone, "Entendido, no se enviará el email. ✅\n\n" + MENU_TEXT)

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

    # Session doc_type always wins over Gemini's guess
    if session.doc_type:
        extracted['doc_type'] = session.doc_type

    session.extracted = extracted
    missing = extracted.get('missing_fields', [])

    # Remove doc_type from missing if already set by user
    if session.doc_type and 'doc_type' in missing:
        missing = [f for f in missing if f != 'doc_type']

    # Email is optional — never block the flow on it
    missing = [f for f in missing if f != 'customer_email']

    if missing:
        preview = _format_partial_preview(extracted)
        missing_msg = _format_missing(missing)
        wa.send_text(phone, f"{preview}\n\n{missing_msg}\n\n_Escribe *0* para cancelar._")
        sessions.save(session)
    else:
        session.state = 'confirming'
        is_cotizacion = extracted.get('doc_type') == 'cotizacion'
        if is_cotizacion:
            preview      = _format_cotizacion_preview(extracted)
            confirm_text = "¿Confirmar datos? Responde *sí* o *no*\n_Escribe *0* para cancelar._"
        else:
            preview      = _format_full_preview(extracted)
            confirm_text = "¿Confirmar y enviar? Responde *sí* o *no*\n_Escribe *0* para cancelar._"
        wa.send_text(phone, f"{preview}\n\n{confirm_text}")
        sessions.save(session)


# ---------------------------------------------------------------------------
# State: submit
# ---------------------------------------------------------------------------

def _handle_submit(phone, session, sessions, wa, config):
    if (session.doc_type or session.extracted.get('doc_type')) == 'cotizacion':
        _handle_cotizacion_confirmed(phone, session, sessions, wa, config)
        return

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
        doc_type_map  = {'factura': '01', 'boleta': '03'}
        doc_type_code = doc_type_map.get(session.doc_type or extracted.get('doc_type', ''), '01')
        doc_label     = 'Factura' if doc_type_code == '01' else 'Boleta'
        series = pos.get_document_series(doc_type_code)
        if not series:
            wa.send_text(phone, f"❌ No se encontró serie de documentos activa para {doc_label}.")
            sessions.clear(phone)
            return

        # 3. Create sale
        items = extracted.get('items', [])
        currency = extracted.get('currency', 'USD')
        price_includes_igv = extracted.get('price_includes_igv', False)

        sale_payload = {
            'customerId':       customer.get('customerId'),
            'customerName':     customer.get('name'),
            'customerDocument': customer.get('documentNumber'),
            'documentType':     doc_type_code,
            'currency':         currency,
            'warehouseId':      'warehouse-lichan',
            'notes':            extracted.get('notes') or extracted.get('delivery') or '',
        }
        sale = pos.create_sale(sale_payload)
        sale_id = sale['saleId']

        # 4. Add items — unitPrice in sale currency (API converts to cents internally)
        for item in items:
            base_price = float(item['unit_price'])
            # API expects price WITH IGV included; use 4 decimals to avoid precision loss
            # on USD prices like 1.40 × 1.18 = 1.652 (would round to 1.65 at 2 decimals)
            unit_price_with_igv = round(base_price if price_includes_igv else base_price * 1.18, 4)
            pos.add_sale_item(sale_id, {
                'productId':   item.get('sku') or 'product-freehand-lichan',
                'productName': _build_product_name(item),
                'productSku':  item.get('sku') or 'LIBRE',
                'productUom':  item.get('unit', 'UNIDADES'),
                'quantity':    float(item['quantity']),
                'unitPrice':   unit_price_with_igv,
            })

        # 5. Add payment (amount in SOLES, API converts to cents internally)
        total_soles = sum(
            float(i['unit_price']) * float(i['quantity']) * (1 if price_includes_igv else 1.18)
            for i in items
        )
        total_soles = round(total_soles, 2)
        pos.add_payment(sale_id, {
            'paymentMethod':  'cash',
            'amount':         total_soles,
            'receivedAmount': total_soles,
        })

        # 6. Complete sale — assigns document number and submits to SUNAT internally
        complete_resp = pos.complete_sale(sale_id, {})
        logger.info(f"complete_sale response: {str(complete_resp)[:400]}")

        full_number  = complete_resp.get('documentNumber') or complete_resp.get('documentFullNumber', '')
        sunat_status = complete_resp.get('sunatStatus', '')   # 'accepted', 'pending', 'rejected', None
        sunat_msg    = complete_resp.get('sunatMessage', '') or ''
        pdf_url      = complete_resp.get('pdfUrl') or ''
        logger.info(f"Parsed: full_number={full_number} sunatStatus={sunat_status} pdfUrl={pdf_url}")

        session.last_sale_id    = sale_id
        session.last_email      = extracted.get('customer', {}).get('email')
        session.last_pdf_url    = pdf_url
        session.last_xml_url       = complete_resp.get('xmlUrl') or ''
        session.last_sunat_doc_id  = complete_resp.get('sunatDocumentId') or ''
        session.last_full_number = full_number
        session.last_doc_label  = doc_label

        sessions.save(session)

        sunat_ok = sunat_status in ('accepted', 'sent', 'pending')

        if sunat_ok or (full_number and not sunat_msg):
            session.state = 'email'
            sessions.save(session)
            msg = f"✅ {doc_label} *{full_number}* enviada a SUNAT."
            if pdf_url:
                msg += f"\n\n📄 PDF: {pdf_url}"
            if session.last_email:
                msg += f"\n\n¿Enviar por correo a *{session.last_email}*? Responde *sí* o *no*"
                wa.send_text(phone, msg)
            else:
                wa.send_text(phone, msg)
                sessions.clear(phone)
        else:
            msg = f"✅ {doc_label} *{full_number}* creada."
            if sunat_msg:
                msg += f"\n⚠️ SUNAT: {sunat_msg}"
            wa.send_text(phone, msg)
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

def _fetch_sunat_doc(full_number: str, sunat_doc_id: str, persona_id: str, persona_token: str) -> dict:
    """
    Fetch XML and PDF URLs from apisunat.
    Tries getById first (if sunatDocumentId stored), then falls back to getAll by serie+number.
    Returns dict with 'xml' and 'pdf_url' keys.
    """
    import requests as _req

    def _parse(data: dict) -> dict:
        doc_id = data.get('id') or sunat_doc_id
        file_name = data.get('fileName', '')
        pdf_url = (data.get('pdf') or {}).get('A4')
        if not pdf_url and doc_id and file_name:
            pdf_url = f"https://back.apisunat.com/documents/{doc_id}/getPDF/A4/{file_name}.PDF"
        return {'xml': data.get('xml'), 'pdf_url': pdf_url}

    if sunat_doc_id:
        try:
            resp = _req.get(
                f"https://back.apisunat.com/documents/{sunat_doc_id}/getById",
                params={"personaId": persona_id, "personaToken": persona_token},
                timeout=10,
            )
            if resp.ok:
                return _parse(resp.json())
        except Exception as e:
            logger.warning(f"getById failed: {e}")

    # Fallback: look up by serie + number via getAll
    if full_number and '-' in full_number:
        serie, number = full_number.split('-', 1)
        try:
            resp = _req.get(
                "https://back.apisunat.com/documents/getAll",
                params={"personaId": persona_id, "personaToken": persona_token,
                        "serie": serie, "number": number, "limit": 1},
                timeout=10,
            )
            if resp.ok:
                docs = resp.json()
                if docs:
                    return _parse(docs[0])
        except Exception as e:
            logger.warning(f"getAll fallback failed: {e}")

    return {'xml': None, 'pdf_url': None}


def _handle_send_email(phone, session, sessions, wa, config):
    from email_client import send_factura_email

    pdf_url       = session.last_pdf_url
    xml_url       = session.last_xml_url
    email         = session.last_email
    full_number   = session.last_full_number or ''
    doc_label     = session.last_doc_label or 'Factura'
    sunat_doc_id  = session.last_sunat_doc_id or ''

    if not email:
        wa.send_text(phone, "No hay correo registrado para el cliente.")
        sessions.clear(phone)
        return

    # Fetch XML (and updated PDF URL) from apisunat
    if config.get('sunat_persona_id') and config.get('sunat_persona_token'):
        doc_data = _fetch_sunat_doc(full_number, sunat_doc_id, config['sunat_persona_id'], config['sunat_persona_token'])
        xml_url  = doc_data.get('xml') or xml_url
        pdf_url  = doc_data.get('pdf_url') or pdf_url
        logger.info(f"apisunat doc fetch: xml={xml_url} pdf={pdf_url}")

    wa.send_text(phone, f"📧 Enviando {doc_label} {full_number} a *{email}*...")

    ok = send_factura_email(
        to_email=email,
        doc_label=doc_label,
        full_number=full_number,
        pdf_url=pdf_url,
        xml_url=xml_url or None,
        ssm_prefix=os.environ['SSM_PREFIX'],
    )

    if ok:
        wa.send_text(phone, f"✅ {doc_label} enviada a *{email}*.")
    else:
        wa.send_text(phone, f"⚠️ No se pudo enviar el correo a {email}. El PDF está en:\n{pdf_url}")

    sessions.clear(phone)


# ---------------------------------------------------------------------------
# Customer resolution
# ---------------------------------------------------------------------------

def _resolve_customer(extracted: dict, pos: PosApiClient):
    """Look up customer by name. Returns customer dict or an error/question string."""
    cust  = extracted.get('customer', {})
    name  = cust.get('name')
    ruc   = cust.get('ruc')
    dni   = cust.get('dni')
    email = cust.get('email')

    doc_number = ruc or dni
    doc_type   = 'RUC' if ruc else ('DNI' if dni else None)

    if not name:
        return "Necesito el nombre del cliente para continuar."

    # Try lookup by name first
    found = pos.search_customer(name)
    if found:
        return found

    # Not found — need a document number to create
    if not doc_number:
        doc_hint = '*RUC* (factura) o *DNI* (boleta)'
        return (
            f"No encontré al cliente *{name}* en el sistema.\n"
            f"Por favor envíame su {doc_hint} para registrarlo."
        )

    try:
        new_customer = pos.create_customer(
            name=name.upper(),
            document_number=doc_number,
            document_type=doc_type,
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
        currency = extracted.get('currency', 'USD')
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
    doc_type = extracted.get('doc_type', 'unknown')
    type_label = {
        'factura':    '🧾 FACTURA',
        'boleta':     '🧾 BOLETA DE VENTA',
        'cotizacion': '📄 COTIZACIÓN',
        'guia':       '🚚 GUÍA DE REMISIÓN',
    }
    header = type_label.get(doc_type, '📄 DOCUMENTO')

    cust = extracted.get('customer', {})
    items = extracted.get('items', [])
    currency = extracted.get('currency', 'USD')
    price_includes_igv = extracted.get('price_includes_igv', False)
    symbol = '$' if currency == 'USD' else 'S/.'

    sep = "─────────────────────"
    lines = [
        f"*{header}*",
        sep,
        f"👤 *{cust.get('name', '-').upper()}*",
    ]
    if cust.get('ruc'):
        lines.append(f"   RUC: {cust['ruc']}")
    if cust.get('contact_person'):
        lines.append(f"   Attn: {cust['contact_person']}")
    if cust.get('email'):
        lines.append(f"   ✉️ {cust['email']}")

    lines.append(sep)
    lines.append("📦 *PRODUCTOS*")

    total_base = 0
    for i, item in enumerate(items, 1):
        qty = float(item.get('quantity', 0))
        price = float(item.get('unit_price', 0))
        unit = item.get('unit', '')
        desc = item.get('description', '?')
        line_total = qty * price
        total_base += line_total

        lines.append(f"\n*{i}. {desc}*")
        if item.get('origin'):
            lines.append(f"   Proc: {item['origin']}")
        if item.get('presentation'):
            lines.append(f"   Pres: {item['presentation']}")
        lines.append(f"   {qty:g} {unit} × {symbol}{price:.2f} = {symbol}{line_total:.2f}")

    lines.append(sep)

    if price_includes_igv:
        base_display = round(total_base / 1.18, 2)
        igv = round(total_base - base_display, 2)
        total = round(total_base, 2)
    else:
        base_display = total_base
        igv = round(total_base * 0.18, 2)
        total = round(total_base + igv, 2)

    lines.append(f"   Valor venta: {symbol}{base_display:.2f}")
    lines.append(f"   IGV (18%):   {symbol}{igv:.2f}")
    lines.append(f"   *TOTAL:      {symbol}{total:.2f} {currency}*")

    if extracted.get('delivery'):
        lines.append(sep)
        lines.append(f"🚚 {extracted['delivery']}")
    if extracted.get('payment_terms'):
        lines.append(f"💳 {extracted['payment_terms']}")

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

def _to_cents(price, includes_igv: bool) -> int:
    price = float(price)
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


# ---------------------------------------------------------------------------
# Cotización flow
# ---------------------------------------------------------------------------

_COMPANY_NAME  = "NEGOCIOS MULTIPLES LICHAN S.A.C."
_COMPANY_RUC   = "20607960225"
_COMPANY_TEL   = "960-113-935"
_COMPANY_EMAIL = "negocios.lichan@outlook.com"


def _next_cotizacion_number() -> str:
    """Atomically increment SSM counter and return COT-YYYY-NNN."""
    ssm        = boto3.client('ssm', region_name='us-west-2')
    param_name = f"{os.environ['SSM_PREFIX']}/cotizacion_counter"
    try:
        resp = ssm.get_parameter(Name=param_name)
        n = int(resp['Parameter']['Value']) + 1
    except ssm.exceptions.ParameterNotFound:
        n = 1
    ssm.put_parameter(Name=param_name, Value=str(n), Type='String', Overwrite=True)
    return f"COT-{datetime.now().year}-{n:03d}"


def _handle_cotizacion_confirmed(phone, session, sessions, wa, config):
    """Dad confirmed cotización data → assign number, show email preview."""
    cot_number = _next_cotizacion_number()
    session.last_cot_number = cot_number
    session.last_email = session.extracted.get('customer', {}).get('email')

    customer_email = session.last_email

    if not customer_email:
        sessions.clear(phone)
        wa.send_text(phone,
            f"✅ Cotización *{cot_number}* confirmada.\n\n"
            "No hay email del cliente registrado. Puedes reenviar el texto directamente.")
        return

    session.state = 'email_preview'
    sessions.save(session)
    wa.send_text(phone, _format_email_preview(session.extracted, cot_number))


def _handle_send_cotizacion_email(phone, session, sessions, wa, config):
    """Dad confirmed email preview → generate PDF and send via SES."""
    from cotizacion_pdf import generate_cotizacion_pdf
    from email_client import send_cotizacion_email

    extracted  = session.extracted
    cot_number = session.last_cot_number
    to_email   = session.last_email

    if not to_email or not cot_number:
        wa.send_text(phone, "❌ No hay email o número de cotización disponible.")
        sessions.clear(phone)
        return

    wa.send_text(phone, "⏳ Generando PDF y enviando...")

    try:
        pdf_bytes    = generate_cotizacion_pdf(extracted, cot_number)
        pdf_filename = f"{cot_number}.pdf"
        cust_name    = extracted.get('customer', {}).get('name', 'Cliente').upper()
        subject      = f"Cotización {cot_number} | {cust_name}"

        ok = send_cotizacion_email(
            to_email=to_email,
            subject=subject,
            body_text=_build_email_body(extracted, cot_number),
            pdf_bytes=pdf_bytes,
            pdf_filename=pdf_filename,
            ssm_prefix=os.environ['SSM_PREFIX'],
        )
        if ok:
            wa.send_text(phone, f"✅ Cotización *{cot_number}* enviada a *{to_email}*")
        else:
            wa.send_text(phone, "❌ No se pudo enviar el email. Intenta de nuevo.")
    except Exception as e:
        logger.exception(f"Error sending cotizacion email: {e}")
        wa.send_text(phone, "❌ Error al generar o enviar la cotización.")
    finally:
        sessions.clear(phone)


# ---------------------------------------------------------------------------
# Cotización formatters
# ---------------------------------------------------------------------------

def _format_cotizacion_preview(extracted: dict) -> str:
    """WhatsApp message 1 — cotización data preview before dad confirms."""
    cust      = extracted.get('customer', {})
    items     = extracted.get('items', [])
    currency  = extracted.get('currency', 'PEN')
    inc_igv   = extracted.get('price_includes_igv', False)
    symbol    = '$' if currency == 'USD' else 'S/.'
    contact   = extracted.get('contact_persons') or cust.get('contact_person', '')
    validity  = extracted.get('validity_days', 15)

    sep = "─────────────────────"
    lines = [
        f"*📄 COTIZACIÓN*",
        f"*{_COMPANY_NAME}*",
        sep,
        f"*Para:* {cust.get('name', '').upper()}",
    ]
    if cust.get('ruc'):
        lines.append(f"*RUC:* {cust['ruc']}")
    if contact:
        lines.append(f"*Attn:* {contact}")
    if cust.get('email'):
        lines.append(f"*Email:* {cust['email']}")

    fecha_line = f"*Fecha:* {datetime.now().strftime('%d/%m/%Y')}"
    if validity:
        fecha_line += f"  |  *Válido:* {validity} días"
    lines += [fecha_line,
        sep,
        "*PRODUCTOS*",
    ]

    total_base = 0.0
    for i, item in enumerate(items, 1):
        qty   = float(item.get('quantity', 0))
        price = float(item.get('unit_price', 0))
        line  = qty * price
        total_base += line
        lines.append(f"\n*{i}. {item.get('description', '?')}*")
        if item.get('origin'):
            lines.append(f"   Proc: {item['origin']}")
        if item.get('presentation'):
            lines.append(f"   Pres: {item['presentation']}")
        lines.append(f"   {qty:g} {item.get('unit','')} × {symbol}{price:,.2f} = {symbol}{line:,.2f}")

    lines.append(sep)

    if inc_igv:
        base_display = round(total_base / 1.18, 2)
        igv   = round(total_base - base_display, 2)
        total = total_base
    else:
        base_display = total_base
        igv   = round(total_base * 0.18, 2)
        total = round(total_base + igv, 2)

    lines += [
        f"   Subtotal:     {symbol}{base_display:,.2f}",
        f"   I.G.V. (18%): {symbol}{igv:,.2f}",
        f"   *TOTAL:       {symbol}{total:,.2f} {currency}*",
        sep,
    ]

    global_origin = extracted.get('global_origin') or (items[0].get('origin') if items else None)
    if global_origin:
        lines.append(f"📦 Procedencia: {global_origin}")
    if extracted.get('delivery'):
        lines.append(f"🚚 Entrega: {extracted['delivery']}")
    payment = extracted.get('payment_detail') or extracted.get('payment_terms')
    if payment:
        lines.append(f"💳 Pago: {payment}")

    no_pres = [i.get('description', '?') for i in items if not i.get('presentation')]
    if no_pres:
        lines.append("")
        for desc in no_pres:
            lines.append(f"⚠️ _Sin presentación: {desc}_")

    return '\n'.join(lines)


def _format_email_preview(extracted: dict, cot_number: str) -> str:
    """WhatsApp message 2 — full email preview before sending to client."""
    cust      = extracted.get('customer', {})
    items     = extracted.get('items', [])
    currency  = extracted.get('currency', 'PEN')
    inc_igv   = extracted.get('price_includes_igv', False)
    symbol    = '$' if currency == 'USD' else 'S/.'
    contact   = extracted.get('contact_persons') or cust.get('contact_person')
    validity  = extracted.get('validity_days')
    to_email  = cust.get('email', '')
    cust_name = cust.get('name', '').upper()
    greeting  = f"Estimado/a {contact}" if contact else f"Estimados {cust_name}"

    total_base = sum(float(i.get('quantity', 0)) * float(i.get('unit_price', 0)) for i in items)
    if inc_igv:
        base_display = round(total_base / 1.18, 2)
        igv   = round(total_base - base_display, 2)
        total = total_base
    else:
        base_display = total_base
        igv   = round(total_base * 0.18, 2)
        total = round(total_base + igv, 2)

    sep = "─────────────────────"
    lines = [
        "📧 *Vista previa del email:*",
        sep,
        f"*Para:* {to_email}",
        f"*Asunto:* {cot_number} | {cust_name}",
        sep,
        f"{greeting},",
        "",
        "Por medio del presente le hacemos llegar nuestra",
        "cotización por los productos solicitados:",
        "",
        "*PRODUCTOS:*",
    ]

    for item in items:
        qty   = float(item.get('quantity', 0))
        price = float(item.get('unit_price', 0))
        line  = qty * price
        lines.append(
            f"  • {item.get('description','?')} — "
            f"{qty:g} {item.get('unit','')} × {symbol}{price:,.2f} = {symbol}{line:,.2f}"
        )

    lines += [
        "",
        f"  Subtotal:     {symbol}{base_display:,.2f}",
        f"  I.G.V. (18%): {symbol}{igv:,.2f}",
        f"  *TOTAL:       {symbol}{total:,.2f} {currency}*",
        "",
        "*Condiciones Comerciales:*",
    ]

    global_origin = extracted.get('global_origin') or (items[0].get('origin') if items else None)
    if global_origin:
        lines.append(f"  Procedencia:   {global_origin}")
    if extracted.get('delivery'):
        lines.append(f"  Entrega:       {extracted['delivery']}")
    payment = extracted.get('payment_detail') or extracted.get('payment_terms')
    if payment:
        lines.append(f"  Forma de pago: {payment}")
    if validity:
        lines.append(f"  Validez:       {validity} días")

    lines += [
        "",
        "_Adjunto encontrará el documento formal en PDF._",
        "Quedamos atentos a su confirmación.",
        "",
        "Saludos cordiales,",
        f"*{_COMPANY_NAME}*",
        f"RUC: {_COMPANY_RUC}  |  Tel: {_COMPANY_TEL}",
        _COMPANY_EMAIL,
        sep,
        f"¿Enviar este email a *{to_email}*?",
        "Responde *sí* o *no*  |  _Escribe *0* para cancelar._",
    ]

    return '\n'.join(lines)


def _build_email_body(extracted: dict, cot_number: str) -> str:
    """Plain text email body (email client fallback / screen reader friendly)."""
    cust      = extracted.get('customer', {})
    items     = extracted.get('items', [])
    currency  = extracted.get('currency', 'PEN')
    inc_igv   = extracted.get('price_includes_igv', False)
    symbol    = '$' if currency == 'USD' else 'S/.'
    contact   = extracted.get('contact_persons') or cust.get('contact_person')
    validity  = extracted.get('validity_days')
    greeting  = f"Estimado/a {contact}" if contact else f"Estimados {cust.get('name', '').upper()}"

    total_base = sum(float(i.get('quantity', 0)) * float(i.get('unit_price', 0)) for i in items)
    if inc_igv:
        base_display = round(total_base / 1.18, 2)
        igv   = round(total_base - base_display, 2)
        total = total_base
    else:
        base_display = total_base
        igv   = round(total_base * 0.18, 2)
        total = round(total_base + igv, 2)

    lines = [
        f"{greeting},",
        "",
        "Por medio del presente le hacemos llegar nuestra cotización por los productos solicitados:",
        "",
        "DETALLE DE PRODUCTOS:",
        "-" * 55,
    ]
    for item in items:
        qty   = float(item.get('quantity', 0))
        price = float(item.get('unit_price', 0))
        desc  = item.get('description', '?')
        if item.get('presentation'):
            desc += f" ({item['presentation']})"
        lines += [
            f"  {desc}",
            f"  {qty:g} {item.get('unit','')} x {symbol}{price:,.2f} = {symbol}{qty*price:,.2f}",
        ]
        if item.get('origin'):
            lines.append(f"  Procedencia: {item['origin']}")
        lines.append("")

    lines += [
        "-" * 55,
        f"  SUBTOTAL:      {symbol}{base_display:,.2f}",
        f"  I.G.V. (18%):  {symbol}{igv:,.2f}",
        f"  TOTAL A PAGAR: {symbol}{total:,.2f} {currency}",
        "-" * 55,
        "",
        "CONDICIONES COMERCIALES:",
    ]

    global_origin = extracted.get('global_origin') or (items[0].get('origin') if items else None)
    if global_origin:
        lines.append(f"  Procedencia:   {global_origin}")
    if extracted.get('delivery'):
        lines.append(f"  Entrega:       {extracted['delivery']}")
    payment = extracted.get('payment_detail') or extracted.get('payment_terms')
    if payment:
        lines.append(f"  Forma de pago: {payment}")
    if validity:
        lines.append(f"  Validez:       {validity} días calendario")

    lines += [
        "",
        "Adjunto encontrará el documento formal en PDF.",
        "Quedamos atentos a su confirmación para generar la orden correspondiente.",
        "",
        "Saludos cordiales,",
        "",
        _COMPANY_NAME,
        f"RUC: {_COMPANY_RUC}",
        f"Telf: {_COMPANY_TEL}",
        _COMPANY_EMAIL,
    ]
    return '\n'.join(lines)
