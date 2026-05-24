"""
Gemini AI client — extracts structured invoice data from dad's informal text.

Input:  list of raw WhatsApp message strings (accumulated)
Output: structured dict with customer, items, currency, missing fields
"""
import json
import logging

from google import genai

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Eres un asistente que extrae datos de facturas, cotizaciones y guías de remisión a partir de mensajes informales de WhatsApp en español.

El usuario es un vendedor peruano que envía mensajes informales describiendo ventas o despachos. Debes extraer los datos estructurados.

REGLAS GENERALES:
- Si el precio tiene "$" es USD. Si tiene "S/." o "Soles" es PEN.
- "MAS IGV" o "+ IGV" → price_includes_igv = false (precio base, se suma IGV).
- "INC. IGV" o "INCLUYE IGV" → price_includes_igv = true.
- Para facturas se necesita RUC del cliente. Para boletas el DNI es opcional.
- Si menciona "FACTURA" o "FACTURAR" → doc_type = "factura"
- Si menciona "BOLETA" → doc_type = "boleta"
- Si menciona "COTIZACION" o "COTIZACIÓN" o "COTIZAR" → doc_type = "cotizacion"
- Si menciona "GUIA" o "GUÍA" o "GUIA DE REMISION" → doc_type = "guia"
- Si menciona "ORDEN DE COMPRA" o "OC" o "PURCHASE ORDER" → doc_type = "orden_compra"
- Si no especifica → doc_type = "unknown"
- Cantidades: interpreta unidades como KGS, KG, UNIDADES, CAJAS, TN, LT, etc.
- Cualquier dirección de email mencionada en el mensaje es SIEMPRE el email del cliente (destinatario de la cotización/factura). El remitente del mensaje es el vendedor de Lichan, no el cliente.
- Nombres de productos: los números que forman parte del nombre o código del producto (ej: "PLUARAFAC LF 413", "BASF 500", "PRODUCTO XR-200") deben incluirse en "description". NO elimines números del nombre del producto. Solo va en "presentation" la marca o fabricante que aparece como palabra separada al final (ej: "BASF", "DOW", "SIKA"), nunca un número suelto.

REGLAS PARA COTIZACIONES:
- "Atención:", "Att:", "Attn:", "A/C:" indica contact_persons (puede ser más de uno, separados por "/").
- "Procedencia:" o "Proc:" a nivel global (no por producto) → global_origin.
- "Válido por X días", "validez X días", "vigencia X días" → validity_days (número entero). Si no se menciona explícitamente → null.
- Forma de pago con detalle entre paréntesis → payment_detail. Ej: "Contado (Depósito en cuenta)" → payment_detail.
- Si no se indica validez, dejar null (no usar valor por defecto).
- Si el usuario dice "poner en obs", "poner en observaciones", "obs:", "en observacion", o cualquier indicación de notas/aclaraciones, el texto indicado va en el campo "notes". Ej: "poner en obs que se dan 1000kg ahora y el resto en 15 días" → notes = "1000kg (50%) entrega inmediata; 50% restante en 15 dias habiles".
- El campo "delivery" es para la condición de entrega general (ej: "Entrega inmediata", "Contra entrega"). Cronogramas de entrega escalonada o aclaraciones especiales van en "notes", no en "delivery".

REGLAS PARA ÓRDENES DE COMPRA:
- En una orden de compra, Lichan es el COMPRADOR y el campo "customer" representa al PROVEEDOR/VENDEDOR.
- Extrae los datos del proveedor en "customer": name, ruc, address, phone, email, contact_person.
- "Lugar de entrega" o "Entregar en" → delivery.
- "Tiempo de entrega", "Fecha de entrega", "Fecha de despacho" → delivery_date.
- Forma de pago → payment_terms o payment_detail.
- Los items incluyen: código de producto (sku), cantidad, unidad, descripción y precio unitario.
- "Código:", "Cód.", "Código de producto" → sku del item.

REGLAS PARA GUÍAS DE REMISIÓN:
- El receptor (destinatario) puede identificarse por RUC (11 dígitos) o DNI (8 dígitos).
- Extraer dirección de partida (departure_address) y dirección de llegada (arrival_address).
- Ubigeo: código de 6 dígitos del distrito peruano (ej: "150101" para Lima Cercado). Si se menciona el distrito extrae el ubigeo, si no se sabe usar "150101".
- Datos del transportista: nombre completo (split en firstname/lastname), DNI, licencia de conducir.
- Placa del vehículo: formato peruano (ej: "ABC-123").
- Fecha de traslado: formato YYYY-MM-DD. Si dice "hoy" usar fecha actual, si dice mañana calcular.
- Peso total en KG.
- Items: descripción, cantidad, unidad (sin precio).

MENSAJES DEL USUARIO:
{messages}

