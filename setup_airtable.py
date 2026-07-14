"""
Create the FB Taverns reconciliation schema in Airtable.

Idempotent: if a table or field already exists, it's left alone. Resulting
table IDs are written to airtable_schema.json for the migration script and
the main reconciliation service to consume.

Tables created (in dependency order):
  Sites, Products, Files, PricingRules, Mismatches

Run once, after updating .env with AIRTABLE_TOKEN + AIRTABLE_BASE_ID.
"""

from __future__ import annotations

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
META_URL = f"https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables"
SCHEMA_OUT = Path(__file__).parent / "airtable_schema.json"


# ---------- field-shape helpers ----------

def f_text(name): return {"name": name, "type": "singleLineText"}
def f_long(name): return {"name": name, "type": "multilineText"}
def f_url(name): return {"name": name, "type": "url"}
def f_number(name, precision=0): return {"name": name, "type": "number", "options": {"precision": precision}}
def f_currency(name): return {"name": name, "type": "currency", "options": {"precision": 4, "symbol": "£"}}
def f_percent(name): return {"name": name, "type": "percent", "options": {"precision": 2}}
def f_date(name): return {"name": name, "type": "date", "options": {"dateFormat": {"name": "iso"}}}
def f_datetime(name): return {"name": name, "type": "dateTime", "options": {"dateFormat": {"name": "iso"}, "timeFormat": {"name": "24hour"}, "timeZone": "Europe/London"}}
def f_select(name, choices): return {"name": name, "type": "singleSelect", "options": {"choices": [{"name": c} for c in choices]}}
def f_checkbox(name): return {"name": name, "type": "checkbox", "options": {"color": "greenBright", "icon": "check"}}
def f_link(name, table_id): return {"name": name, "type": "multipleRecordLinks", "options": {"linkedTableId": table_id}}
# NB: createdTime fields can't be created via the metadata API (Apr 2026).
# Airtable records have a built-in createdTime accessible via the data API anyway.


# ---------- table specs (links resolved later, in order) ----------

def site_table_spec():
    return {
        "name": "Sites",
        "description": "FB Taverns sites. Authoritative status per date lives in PricingRules; this is for current/default info.",
        "fields": [
            f_text("site_id"),
            f_text("name"),
            f_text("account_no"),  # LWC customer account number (from the weekly files' ACCOUNT NO)
            f_select("status", ["tenanted", "managed"]),
            f_select("country", ["england", "scotland"]),
            f_long("notes"),
        ],
    }


def product_table_spec():
    return {
        "name": "Products",
        "description": "Drink SKUs across suppliers.",
        "fields": [
            f_text("product_code"),
            f_text("description"),
            f_select("supplier", ["LWC", "Tennents"]),
            f_checkbox("retro_eligible"),
        ],
    }


def files_table_spec():
    return {
        "name": "Files",
        "description": "Every supplier file ever ingested. Immutable audit log.",
        "fields": [
            f_text("file_name"),
            f_select("supplier", ["LWC", "Tennents"]),
            f_datetime("received_at"),
            f_number("line_count"),
            f_select("parse_status", ["ok", "partial", "failed"]),
            f_text("raw_hash"),
            f_url("stored_path"),
        ],
    }


def pricing_rules_table_spec(sites_id, products_id):
    return {
        "name": "PricingRules",
        "description": "Effective-dated pricing rules. Append-only: edits create new rows and close old ones via valid_to.",
        "fields": [
            f_text("rule_key"),
            f_link("site", sites_id),
            f_link("product", products_id),
            f_currency("tenant_price"),
            f_currency("fb_price"),
            f_percent("retro_pct"),
            f_date("valid_from"),
            f_date("valid_to"),
            f_select("status", ["tenanted", "managed", "supported"]),
            f_text("reason"),
            f_text("source"),
        ],
    }


def mismatches_table_spec(sites_id, products_id, rules_id, files_id):
    return {
        "name": "Mismatches",
        "description": "Reconciliation findings. Status drives the review workflow.",
        "fields": [
            f_text("mismatch_key"),
            f_select("type", [
                "wrong_tenant_price",
                "wrong_fb_price",
                "site_should_be_managed",
                "unknown_site",
                "unknown_product",
                "no_rule_for_line",
                "lwc_arithmetic_error",
                "retro_missing",
                "retro_wrong_amount",
                "retro_on_ineligible_product",
                "retro_wrong_pct",
            ]),
            f_select("severity", ["low", "medium", "high"]),
            f_link("file", files_id),
            f_link("site", sites_id),
            f_link("product", products_id),
            f_link("rule", rules_id),
            f_text("invoice_no"),
            f_date("invoice_date"),
            f_number("qty", precision=2),
            f_currency("expected_tenant_price"),
            f_currency("actual_tenant_price"),
            f_currency("expected_fb_price"),
            f_currency("actual_fb_price"),
            f_currency("delta_per_unit"),
            f_currency("delta_total"),
            f_select("status", ["open", "acknowledged", "resolved"]),
            f_long("notes"),
        ],
    }


# ---------- API helpers ----------

def list_tables() -> list[dict]:
    r = requests.get(META_URL, headers=HEADERS)
    r.raise_for_status()
    return r.json()["tables"]


def find_table(name: str, tables: list[dict]) -> dict | None:
    for t in tables:
        if t["name"] == name:
            return t
    return None


def create_table(spec: dict) -> dict:
    r = requests.post(META_URL, headers=HEADERS, json=spec)
    if r.status_code >= 300:
        sys.exit(f"Create table {spec['name']} failed: {r.status_code} {r.text}")
    return r.json()


def add_missing_fields(table: dict, spec: dict) -> int:
    existing_field_names = {f["name"] for f in table.get("fields", [])}
    table_id = table["id"]
    added = 0
    for field_spec in spec["fields"]:
        if field_spec["name"] in existing_field_names:
            continue
        url = f"{META_URL}/{table_id}/fields"
        r = requests.post(url, headers=HEADERS, json=field_spec)
        if r.status_code >= 300:
            sys.exit(f"Add field {field_spec['name']} to {spec['name']} failed: {r.status_code} {r.text}")
        added += 1
        time.sleep(0.2)
    return added


def upsert_table(spec: dict, tables: list[dict]) -> dict:
    existing = find_table(spec["name"], tables)
    if existing:
        added = add_missing_fields(existing, spec)
        print(f"  table {spec['name']:14s} exists ({existing['id']}); +{added} fields")
        return existing
    print(f"  creating table {spec['name']}…")
    created = create_table(spec)
    print(f"    created {spec['name']} ({created['id']})")
    return created


# ---------- main ----------

def main() -> int:
    print(f"Setting up schema in base {BASE_ID}…")
    tables = list_tables()

    sites = upsert_table(site_table_spec(), tables)
    products = upsert_table(product_table_spec(), tables)
    files_t = upsert_table(files_table_spec(), tables)

    tables = list_tables()
    rules = upsert_table(pricing_rules_table_spec(sites["id"], products["id"]), tables)

    tables = list_tables()
    mismatches = upsert_table(
        mismatches_table_spec(sites["id"], products["id"], rules["id"], files_t["id"]),
        tables,
    )

    schema = {
        "base_id": BASE_ID,
        "tables": {
            "Sites": sites["id"],
            "Products": products["id"],
            "Files": files_t["id"],
            "PricingRules": rules["id"],
            "Mismatches": mismatches["id"],
        },
    }
    SCHEMA_OUT.write_text(json.dumps(schema, indent=2))
    print(f"\nWrote schema map to {SCHEMA_OUT}")
    print("\nNow run: python migrate_csv_to_airtable.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
