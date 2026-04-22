"""
Client for the new_dc_api_2026 POS API.

Wraps the endpoints the bot needs:
  - Customer lookup / creation
  - Product search
  - Sale create → add items → complete → send to SUNAT
  - Document series lookup
"""
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class PosApiError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"POS API {status_code}: {message}")


class PosApiClient:
    def __init__(self, base_url: str, token: str):
        self._base = base_url.rstrip('/')
        self._headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Customers
    # ------------------------------------------------------------------

    def search_customer(self, name: str) -> Optional[dict]:
        """Search customer by name, return best match or None."""
        resp = self._get('/core/customers', params={'search': name, 'limit': 5})
        customers = resp.get('customers') or resp.get('items') or []
        if not customers:
            return None
        # Return the first result — Claude already extracted the name so it should match
        return customers[0]

    def create_customer(self, name: str, document_number: str, document_type: str,
                        email: str = None, phone: str = None) -> dict:
        payload = {
            'name': name,
            'documentNumber': document_number,
            'documentType': document_type,  # 'RUC' | 'DNI' | 'CE'
            'customerType': 'business' if document_type == 'RUC' else 'individual',
        }
        if email:
            payload['email'] = email
        if phone:
            payload['phone'] = phone
        return self._post('/core/customers', payload)

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def search_products(self, query: str) -> list:
        """Fuzzy search products by name/SKU."""
        resp = self._get('/core/sales/search-product', params={'q': query, 'limit': 5})
        return resp.get('products') or resp.get('items') or []

    # ------------------------------------------------------------------
    # Document series
    # ------------------------------------------------------------------

    def get_document_series(self, document_type: str) -> Optional[dict]:
        """Get the active document series for factura (01) or boleta (03)."""
        resp = self._get('/core/settings/document-series')
        series_list = resp.get('series') or resp.get('items') or []
        for s in series_list:
            if s.get('documentType') == document_type and s.get('isActive'):
                return s
        return None

    # ------------------------------------------------------------------
    # Sales
    # ------------------------------------------------------------------

    def create_sale(self, payload: dict) -> dict:
        """POST /core/sales — creates a draft sale."""
        resp = self._post('/core/sales', payload)
        return resp.get('sale') or resp

    def add_sale_item(self, sale_id: str, item: dict) -> dict:
        """POST /core/sales/{saleId}/items — adds a line item."""
        resp = self._post(f'/core/sales/{sale_id}/items', item)
        return resp.get('item') or resp

    def complete_sale(self, sale_id: str, payment: dict) -> dict:
        """POST /core/sales/{saleId}/complete — finalizes the sale."""
        resp = self._post(f'/core/sales/{sale_id}/complete', payment)
        return resp.get('sale') or resp

    def send_to_sunat(self, sale_id: str) -> dict:
        """POST /core/sales/{saleId}/send-sunat — emits to SUNAT."""
        resp = self._post(f'/core/sales/{sale_id}/send-sunat', {})
        return resp

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(
            f"{self._base}{path}",
            headers=self._headers,
            params=params,
            timeout=20,
        )
        self._raise_for_error(resp)
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        resp = requests.post(
            f"{self._base}{path}",
            headers=self._headers,
            json=payload,
            timeout=30,
        )
        self._raise_for_error(resp)
        return resp.json()

    def _raise_for_error(self, resp: requests.Response):
        if not resp.ok:
            try:
                msg = resp.json().get('error') or resp.json().get('message') or resp.text[:200]
            except Exception:
                msg = resp.text[:200]
            raise PosApiError(resp.status_code, msg)
