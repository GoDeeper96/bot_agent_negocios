"""
Microsoft Graph API email client.
Sends cotización PDF as attachment from negocios.lichan@outlook.com.

Auth: OAuth2 refresh token stored in SSM (SecureString).
Token rotation: if Microsoft issues a new refresh token, it's updated in SSM automatically.
"""
import base64
import logging

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
