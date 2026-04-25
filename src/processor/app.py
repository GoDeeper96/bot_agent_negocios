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
from sunat_client import SunatClient, _ubigeo
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
        phone      = event['from']
        text       = event['text'].strip()
        message_id = event.get('message_id', '')

        config = _load_config()

        # Security: only whitelisted numbers
        if phone not in config['allowed_numbers']:
            logger.warning(f"Blocked message from unauthorized number: {phone}")
            return

        wa = WhatsAppClient(config['meta_token'], config['phone_number_id'])
        sessions = SessionManager()
        session = sessions.get(phone)

        # Deduplicate: Meta occasionally delivers the same message twice
        if message_id and message_id == session.last_message_id:
            logger.warning(f"Duplicate message_id {message_id} — skipping")
            return

        if message_id:
            session.last_message_id = message_id

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
_EXAMPLES_MENU = {'4.1', '4.2', '4.3'}

MENU_TEXT = (
    "Hola 👋 ¿Qué deseas crear?\n\n"
    "1️⃣  Factura\n"
    "2️⃣  Cotización\n"
    "3️⃣  Guía de Remisión\n"
    "4️⃣  Ver ejemplos\n\n"
    "Responde con el número o envía directamente los datos.\n"
    "Escribe *0* en cualquier momento para cancelar."
)

