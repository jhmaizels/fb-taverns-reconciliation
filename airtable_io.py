"""
Airtable read/write for FB Taverns reconciliation.

Functions exposed to reconcile.py:
  load_rules_from_airtable()         -> list[Rule]
  load_sites_from_airtable()         -> dict[str, dict]
  upsert_pricing_rules(rules, close_keys_at_date) -> (created, updated, closed)
  upsert_file_record(...)            -> Airtable record id of the Files row
  write_mismatches(mismatches, file_record_id) -> int
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

from reconcile import Mismatch, Rule, _parse_date  # type: ignore

load_dotenv()
TOKEN = os.environ["AIRTABLE_TOKEN"]
BASE_ID = os.environ["AIRTABLE_BASE_ID"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
DATA_URL = f"https://api.airtable.com/v0/{BASE_ID}"

ROOT = Path(__file__).parent
SCHEMA = json.loads((ROOT / "airtable_schema.json").read_text())
T = SCHEMA["tables"]

BATCH_SIZE = 10


# ---------- low-level ----------

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


def _batch(records: list[dict], op: str, table_id: str) -> list[dict]:
    method = requests.post if op == "create" else requests.patch
    results: list[dict] = []
    for i in range(0, len(records), BATCH_SIZE):
        chunk = records[i : i + BATCH_SIZE]
        r = method(f"{DATA_URL}/{table_id}", headers=HEADERS, json={"records": chunk, "typecast": True})
        if r.status_code >= 300:
            sys.exit(f"{op} {table_id} batch failed: {r.status_code} {r.text}")
        results.extend(r.json().get("records", []))
        time.sleep(0.25)
    return results


# ---------- read: master + sites ----------

def load_sites_from_airtable() -> dict[str, dict]:
    rows = _list_all(T["Sites"], fields=["site_id", "name", "status", "country", "notes"])
    sites: dict[str, dict] = {}
    for rec in rows:
        f = rec["fields"]
        sid = f.get("site_id")
        if not sid:
            continue
        sites[sid] = {
            "name": f.get("name", "") or "",
            "status": (f.get("status") or "tenanted").strip().lower(),
            "country": f.get("country", "") or "",
            "notes": f.get("notes", "") or "",
            "_rec_id": rec["id"],
        }
    return sites


def load_rules_from_airtable() -> list[Rule]:
    sites_by_id = {rec["id"]: rec["fields"].get("site_id") for rec in _list_all(T["Sites"], fields=["site_id"])}
    products_by_id = {
        rec["id"]: (rec["fields"].get("product_code"), rec["fields"].get("description") or "")
        for rec in _list_all(T["Products"], fields=["product_code", "description"])
    }
    rows = _list_all(T["PricingRules"])
    rules: list[Rule] = []
    for rec in rows:
        f = rec["fields"]
        site_links = f.get("site") or []
        product_links = f.get("product") or []
        if not site_links or not product_links:
            continue
        sid = sites_by_id.get(site_links[0])
        prod = products_by_id.get(product_links[0])
        if not sid or not prod:
            continue
        rules.append(
            Rule(
                site_id=sid,
                product_code=prod[0] or "",
                product_desc=prod[1],
                tenant_price=float(f["tenant_price"]) if f.get("tenant_price") is not None else None,
                fb_price=float(f["fb_price"]) if f.get("fb_price") is not None else None,
                retro_pct=float(f.get("retro_pct") or 0.0),
                valid_from=_parse_date(f.get("valid_from")),
                valid_to=_parse_date(f.get("valid_to")),
                status=f.get("status") or "tenanted",
                reason=f.get("reason") or "",
                source=f.get("source") or "",
            )
        )
    return rules


# ---------- write: rules ----------

def _rule_key(site_id: str, product_code: str, valid_from: date | None) -> str:
    vf = valid_from.isoformat() if valid_from else "open"
    return f"{site_id}|{product_code}|{vf}"


def _site_lookup() -> dict[str, str]:
    return {
        rec["fields"].get("site_id"): rec["id"]
        for rec in _list_all(T["Sites"], fields=["site_id"])
        if rec["fields"].get("site_id")
    }


def _product_lookup() -> dict[str, str]:
    return {
        rec["fields"].get("product_code"): rec["id"]
        for rec in _list_all(T["Products"], fields=["product_code"])
        if rec["fields"].get("product_code")
    }


def _ensure_sites_and_products(rules: list[Rule]) -> tuple[dict[str, str], dict[str, str]]:
    """Auto-create any sites/products referenced in rules but missing in Airtable."""
    site_ids = _site_lookup()
    product_ids = _product_lookup()

    new_sites = []
    seen_sites = set()
    for r in rules:
        if r.site_id and r.site_id not in site_ids and r.site_id not in seen_sites:
            seen_sites.add(r.site_id)
            new_sites.append({"fields": {"site_id": r.site_id, "status": "tenanted", "country": "england"}})
    if new_sites:
        created = _batch(new_sites, "create", T["Sites"])
        for rec in created:
            site_ids[rec["fields"]["site_id"]] = rec["id"]
        print(f"  auto-created {len(created)} sites")

    new_products = []
    seen_products: dict[str, str] = {}
    for r in rules:
        if r.product_code and r.product_code not in product_ids and r.product_code not in seen_products:
            seen_products[r.product_code] = r.product_desc or ""
            new_products.append(
                {"fields": {
                    "product_code": r.product_code,
                    "description": r.product_desc or "",
                    "supplier": "LWC",
                    "retro_eligible": (r.retro_pct or 0) > 0,
                }}
            )
    if new_products:
        created = _batch(new_products, "create", T["Products"])
        for rec in created:
            product_ids[rec["fields"]["product_code"]] = rec["id"]
        print(f"  auto-created {len(created)} products")

    return site_ids, product_ids


def upsert_pricing_rules(rules: list[Rule], close_keys_at_date: date | None) -> tuple[int, int, int]:
    """Push rules into PricingRules. Closes any prior open rule for the same (site, product) at close_keys_at_date."""
    if not rules:
        return 0, 0, 0

    site_ids, product_ids = _ensure_sites_and_products(rules)
    table_id = T["PricingRules"]
    existing = _list_all(table_id, fields=["rule_key", "valid_to", "site", "product"])
    by_key = {rec["fields"].get("rule_key"): rec["id"] for rec in existing}

    keys_in_new = {(r.site_id, r.product_code) for r in rules}

    closed = 0
    if close_keys_at_date:
        existing_open: list[dict] = []
        for rec in existing:
            f = rec["fields"]
            if f.get("valid_to"):
                continue
            site_link = (f.get("site") or [None])[0]
            product_link = (f.get("product") or [None])[0]
            sid = next((k for k, v in site_ids.items() if v == site_link), None)
            code = next((k for k, v in product_ids.items() if v == product_link), None)
            if sid and code and (sid, code) in keys_in_new:
                existing_open.append({"id": rec["id"], "fields": {"valid_to": close_keys_at_date.isoformat()}})
        if existing_open:
            _batch(existing_open, "update", table_id)
            closed = len(existing_open)

    to_create, to_update = [], []
    for r in rules:
        key = _rule_key(r.site_id, r.product_code, r.valid_from)
        fields: dict = {
            "rule_key": key,
            "site": [site_ids[r.site_id]] if r.site_id in site_ids else [],
            "product": [product_ids[r.product_code]] if r.product_code in product_ids else [],
            "valid_from": r.valid_from.isoformat() if r.valid_from else None,
            "valid_to": r.valid_to.isoformat() if r.valid_to else None,
            "status": r.status,
            "reason": r.reason,
            "source": r.source,
        }
        if r.tenant_price is not None:
            fields["tenant_price"] = r.tenant_price
        if r.fb_price is not None:
            fields["fb_price"] = r.fb_price
        if r.retro_pct:
            fields["retro_pct"] = r.retro_pct
        fields = {k: v for k, v in fields.items() if v is not None}
        if key in by_key:
            to_update.append({"id": by_key[key], "fields": fields})
        else:
            to_create.append({"fields": fields})

    created = len(_batch(to_create, "create", table_id)) if to_create else 0
    updated = len(_batch(to_update, "update", table_id)) if to_update else 0
    return created, updated, closed


# ---------- write: file + mismatches ----------

def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def upsert_products_with_retros(products: dict[str, dict]) -> tuple[int, int]:
    """
    Upsert into Products table by product_code. Sets description and retro_per_keg.
    Used after parse_fb_cost_file so retro-only products (with no per-site tenant
    prices) still land in Products and the retro reconciler can find them.
    """
    table_id = T["Products"]
    existing = _list_all(table_id, fields=["product_code"])
    by_code = {rec["fields"].get("product_code"): rec["id"] for rec in existing}

    to_create, to_update = [], []
    for code, info in products.items():
        fields = {
            "product_code": code,
            "description": info.get("name") or "",
            "supplier": "LWC",
            "retro_eligible": float(info.get("retro_per_keg") or 0) > 0,
            "retro_per_keg": float(info.get("retro_per_keg") or 0.0),
        }
        if code in by_code:
            to_update.append({"id": by_code[code], "fields": fields})
        else:
            to_create.append({"fields": fields})

    created = len(_batch(to_create, "create", table_id)) if to_create else 0
    updated = len(_batch(to_update, "update", table_id)) if to_update else 0
    return created, updated


def get_active_master_info() -> dict:
    """
    Summary of the currently-active pricing master so the reconciliation page
    can show 'using master <X> loaded on <date>' to the operator.

    Returns: {sources: list[str], latest_valid_from: str|None,
              active_rule_count: int, products_with_retro: int}
    """
    from collections import Counter
    sources: Counter[str] = Counter()
    latest_vf: str | None = None
    active_count = 0
    for rec in _list_all(T["PricingRules"], fields=["source", "valid_from", "valid_to"]):
        f = rec["fields"]
        if f.get("valid_to"):
            continue
        active_count += 1
        if f.get("source"):
            sources[f["source"]] += 1
        if f.get("valid_from") and (latest_vf is None or f["valid_from"] > latest_vf):
            latest_vf = f["valid_from"]
    products_with_retro = sum(
        1
        for rec in _list_all(T["Products"], fields=["retro_per_keg"])
        if (rec["fields"].get("retro_per_keg") or 0) > 0
    )
    return {
        "sources": [name for name, _ in sources.most_common()],
        "latest_valid_from": latest_vf,
        "active_rule_count": active_count,
        "products_with_retro": products_with_retro,
    }


def load_agreed_retros() -> dict[str, dict]:
    """Returns {product_code: {description, agreed_retro}} from Products.retro_per_keg."""
    out: dict[str, dict] = {}
    for rec in _list_all(T["Products"], fields=["product_code", "description", "retro_per_keg"]):
        f = rec["fields"]
        code = f.get("product_code")
        if not code:
            continue
        out[code] = {
            "description": f.get("description", "") or "",
            "agreed_retro": float(f.get("retro_per_keg") or 0.0),
        }
    return out


def upsert_file_record(
    file_path: str,
    supplier: str,
    line_count: int,
    parse_status: str = "ok",
    stored_path: str | None = None,
    file_name_override: str | None = None,
) -> str:
    """Insert (or look up by hash) a row in Files. Returns Airtable record id."""
    table_id = T["Files"]
    file_name = file_name_override or os.path.basename(file_path)
    raw_hash = _file_hash(file_path)
    existing = _list_all(table_id, fields=["raw_hash"])
    for rec in existing:
        if rec["fields"].get("raw_hash") == raw_hash:
            return rec["id"]

    fields = {
        "file_name": file_name,
        "supplier": supplier,
        "received_at": datetime.now().isoformat(timespec="seconds"),
        "line_count": line_count,
        "parse_status": parse_status,
        "raw_hash": raw_hash,
    }
    if stored_path:
        fields["stored_path"] = stored_path
    created = _batch([{"fields": fields}], "create", table_id)
    return created[0]["id"]


def _rule_lookup() -> dict[str, str]:
    return {
        rec["fields"].get("rule_key"): rec["id"]
        for rec in _list_all(T["PricingRules"], fields=["rule_key"])
        if rec["fields"].get("rule_key")
    }


def write_retro_findings(retro_summary, file_record_id: str) -> int:
    """
    Persist retro findings (under-paid, over-paid, paid-not-on-master) to the
    Mismatches table for audit. typecast=True auto-creates the new singleSelect
    options. Section 4 (multi-rate) and Section 5 (agreed-not-delivered) are
    diagnostic / informational and are not persisted.
    """
    product_ids = _product_lookup()
    table_id = T["Mismatches"]
    payload: list[dict] = []

    def _key(prefix: str, code: str) -> str:
        return f"{file_record_id}|retro|{prefix}|{code}"

    for r in retro_summary.under_payments:
        fields: dict = {
            "mismatch_key": _key("under", r.product_code),
            "type": "retro_under_paid",
            "severity": "high" if abs(r.total_delta) >= 50 else ("medium" if abs(r.total_delta) >= 5 else "low"),
            "file": [file_record_id],
            "expected_fb_price": float(r.agreed),
            "actual_fb_price": float(r.rates_paid[0]) if r.rates_paid else 0.0,
            "delta_per_unit": float(r.rates_paid[0]) - float(r.agreed) if r.rates_paid else -float(r.agreed),
            "delta_total": float(r.total_delta),
            "qty": float(r.kegs),
            "status": "open",
            "notes": f"Rates paid: {r.rates_paid}",
        }
        if r.product_code in product_ids:
            fields["product"] = [product_ids[r.product_code]]
        payload.append({"fields": fields})

    for r in retro_summary.over_payments:
        fields = {
            "mismatch_key": _key("over", r.product_code),
            "type": "retro_over_paid",
            "severity": "medium",
            "file": [file_record_id],
            "expected_fb_price": float(r.agreed),
            "actual_fb_price": float(r.rates_paid[0]) if r.rates_paid else 0.0,
            "delta_per_unit": float(r.rates_paid[0]) - float(r.agreed) if r.rates_paid else 0.0,
            "delta_total": float(r.total_delta),
            "qty": float(r.kegs),
            "status": "open",
            "notes": f"Rates paid: {r.rates_paid}",
        }
        if r.product_code in product_ids:
            fields["product"] = [product_ids[r.product_code]]
        payload.append({"fields": fields})

    for r in retro_summary.paid_not_on_master:
        fields = {
            "mismatch_key": _key("nomaster", r.product_code),
            "type": "retro_paid_not_on_master",
            "severity": "medium",
            "file": [file_record_id],
            "actual_fb_price": float(r.rates_paid[0]) if r.rates_paid else 0.0,
            "delta_total": float(r.total_received),
            "qty": float(r.kegs),
            "status": "open",
            "notes": f"Paid £{r.total_received:.2f} on a product with no agreed retro on master.",
        }
        if r.product_code in product_ids:
            fields["product"] = [product_ids[r.product_code]]
        payload.append({"fields": fields})

    if not payload:
        return 0
    return len(_batch(payload, "create", table_id))


def write_mismatches(mismatches: list[Mismatch], file_record_id: str) -> int:
    if not mismatches:
        return 0
    site_ids = _site_lookup()
    product_ids = _product_lookup()
    rule_ids = _rule_lookup()
    table_id = T["Mismatches"]

    payload: list[dict] = []
    for i, m in enumerate(mismatches, 1):
        line = m.line
        key = (
            f"{file_record_id}|{i:04d}|{line.site_id}|{line.product_code}|"
            f"{line.invoice_no}|{m.type}"
        )
        fields: dict = {
            "mismatch_key": key,
            "type": m.type,
            "severity": m.severity,
            "file": [file_record_id],
            "invoice_no": str(line.invoice_no),
            "invoice_date": line.invoice_date.isoformat() if line.invoice_date else None,
            "qty": float(line.qty),
            "delta_per_unit": float(m.delta_per_unit),
            "delta_total": float(m.delta_total),
            "status": "open",
            "notes": m.notes or "",
        }
        if line.site_id in site_ids:
            fields["site"] = [site_ids[line.site_id]]
        if line.product_code in product_ids:
            fields["product"] = [product_ids[line.product_code]]
        if m.rule:
            rk = _rule_key(m.rule.site_id, m.rule.product_code, m.rule.valid_from)
            if rk in rule_ids:
                fields["rule"] = [rule_ids[rk]]
        for fk, val in [
            ("expected_tenant_price", m.expected_tenant_price),
            ("actual_tenant_price", m.actual_tenant_price),
            ("expected_fb_price", m.expected_fb_price),
            ("actual_fb_price", m.actual_fb_price),
        ]:
            if val is not None:
                fields[fk] = float(val)
        fields = {k: v for k, v in fields.items() if v is not None}
        payload.append({"fields": fields})

    created = _batch(payload, "create", table_id)
    return len(created)