Responde ÚNICAMENTE con un JSON válido con esta estructura (sin markdown, sin explicaciones):
{{
  "doc_type": "factura|boleta|cotizacion|guia|orden_compra|unknown",
  "currency": "USD|PEN",
  "price_includes_igv": false,
  "customer": {{
    "name": "nombre o null",
    "ruc": "11 dígitos o null",
    "dni": "8 dígitos o null",
    "email": "email o null",
    "address": "dirección o null (especialmente para orden_compra: dirección del proveedor)",
    "phone": "teléfono o null (para orden_compra: teléfono del proveedor)",
    "contact_person": "nombre contacto principal o null"
  }},
  "contact_persons": "Ing. Roberto Roeder / Srta. Rojana Hurtado o null",
  "global_origin": "China o null (procedencia global del pedido, no por producto)",
  "validity_days": null,
  "payment_detail": "Contado (Depósito en Cuenta Corriente) o null",
  "items": [
    {{
      "description": "descripción del producto",
      "origin": "procedencia del producto o null",
      "presentation": "presentación/empaque o null",
      "quantity": número,
      "unit": "KGS|UNIDADES|CAJAS|TN|LT|etc",
      "unit_price": número,
      "sku": "código o null"
    }}
  ],
  "delivery": "condición de entrega o lugar de entrega o null",
  "delivery_date": "fecha de entrega en formato DD/MM/YYYY o null (para orden_compra: tiempo de entrega)",
  "payment_terms": "CONTADO|CREDITO 30 DIAS|etc o null",
  "notes": "observaciones adicionales o null",
  "guia": {{
    "receiver_ruc": "RUC 11 dígitos o null",
    "receiver_name": "razón social del destinatario o null",
    "total_weight_kg": número o null,
    "departure_address": "dirección de partida o null",
    "departure_ubigeo": "6 dígitos o null",
    "arrival_address": "dirección de llegada o null",
    "arrival_ubigeo": "6 dígitos o null",
    "transport_date": "YYYY-MM-DD o null",
    "driver_firstname": "primer nombre del conductor o null",
    "driver_lastname": "apellido del conductor o null",
    "driver_dni": "DNI 8 dígitos del conductor o null",
    "driver_license": "número de licencia de conducir o null",
    "vehicle_plate": "placa del vehículo o null",
    "transport_mode": "01 o 02 (01=privado, 02=público/tercero)"
  }},
  "missing_fields": ["lista de campos requeridos que faltan"]
}}

Para missing_fields incluye:
- "customer_name" si falta nombre del cliente (o proveedor en orden_compra)
- "customer_ruc" si es factura y falta RUC
- "customer_doc" si es cotizacion o boleta y falta tanto RUC como DNI (se requiere uno de los dos)
- "customer_email" si falta email (para envío de cotización)
- "items" si no hay productos
- "quantity" si falta cantidad de algún producto
- "unit_price" si falta precio de algún producto (no aplica para guía)
- "doc_type" si no se sabe qué tipo de documento es
- SOLO si doc_type = "guia": "guia_receiver" si falta nombre o RUC del destinatario
- SOLO si doc_type = "guia": "guia_driver" si faltan datos del conductor (nombre, DNI o licencia)
- SOLO si doc_type = "guia": "guia_vehicle" si falta la placa del vehículo
- SOLO si doc_type = "guia": "guia_addresses" si faltan las direcciones de partida/llegada
- SOLO si doc_type = "guia": "guia_weight" si falta el peso total
- SOLO si doc_type = "guia": "guia_date" si falta la fecha de traslado
- NUNCA incluyas campos "guia_*" en missing_fields si doc_type es factura, boleta, cotizacion u orden_compra"""


STOCK_MOVEMENT_PROMPT = """Eres un asistente que extrae datos de movimientos de stock a partir de mensajes informales en español.

El usuario es un vendedor peruano que registra ingresos o salidas de productos.

REGLAS:
- "ingresé", "recibí", "llegó", "compré", "entrada de" → movement_type = "entry"
- "salida de", "despachamos", "vendimos", "consumimos" → movement_type = "exit"
- "ajuste", "corrección de stock" → movement_type = "adjustment"
- Si no se especifica → movement_type = "entry" (lo más común)
- Razones de entrada: "purchase" (compra), "return" (devolución), "initial" (stock inicial), "adjustment"
- Razones de salida: "sale" (venta), "damage" (daño), "expired" (vencido), "correction"
- Referencia: número de OC, factura, guía u otro documento mencionado
- Cantidad: extraer número y unidad (KG, UNIDADES, CAJAS, etc.)
- Producto: nombre o SKU mencionado. Si menciona código (ej: NL-POT-001) usar como sku.

MENSAJES:
{messages}

Responde ÚNICAMENTE con JSON válido (sin markdown):
{{
  "movement_type": "entry|exit|adjustment",
  "reason": "purchase|return|initial|adjustment|sale|damage|expired|correction",
  "product_name": "nombre del producto o null",
  "sku": "código SKU o null",
  "quantity": número,
  "unit": "KG|UNIDADES|CAJAS|etc",
  "reference": "número de OC/factura/guía o null",
  "notes": "observaciones adicionales o null",
  "missing_fields": ["product_name si falta", "quantity si falta"]
}}"""


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

    def extract_stock_movement(self, messages: list) -> dict:
        """Extract stock movement data from accumulated WhatsApp messages."""
        messages_text = "\n".join(f"- {m}" for m in messages)
        prompt = STOCK_MOVEMENT_PROMPT.format(messages=messages_text)

        response = self._client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        logger.info(f"Gemini stock extraction: {raw[:200]}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"Gemini returned invalid JSON for stock: {raw}")
            raise ValueError(f"No se pudo procesar el movimiento: {e}")
