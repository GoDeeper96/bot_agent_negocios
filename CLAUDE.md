# Negocios Múltiples Lichan — bot y cotizaciones

Repo **público**: nunca escribir aquí correos de clientes, números de cuenta, llaves ni
datos de clientes. Esos valores vienen de variables de entorno o de la base de datos del POS.

## Preparación (sesiones en la nube)

```bash
pip install -q -r src/processor/requirements.txt
```

Variables de entorno necesarias: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_DEFAULT_REGION=us-west-2`, `COT_PREVIEW_EMAIL`, `LICHAN_BCP_USD_CTA`, `LICHAN_BCP_USD_CCI`.
Si falta alguna, avisar al usuario: no inventar valores.

## Cómo hacer una cotización

Herramienta: `scripts/cotizacion.py`. **No** crear scripts `send_cot_*.py` nuevos.

1. **Cliente**: `python scripts/cotizacion.py cliente "<nombre>"` → nombre exacto, RUC y email.
   Si no aparece, pedir RUC y email al usuario.
2. **Productos** (opcional, para el nombre exacto): `python scripts/cotizacion.py productos <texto>`.
3. **Datos faltantes**: si el usuario no dio la **presentación** de cada producto o la
   **entrega**, preguntarlas antes de seguir. El script falla si faltan.
4. Escribir `cotizaciones/<cliente>_<producto>.json` (carpeta ignorada por git):

   ```json
   {
     "to": ["correo@cliente.com"],
     "cotizacion": {
       "customer": {"name": "EMPRESA S.A.C", "ruc": "20XXXXXXXXX"},
       "currency": "USD",
       "price_includes_igv": false,
       "payment_terms": "CONTADO",
       "delivery": "Máx. 03 días útiles en sus almacenes de Lima",
       "notes": null,
       "items": [
         {"description": "PRODUCTO", "origin": "China", "presentation": "Cilindros x 200 kgs",
          "quantity": 1000, "unit": "KGS", "unit_price": 1.50}
       ]
     }
   }
   ```

   - "a $X más IGV" → `price_includes_igv: false`. "incluido IGV" → `true`.
   - `payment_terms: "CONTADO"` sin `payment_detail` → el script pone solo la cuenta BCP.
     Para crédito u otras condiciones, escribir `payment_detail` con lo que diga el usuario.
   - Varios productos para el mismo cliente van en **una sola** cotización.
5. **Vista previa**: `python scripts/cotizacion.py preview cotizaciones/<archivo>.json`.
   Esto toma el siguiente número COT y envía el PDF al correo de vista previa. Mostrar al
   usuario el resumen completo (cliente, ítems, totales, entrega, pago) y el número COT.
6. **Esperar el OK explícito del usuario.** Nunca enviar al cliente sin confirmación.
7. **Envío**: `python scripts/cotizacion.py send cotizaciones/<archivo>.json --cot COT-AAAA-NNN`
   (el **mismo** número de la vista previa; no generar otro).

Si el usuario pide cambios después de la vista previa: editar el JSON y repetir
`preview ... --cot COT-AAAA-NNN` con el mismo número.

`python scripts/cotizacion.py pdf <archivo>.json` genera solo el PDF en `out/`, sin correo
ni contador. Sirve para pruebas.

## Reglas del correo

- Tono formal. Nada de "Don X" ni apodos.
- El correo lleva el footer `footer_negocios_multiples_lichan.png` (lo pone `email_client.py`).
- Números de cuenta: solo los de las variables de entorno. Nunca escribirlos de memoria.
