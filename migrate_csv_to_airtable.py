"""
One-shot migration of the local Phase 1 CSVs into Airtable.

Order:
  1. Sites        (from sites.csv)
  2. Products     (derived from master_pricing.csv — one row per unique product_code)
  3. PricingRules (from master_pricing.csv, with site/product linked records)

Idempotent: existing records are matched by their natural key and either skipped
or updated. Run as many times as you want.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["AIRTABLE_TOKEN"]
BASE_ID = os.environ["AIRTABLE_BASE_ID"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
DATA_URL = f"https://api.airtable.com/v0/{BASE_ID}"

ROOT = Path(__file__).parent
SCHEMA = json.loads((ROOT / "airtable_schema.json").read_text())
TABLE_IDS = SCHEMA["tables"]

BATCH_SIZE = 10  # Airtable hard limit per batch create/update


# ---------- HTTP helpers ----------

def _list_all(table_id: str, fields: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    params: dict = {"pageSize": 100}
    if fields:
        params["fields[]"] = fields
    offset = None
    while True:
        if offset:
            params["offset"] = offset
        r = requests.get(f"{DATA_URL}/{table_id}", headers=HEADERS, params=params)
        if r.status_code >= 300:
            sys.exit(f"List {table_id} failed: {r.status_code} {r.text}")
        body = r.json()
        out.extend(body["records"])
        offset = body.get("offset")
        if not offset:
            break
    return out


def _batch(records: list[dict], op: str, table_id: str) -> int:
    """op = 'create' (POST) or 'update' (PATCH)."""
    method = requests.post if op == "create" else requests.patch
    done = 0
    for i in range(0, len(records), BATCH_SIZE):
        chunk = records[i : i + BATCH_SIZE]
        body = {"records": chunk, "typecast": True}
        r = method(f"{DATA_URL}/{table_id}", headers=HEADERS, json=body)
        if r.status_code >= 300:
            sys.exit(f"{op} batch failed: {r.status_code} {r.text}")
        done += len(chunk)
        time.sleep(0.25)
    return done


# ---------- Sites ----------

def migrate_sites() -> dict[str, str]:
    """Returns {site_id: airtable_record_id}."""
    print("\n[Sites]")
    table_id = TABLE_IDS["Sites"]
    rows: list[dict] = []
    with (ROOT / "sites.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "site_id": r["site_id"].strip(),
                    "name": r.get("name", "").strip(),
                    "status": (r.get("status") or "tenanted").strip().lower(),
                    "country": "england",  # Phase 2 = LWC only
                    "notes": r.get("notes", "").strip(),
                }
            )
    existing = _list_all(table_id, fields=["site_id"])
    by_sid = {rec["fields"].get("site_id"): rec["id"] for rec in existing}

    to_create, to_update = [], []
    for row in rows:
        sid = row["site_id"]
        if sid in by_sid:
            to_update.append({"id": by_sid[sid], "fields": row})
        else:
            to_create.append({"fields": row})

    created = _batch(to_create, "create", table_id) if to_create else 0
    updated = _batch(to_update, "update", table_id) if to_update else 0
    print(f"  created={created} updated={updated}")

    refreshed = _list_all(table_id, fields=["site_id"])
    return {rec["fields"]["site_id"]: rec["id"] for rec in refreshed if rec["fields"].get("site_id")}


# ---------- Products ----------

def migrate_products() -> dict[str, str]:
    print("\n[Products]")
    table_id = TABLE_IDS["Products"]
    seen: dict[str, dict] = {}
    with (ROOT / "master_pricing.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            code = r["product_code"].strip()
            if not code:
                continue
            if code not in seen:
                seen[code] = {
                    "product_code": code,
                    "description": (r.get("product_desc") or "").strip(),
                    "supplier": "LWC",
                    "retro_eligible": float(r.get("retro_pct") or 0) > 0,
                }
            elif not seen[code]["description"] and r.get("product_desc"):
                seen[code]["description"] = r["product_desc"].strip()

    existing = _list_all(table_id, fields=["product_code"])
    by_code = {rec["fields"].get("product_code"): rec["id"] for rec in existing}

    to_create, to_update = [], []
    for code, fields in seen.items():
        if code in by_code:
            to_update.append({"id": by_code[code], "fields": fields})
        else:
            to_create.append({"fields": fields})

    created = _batch(to_create, "create", table_id) if to_create else 0
    updated = _batch(to_update, "update", table_id) if to_update else 0
    print(f"  unique products={len(seen)}  created={created} updated={updated}")

    refreshed = _list_all(table_id, fields=["product_code"])
    return {rec["fields"]["product_code"]: rec["id"] for rec in refreshed if rec["fields"].get("product_code")}


# ---------- PricingRules ----------

def _rule_key(site_id: str, product_code: str, valid_from: str) -> str:
    return f"{site_id}|{product_code}|{valid_from or 'open'}"


def migrate_rules(site_ids: dict[str, str], product_ids: dict[str, str]) -> None:
    print("\n[PricingRules]")
    table_id = TABLE_IDS["PricingRules"]
    rows: list[dict] = []
    skipped = 0
    with (ROOT / "master_pricing.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sid = r["site_id"].strip()
            code = r["product_code"].strip()
            if sid not in site_ids:
                skipped += 1
                continue
            if code not in product_ids:
                skipped += 1
                continue
            key = _rule_key(sid, code, r.get("valid_from", ""))
            fields: dict = {
                "rule_key": key,
                "site": [site_ids[sid]],
                "product": [product_ids[code]],
                "valid_from": r["valid_from"] or None,
                "valid_to": r["valid_to"] or None,
                "status": r.get("status") or "tenanted",
                "reason": r.get("reason") or "",
                "source": r.get("source") or "",
            }
            if r.get("tenant_price"):
                fields["tenant_price"] = float(r["tenant_price"])
            if r.get("fb_price"):
                fields["fb_price"] = float(r["fb_price"])
            if r.get("retro_pct"):
                fields["retro_pct"] = float(r["retro_pct"])
            fields = {k: v for k, v in fields.items() if v is not None}
            rows.append(fields)

    existing = _list_all(table_id, fields=["rule_key"])
    by_key = {rec["fields"].get("rule_key"): rec["id"] for rec in existing}

    to_create, to_update = [], []
    for fields in rows:
        key = fields["rule_key"]
        if key in by_key:
            to_update.append({"id": by_key[key], "fields": fields})
        else:
            to_create.append({"fields": fields})

    created = _batch(to_create, "create", table_id) if to_create else 0
    updated = _batch(to_update, "update", table_id) if to_update else 0
    print(f"  rows={len(rows)}  skipped(no site/product link)={skipped}  created={created} updated={updated}")


# ---------- main ----------

def main() -> int:
    site_ids = migrate_sites()
    product_ids = migrate_products()
    migrate_rules(site_ids, product_ids)
    print("\nDone. Open the base in Airtable to spot-check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
