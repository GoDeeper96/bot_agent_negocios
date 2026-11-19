"""
Generic cotización tool — works locally and in Claude Code cloud sessions.

Subcommands
-----------
  cliente   <nombre>                 Search a customer in the POS DB (name, RUC, email)
  productos [texto]                  List / search products in the POS DB
  pdf       <cot.json>               Generate the PDF only (no counter, no email) -> out/
  preview   <cot.json>               Next COT number + PDF + email to the PREVIEW address
  send      <cot.json> --cot COT-…   Send the SAME COT number to the customer(s)

cot.json format
---------------
  {
    "to": ["cliente@empresa.com"],          # customer recipients (used by `send`)
    "cotizacion": { ...EXTRACTED dict... }  # same structure as the old send_cot_*.py
  }
  If cotizacion.payment_terms == "CONTADO" and payment_detail is empty, the BCP
  account text is filled in from env vars (never typed by hand).

Environment
-----------
  AWS credentials: AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (cloud) or AWS_PROFILE
                   (defaults to the local "eyatech" profile when no keys are set)
  COT_PREVIEW_EMAIL   preview recipient (required for `preview`)
  LICHAN_BCP_USD_CTA  BCP Dólares account number
  LICHAN_BCP_USD_CCI  BCP Dólares CCI
"""
import argparse
import json
import os
import pathlib
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)  # PDF/email code loads logo/footer by relative path
sys.path.insert(0, str(ROOT / "src" / "processor"))

if not os.environ.get("AWS_ACCESS_KEY_ID"):
    os.environ.setdefault("AWS_PROFILE", "eyatech")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

import boto3  # noqa: E402

SSM_PREFIX = os.environ.get("SSM_PREFIX", "/bot-agent/dev")
OUT_DIR = ROOT / "out"


# ── helpers ────────────────────────────────────────────────────────────────

def _load(path: str) -> tuple[list, dict]:
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    to = data.get("to") or []
    cot = data["cotizacion"]
    cot.setdefault("doc_type", "cotizacion")
    cot.setdefault("currency", "USD")
    cot.setdefault("price_includes_igv", False)
    for k in ("contact_persons", "global_origin", "validity_days", "notes"):
        cot.setdefault(k, None)
    for it in cot.get("items", []):
        it.setdefault("sku", None)
        it.setdefault("origin", None)

    if (cot.get("payment_terms") or "").upper() == "CONTADO" and not cot.get("payment_detail"):
        cta, cci = os.environ.get("LICHAN_BCP_USD_CTA"), os.environ.get("LICHAN_BCP_USD_CCI")
        if not (cta and cci):
            sys.exit("ERROR: set LICHAN_BCP_USD_CTA and LICHAN_BCP_USD_CCI (or give payment_detail)")
        cot["payment_detail"] = f"CONTADO (Depósito en Cta. Cte. BCP Dólares N° {cta} / CCI: {cci})"

    if to:
        cot.setdefault("customer", {})["email"] = ", ".join(to)
    _validate(cot)
    return to, cot


def _validate(cot: dict):
    errors = []
    if not (cot.get("customer") or {}).get("name"):
        errors.append("customer.name vacío")
    items = cot.get("items") or []
    if not items:
        errors.append("sin items")
    for i, it in enumerate(items, 1):
        for k in ("description", "quantity", "unit", "unit_price"):
            if it.get(k) in (None, ""):
                errors.append(f"item {i}: falta {k}")
        if not it.get("presentation"):
            errors.append(f"item {i}: falta presentation (preguntar al usuario)")
    if not cot.get("delivery"):
        errors.append("falta delivery / entrega (preguntar al usuario)")
    if errors:
        sys.exit("ERROR en la cotización:\n  - " + "\n  - ".join(errors))


def _ssm():
    return boto3.client("ssm", region_name="us-west-2")


