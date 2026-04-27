"""
Quick test: submit a USD factura directly to apisunat.
Run from repo root: python test_sunat.py
"""
import os
import json
import sys

# Load .env.dev
env = {}
with open(".env.dev") as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

persona_id    = env["personaId"]
persona_token = env["personaToken"]

sys.path.insert(0, "src/processor")
from sunat_client import SunatClient, build_document

# ── Test data (matches the example the user provided) ──────────────────────
SERIES = "F149"
NUMBER = "00000010"

items = [
    {
        "description": "TRIPOLIFOSFATO DE SODIO",
        "quantity": 1000,
        "unit": "kg",
        "unit_price": 1.40,   # USD, WITHOUT IGV
    }
]

doc = build_document(
    doc_type_code="01",           # Factura
    series=SERIES,
    number=NUMBER,
    currency="USD",
    customer_scheme_id="6",       # RUC
    customer_doc_number="10092978759",
    customer_name="JULON SALCEDO WILMER",
    customer_address="CAL. SAN PEDRO (SAN ANTONIO DE PADUA) SAN JUAN DE MIRAFLORES LIMA LIMA",
    items=items,
    price_includes_igv=False,
)

print("Document to send:")
print(json.dumps(doc, indent=2, ensure_ascii=False))
print()

answer = input("Submit to apisunat? [y/N] ").strip().lower()
if answer != "y":
    print("Aborted.")
    sys.exit(0)

client = SunatClient(persona_id, persona_token)
try:
    resp = client.send_invoice(
        doc_type_code="01",
        series=SERIES,
        number=NUMBER,
        currency="USD",
        customer_scheme_id="6",
        customer_doc_number="10092978759",
        customer_name="JULON SALCEDO WILMER",
        customer_address="CAL. SAN PEDRO (SAN ANTONIO DE PADUA) SAN JUAN DE MIRAFLORES LIMA LIMA",
        items=items,
        price_includes_igv=False,
    )
    print("apisunat response:")
    print(json.dumps(resp, indent=2, ensure_ascii=False))
except Exception as e:
    print(f"Error: {e}")
