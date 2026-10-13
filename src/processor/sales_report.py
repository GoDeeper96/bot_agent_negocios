"""
Sales report generator using apisunat.com's getAll + XML parsing.

Usage:
    python sales_report.py [--year 2025] [--type 01,03] [--csv output.csv]
                           [--persona-id ID] [--persona-token TOKEN]

Credentials fall back to .env.dev file if CLI args are omitted.

Doc types:
    01 = Factura
    03 = Boleta
    09 = Guia de Remision (skipped for sales totals by default)
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

_BASE_URL = "https://back.apisunat.com"
_DOC_TYPE_LABEL = {"01": "Factura", "03": "Boleta", "09": "Guia"}
_UBL_NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

def _load_env_dev() -> dict:
    env_path = Path(__file__).parent.parent.parent / ".env.dev"
    result = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
    return result


def _get_credentials(args) -> tuple[str, str]:
    persona_id = args.persona_id or os.environ.get("PERSONA_ID")
    persona_token = args.persona_token or os.environ.get("PERSONA_TOKEN")
    if not persona_id or not persona_token:
        env = _load_env_dev()
        persona_id = persona_id or env.get("personaId")
        persona_token = persona_token or env.get("personaToken")
    if not persona_id or not persona_token:
        print("ERROR: No credentials found. Pass --persona-id and --persona-token, "
              "set PERSONA_ID/PERSONA_TOKEN env vars, or add them to .env.dev")
        sys.exit(1)
    return persona_id, persona_token


# ---------------------------------------------------------------------------
# apisunat getAll (paginated)
# ---------------------------------------------------------------------------

def _year_to_unix(year: int) -> tuple[int, int]:
    start = int(datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    return start, end


def fetch_all_documents(persona_id: str, persona_token: str,
                        from_ts: int, to_ts: int,
                        doc_types: list[str]) -> list[dict]:
    """Paginate through getAll for each doc type and return merged list."""
    all_docs = []
    for doc_type in doc_types:
        skip = 0
        limit = 100
        print(f"  Fetching type={doc_type} ({_DOC_TYPE_LABEL.get(doc_type, doc_type)})...")
        while True:
            params = {
                "personaId": persona_id,
                "personaToken": persona_token,
                "from": from_ts,
                "to": to_ts,
                "type": doc_type,
                "limit": limit,
                "skip": skip,
                "order": "ASC",
            }
            resp = requests.get(f"{_BASE_URL}/documents/getAll", params=params, timeout=30)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_docs.extend(batch)
            print(f"    skip={skip} → got {len(batch)} docs (total so far: {len(all_docs)})")
            if len(batch) < limit:
                break
            skip += limit
    return all_docs


# ---------------------------------------------------------------------------
# XML parsing to extract monetary totals
# ---------------------------------------------------------------------------

def _parse_xml_amounts(xml_url: str, session: requests.Session) -> dict:
    """Download the signed UBL 2.1 XML and extract monetary fields."""
    try:
        resp = session.get(xml_url, timeout=20)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        def find_text(path: str) -> str | None:
            el = root.find(path, _UBL_NS)
            return el.text.strip() if el is not None and el.text else None

        # Currency
        currency = find_text("cbc:DocumentCurrencyCode") or "PEN"

        # LegalMonetaryTotal
        base = find_text("cac:LegalMonetaryTotal/cbc:LineExtensionAmount")
        total_inc = find_text("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount")
        payable = find_text("cac:LegalMonetaryTotal/cbc:PayableAmount")

        # IGV from TaxTotal
        igv = find_text("cac:TaxTotal/cbc:TaxAmount")

        return {
            "currency": currency,
            "base": float(base) if base else 0.0,
            "igv": float(igv) if igv else 0.0,
            "total": float(payable or total_inc or 0),
            "parse_error": None,
        }
    except Exception as e:
        return {"currency": "?", "base": 0.0, "igv": 0.0, "total": 0.0, "parse_error": str(e)}


# ---------------------------------------------------------------------------
# Main report logic
# ---------------------------------------------------------------------------

def build_report(persona_id: str, persona_token: str,
                 year: int, doc_types: list[str],
                 csv_path: str | None) -> None:
    from_ts, to_ts = _year_to_unix(year)
    print(f"\nFetching documents for year {year} "
          f"({datetime.fromtimestamp(from_ts, tz=timezone.utc).date()} -> "
          f"{datetime.fromtimestamp(to_ts, tz=timezone.utc).date()})...")

    docs = fetch_all_documents(persona_id, persona_token, from_ts, to_ts, doc_types)

    if not docs:
        print(f"\nNo documents found for {year}.")
        return

    print(f"\nFound {len(docs)} documents. Downloading XMLs to extract amounts...\n")

    session = requests.Session()
    rows = []
    errors = []

    for i, doc in enumerate(docs, 1):
        doc_id = doc.get("id", "")
        file_name = doc.get("fileName", "")
        xml_url = doc.get("xml")
        status = doc.get("status", "")
        issue_ts = doc.get("issueTime", 0)
        issue_date = (datetime.fromtimestamp(issue_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                      if issue_ts else "")

        # Derive doc type and serie/number from fileName (e.g. 20607960225-01-F001-00000123)
        parts = file_name.split("-") if file_name else []
        doc_type = parts[1] if len(parts) > 1 else ""
        serie = parts[2] if len(parts) > 2 else ""
        number = parts[3] if len(parts) > 3 else ""

        amounts = {"currency": "?", "base": 0.0, "igv": 0.0, "total": 0.0, "parse_error": "no xml"}
        if xml_url:
            amounts = _parse_xml_amounts(xml_url, session)
            # small delay to be polite to the server
            if i % 10 == 0:
                time.sleep(0.5)

        row = {
            "doc_id": doc_id,
            "file_name": file_name,
            "date": issue_date,
            "type": _DOC_TYPE_LABEL.get(doc_type, doc_type),
            "serie": serie,
            "number": number,
            "status": status,
            "currency": amounts["currency"],
            "base": amounts["base"],
            "igv": amounts["igv"],
            "total": amounts["total"],
            "error": amounts["parse_error"] or "",
        }
        rows.append(row)

        if amounts["parse_error"]:
            errors.append(f"  [{file_name}] {amounts['parse_error']}")

        # Progress
        print(f"  [{i:3d}/{len(docs)}] {file_name:<40} "
              f"{amounts['currency']} {amounts['total']:>10.2f}  {status}")

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  SALES REPORT — {year}")
    print(f"{'='*60}")

    # Group by currency
    from collections import defaultdict
    by_currency: dict[str, dict] = defaultdict(lambda: {"base": 0.0, "igv": 0.0, "total": 0.0, "count": 0})

    accepted_statuses = {"ACEPTADO", "PENDIENTE"}  # include PENDIENTE (not yet confirmed rejected)
    accepted_rows = [r for r in rows if r["status"] in accepted_statuses or not r["status"]]
    skipped = len(rows) - len(accepted_rows)

    for r in accepted_rows:
        cur = r["currency"]
        by_currency[cur]["base"] += r["base"]
        by_currency[cur]["igv"] += r["igv"]
        by_currency[cur]["total"] += r["total"]
        by_currency[cur]["count"] += 1

    print(f"  Total documents fetched : {len(rows)}")
    if skipped:
        print(f"  Skipped (RECHAZADO)     : {skipped}")
    print(f"  Accepted / counted      : {len(accepted_rows)}")
    print()

    for cur, sums in sorted(by_currency.items()):
        print(f"  Currency : {cur}")
        print(f"    Documents : {sums['count']}")
        print(f"    Base (sin IGV) : {sums['base']:>14,.2f}")
        print(f"    IGV (18%)      : {sums['igv']:>14,.2f}")
        print(f"    Total payable  : {sums['total']:>14,.2f}")
        print()

    if errors:
        print(f"  Warnings ({len(errors)} XML parse errors):")
        for e in errors[:10]:
            print(e)
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")
        print()

    # ---------------------------------------------------------------------------
    # CSV export
    # ---------------------------------------------------------------------------
    if csv_path:
        fieldnames = ["date", "type", "serie", "number", "status",
                      "currency", "base", "igv", "total", "file_name", "doc_id", "error"]
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r[k] for k in fieldnames})
        print(f"  CSV saved to: {csv_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate sales report from apisunat.com")
    parser.add_argument("--year", type=int, default=2025, help="Year to report (default: 2025)")
    parser.add_argument("--type", dest="doc_types", default="01,03",
                        help="Comma-separated doc type codes (default: 01,03)")
    parser.add_argument("--csv", dest="csv_path", default=None,
                        help="Optional path to save CSV export")
    parser.add_argument("--persona-id", default=None)
    parser.add_argument("--persona-token", default=None)
    args = parser.parse_args()

    persona_id, persona_token = _get_credentials(args)
    doc_types = [t.strip() for t in args.doc_types.split(",") if t.strip()]

    build_report(
        persona_id=persona_id,
        persona_token=persona_token,
        year=args.year,
        doc_types=doc_types,
        csv_path=args.csv_path,
    )


if __name__ == "__main__":
    main()