def _next_cot_number() -> str:
    param = f"{SSM_PREFIX}/cotizacion_counter"
    ssm = _ssm()
    n = int(ssm.get_parameter(Name=param)["Parameter"]["Value"]) + 1
    ssm.put_parameter(Name=param, Value=str(n), Type="String", Overwrite=True)
    return f"COT-{datetime.now().year}-{n:03d}"


def _make_pdf(cot: dict, cot_number: str) -> bytes:
    from cotizacion_pdf import generate_cotizacion_pdf
    pdf = generate_cotizacion_pdf(cot, cot_number)
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{cot_number}.pdf"
    path.write_bytes(pdf)
    print(f"PDF: {path} ({len(pdf):,} bytes)")
    return pdf


def _email(recipients: list, cot: dict, cot_number: str, pdf: bytes) -> bool:
    from email_client import send_cotizacion_email
    subject = f"Cotizacion {cot_number} | {cot['customer']['name']}"
    ok_all = True
    for to in recipients:
        ok = send_cotizacion_email(
            to_email=to, subject=subject, body_text="", pdf_bytes=pdf,
            pdf_filename=f"{cot_number}.pdf", ssm_prefix=SSM_PREFIX,
            extracted=cot, cot_number=cot_number,
        )
        print(f"  {'OK' if ok else 'FALLÓ'} -> {to} — {subject}")
        ok_all &= ok
    return ok_all


def _pos():
    from pos_api import PosApiClient
    return PosApiClient("", "")


# ── subcommands ────────────────────────────────────────────────────────────

def cmd_cliente(a):
    c = _pos().search_customer(a.nombre)
    if not c:
        print("No encontrado.")
        return
    print(json.dumps({k: c.get(k) for k in ("name", "documentType", "documentNumber", "email", "phone", "address")},
                     ensure_ascii=False, indent=2))


def cmd_productos(a):
    prods = _pos().list_products()
    q = (a.texto or "").lower()
    for p in sorted(prods, key=lambda p: p.get("name") or ""):
        name = p.get("name") or ""
        if q and q not in name.lower() and q not in (p.get("sku") or "").lower():
            continue
        print(f"{p.get('sku') or '-':<12} {name}")


def cmd_pdf(a):
    _, cot = _load(a.archivo)
    _make_pdf(cot, a.cot or "COT-BORRADOR")


def cmd_preview(a):
    preview = os.environ.get("COT_PREVIEW_EMAIL")
    if not preview:
        sys.exit("ERROR: set COT_PREVIEW_EMAIL")
    _, cot = _load(a.archivo)
    cot_number = a.cot or _next_cot_number()
    print(f"Número: {cot_number}")
    pdf = _make_pdf(cot, cot_number)
    ok = _email([preview], cot, cot_number, pdf)
    print(f"\nVista previa enviada. Para enviar al cliente:\n"
          f"  python scripts/cotizacion.py send {a.archivo} --cot {cot_number}")
    sys.exit(0 if ok else 1)


def cmd_send(a):
    to, cot = _load(a.archivo)
    if not to:
        sys.exit("ERROR: el JSON no tiene destinatarios en 'to'")
    pdf = _make_pdf(cot, a.cot)
    sys.exit(0 if _email(to, cot, a.cot, pdf) else 1)


def main():
    p = argparse.ArgumentParser(description="Cotizaciones Negocios Múltiples Lichan")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("cliente"); s.add_argument("nombre"); s.set_defaults(fn=cmd_cliente)
    s = sub.add_parser("productos"); s.add_argument("texto", nargs="?"); s.set_defaults(fn=cmd_productos)
    s = sub.add_parser("pdf"); s.add_argument("archivo"); s.add_argument("--cot"); s.set_defaults(fn=cmd_pdf)
    s = sub.add_parser("preview"); s.add_argument("archivo"); s.add_argument("--cot", help="reusar número")
    s.set_defaults(fn=cmd_preview)
    s = sub.add_parser("send"); s.add_argument("archivo"); s.add_argument("--cot", required=True)
    s.set_defaults(fn=cmd_send)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
