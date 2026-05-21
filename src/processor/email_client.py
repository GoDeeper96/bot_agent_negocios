"""
Microsoft Graph API email client.
Sends cotización PDF as attachment from negocios.lichan@outlook.com.

Auth: OAuth2 refresh token stored in SSM (SecureString).
Token rotation: if Microsoft issues a new refresh token, it's updated in SSM automatically.
"""
import base64
import io
import logging
import os
import zipfile

import boto3
import requests

logger = logging.getLogger(__name__)

_TENANT        = "common"      # org-registered app + personal Microsoft accounts
_TOKEN_URL     = f"https://login.microsoftonline.com/{_TENANT}/oauth2/v2.0/token"
_SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
_SCOPES        = "Mail.Send offline_access"

_client_id_cache: str | None = None  # cached per Lambda container (never changes)


def _ssm():
    return boto3.client("ssm", region_name="us-west-2")


def _get_client_id(ssm_prefix: str) -> str:
    global _client_id_cache
    if not _client_id_cache:
        resp = _ssm().get_parameter(Name=f"{ssm_prefix}/graph/client_id")
        _client_id_cache = resp["Parameter"]["Value"]
    return _client_id_cache


def _get_refresh_token(ssm_prefix: str) -> str:
    resp = _ssm().get_parameter(
        Name=f"{ssm_prefix}/graph/refresh_token",
        WithDecryption=True,
    )
    return resp["Parameter"]["Value"]


def _save_refresh_token(ssm_prefix: str, token: str) -> None:
    try:
        _ssm().put_parameter(
            Name=f"{ssm_prefix}/graph/refresh_token",
            Value=token,
            Type="SecureString",
            Overwrite=True,
        )
    except Exception as e:
        logger.warning(f"Could not rotate refresh token in SSM: {e}")


