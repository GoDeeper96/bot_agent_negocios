"""
Client for the new_dc_api_2026 POS API.

Invokes fourdist Lambda functions directly (bypasses API Gateway) using
boto3 Lambda.invoke — same AWS account, no HTTP auth needed.
"""
import json
import logging
import os
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

# Lambda function names — resolved from env or defaults
_STAGE = os.environ.get('STAGE', 'dev')
_CUSTOMER_FN = f"fourdist-{_STAGE}-CustomerFunction-2Tfdyup6KDnx"
_SALES_FN    = f"fourdist-{_STAGE}-SalesFunction-P80nMbBhr9lC"
_SETTINGS_FN = f"fourdist-{_STAGE}-SettingsFunction-hXhU81XQ3dWZ"


class PosApiError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"POS API {status_code}: {message}")


def _make_auth_user(user_id: str, enterprise_id: str, company_id: str) -> dict:
    return {
        "userId":       user_id,
        "enterpriseId": enterprise_id,
        "companyId":    company_id,
        "roleId":       "role-master-admin",
        "roleLevel":    1,
        "permissions":  ["admin:all"],
    }


def _invoke(function_name: str, method: str, path: str,
            body: dict = None, query: dict = None,
            path_params: dict = None, auth_user: dict = None) -> dict:
    """Invoke a fourdist Lambda directly with a synthetic API Gateway event."""
    event = {
        "httpMethod":    method,
        "path":          path,
        "body":          json.dumps(body or {}),
        "pathParameters":       path_params or {},
        "queryStringParameters": query or {},
        "requestContext": {
            "stage":        _STAGE,
            "resourcePath": path,
            "authorizer": {
                "claims": {
                    "sub":   (auth_user or {}).get("userId", ""),
                    "email": "",
                }
            }
        }
    }

    client = boto3.client("lambda", region_name="us-west-2")
    resp = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode(),
    )

    raw = resp["Payload"].read()
    result = json.loads(raw)

    if resp.get("FunctionError"):
        raise PosApiError(500, f"Lambda error: {result}")

    status = result.get("statusCode", 200)
    try:
        response_body = json.loads(result.get("body", "{}"))
    except Exception:
        response_body = {}

    if status >= 400:
        msg = response_body.get("error") or response_body.get("message") or str(response_body)[:200]
        logger.error(f"fourdist {function_name} {method} {path} → {status}: {msg}")
        raise PosApiError(status, msg)

    return response_body


class PosApiClient:
    def __init__(self, base_url: str, token: str):
        # base_url and token kept for interface compatibility but not used
        # Auth is implicit via Lambda execution role (same AWS account)
        self._auth = _make_auth_user(
            user_id="a8c1e360-8071-70c6-2465-fe534058050e",
            enterprise_id="enterprise-0658d531-21f9-48ad-8ed7-6a9fb65a91c0",
            company_id="company-lichan",
        )

    # ------------------------------------------------------------------
    # Customers
    # ------------------------------------------------------------------

    def search_customer(self, name: str) -> Optional[dict]:
        resp = _invoke(_CUSTOMER_FN, "GET", "/core/customers",
                       query={"search": name, "limit": "5"},
                       auth_user=self._auth)
        logger.info(f"search_customer raw response: {str(resp)[:300]}")
        customers = resp.get("customers") or resp.get("items") or []
        data = resp.get("data")
        if data and not customers:
            # data may be a list or a dict with numeric keys
            if isinstance(data, list):
                customers = data
            elif isinstance(data, dict):
                customers = list(data.values())
        if not customers:
            return None
        return customers[0]

    def create_customer(self, name: str, document_number: str, document_type: str,
                        email: str = None, phone: str = None) -> dict:
        payload = {
            "name":           name,
            "documentNumber": document_number,
            "documentType":   document_type,
            "customerType":   "business" if document_type == "RUC" else "individual",
        }
        if email:
            payload["email"] = email
        if phone:
            payload["phone"] = phone
        return _invoke(_CUSTOMER_FN, "POST", "/core/customers",
                       body=payload, auth_user=self._auth)

    # ------------------------------------------------------------------
    # Document series
    # ------------------------------------------------------------------

    def get_document_series(self, document_type: str) -> Optional[dict]:
        resp = _invoke(_SETTINGS_FN, "GET", "/core/settings/document-series",
                       auth_user=self._auth)
        logger.info(f"get_document_series raw response: {str(resp)[:300]}")
        series_list = resp.get("series") or resp.get("items") or []
        data = resp.get("data")
        if data and not series_list:
            series_list = data if isinstance(data, list) else list(data.values())
        for s in series_list:
            if s.get("documentType") == document_type and s.get("isActive"):
                return s
        return None

    # ------------------------------------------------------------------
    # Sales
    # ------------------------------------------------------------------

    def create_sale(self, payload: dict) -> dict:
        resp = _invoke(_SALES_FN, "POST", "/core/sales",
                       body=payload, auth_user=self._auth)
        return resp.get("sale") or resp.get("data") or resp

    def add_sale_item(self, sale_id: str, item: dict) -> dict:
        resp = _invoke(_SALES_FN, "POST", f"/core/sales/{sale_id}/items",
                       body=item, path_params={"saleId": sale_id},
                       auth_user=self._auth)
        return resp.get("item") or resp.get("data") or resp

    def complete_sale(self, sale_id: str, payment: dict) -> dict:
        resp = _invoke(_SALES_FN, "POST", f"/core/sales/{sale_id}/complete",
                       body=payment, path_params={"saleId": sale_id},
                       auth_user=self._auth)
        return resp.get("sale") or resp.get("data") or resp

    def send_to_sunat(self, sale_id: str) -> dict:
        resp = _invoke(_SALES_FN, "POST", f"/core/sales/{sale_id}/send-sunat",
                       body={}, path_params={"saleId": sale_id},
                       auth_user=self._auth)
        return resp
