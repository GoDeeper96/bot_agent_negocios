"""
Session manager — stores conversation state in DynamoDB.

Each session is keyed by phone number and expires after 30 minutes of inactivity.

States:
  idle          — no active conversation
  collecting    — accumulating messages, extracting data
  confirming    — showing preview, waiting for sí/no
  email_preview — cotización confirmed, showing email preview before send
  email         — invoice done, asking whether to email PDF
"""
import json
import logging
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 30 * 60  # 30 minutes


def _to_decimal(obj):
    """Recursively convert floats to Decimal for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_decimal(v) for v in obj]
    return obj


class Session:
    def __init__(self, phone_number: str, data: dict):
        self.phone_number = phone_number
        self.state = data.get('state', 'idle')
        self.doc_type = data.get('doc_type')           # 'factura' | 'cotizacion' | 'guia'
        self.messages = data.get('messages', [])       # raw texts accumulated
        self.extracted = data.get('extracted', {})     # Claude's last extraction
        self.pending_sale = data.get('pending_sale', {})  # sale data ready to submit
        self.last_sale_id    = data.get('last_sale_id')      # saleId after creation
        self.last_pdf_url    = data.get('last_pdf_url')      # PDF URL after SUNAT
        self.last_xml_url       = data.get('last_xml_url')         # XML URL after SUNAT
        self.last_sunat_doc_id  = data.get('last_sunat_doc_id')     # apisunat documentId for polling
        self.last_email      = data.get('last_email')        # customer email for PDF send
        self.last_full_number = data.get('last_full_number') # e.g. "F149-00000011"
        self.last_doc_label  = data.get('last_doc_label')    # "Factura" or "Boleta"
        self.last_cot_number = data.get('last_cot_number')   # COT-YYYY-NNN assigned on confirm
        self.last_message_id = data.get('last_message_id')   # for deduplication

    def add_message(self, text: str):
        self.messages.append(text)
        # Keep last 20 messages to avoid bloat
        if len(self.messages) > 20:
            self.messages = self.messages[-20:]

    def to_dict(self) -> dict:
        return {
            'state': self.state,
            'doc_type': self.doc_type,
            'messages': self.messages,
            'extracted': self.extracted,
            'pending_sale': self.pending_sale,
            'last_sale_id':     self.last_sale_id,
            'last_pdf_url':     self.last_pdf_url,
            'last_xml_url':        self.last_xml_url,
            'last_sunat_doc_id':   self.last_sunat_doc_id,
            'last_email':       self.last_email,
            'last_full_number': self.last_full_number,
            'last_doc_label':   self.last_doc_label,
            'last_cot_number':  self.last_cot_number,
            'last_message_id':  self.last_message_id,
        }


class SessionManager:
    def __init__(self):
        dynamodb = boto3.resource('dynamodb')
        self._table = dynamodb.Table(os.environ['SESSIONS_TABLE'])

    def get(self, phone_number: str) -> Session:
        resp = self._table.get_item(Key={'phoneNumber': phone_number})
        data = resp.get('Item', {})
        return Session(phone_number, data)

    def save(self, session: Session):
        item = {
            'phoneNumber': session.phone_number,
            'ttl': int(time.time()) + SESSION_TTL_SECONDS,
            **session.to_dict(),
        }
        # DynamoDB can't store None values or floats
        item = {k: v for k, v in item.items() if v is not None}
        item = _to_decimal(item)
        self._table.put_item(Item=item)

    def clear(self, phone_number: str):
        """Reset to idle, keep no state."""
        self._table.put_item(Item={
            'phoneNumber': phone_number,
            'state': 'idle',
            'messages': [],
            'extracted': {},
            'pending_sale': {},
            'ttl': int(time.time()) + SESSION_TTL_SECONDS,
        })
