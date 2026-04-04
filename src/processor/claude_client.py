"""
Gemini AI client — extracts structured invoice data from dad's informal text.

Input:  list of raw WhatsApp message strings (accumulated)
Output: structured dict with customer, items, currency, missing fields
"""
import json
import logging

from google import genai

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Eres un asistente que extrae datos de facturas y cotizaciones a partir de mensajes informales de WhatsApp en español.

El usuario es un vendedor peruano que envía mensajes informales describiendo ventas. Debes extraer los datos estructurados.

REGLAS:
- Si el precio tiene "$" es USD. Si tiene "S/." o "Soles" es PEN.
- "MAS IGV" o "+ IGV" significa que el IGV (18%) se agrega al precio indicado. El precio mostrado es BASE (sin IGV).
- Si dice "INC. IGV" o "INCLUYE IGV" el precio ya incluye el IGV.
- Para facturas se necesita RUC del cliente. Para boletas el DNI es opcional.
- Si menciona "FACTURA" o "FACTURAR" → doc_type = "factura"
- Si menciona "BOLETA" → doc_type = "boleta"
- Si menciona "COTIZACION" o "COTIZACIÓN" → doc_type = "cotizacion"
- Si no especifica → doc_type = "unknown"
- Cantidades: interpreta unidades como KGS, KG, UNIDADES, CAJAS, etc.

MENSAJES DEL USUARIO:
{messages}

Responde ÚNICAMENTE con un JSON válido con esta estructura (sin markdown, sin explicaciones):
{{
  "doc_type": "factura|boleta|cotizacion|unknown",
  "currency": "USD|PEN",
  "price_includes_igv": false,
  "customer": {{
    "name": "nombre o null",
    "ruc": "11 dígitos o null",
    "dni": "8 dígitos o null",
    "email": "email o null",
    "contact_person": "nombre contacto o null",
    "address": "dirección o null"
  }},
  "items": [
    {{
      "description": "descripción del producto",
      "origin": "procedencia o null",
      "presentation": "presentación/empaque o null",
      "quantity": número,
      "unit": "KGS|UNIDADES|CAJAS|etc",
      "unit_price": número (precio base sin IGV),
      "sku": "código o null"
    }}
  ],
  "delivery": "condición de entrega o null",
  "payment_terms": "CONTADO|CREDITO 30 DIAS|etc o null",
  "notes": "observaciones adicionales o null",
  "missing_fields": ["lista de campos requeridos que faltan"]
}}

Para missing_fields incluye los que faltan para procesar el documento:
- "customer_name" si falta nombre del cliente
- "customer_ruc" si es factura y falta RUC
- "customer_email" si falta email (para envío de PDF)
- "items" si no hay productos
- "quantity" si falta cantidad de algún producto
- "unit_price" si falta precio de algún producto
- "doc_type" si no se sabe si es factura, boleta o cotización"""


class GeminiClient:
    def __init__(self, api_key: str):
        self._client = genai.Client(api_key=api_key)

    def extract_invoice_data(self, messages: list, doc_type: str = None) -> dict:
        """
        Extract structured invoice data from accumulated WhatsApp messages.
        Returns parsed dict, or raises on failure.
        """
        messages_text = "\n".join(f"- {m}" for m in messages)
        doc_hint = f"\nEl usuario ya seleccionó el tipo de documento: *{doc_type}*. Usa este valor para doc_type." if doc_type else ""
        prompt = EXTRACTION_PROMPT.format(messages=messages_text) + doc_hint

        response = self._client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
        )
        raw = response.text.strip()

        # Strip markdown code blocks if Gemini wraps the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        logger.info(f"Gemini extraction: {raw[:200]}")

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"Gemini returned invalid JSON: {raw}")
            raise ValueError(f"No se pudo procesar la extracción: {e}")