def _get_access_token(ssm_prefix: str) -> str:
    """Exchange refresh token for a fresh access token, rotating SSM if Microsoft issues a new refresh token."""
    client_id     = _get_client_id(ssm_prefix)
    refresh_token = _get_refresh_token(ssm_prefix)

    resp = requests.post(_TOKEN_URL, data={
        "client_id":     client_id,
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "scope":         _SCOPES,
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        _save_refresh_token(ssm_prefix, new_refresh)

    return data["access_token"]


_COMPANY_NAME = "NEGOCIOS MULTIPLES LICHAN S.A.C."
_COMPANY_RUC  = "20607960225"
_COMPANY_ADDR = "AV. GUILLERMO BILLINGHURST NRO. 1089 URB. SAN JUAN ZN. D- SAN JUAN DE MIRAFLORES - LIMA - LIMA"
_COMPANY_PHONE = "960113935"
_COMPANY_EMAIL = "negocios.lichan@outlook.com"

_FACTURA_HTML_TMPL = """\
<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#333;">
<p>Estimados, buenas tardes,</p>
<p>Se adjunta {doc_label} <strong>{full_number}</strong>.{pdf_link_line}</p>
<p>Saludos cordiales / Best regards</p>
<br>
<img src="cid:logo_lichan" alt="{company}" style="max-width:220px;"><br><br>
<strong>{company}</strong><br>
RUC: {ruc}<br>
{address}<br>
Telf.: {phone}<br>
Email: <a href="mailto:{email}">{email}</a>
</body></html>
"""


def _download_bytes(url: str, timeout: int = 15) -> bytes | None:
    """Download a URL and return raw bytes, or None on failure."""
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.warning(f"Could not download {url}: {e}")
        return None


def _unzip_first_file(zip_bytes: bytes) -> tuple[bytes, str] | tuple[None, None]:
    """Extract the first file from a ZIP archive. Returns (file_bytes, filename)."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            name = zf.namelist()[0]
            return zf.read(name), name
    except Exception as e:
        logger.warning(f"Could not unzip: {e}")
        return None, None


def send_factura_email(
    to_email: str,
    doc_label: str,
    full_number: str,
    pdf_url: str,
    xml_url: str | None,
    ssm_prefix: str,
    cdr_url: str | None = None,
) -> bool:
    """
    Send factura/boleta email with PDF (and XML if available) via Microsoft Graph.
    Downloads the files from apisunat before attaching.
    """
    try:
        pdf_bytes = _download_bytes(pdf_url) if pdf_url else None

        # Download and unzip XML
        xml_bytes, xml_name = None, f"{full_number}.xml"
        if xml_url:
            xml_zip = _download_bytes(xml_url)
            if xml_zip:
                xml_bytes, xml_name = _unzip_first_file(xml_zip)

        # Download and unzip CDR
        cdr_bytes, cdr_name = None, f"R-{full_number}.xml"
        if cdr_url:
            cdr_zip = _download_bytes(cdr_url)
            if cdr_zip:
                cdr_bytes, cdr_name = _unzip_first_file(cdr_zip)

        pdf_name = pdf_url.split("/")[-1] if pdf_url else f"{full_number}.pdf"

        pdf_link_line = (
            f' Ver PDF: <a href="{pdf_url}">{pdf_url}</a>' if pdf_url and not pdf_bytes else ''
        )
        html_body = _FACTURA_HTML_TMPL.format(
            doc_label=doc_label,
            full_number=full_number,
            pdf_link_line=pdf_link_line,
            company=_COMPANY_NAME,
            ruc=_COMPANY_RUC,
            address=_COMPANY_ADDR,
            phone=_COMPANY_PHONE,
            email=_COMPANY_EMAIL,
        )

        attachments = []

        # Inline logo
        _logo_path = os.path.join(os.path.dirname(__file__), "logo_negocios_multiples_lichan.png")
        try:
            with open(_logo_path, "rb") as _f:
                _logo_bytes = _f.read()
            attachments.append({
                "@odata.type":  "#microsoft.graph.fileAttachment",
                "name":         "logo_lichan.png",
                "contentType":  "image/png",
                "contentId":    "logo_lichan",
                "isInline":     True,
                "contentBytes": base64.b64encode(_logo_bytes).decode(),
            })
        except Exception as _e:
            logger.warning(f"Could not load logo: {_e}")

        if pdf_bytes:
            attachments.append({
                "@odata.type":  "#microsoft.graph.fileAttachment",
                "name":         pdf_name,
                "contentType":  "application/pdf",
                "contentBytes": base64.b64encode(pdf_bytes).decode(),
            })
        if xml_bytes:
            attachments.append({
                "@odata.type":  "#microsoft.graph.fileAttachment",
                "name":         xml_name,
                "contentType":  "application/xml",
                "contentBytes": base64.b64encode(xml_bytes).decode(),
            })
        if cdr_bytes:
            attachments.append({
                "@odata.type":  "#microsoft.graph.fileAttachment",
                "name":         cdr_name,
                "contentType":  "application/xml",
                "contentBytes": base64.b64encode(cdr_bytes).decode(),
            })

        access_token = _get_access_token(ssm_prefix)
        subject = f"{doc_label.upper()} {full_number} - LICHAN"

        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": to_email}}],
                "attachments": attachments,
            }
        }

        resp = requests.post(
            _SEND_MAIL_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"Factura email sent to {to_email} | {subject}")
        return True

    except Exception as e:
        logger.error(f"send_factura_email failed → {to_email}: {e}")
        return False


def _build_cotizacion_html(extracted: dict, cot_number: str) -> str:
    """Build HTML body for cotización email with product table and Lichan footer."""
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

    # Product rows
    rows_html = ""
    for item in items:
        qty   = float(item.get('quantity', 0))
        price = float(item.get('unit_price', 0))
        desc  = item.get('description', '')
        if item.get('presentation'):
            desc += f"<br><small style='color:#666'>{item['presentation']}</small>"
        if item.get('origin'):
            desc += f"<br><small style='color:#666'>Proc: {item['origin']}</small>"
        line_total = qty * price
        rows_html += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">{desc}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center;">{qty:g} {item.get('unit','')}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">{symbol}{price:,.2f}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;font-weight:bold;">{symbol}{line_total:,.2f}</td>
        </tr>"""

    # Totals rows
    totals_html = f"""
        <tr style="background:#f9f9f9;">
          <td colspan="3" style="padding:6px 12px;text-align:right;color:#555;">Subtotal:</td>
          <td style="padding:6px 12px;text-align:right;">{symbol}{base_display:,.2f}</td>
        </tr>
        <tr style="background:#f9f9f9;">
          <td colspan="3" style="padding:6px 12px;text-align:right;color:#555;">I.G.V. (18%):</td>
          <td style="padding:6px 12px;text-align:right;">{symbol}{igv:,.2f}</td>
        </tr>
        <tr style="background:#8b1a1a;color:#fff;">
          <td colspan="3" style="padding:8px 12px;text-align:right;font-weight:bold;">TOTAL A PAGAR:</td>
          <td style="padding:8px 12px;text-align:right;font-weight:bold;">{symbol}{total:,.2f} {currency}</td>
        </tr>"""

    # Commercial conditions
    conditions = []
    global_origin = extracted.get('global_origin') or (items[0].get('origin') if items else None)
    if global_origin:
        conditions.append(f"<tr><td style='padding:4px 12px;color:#555;width:140px;'>Procedencia:</td><td style='padding:4px 12px;font-weight:bold;'>{global_origin}</td></tr>")
    if extracted.get('delivery'):
        conditions.append(f"<tr><td style='padding:4px 12px;color:#555;'>Entrega:</td><td style='padding:4px 12px;font-weight:bold;'>{extracted['delivery']}</td></tr>")
    payment = extracted.get('payment_detail') or extracted.get('payment_terms')
    if payment:
        conditions.append(f"<tr><td style='padding:4px 12px;color:#555;'>Forma de pago:</td><td style='padding:4px 12px;font-weight:bold;'>{payment}</td></tr>")
    if validity:
        conditions.append(f"<tr><td style='padding:4px 12px;color:#555;'>Validez:</td><td style='padding:4px 12px;font-weight:bold;'>{validity} días calendario</td></tr>")
    if extracted.get('notes'):
        conditions.append(f"<tr><td style='padding:4px 12px;color:#555;vertical-align:top;'>Observaciones:</td><td style='padding:4px 12px;'>{extracted['notes']}</td></tr>")

    conditions_html = ""
    if conditions:
        conditions_html = f"""
        <h3 style="color:#8b1a1a;margin-top:24px;">Condiciones Comerciales</h3>
        <table style="border-collapse:collapse;font-size:14px;">{''.join(conditions)}</table>"""

    return f"""\
<html>
<body style="font-family:Arial,sans-serif;font-size:14px;color:#333;max-width:700px;margin:0 auto;">

<p>{greeting},</p>
<p>Por medio del presente le hacemos llegar nuestra cotización <strong>{cot_number}</strong> por los productos solicitados:</p>

<table style="width:100%;border-collapse:collapse;font-size:14px;margin-top:16px;">
  <thead>
    <tr style="background:#8b1a1a;color:#fff;">
      <th style="padding:10px 12px;text-align:left;">Descripción</th>
      <th style="padding:10px 12px;text-align:center;">Cantidad</th>
      <th style="padding:10px 12px;text-align:right;">Precio Unit.</th>
      <th style="padding:10px 12px;text-align:right;">Total</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
  <tfoot>{totals_html}</tfoot>
</table>

{conditions_html}

<p style="margin-top:24px;">Adjunto encontrará el documento formal en PDF.<br>
Quedamos atentos a su confirmación para generar la orden correspondiente.</p>

<p>Saludos cordiales / Best regards</p>
<br>
<img src="cid:logo_lichan" alt="{_COMPANY_NAME}" style="max-width:220px;"><br><br>
<strong>{_COMPANY_NAME}</strong><br>
RUC: {_COMPANY_RUC}<br>
{_COMPANY_ADDR}<br>
Telf.: {_COMPANY_PHONE}<br>
Email: <a href="mailto:{_COMPANY_EMAIL}">{_COMPANY_EMAIL}</a>

</body>
</html>"""


def _build_orden_compra_html(extracted: dict, oc_number: str) -> str:
    """Build HTML body for Orden de Compra email."""
    supplier  = extracted.get('customer', {})
    items     = extracted.get('items', [])
    currency  = extracted.get('currency', 'USD')
    inc_igv   = extracted.get('price_includes_igv', False)
    symbol    = '$' if currency == 'USD' else 'S/.'
    cust_name = (supplier.get('name') or '').upper()

    total_base = sum(float(i.get('quantity', 0)) * float(i.get('unit_price', 0)) for i in items)
    if inc_igv:
        base_display = round(total_base / 1.18, 2)
        igv   = round(total_base - base_display, 2)
        total = total_base
    else:
        base_display = total_base
        igv   = round(total_base * 0.18, 2)
        total = round(total_base + igv, 2)

    rows_html = ""
    for idx, item in enumerate(items, 1):
        qty   = float(item.get('quantity', 0))
        price = float(item.get('unit_price', 0))
        sku   = item.get('sku') or ''
        desc  = item.get('description', '')
        if sku:
            desc = f"[{sku}] {desc}"
        line_total = qty * price
        rows_html += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center;">{str(idx).zfill(3)}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">{desc}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center;">{qty:g} {item.get('unit','')}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">{symbol}{price:,.4f}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;font-weight:bold;">{symbol}{line_total:,.2f}</td>
        </tr>"""

    totals_html = f"""
        <tr style="background:#f9f9f9;">
          <td colspan="4" style="padding:6px 12px;text-align:right;color:#555;">IGV (18%):</td>
          <td style="padding:6px 12px;text-align:right;">{symbol}{igv:,.2f}</td>
        </tr>
        <tr style="background:#8b1a1a;color:#fff;">
          <td colspan="4" style="padding:8px 12px;text-align:right;font-weight:bold;">TOTAL:</td>
          <td style="padding:8px 12px;text-align:right;font-weight:bold;">{symbol}{total:,.2f} {currency}</td>
        </tr>"""

    conditions = []
    payment = extracted.get('payment_detail') or extracted.get('payment_terms')
    if payment:
        conditions.append(f"<tr><td style='padding:4px 12px;color:#555;width:160px;'>Forma de pago:</td><td style='padding:4px 12px;font-weight:bold;'>{payment}</td></tr>")
    if extracted.get('delivery'):
        conditions.append(f"<tr><td style='padding:4px 12px;color:#555;'>Lugar de entrega:</td><td style='padding:4px 12px;font-weight:bold;'>{extracted['delivery']}</td></tr>")
    if extracted.get('delivery_date'):
        conditions.append(f"<tr><td style='padding:4px 12px;color:#555;'>Tiempo de entrega:</td><td style='padding:4px 12px;font-weight:bold;'>{extracted['delivery_date']}</td></tr>")
    if extracted.get('notes'):
        conditions.append(f"<tr><td style='padding:4px 12px;color:#555;'>Observaciones:</td><td style='padding:4px 12px;'>{extracted['notes']}</td></tr>")

    conditions_html = ""
    if conditions:
        conditions_html = f"""
        <h3 style="color:#8b1a1a;margin-top:24px;">Condiciones</h3>
        <table style="border-collapse:collapse;font-size:14px;">{''.join(conditions)}</table>"""

    return f"""\
<html>
<body style="font-family:Arial,sans-serif;font-size:14px;color:#333;max-width:700px;margin:0 auto;">

<p>Estimados {cust_name},</p>
<p>Adjunto encontrará nuestra <strong>Orden de Compra {oc_number}</strong>.<br>
Por favor confirmar recepción y fecha de despacho.</p>

<table style="width:100%;border-collapse:collapse;font-size:14px;margin-top:16px;">
  <thead>
    <tr style="background:#8b1a1a;color:#fff;">
      <th style="padding:10px 12px;text-align:center;">#</th>
      <th style="padding:10px 12px;text-align:left;">Descripción</th>
      <th style="padding:10px 12px;text-align:center;">Cantidad</th>
      <th style="padding:10px 12px;text-align:right;">Precio Unit.</th>
      <th style="padding:10px 12px;text-align:right;">Total</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
  <tfoot>{totals_html}</tfoot>
</table>

{conditions_html}

<p style="margin-top:24px;">Quedo atento a su confirmación.</p>
<p>Saludos cordiales / Best regards</p>
<br>
<img src="cid:logo_lichan" alt="{_COMPANY_NAME}" style="max-width:220px;"><br><br>
<strong>{_COMPANY_NAME}</strong><br>
RUC: {_COMPANY_RUC}<br>
{_COMPANY_ADDR}<br>
Telf.: {_COMPANY_PHONE}<br>
Email: <a href="mailto:{_COMPANY_EMAIL}">{_COMPANY_EMAIL}</a>

</body>
</html>"""


def send_orden_compra_email(
    to_email: str,
    subject: str,
    pdf_bytes: bytes,
    pdf_filename: str,
    ssm_prefix: str,
    extracted: dict = None,
    oc_number: str = "",
) -> bool:
    """Send Orden de Compra email with PDF attachment via Microsoft Graph API."""
    try:
        access_token = _get_access_token(ssm_prefix)

        html_body  = _build_orden_compra_html(extracted, oc_number)
        body_block = {"contentType": "HTML", "content": html_body}

        attachments = []

        _logo_path = os.path.join(os.path.dirname(__file__), "logo_negocios_multiples_lichan.png")
        try:
            with open(_logo_path, "rb") as _f:
                _logo_bytes = _f.read()
            attachments.append({
                "@odata.type":  "#microsoft.graph.fileAttachment",
                "name":         "logo_lichan.png",
                "contentType":  "image/png",
                "contentId":    "logo_lichan",
                "isInline":     True,
                "contentBytes": base64.b64encode(_logo_bytes).decode(),
            })
        except Exception as _e:
            logger.warning(f"Could not load logo: {_e}")

        attachments.append({
            "@odata.type":  "#microsoft.graph.fileAttachment",
            "name":         pdf_filename,
            "contentType":  "application/pdf",
            "contentBytes": base64.b64encode(pdf_bytes).decode(),
        })

        payload = {
            "message": {
                "subject": subject,
                "body": body_block,
                "toRecipients": [{"emailAddress": {"address": to_email}}],
                "attachments": attachments,
            }
        }

        resp = requests.post(
            _SEND_MAIL_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"Graph API OC email sent to {to_email} | {subject}")
        return True

    except Exception as e:
        logger.error(f"Graph API send_orden_compra_email failed → {to_email}: {e}")
        return False


def send_cotizacion_email(
    to_email: str,
    subject: str,
    body_text: str,
    pdf_bytes: bytes,
    pdf_filename: str,
    ssm_prefix: str,
    extracted: dict = None,
    cot_number: str = "",
) -> bool:
    """Send cotización email with PDF attachment via Microsoft Graph API. Returns True on success."""
    try:
        access_token = _get_access_token(ssm_prefix)

        if extracted is not None:
            html_body  = _build_cotizacion_html(extracted, cot_number)
            body_block = {"contentType": "HTML", "content": html_body}
        else:
            body_block = {"contentType": "Text", "content": body_text}

        attachments = []

        # Inline logo
        _logo_path = os.path.join(os.path.dirname(__file__), "logo_negocios_multiples_lichan.png")
        try:
            with open(_logo_path, "rb") as _f:
                _logo_bytes = _f.read()
            attachments.append({
                "@odata.type":  "#microsoft.graph.fileAttachment",
                "name":         "logo_lichan.png",
                "contentType":  "image/png",
                "contentId":    "logo_lichan",
                "isInline":     True,
                "contentBytes": base64.b64encode(_logo_bytes).decode(),
            })
        except Exception as _e:
            logger.warning(f"Could not load logo: {_e}")

        attachments.append({
            "@odata.type":  "#microsoft.graph.fileAttachment",
            "name":         pdf_filename,
            "contentType":  "application/pdf",
            "contentBytes": base64.b64encode(pdf_bytes).decode(),
        })

        payload = {
            "message": {
                "subject": subject,
                "body": body_block,
                "toRecipients": [{"emailAddress": {"address": to_email}}],
                "attachments": attachments,
            }
        }

        resp = requests.post(
            _SEND_MAIL_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"Graph API cotizacion email sent to {to_email} | {subject}")
        return True

    except Exception as e:
        logger.error(f"Graph API send_cotizacion_email failed → {to_email}: {e}")
        return False
