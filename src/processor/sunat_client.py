"""
Direct SUNAT invoice submission via apisunat.com API.
Builds a valid UBL 2.1 JSON document (matching the apisunat platform format)
and POSTs it for electronic signing and SUNAT emission.

Key fix vs. internal POS API path: cbc:InvoiceTypeCode includes listID="0101"
(SUNAT error 3244 fires when that attribute is absent).
"""
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

_COMPANY_RUC  = "20607960225"
_COMPANY_NAME = "NEGOCIOS MULTIPLES LICHAN S.A.C."
_COMPANY_ADDR = "Av. Guillermo Billinghurts 1089-A, San Juan de Miraflores, Lima"

_APISUNAT_URL = "https://back.apisunat.com/personas/v1/sendBill"

# Informal unit label → SUNAT unitCode
_UOM_MAP = {
    "kg": "KGM", "kgs": "KGM", "kilogramo": "KGM", "kilogramos": "KGM",
    "kilos": "KGM", "kilo": "KGM",
    "und": "NIU", "unidad": "NIU", "unidades": "NIU", "un": "NIU",
    "piezas": "NIU", "pieza": "NIU", "pza": "NIU", "unid": "NIU",
    "caja": "CA", "cajas": "CA",
    "saco": "BG", "sacos": "BG", "bolsa": "BG", "bolsas": "BG",
    "litro": "LTR", "litros": "LTR", "lt": "LTR",
    "servicio": "ZZ", "servicios": "ZZ",
    "tonelada": "TNE", "toneladas": "TNE", "tn": "TNE", "ton": "TNE",
}

_CURRENCY_WORDS = {"PEN": "SOLES", "USD": "DÓLARES ESTADOUNIDENSES"}

_ONES = [
    "", "UNO", "DOS", "TRES", "CUATRO", "CINCO", "SEIS", "SIETE", "OCHO", "NUEVE",
    "DIEZ", "ONCE", "DOCE", "TRECE", "CATORCE", "QUINCE", "DIECISÉIS",
    "DIECISIETE", "DIECIOCHO", "DIECINUEVE",
]
_TENS = ["", "", "VEINTE", "TREINTA", "CUARENTA", "CINCUENTA",
         "SESENTA", "SETENTA", "OCHENTA", "NOVENTA"]
_HUNDREDS = [
    "", "CIENTO", "DOSCIENTOS", "TRESCIENTOS", "CUATROCIENTOS", "QUINIENTOS",
    "SEISCIENTOS", "SETECIENTOS", "OCHOCIENTOS", "NOVECIENTOS",
]