_EXAMPLES_SUBMENU = (
    "*4️⃣ Ejemplos de uso*\n\n"
    "4️⃣.1️⃣  Ejemplo Factura\n"
    "4️⃣.2️⃣  Ejemplo Cotización\n"
    "4️⃣.3️⃣  Ejemplo Guía de Remisión\n\n"
    "Responde con *4.1*, *4.2* o *4.3*"
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


def _detect_doc_intent(text_lower: str) -> str | None:
    """Return doc_type if the message clearly signals an intent, else None."""
    if any(k in text_lower for k in ('cotizacion', 'cotización', 'cotizar')):
        return 'cotizacion'
    if any(k in text_lower for k in ('guia de remision', 'guía de remisión', 'guia remision')):
        return 'guia'
    if any(k in text_lower for k in ('factura', 'facturar')):
        return 'factura'
    if 'boleta' in text_lower:
        return 'boleta'
    return None


def _handle_example(phone, option: str, wa):
    examples = {'4.1': _EXAMPLE_FACTURA, '4.2': _EXAMPLE_COTIZACION, '4.3': _EXAMPLE_GUIA}
    wa.send_text(phone, examples[option])


def _dispatch(phone, text, session, sessions, wa, config):
    text_lower = text.lower().strip()

    # Global cancel / back to menu — works in any state
    if text_lower in _CANCEL_WORDS and session.state != 'idle':
        sessions.clear(phone)
        wa.send_text(phone, "Operación cancelada.\n\n" + MENU_TEXT)
        return

    # Examples submenu — works from any state
    if text_lower == '4':
        wa.send_text(phone, _EXAMPLES_SUBMENU)
        return
    if text_lower in _EXAMPLES_MENU:
        _handle_example(phone, text_lower, wa)
        return

    # Idle: show menu or detect doc type from first message
    if session.state == 'idle':
        if text_lower in _MENU_OPTIONS:
            session.doc_type = _MENU_OPTIONS[text_lower]
            session.state = 'collecting'
            sessions.save(session)
            wa.send_text(phone, f"*{session.doc_type.capitalize()}* seleccionada ✅\nEnvíame los datos del cliente y productos.")
        else:
            # If intent detected → skip menu, go straight to extraction
            # If no intent → show menu so user can pick doc type
            detected = _detect_doc_intent(text_lower)
            session.state = 'collecting'
            session.add_message(text)
            if detected:
                session.doc_type = detected
                sessions.save(session)
                _handle_collecting(phone, None, session, sessions, wa, config)
            else:
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

    # RUC only required for factura — cotizacion/boleta accept RUC or DNI
    doc_now = extracted.get('doc_type')
    if doc_now in ('cotizacion', 'boleta', 'guia'):
        missing = [f for f in missing if f != 'customer_ruc']
    # For cotizacion/boleta: require at least one of RUC or DNI
    if doc_now in ('cotizacion', 'boleta'):
        cust = extracted.get('customer', {})
        if not cust.get('ruc') and not cust.get('dni'):
            missing = [f for f in missing if f != 'customer_ruc']
            if 'customer_doc' not in missing:
                missing.append('customer_doc')

    # Email is optional — never block the flow on it
    missing = [f for f in missing if f != 'customer_email']

    if missing:
        preview = _format_partial_preview(extracted)
        missing_msg = _format_missing(missing)
        wa.send_text(phone, f"{preview}\n\n{missing_msg}\n\n_Escribe *0* para cancelar._")
        sessions.save(session)
    else:
        session.state = 'confirming'
        doc_type_now = extracted.get('doc_type')
        if doc_type_now == 'cotizacion':
            preview      = _format_cotizacion_preview(extracted)
            confirm_text = "¿Confirmar datos? Responde *sí* o *no*\n_Escribe *0* para cancelar._"
        elif doc_type_now == 'guia':
            preview      = _format_guia_preview(extracted)
            confirm_text = "¿Confirmar y emitir guía? Responde *sí* o *no*\n_Escribe *0* para cancelar._"
        else:
            preview      = _format_full_preview(extracted)
            confirm_text = "¿Confirmar y enviar? Responde *sí* o *no*\n_Escribe *0* para cancelar._"
        wa.send_text(phone, f"{preview}\n\n{confirm_text}")
        sessions.save(session)


# ---------------------------------------------------------------------------
# State: submit
# ---------------------------------------------------------------------------

def _handle_submit(phone, session, sessions, wa, config):
    doc_type_now = session.doc_type or session.extracted.get('doc_type')
    if doc_type_now == 'cotizacion':
        _handle_cotizacion_confirmed(phone, session, sessions, wa, config)
        return
    if doc_type_now == 'guia':
        _handle_guia_submit(phone, session, sessions, wa, config)
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
# Guia de Remision helpers
# ---------------------------------------------------------------------------

_GUIA_SERIE   = "T001"
_COMPANY_ADDR = "CAL.LOS EUCALIPTOS MZA. A LOTE. 5 VILLA EL SALVADOR LIMA LIMA"


def _next_guia_number(ssm_prefix: str) -> str:
    """Atomically increment and return the next guia number (zero-padded to 8 digits)."""
    ssm = boto3.client('ssm', region_name='us-west-2')
    param_name = f"{ssm_prefix}/guia_counter"
    try:
        resp = ssm.get_parameter(Name=param_name)
        current = int(resp['Parameter']['Value'])
    except Exception:
        current = 0
    next_val = current + 1
    ssm.put_parameter(Name=param_name, Value=str(next_val), Type='String', Overwrite=True)
    return str(next_val).zfill(8)


def _format_guia_preview(extracted: dict) -> str:
    guia = extracted.get('guia') or {}
    items = extracted.get('items', [])
    sep = "─────────────────────"
    lines = [
        "*🚚 GUÍA DE REMISIÓN*",
        sep,
        f"👤 *{(guia.get('receiver_name') or '-').upper()}*",
    ]
    if guia.get('receiver_ruc'):
        lines.append(f"   RUC: {guia['receiver_ruc']}")
    lines.append(sep)
    lines.append("📦 *BIENES*")
    for item in items:
        qty = item.get('quantity', '?')
        unit = item.get('unit', '')
        lines.append(f"  • {item.get('description', '?')} — {qty} {unit}")
    if guia.get('total_weight_kg'):
        lines.append(f"   ⚖️ Peso total: {guia['total_weight_kg']} KG")
    lines.append(sep)
    if guia.get('departure_address'):
        lines.append(f"🏭 Partida:  {guia['departure_address']}")
    if guia.get('arrival_address'):
        lines.append(f"📍 Llegada:  {guia['arrival_address']}")
    if guia.get('transport_date'):
        lines.append(f"📅 Fecha traslado: {guia['transport_date']}")
    lines.append(sep)
    driver_name = ' '.join(filter(None, [guia.get('driver_firstname'), guia.get('driver_lastname')]))
    if driver_name:
        lines.append(f"🚗 Conductor: {driver_name}")
    if guia.get('driver_dni'):
        lines.append(f"   DNI: {guia['driver_dni']}")
    if guia.get('driver_license'):
        lines.append(f"   Licencia: {guia['driver_license']}")
    if guia.get('vehicle_plate'):
        lines.append(f"   Placa: {guia['vehicle_plate'].upper()}")
    return '\n'.join(lines)


def _handle_guia_submit(phone, session, sessions, wa, config):
    wa.send_text(phone, "⏳ Emitiendo guía de remisión...")
    extracted = session.extracted
    guia = extracted.get('guia') or {}

    try:
        ssm_prefix = os.environ['SSM_PREFIX']
        number = _next_guia_number(ssm_prefix)

        sunat = SunatClient(
            persona_id=config['sunat_persona_id'],
            persona_token=config['sunat_persona_token'],
        )

        # Departure ubigeo: use extracted or default Lima Cercado
        dep_ubigeo = guia.get('departure_ubigeo') or _ubigeo(guia.get('departure_address', ''))
        arr_ubigeo = guia.get('arrival_ubigeo') or _ubigeo(guia.get('arrival_address', ''))

        result = sunat.send_guia(
            serie=_GUIA_SERIE,
            number=number,
            receiver_ruc=guia.get('receiver_ruc', ''),
            receiver_name=guia.get('receiver_name', ''),
            items=extracted.get('items', []),
            total_weight_kg=float(guia.get('total_weight_kg') or 1),
            departure_address=guia.get('departure_address', _COMPANY_ADDR),
            departure_ubigeo=dep_ubigeo,
            arrival_address=guia.get('arrival_address', ''),
            arrival_ubigeo=arr_ubigeo,
            transport_date=guia.get('transport_date') or datetime.now().strftime('%Y-%m-%d'),
            driver_firstname=guia.get('driver_firstname', ''),
            driver_lastname=guia.get('driver_lastname', ''),
            driver_dni=guia.get('driver_dni', ''),
            driver_license=guia.get('driver_license', ''),
            vehicle_plate=guia.get('vehicle_plate', ''),
            transport_mode=guia.get('transport_mode', '02'),
        )

        full_number = f"{_GUIA_SERIE}-{number}"
        faults = result.get('faults', [])
        if faults:
            fault_msg = '; '.join(str(f) for f in faults)
            wa.send_text(phone, f"⚠️ Guía *{full_number}* emitida con observaciones SUNAT:\n{fault_msg}")
        else:
            msg = f"✅ Guía de remisión *{full_number}* emitida correctamente."
            pdf_url = result.get('pdfUrl')
            if pdf_url:
                msg += f"\n\n📄 PDF: {pdf_url}"
            wa.send_text(phone, msg)

        sessions.clear(phone)

    except Exception as e:
        logger.exception(f"Error submitting guia: {e}")
        wa.send_text(phone, f"❌ Error al emitir guía: {e}")
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
        return {'xml': data.get('xml'), 'cdr': data.get('cdr'), 'pdf_url': pdf_url}

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

    wa.send_text(phone, f"📧 Enviando {doc_label} {full_number} a *{email}*...")

    # Fetch XML, CDR and updated PDF URL from apisunat
    cdr_url = ''
    if config.get('sunat_persona_id') and config.get('sunat_persona_token'):
        import time as _time
        pid, ptok = config['sunat_persona_id'], config['sunat_persona_token']
        doc_data = _fetch_sunat_doc(full_number, sunat_doc_id, pid, ptok)
        xml_url  = doc_data.get('xml') or xml_url
        cdr_url  = doc_data.get('cdr') or ''
        pdf_url  = doc_data.get('pdf_url') or pdf_url
        # CDR lags ~30s behind XML (SUNAT processing time) — retry once if missing
        if not cdr_url:
            _time.sleep(20)
            doc_data2 = _fetch_sunat_doc(full_number, sunat_doc_id, pid, ptok)
            cdr_url = doc_data2.get('cdr') or ''
            if not xml_url:
                xml_url = doc_data2.get('xml') or xml_url
        logger.info(f"apisunat doc fetch: xml={bool(xml_url)} cdr={bool(cdr_url)} pdf={bool(pdf_url)}")

    ok = send_factura_email(
        to_email=email,
        doc_label=doc_label,
        full_number=full_number,
        pdf_url=pdf_url,
        xml_url=xml_url or None,
        cdr_url=cdr_url or None,
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
        'customer_name':  '👤 Nombre del cliente',
        'customer_ruc':   '🔢 RUC del cliente (requerido para factura)',
        'customer_doc':   '🔢 RUC o DNI del cliente',
        'customer_email': '📧 Correo del cliente (para envío de PDF)',
        'items':          '📦 Descripción de productos',
        'quantity':       '🔢 Cantidad del producto',
        'unit_price':     '💰 Precio unitario',
        'doc_type':       '📄 Tipo de documento (¿factura o boleta?)',
        'guia_receiver':  '👤 Nombre/RUC del destinatario',
        'guia_driver':    '🚗 Datos del conductor (nombre, DNI, licencia)',
        'guia_vehicle':   '🔑 Placa del vehículo',
        'guia_addresses': '📍 Dirección de partida y llegada',
        'guia_weight':    '⚖️ Peso total de los bienes (KG)',
        'guia_date':      '📅 Fecha de traslado',
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
_COMPANY_TEL   = "960113935"
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
            body_text="",
            pdf_bytes=pdf_bytes,
            pdf_filename=pdf_filename,
            ssm_prefix=os.environ['SSM_PREFIX'],
            extracted=extracted,
            cot_number=cot_number,
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
    validity  = extracted.get('validity_days')

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
