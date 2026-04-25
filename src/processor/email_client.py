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
_COMPANY_ADDR = "CAL.LOS EUCALIPTOS MZA. A LOTE. 5 VILLA EL SALVADOR LIMA LIMA"
_COMPANY_PHONE = "994-494-494"
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


def send_cotizacion_email(
    to_email: str,
    subject: str,
    body_text: str,
    pdf_bytes: bytes,
    pdf_filename: str,
    ssm_prefix: str,
) -> bool:
    """Send email with PDF attachment via Microsoft Graph API. Returns True on success."""
    try:
        access_token = _get_access_token(ssm_prefix)

        payload = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "Text",
                    "content": body_text,
                },
                "toRecipients": [
                    {"emailAddress": {"address": to_email}}
                ],
                "attachments": [{
                    "@odata.type":  "#microsoft.graph.fileAttachment",
                    "name":         pdf_filename,
                    "contentType":  "application/pdf",
                    "contentBytes": base64.b64encode(pdf_bytes).decode(),
                }],
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
        logger.info(f"Graph API email sent to {to_email} | {subject}")
        return True

    except Exception as e:
        logger.error(f"Graph API send_email failed → {to_email}: {e}")
        return False
