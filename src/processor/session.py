"""
Session manager — stores conversation state in DynamoDB.

Each session is keyed by phone number and expires after 30 minutes of inactivity.

States:
  idle        — no active conversation
  collecting  — accumulating messages, extracting data
  confirming  — showing preview, waiting for sí/no
  email       — invoice done, asking whether to email PDF
"""
import json
import logging
import os
import time

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 30 * 60  # 30 minutes


class Session:
    def __init__(self, phone_number: str, data: dict):
        self.phone_number = phone_number
        self.state = data.get('state', 'idle')
        self.messages = data.get('messages', [])       # raw texts accumulated
        self.extracted = data.get('extracted', {})     # Claude's last extraction
        self.pending_sale = data.get('pending_sale', {})  # sale data ready to submit
        self.last_sale_id = data.get('last_sale_id')   # saleId after creation
        self.last_pdf_url = data.get('last_pdf_url')   # PDF URL after SUNAT
        self.last_email = data.get('last_email')       # customer email for PDF send

    def add_message(self, text: str):
        self.messages.append(text)
        # Keep last 20 messages to avoid bloat
        if len(self.messages) > 20:
            self.messages = self.messages[-20:]

    def to_dict(self) -> dict:
        return {
            'state': self.state,
            'messages': self.messages,
            'extracted': self.extracted,
            'pending_sale': self.pending_sale,
            'last_sale_id': self.last_sale_id,
            'last_pdf_url': self.last_pdf_url,
            'last_email': self.last_email,
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
        # DynamoDB can't store None values
        item = {k: v for k, v in item.items() if v is not None}
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