def _int_to_words(n: int) -> str:
    if n == 0:
        return "CERO"
    if n == 100:
        return "CIEN"
    parts = []
    if n >= 1_000_000:
        m = n // 1_000_000
        parts.append("UN MILLÓN" if m == 1 else _int_to_words(m) + " MILLONES")
        n %= 1_000_000
    if n >= 1_000:
        t = n // 1_000
        parts.append("MIL" if t == 1 else _int_to_words(t) + " MIL")
        n %= 1_000
    if n >= 100:
        parts.append(_HUNDREDS[n // 100])
        n %= 100
    if n >= 20:
        r = n % 10
        parts.append(_TENS[n // 10] + (" Y " + _ONES[r] if r else ""))
    elif n > 0:
        parts.append(_ONES[n])
    return " ".join(parts)


def amount_in_words(amount: float, currency: str) -> str:
    """Return SUNAT-formatted amount-in-words note (cbc:Note content)."""
    cents = round((amount - int(amount)) * 100)
    label = _CURRENCY_WORDS.get(currency.upper(), currency)
    return f"{_int_to_words(int(amount))} CON {cents:02d}/100 {label}"


def _uom(unit: str) -> str:
    return _UOM_MAP.get((unit or "").lower().strip(), "NIU")


def _r(v) -> float:
    return round(float(v), 2)


def build_document(
    doc_type_code: str,
    series: str,
    number: str,
    currency: str,
    customer_scheme_id: str,
    customer_doc_number: str,
    customer_name: str,
    customer_address: str,
    items: list,
    price_includes_igv: bool,
    payment_type: str = "Contado",
    issue_date: str = None,
    issue_time: str = None,
) -> dict:
    """
    Build the full apisunat payload dict for a factura (01) or boleta (03).

    items entries: {description, quantity, unit, unit_price}
    price_includes_igv: True if unit_price already contains IGV (18%).
    """
    now = datetime.now()
    issue_date = issue_date or now.strftime("%Y-%m-%d")
    issue_time = issue_time or now.strftime("%H:%M:%S")
    doc_id = f"{series}-{number}"
    currency = currency.upper()

    lines_data = []
    for idx, item in enumerate(items, 1):
        qty = float(item["quantity"])
        price = float(item["unit_price"])
        if price_includes_igv:
            unit_sin = _r(price / 1.18)
            unit_con = round(float(price), 4)
        else:
            unit_sin = _r(price)
            unit_con = round(float(price) * 1.18, 4)
        line_ext = _r(qty * unit_sin)
        line_igv = _r(line_ext * 0.18)
        lines_data.append({
            "idx": idx,
            "desc": item.get("description", "Producto"),
            "qty": qty,
            "unit": _uom(item.get("unit", "")),
            "unit_sin": unit_sin,
            "unit_con": unit_con,
            "line_ext": line_ext,
            "line_igv": line_igv,
        })

    total_base = _r(sum(l["line_ext"] for l in lines_data))
    total_igv  = _r(sum(l["line_igv"] for l in lines_data))
    total      = _r(total_base + total_igv)

    def _cur(val):
        return {"_attributes": {"currencyID": currency}, "_text": val}

    invoice_lines = []
    for ld in lines_data:
        invoice_lines.append({
            "cbc:ID": {"_text": ld["idx"]},
            "cbc:InvoicedQuantity": {
                "_attributes": {"unitCode": ld["unit"]},
                "_text": ld["qty"],
            },
            "cbc:LineExtensionAmount": _cur(ld["line_ext"]),
            "cac:PricingReference": {
                "cac:AlternativeConditionPrice": {
                    "cbc:PriceAmount": _cur(ld["unit_con"]),
                    "cbc:PriceTypeCode": {"_text": "01"},
                }
            },
            "cac:TaxTotal": {
                "cbc:TaxAmount": _cur(ld["line_igv"]),
                "cac:TaxSubtotal": [{
                    "cbc:TaxableAmount": _cur(ld["line_ext"]),
                    "cbc:TaxAmount": _cur(ld["line_igv"]),
                    "cac:TaxCategory": {
                        "cbc:Percent": {"_text": 18},
                        "cbc:TaxExemptionReasonCode": {"_text": "10"},
                        "cac:TaxScheme": {
                            "cbc:ID": {"_text": "1000"},
                            "cbc:Name": {"_text": "IGV"},
                            "cbc:TaxTypeCode": {"_text": "VAT"},
                        },
                    },
                }],
            },
            "cac:Item": {"cbc:Description": {"_text": ld["desc"]}},
            "cac:Price": {"cbc:PriceAmount": _cur(ld["unit_sin"])},
        })

    body = {
        "cbc:UBLVersionID": {"_text": "2.1"},
        "cbc:CustomizationID": {"_text": "2.0"},
        "cbc:ID": {"_text": doc_id},
        "cbc:IssueDate": {"_text": issue_date},
        "cbc:IssueTime": {"_text": issue_time},
        # listID="0101" is REQUIRED — its absence causes SUNAT error 3244
        "cbc:InvoiceTypeCode": {
            "_attributes": {"listID": "0101"},
            "_text": doc_type_code,
        },
        "cbc:Note": [{
            "_text": amount_in_words(total, currency),
            "_attributes": {"languageLocaleID": "1000"},
        }],
        "cbc:DocumentCurrencyCode": {"_text": currency},
        "cac:AccountingSupplierParty": {
            "cac:Party": {
                "cac:PartyIdentification": {
                    "cbc:ID": {"_attributes": {"schemeID": "6"}, "_text": _COMPANY_RUC}
                },
                "cac:PartyLegalEntity": {
                    "cbc:RegistrationName": {"_text": _COMPANY_NAME},
                    "cac:RegistrationAddress": {
                        "cbc:AddressTypeCode": {"_text": "0000"},
                        "cac:AddressLine": {"cbc:Line": {"_text": _COMPANY_ADDR}},
                    },
                },
            }
        },
        "cac:AccountingCustomerParty": {
            "cac:Party": {
                "cac:PartyIdentification": {
                    "cbc:ID": {
                        "_attributes": {"schemeID": customer_scheme_id},
                        "_text": customer_doc_number,
                    }
                },
                "cac:PartyLegalEntity": {
                    "cbc:RegistrationName": {"_text": customer_name.upper()},
                    "cac:RegistrationAddress": {
                        "cac:AddressLine": {"cbc:Line": {"_text": customer_address or "-"}}
                    },
                },
            }
        },
        "cac:TaxTotal": {
            "cbc:TaxAmount": _cur(total_igv),
            "cac:TaxSubtotal": [{
                "cbc:TaxableAmount": _cur(total_base),
                "cbc:TaxAmount": _cur(total_igv),
                "cac:TaxCategory": {
                    "cac:TaxScheme": {
                        "cbc:ID": {"_text": "1000"},
                        "cbc:Name": {"_text": "IGV"},
                        "cbc:TaxTypeCode": {"_text": "VAT"},
                    }
                },
            }],
        },
        "cac:LegalMonetaryTotal": {
            "cbc:LineExtensionAmount": _cur(total_base),
            "cbc:TaxInclusiveAmount": _cur(total),
            "cbc:PayableAmount": _cur(total),
        },
        "cac:PaymentTerms": [{
            "cbc:ID": {"_text": "FormaPago"},
            "cbc:PaymentMeansID": {"_text": payment_type},
        }],
        "cac:InvoiceLine": invoice_lines,
    }

    file_name = f"{_COMPANY_RUC}-{doc_type_code}-{series}-{number}"
    return {"fileName": file_name, "documentBody": body}


# ---------------------------------------------------------------------------
# Ubigeo lookup (common Lima/Peru districts)
# ---------------------------------------------------------------------------
_UBIGEO = {
    # Lima province
    "lima":                  "150101",
    "san juan de miraflores": "150130",
    "villa el salvador":     "150142",
    "villa maria del triunfo": "150141",
    "chorrillos":            "150108",
    "surco":                 "150137", "santiago de surco": "150137",
    "miraflores":            "150122",
    "san borja":             "150131",
    "san isidro":            "150133",
    "la molina":             "150113",
    "ate":                   "150103",
    "san luis":              "150134",
    "el agustino":           "150110",
    "la victoria":           "150116",
    "lince":                 "150119",
    "breña":                 "150105",
    "jesus maria":           "150112",
    "magdalena":             "150120",
    "pueblo libre":          "150126",
    "san miguel":            "150135",
    "callao":                "070101",
    # Other departments (abbreviated)
    "trujillo":              "130101",
    "arequipa":              "040101",
    "chiclayo":              "140101",
    "piura":                 "200101",
    "cusco":                 "080101",
    "iquitos":               "160101",
    "huancayo":              "120101",
}

def _ubigeo(district: str) -> str:
    """Return SUNAT ubigeo code for a district name, default Lima."""
    if not district:
        return "150101"
    return _UBIGEO.get(district.lower().strip(), "150101")


# ---------------------------------------------------------------------------
# Guia de Remision builder
# ---------------------------------------------------------------------------

def build_guia_document(
    serie: str,
    number: str,
    receiver_ruc: str,
    receiver_name: str,
    items: list,                 # [{description, quantity, unit}]
    total_weight_kg: float,
    departure_address: str,
    departure_ubigeo: str,
    arrival_address: str,
    arrival_ubigeo: str,
    transport_date: str,         # YYYY-MM-DD
    driver_firstname: str,
    driver_lastname: str,
    driver_dni: str,
    driver_license: str,         # licencia de conducir
    vehicle_plate: str,
    transport_mode: str = "02",  # 01=privado, 02=público
    issue_date: str = None,
    issue_time: str = None,
) -> dict:
    """Build apisunat payload for Guia de Remision Remitente (type 09)."""
    now = datetime.now()
    issue_date  = issue_date  or now.strftime("%Y-%m-%d")
    issue_time  = issue_time  or now.strftime("%H:%M:%S")
    doc_id      = f"{serie}-{number}"
    file_name   = f"{_COMPANY_RUC}-09-{serie}-{number}"

    lines = []
    for idx, item in enumerate(items, 1):
        qty  = float(item["quantity"])
        unit = _uom(item.get("unit", ""))
        lines.append({
            "cbc:ID": {"_text": idx},
            "cbc:DeliveredQuantity": {
                "_attributes": {"unitCode": unit},
                "_text": qty,
            },
            "cac:OrderLineReference": {"cbc:LineID": {"_text": idx}},
            "cac:Item": {"cbc:Description": {"_text": item.get("description", "Producto")}},
        })

    body = {
        "cbc:UBLVersionID":          {"_text": "2.1"},
        "cbc:CustomizationID":       {"_text": "2.0"},
        "cbc:ID":                    {"_text": doc_id},
        "cbc:IssueDate":             {"_text": issue_date},
        "cbc:IssueTime":             {"_text": issue_time},
        "cbc:DespatchAdviceTypeCode": {"_text": "09"},
        "cac:DespatchSupplierParty": {
            "cac:Party": {
                "cac:PartyIdentification": {
                    "cbc:ID": {"_attributes": {"schemeID": "6"}, "_text": _COMPANY_RUC}
                },
                "cac:PartyLegalEntity": {
                    "cbc:RegistrationName": {"_text": _COMPANY_NAME},
                    "cac:RegistrationAddress": {
                        "cac:AddressLine": {"cbc:Line": {"_text": _COMPANY_ADDR}}
                    },
                },
            }
        },
        "cac:DeliveryCustomerParty": {
            "cac:Party": {
                "cac:PartyIdentification": {
                    "cbc:ID": {"_attributes": {"schemeID": "6"}, "_text": receiver_ruc}
                },
                "cac:PartyLegalEntity": {
                    "cbc:RegistrationName": {"_text": receiver_name.upper()}
                },
            }
        },
        "cac:Shipment": {
            "cbc:ID":                  {"_text": "SUNAT_Envio"},
            "cbc:HandlingCode":        {"_text": "01"},
            "cbc:GrossWeightMeasure":  {
                "_attributes": {"unitCode": "KGM"},
                "_text": round(float(total_weight_kg), 2),
            },
            "cac:ShipmentStage": {
                "cbc:TransportModeCode": {"_text": transport_mode},
                "cac:TransitPeriod": {
                    "cbc:StartDate": {"_text": transport_date}
                },
                "cac:DriverPerson": [{
                    "cbc:ID": {
                        "_attributes": {"schemeID": "1"},
                        "_text": driver_dni,
                    },
                    "cbc:FirstName":  {"_text": driver_firstname.upper()},
                    "cbc:FamilyName": {"_text": driver_lastname.upper()},
                    "cbc:JobTitle":   {"_text": "Principal"},
                    "cac:IdentityDocumentReference": {
                        "cbc:ID": {"_text": driver_license.upper()}
                    },
                }],
            },
            "cac:Delivery": {
                "cac:DeliveryAddress": {
                    "cbc:ID": {"_text": arrival_ubigeo},
                    "cac:AddressLine": {"cbc:Line": {"_text": arrival_address}},
                },
                "cac:Despatch": {
                    "cac:DespatchAddress": {
                        "cbc:ID": {"_text": departure_ubigeo},
                        "cac:AddressLine": {"cbc:Line": {"_text": departure_address}},
                    }
                },
            },
            "cac:TransportHandlingUnit": {
                "cac:TransportEquipment": {
                    "cbc:ID": {"_text": vehicle_plate.upper()}
                }
            },
        },
        "cac:DespatchLine": lines,
    }

    return {"fileName": file_name, "documentBody": body}


class SunatClient:
    def __init__(self, persona_id: str, persona_token: str):
        self._persona_id = persona_id
        self._token = persona_token

    def send_invoice(
        self,
        doc_type_code: str,
        series: str,
        number: str,
        currency: str,
        customer_scheme_id: str,
        customer_doc_number: str,
        customer_name: str,
        customer_address: str,
        items: list,
        price_includes_igv: bool,
        payment_type: str = "Contado",
    ) -> dict:
        """Submit invoice to apisunat. Returns the API response dict."""
        doc = build_document(
            doc_type_code=doc_type_code,
            series=series,
            number=number,
            currency=currency,
            customer_scheme_id=customer_scheme_id,
            customer_doc_number=customer_doc_number,
            customer_name=customer_name,
            customer_address=customer_address,
            items=items,
            price_includes_igv=price_includes_igv,
            payment_type=payment_type,
        )
        payload = {
            "personaId":   self._persona_id,
            "personaToken": self._token,
            "fileName":    doc["fileName"],
            "documentBody": doc["documentBody"],
        }
        import json as _json
        logger.info(f"Sending to apisunat: {doc['fileName']}")
        logger.info(f"apisunat payload: {_json.dumps(payload, ensure_ascii=False)[:2000]}")
        resp = requests.post(_APISUNAT_URL, json=payload, timeout=30)
        if not resp.ok:
            logger.error(f"apisunat {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
        result = resp.json()
        logger.info(f"apisunat response: {str(result)[:400]}")

        status = result.get("status", "")
        faults = result.get("faults") or []
        accepted = status not in ("RECHAZADO",) and not faults
        pending  = status == "PENDIENTE"

        return {
            "accepted": accepted,
            "pending":  pending,
            "documentId": result.get("documentId"),
            "pdfUrl":  result.get("pdf", {}).get("A4") or result.get("pdfUrl"),
            "xmlUrl":  result.get("xml"),
            "faults":  faults,
            "raw":     result,
        }

    def send_guia(
        self,
        serie: str,
        number: str,
        receiver_ruc: str,
        receiver_name: str,
        items: list,
        total_weight_kg: float,
        departure_address: str,
        departure_ubigeo: str,
        arrival_address: str,
        arrival_ubigeo: str,
        transport_date: str,
        driver_firstname: str,
        driver_lastname: str,
        driver_dni: str,
        driver_license: str,
        vehicle_plate: str,
        transport_mode: str = "02",
    ) -> dict:
        """Submit guia de remision to apisunat. Returns the API response dict."""
        doc = build_guia_document(
            serie=serie,
            number=number,
            receiver_ruc=receiver_ruc,
            receiver_name=receiver_name,
            items=items,
            total_weight_kg=total_weight_kg,
            departure_address=departure_address,
            departure_ubigeo=departure_ubigeo,
            arrival_address=arrival_address,
            arrival_ubigeo=arrival_ubigeo,
            transport_date=transport_date,
            driver_firstname=driver_firstname,
            driver_lastname=driver_lastname,
            driver_dni=driver_dni,
            driver_license=driver_license,
            vehicle_plate=vehicle_plate,
            transport_mode=transport_mode,
        )
        payload = {
            "personaId":    self._persona_id,
            "personaToken": self._token,
            "fileName":     doc["fileName"],
            "documentBody": doc["documentBody"],
        }
        import json as _json
        logger.info(f"Sending guia to apisunat: {doc['fileName']}")
        logger.info(f"apisunat guia payload: {_json.dumps(payload, ensure_ascii=False)[:2000]}")
        resp = requests.post(_APISUNAT_URL, json=payload, timeout=30)
        if not resp.ok:
            logger.error(f"apisunat guia {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
        result = resp.json()
        logger.info(f"apisunat guia response: {str(result)[:400]}")

        status = result.get("status", "")
        faults = result.get("faults") or []
        accepted = status not in ("RECHAZADO",) and not faults

        return {
            "accepted":   accepted,
            "documentId": result.get("documentId"),
            "pdfUrl":     result.get("pdf", {}).get("A4") or result.get("pdfUrl"),
            "xmlUrl":     result.get("xml"),
            "faults":     faults,
            "raw":        result,
        }
