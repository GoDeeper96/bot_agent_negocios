"""
Meta Cloud API client — sends WhatsApp messages.
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com/v22.0"


class WhatsAppClient:
    def __init__(self, token: str, phone_number_id: str):
        self._token = token
        self._phone_number_id = phone_number_id
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def send_text(self, to: str, text: str):
        """Send a plain text message."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text, "preview_url": False},
        }
        self._post(payload)

    def _post(self, payload: dict):
        url = f"{GRAPH_URL}/{self._phone_number_id}/messages"
        resp = requests.post(url, json=payload, headers=self._headers, timeout=15)
        if not resp.ok:
            logger.error(f"WhatsApp API error {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()
        return resp.json()
