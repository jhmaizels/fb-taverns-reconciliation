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
import logging
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

from reconcile import Mismatch, Rule, _parse_date  # type: ignore

load_dotenv()
# Read defensively: AIRTABLE_TOKEN / AIRTABLE_BASE_ID are sync:false in Render
# (set by hand in the dashboard). os.environ[...] here would raise at import
# time if either is missing, taking the whole app — including /healthz — down
# on a misconfigured deploy. Default to None and fail cleanly at first use (see
# _require_env) so the process still boots and only Airtable routes error.
TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
DATA_URL = f"https://api.airtable.com/v0/{BASE_ID}"

ROOT = Path(__file__).parent
SCHEMA = json.loads((ROOT / "airtable_schema.json").read_text())
T = SCHEMA["tables"]
# Reverse map (table id -> human name) for readable timing logs.
T_NAME = {v: k for k, v in T.items()}

BATCH_SIZE = 10

logger = logging.getLogger("fbtaverns.airtable")


class AirtableError(RuntimeError):
    """Raised on a non-retryable Airtable API error (or once retries are exhausted)."""


def _require_env() -> None:
    missing = [name for name, val in (("AIRTABLE_TOKEN", TOKEN), ("AIRTABLE_BASE_ID", BASE_ID)) if not val]
    if missing:
        raise AirtableError("Airtable is not configured: missing " + ", ".join(missing))


_MAX_RETRIES = 4


def _do(method, url, *, params=None, json_body=None, idempotent=True):
    """
    One Airtable HTTP call with bounded backoff. Raises AirtableError instead of
    sys.exit() so a transient failure kills one request, not the uvicorn worker.

    Retry policy:
      - 429 (rate limited): always retried — the request is rejected by the rate
        limiter and never executed, so re-issuing it cannot duplicate anything.
      - 5xx: retried ONLY when idempotent=True (GET list / PATCH update). For a
        create POST (idempotent=False) a 5xx may arrive AFTER Airtable has
        committed the row, and re-issuing would persist a DUPLICATE (there is no
        server-side uniqueness on rule_key / raw_hash / mismatch_key; dedup is
        read-existing-first, above this layer) — so create POSTs fail fast on 5xx.
      - any other 4xx (e.g. a 422 bad-field) always fails fast rather than being
        silently retried against typecast auto-create.
    """
    _require_env()
    delay = 1.0
    last = None
    for attempt in range(_MAX_RETRIES + 1):
        r = method(url, headers=HEADERS, params=params, json=json_body)
        if r.status_code < 300:
            return r
        last = r
        retryable = r.status_code == 429 or (r.status_code >= 500 and idempotent)
        if retryable and attempt < _MAX_RETRIES:
            ra = r.headers.get("Retry-After")
            try:
                wait = float(ra) if ra else delay
            except (TypeError, ValueError):
                wait = delay
            time.sleep(min(wait, 30.0))
            delay = min(delay * 2, 30.0)
            continue
        break
    detail = f"{last.status_code} {last.text[:300]}" if last is not None else "no response"
    raise AirtableError(f"{getattr(method, '__name__', '?').upper()} {url} failed: {detail}")


# ---------- low-level ----------

def _list_all(table_id: str, fields: list[str] | None = None) -> list[dict]:
    start = time.perf_counter()
    out: list[dict] = []
    pages = 0
    try:
        params: dict = {"pageSize": 100}
        if fields:
            params["fields[]"] = fields
        offset = None
        while True:
            if offset:
                params["offset"] = offset
            r = _do(requests.get, f"{DATA_URL}/{table_id}", params=params)
            body = r.json()
            out.extend(body["records"])
            pages += 1
            offset = body.get("offset")
            if not offset:
                break
        return out
    finally:
        logger.info(
            "GET %s pages=%d records=%d %.0fms",
            T_NAME.get(table_id, table_id), pages, len(out), (time.perf_counter() - start) * 1000,
        )


def _batch(records: list[dict], op: str, table_id: str) -> list[dict]:
    start = time.perf_counter()
    method = requests.post if op == "create" else requests.patch
    results: list[dict] = []
    n = len(records)
    try:
        for i in range(0, n, BATCH_SIZE):
            chunk = records[i : i + BATCH_SIZE]
            # Create POSTs are not idempotent — a 5xx after a server-side commit
            # must NOT be retried (would duplicate). Updates (PATCH) are safe.
            r = _do(
                method, f"{DATA_URL}/{table_id}",
                json_body={"records": chunk, "typecast": True},
                idempotent=(op == "update"),
            )
            results.extend(r.json().get("records", []))
            # Throttle between chunks only — sleeping after the final chunk gates
            # nothing and was ~0.25s of dead time per _batch call (6-9 per upload).
            if i + BATCH_SIZE < n:
                time.sleep(0.25)
        return results
    finally:
        logger.info(
            "WRITE %s op=%s records=%d %.0fms",
            T_NAME.get(table_id, table_id), op, n, (time.perf_counter() - start) * 1000,
        )


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


# Projection covering every field _rule_from_rec reads (link fields included).
# Shared by load_rules_from_airtable and the master snapshot so neither pulls
# the full record set. createdTime is record metadata, returned regardless.
_RULE_FIELDS = [
    "rule_key", "site", "product", "tenant_price", "fb_price", "retro_pct",
    "valid_from", "valid_to", "status", "reason", "source",
]


def _rule_from_rec(rec: dict, sites_by_id: dict, products_by_id: dict) -> Rule | None:
    """Build a Rule from a raw PricingRules record using id->key resolution maps.
    Returns None for rules whose site/product link can't be resolved."""
    f = rec["fields"]
    site_links = f.get("site") or []
    product_links = f.get("product") or []
    if not site_links or not product_links:
        return None
    sid = sites_by_id.get(site_links[0])
    prod = products_by_id.get(product_links[0])
    if not sid or not prod:
        return None
    return Rule(
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


def load_rules_from_airtable() -> list[Rule]:
    sites_by_id = {rec["id"]: rec["fields"].get("site_id") for rec in _list_all(T["Sites"], fields=["site_id"])}
    products_by_id = {
        rec["id"]: (rec["fields"].get("product_code"), rec["fields"].get("description") or "")
        for rec in _list_all(T["Products"], fields=["product_code", "description"])
    }
    rules: list[Rule] = []
    for rec in _list_all(T["PricingRules"], fields=_RULE_FIELDS):
        r = _rule_from_rec(rec, sites_by_id, products_by_id)
        if r is not None:
            rules.append(r)
    return rules


# ---------- master snapshot (one coherent read, TTL-cached) ----------

@dataclass
class MasterSnapshot:
    """One coherent read of the LWC pricing master, reused across a single
    /upload and cached briefly across uploads, so Sites/Products/PricingRules
    are each fetched once rather than ~3x per upload."""
    sites: dict          # site_id -> {name,status,country,notes,_rec_id}  (== load_sites_from_airtable)
    rules: list          # list[Rule]                                       (== load_rules_from_airtable)
    site_ids: dict       # site_id -> record id      (write-side link resolution)
    product_ids: dict    # product_code -> record id
    rule_ids: dict       # rule_key -> record id
    banner_info: dict    # == get_active_master_info()


# Module-level cache. Render `starter` is always-on, so it survives between
# requests. TTL-bounded and explicitly invalidated by every master-mutating
# write (upsert_pricing_rules / upsert_products_with_retros). Never holds Files
# or Mismatches (those are written every upload and must stay fresh).
MASTER_CACHE_TTL = float(os.environ.get("MASTER_CACHE_TTL", "60"))
_MASTER_CACHE: dict = {"snapshot": None, "ts": 0.0, "gen": 0, "refreshing": False}
_MASTER_CACHE_LOCK = threading.Lock()


def invalidate_master_cache() -> None:
    with _MASTER_CACHE_LOCK:
        _MASTER_CACHE["snapshot"] = None
        _MASTER_CACHE["ts"] = 0.0
        _MASTER_CACHE["gen"] += 1


def refresh_master_cache_async() -> None:
    """Kick ONE background rebuild of the snapshot (no-op if one is running).

    Exists so page reads can be STALE-WHILE-REVALIDATE: the inline rebuild takes
    ~30s+ (three full-table Airtable sweeps, rate-limited), which blows through
    the tenancy-hub proxy's ~30s timeout and surfaced to users as a bare
    "Internal Server Error" whenever the 60s TTL had lapsed. Serving the stale
    snapshot instantly and refreshing here keeps every page render sub-second."""
    with _MASTER_CACHE_LOCK:
        if _MASTER_CACHE["refreshing"]:
            return
        _MASTER_CACHE["refreshing"] = True
        gen_before = _MASTER_CACHE["gen"]

    def _work() -> None:
        try:
            snap = _fetch_master_snapshot()
            with _MASTER_CACHE_LOCK:
                # Publish only if no master write invalidated during the fetch.
                if _MASTER_CACHE["gen"] == gen_before:
                    _MASTER_CACHE["snapshot"] = snap
                    _MASTER_CACHE["ts"] = time.monotonic()
        except Exception:
            logging.getLogger("fbtaverns.airtable").warning(
                "background master-cache refresh failed", exc_info=True
            )
        finally:
            with _MASTER_CACHE_LOCK:
                _MASTER_CACHE["refreshing"] = False

    threading.Thread(target=_work, daemon=True, name="master-cache-refresh").start()


def _fetch_master_snapshot() -> MasterSnapshot:
    site_recs = _list_all(T["Sites"], fields=["site_id", "name", "status", "country", "notes"])
    product_recs = _list_all(T["Products"], fields=["product_code", "description", "retro_per_keg"])
    rule_recs = _list_all(T["PricingRules"], fields=_RULE_FIELDS)

    sites: dict[str, dict] = {}
    site_ids: dict[str, str] = {}
    sites_by_id: dict[str, str] = {}
    for rec in site_recs:
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
        site_ids[sid] = rec["id"]
        sites_by_id[rec["id"]] = sid

    product_ids: dict[str, str] = {}
    products_by_id: dict[str, tuple] = {}
    for rec in product_recs:
        f = rec["fields"]
        code = f.get("product_code")
        if not code:
            continue
        product_ids[code] = rec["id"]
        products_by_id[rec["id"]] = (code, f.get("description") or "")

    rules: list[Rule] = []
    rule_ids: dict[str, str] = {}
    sources: Counter = Counter()
    latest_vf: str | None = None
    latest_uploaded: str | None = None
    active_count = 0
    for rec in rule_recs:
        f = rec["fields"]
        rk = f.get("rule_key")
        if rk:
            rule_ids[rk] = rec["id"]
        r = _rule_from_rec(rec, sites_by_id, products_by_id)
        if r is not None:
            rules.append(r)
        # Banner counts only currently-active rules (valid_to empty).
        if f.get("valid_to"):
            continue
        active_count += 1
        if f.get("source"):
            sources[f["source"]] += 1
        if f.get("valid_from") and (latest_vf is None or f["valid_from"] > latest_vf):
            latest_vf = f["valid_from"]
        ct = rec.get("createdTime")
        if ct and (latest_uploaded is None or ct > latest_uploaded):
            latest_uploaded = ct

    products_with_retro = sum(
        1 for rec in product_recs if (rec["fields"].get("retro_per_keg") or 0) > 0
    )
    banner_info = {
        "sources": [name for name, _ in sources.most_common()],
        "latest_valid_from": latest_vf,
        "latest_uploaded_at": latest_uploaded,
        "active_rule_count": active_count,
        "products_with_retro": products_with_retro,
    }
    return MasterSnapshot(
        sites=sites, rules=rules, site_ids=site_ids,
        product_ids=product_ids, rule_ids=rule_ids, banner_info=banner_info,
    )


def load_master_snapshot(force_refresh: bool = False) -> MasterSnapshot:
    """Return a master snapshot, served from a short-lived cache unless it has
    expired or been invalidated by a master write.

    The network fetch runs OUTSIDE the lock (so concurrent uploads don't
    serialise on it), but the result is only published if no invalidation ran
    during the fetch — a generation check that stops a slow in-flight fetch from
    resurrecting a snapshot taken before a concurrent master write.
    """
    with _MASTER_CACHE_LOCK:
        cached = _MASTER_CACHE["snapshot"]
        ts = _MASTER_CACHE["ts"]
        gen_before = _MASTER_CACHE["gen"]
    if not force_refresh and cached is not None:
        if (time.monotonic() - ts) < MASTER_CACHE_TTL:
            return cached
        # STALE-WHILE-REVALIDATE: the inline rebuild takes ~30s+ and times out
        # the tenancy-hub proxy (bare 500s to users). Serve the expired snapshot
        # NOW and refresh in the background. Master writes still invalidate to
        # None, so post-write reads take the coherent inline fetch below.
        refresh_master_cache_async()
        return cached
    snap = _fetch_master_snapshot()
    with _MASTER_CACHE_LOCK:
        if _MASTER_CACHE["gen"] == gen_before:
            _MASTER_CACHE["snapshot"] = snap
            _MASTER_CACHE["ts"] = time.monotonic()
    return snap


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


def upsert_pricing_rules(
    rules: list[Rule],
    close_keys_at_date: date | None,
    lookups_out: dict | None = None,
) -> tuple[int, int, int]:
    """Push rules into PricingRules. Closes any prior open rule for the same
    (site, product) at close_keys_at_date.

    If lookups_out is given it is populated with {'site_ids', 'product_ids'}
    (the maps built here) so a caller can reuse them — e.g. /upload-master
    feeding upsert_products_with_retros without re-reading the Products table.
    """
    if not rules:
        return 0, 0, 0

    site_ids, product_ids = _ensure_sites_and_products(rules)
    if lookups_out is not None:
        lookups_out["site_ids"] = site_ids
        lookups_out["product_ids"] = product_ids
    table_id = T["PricingRules"]
    existing = _list_all(table_id, fields=["rule_key", "valid_from", "valid_to", "site", "product"])
    by_key = {rec["fields"].get("rule_key"): rec["id"] for rec in existing}

    keys_in_new = {(r.site_id, r.product_code) for r in rules}

    # Pre-compute the set of rule_keys for rules in the new batch; any existing
    # record with a matching rule_key IS being updated, not replaced, and must
    # never be closed by this pass (otherwise a same-date re-upload accidentally
    # closes everything before re-opening it).
    new_rule_keys = {
        _rule_key(r.site_id, r.product_code, r.valid_from) for r in rules
    }

    closed = 0
    if close_keys_at_date:
        # Reverse maps so each existing open rule's links resolve in O(1) rather
        # than scanning site_ids / product_ids per rule (was O(existing x sites)
        # + O(existing x products)). Behaviour is otherwise unchanged.
        id_to_site = {v: k for k, v in site_ids.items()}
        id_to_code = {v: k for k, v in product_ids.items()}
        existing_open: list[dict] = []
        for rec in existing:
            f = rec["fields"]
            if f.get("valid_to"):
                continue
            # Belt: don't close rules being updated in place.
            if f.get("rule_key") in new_rule_keys:
                continue
            # Braces: don't close rules whose valid_from is on/after the close
            # date (same-date or future rules should stay active).
            vf_str = f.get("valid_from")
            if vf_str:
                vf_date = _parse_date(vf_str)
                if vf_date and vf_date >= close_keys_at_date:
                    continue
            site_link = (f.get("site") or [None])[0]
            product_link = (f.get("product") or [None])[0]
            sid = id_to_site.get(site_link)
            code = id_to_code.get(product_link)
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
    invalidate_master_cache()
    return created, updated, closed


# ---------- write: file + mismatches ----------

def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def upsert_products_with_retros(products: dict[str, dict], existing_by_code: dict | None = None) -> tuple[int, int]:
    """
    Upsert into Products table by product_code. Sets description and retro_per_keg.
    Used after parse_fb_cost_file so retro-only products (with no per-site tenant
    prices) still land in Products and the retro reconciler can find them.

    existing_by_code: optional pre-built {product_code: record id} map (e.g. the
    product_ids from upsert_pricing_rules' lookups_out) to skip re-reading Products.
    """
    table_id = T["Products"]
    if existing_by_code is None:
        existing = _list_all(table_id, fields=["product_code"])
        by_code = {rec["fields"].get("product_code"): rec["id"] for rec in existing}
    else:
        by_code = existing_by_code

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
    invalidate_master_cache()
    return created, updated


def get_active_master_info() -> dict:
    """
    Summary of the currently-active pricing master so the reconciliation page
    can show 'using master <X> uploaded <date>' to the operator.

    Returns: {sources, latest_valid_from, latest_uploaded_at,
              active_rule_count, products_with_retro}

    Served from the shared (TTL-cached) master snapshot so the index page, the
    post-upload banner and the /upload read path all share one fetch.

    Always returns a dict — callers do .get(...) on it, so a None here would 500
    the estate picker / banner.
    """
    info = load_master_snapshot().banner_info
    return info if isinstance(info, dict) else {}


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


# ---------- Tennents Direct I/O ----------

def load_tennents_agreements():
    """Load TennentsAgreements rows as Agreement dataclass instances."""
    from tennents import Agreement  # local import to avoid circular dep
    out = []
    for rec in _list_all(T["TennentsAgreements"]):
        f = rec["fields"]
        if not f.get("account") or not f.get("sku_code"):
            continue
        out.append(Agreement(
            account=f.get("account", "") or "",
            customer_name=f.get("customer_name", "") or "",
            sku_code=f.get("sku_code", "") or "",
            sku_desc=f.get("sku_desc", "") or "",
            tenant_invoice=float(f.get("tenant_invoice") or 0),
            fb_net_price=float(f.get("fb_net_price") or 0),
            off_invoice_per_brl=float(f.get("off_invoice_per_brl") or 0),
            retro_per_brl=float(f.get("retro_per_brl") or 0),
            total_per_brl=float(f.get("total_per_brl") or 0),
            source=f.get("source", "") or "",
        ))
    return out


def replace_tennents_master(agreements, source: str) -> tuple[int, int]:
    """
    Wipe TennentsAgreements and replace with new master.
    Returns (deleted, created).
    """
    table_id = T["TennentsAgreements"]
    existing_ids = [rec["id"] for rec in _list_all(table_id, fields=["agreement_key"])]
    deleted = 0
    if existing_ids:
        for i in range(0, len(existing_ids), 10):
            chunk = existing_ids[i:i+10]
            # Route deletes through _do for consistent backoff/error handling
            # (was a bare requests.delete + raise_for_status). Throttle between
            # chunks only — no sleep after the final chunk. idempotent=False so a
            # 5xx fails fast rather than re-deleting (the whole replace is re-run
            # safely from the top, which re-lists remaining ids).
            _do(requests.delete, f"{DATA_URL}/{table_id}", params={"records[]": chunk}, idempotent=False)
            deleted += len(chunk)
            if i + 10 < len(existing_ids):
                time.sleep(0.25)

    payload = []
    for ag in agreements:
        payload.append({"fields": {
            "agreement_key": f"{ag.account}|{ag.sku_code}",
            "account": ag.account,
            "customer_name": ag.customer_name,
            "sku_code": ag.sku_code,
            "sku_desc": ag.sku_desc,
            "tenant_invoice": float(ag.tenant_invoice),
            "fb_net_price": float(ag.fb_net_price),
            "off_invoice_per_brl": float(ag.off_invoice_per_brl),
            "retro_per_brl": float(ag.retro_per_brl),
            "total_per_brl": float(ag.total_per_brl),
            "source": source,
        }})
    created = len(_batch(payload, "create", table_id)) if payload else 0
    return deleted, created


def get_tennents_master_info() -> dict:
    """Summary for the Tennents card on the index page."""
    from collections import Counter
    sources: Counter[str] = Counter()
    customers: set = set()
    latest: str | None = None
    count = 0
    for rec in _list_all(T["TennentsAgreements"]):
        f = rec["fields"]
        count += 1
        if f.get("source"):
            sources[f["source"]] += 1
        if f.get("account"):
            customers.add(f["account"])
        ct = rec.get("createdTime")
        if ct and (latest is None or ct > latest):
            latest = ct
    return {
        "sources": [s for s, _ in sources.most_common()],
        "agreement_count": count,
        "customer_count": len(customers),
        "latest_uploaded_at": latest,
    }


def write_tennents_findings(summary, file_record_id: str) -> int:
    """Persist Tennents reconciliation findings to the Mismatches table."""
    table_id = T["Mismatches"]
    payload = []

    def _key(prefix: str, *bits) -> str:
        return f"{file_record_id}|tennents|{prefix}|" + "|".join(str(b) for b in bits)

    for r in summary.invoice_mismatches:
        payload.append({"fields": {
            "mismatch_key": _key("invoice", r.account, r.sku_code),
            "type": "tennents_wrong_invoice",
            "severity": "high" if abs(r.delta_per_unit * r.kegs) >= 50 else "medium",
            "file": [file_record_id],
            "expected_tenant_price": float(r.expected),
            "actual_tenant_price": float(r.actual),
            "delta_per_unit": float(r.delta_per_unit),
            "delta_total": float(r.delta_per_unit * r.kegs),
            "qty": float(r.kegs),
            "status": "open",
            "notes": f"Tennents {r.account} {r.customer_name} / {r.sku_code} {r.sku_desc}",
        }})

    for r in summary.fb_price_mismatches:
        payload.append({"fields": {
            "mismatch_key": _key("fb", r.sku_code, round(r.expected, 4), round(r.actual, 4)),
            "type": "tennents_wrong_fb_price",
            "severity": "medium",
            "file": [file_record_id],
            "expected_fb_price": float(r.expected),
            "actual_fb_price": float(r.actual),
            "delta_per_unit": float(r.delta_per_unit),
            "delta_total": float(r.delta_per_unit * r.total_kegs),
            "qty": float(r.total_kegs),
            "status": "open",
            "notes": f"Tennents {r.sku_code} {r.sku_desc} across {len(r.sites_affected)} sites",
        }})

    for r in summary.discount_mismatches:
        sev = "high" if abs(r.delta_total) >= 100 else ("medium" if abs(r.delta_total) >= 10 else "low")
        payload.append({"fields": {
            "mismatch_key": _key("disc", r.account, r.sku_code),
            "type": "tennents_wrong_discount",
            "severity": sev,
            "file": [file_record_id],
            "expected_fb_price": float(r.expected),  # repurpose for £/Brl
            "actual_fb_price": float(r.actual),
            "delta_per_unit": float(r.delta_per_brl),
            "delta_total": float(r.delta_total),
            "qty": float(r.barrels),
            "status": "open",
            "notes": (
                f"Tennents {r.account} {r.customer_name} / {r.sku_code} {r.sku_desc} — "
                f"discount £/Brl: master {r.expected:+.2f}, actual {r.actual:+.2f}"
            ),
        }})

    for r in summary.not_on_master:
        payload.append({"fields": {
            "mismatch_key": _key("nomaster", r.account, r.sku_code),
            "type": "tennents_not_on_master",
            "severity": "medium",
            "file": [file_record_id],
            "qty": float(r.kegs),
            "status": "open",
            "notes": (
                f"Tennents {r.account} {r.customer_name} / {r.sku_code} {r.sku_desc} — "
                f"delivered {r.kegs:g} kegs but no master agreement exists. "
                f"Avg invoice £{r.avg_invoice:.2f}, avg discount £{r.avg_discount_per_brl:.2f}/Brl."
            ),
        }})

    for r in summary.customers_not_on_master:
        acct, name = r
        payload.append({"fields": {
            "mismatch_key": _key("newcust", acct),
            "type": "tennents_new_customer",
            "severity": "medium",
            "file": [file_record_id],
            "status": "open",
            "notes": f"Tennents new customer {acct} {name} — needs master entries built.",
        }})

    if not payload:
        return 0
    return len(_batch(payload, "create", table_id))


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


def write_mismatches(
    mismatches: list[Mismatch],
    file_record_id: str,
    site_ids: dict | None = None,
    product_ids: dict | None = None,
    rule_ids: dict | None = None,
) -> int:
    if not mismatches:
        return 0
    # Reuse the caller's master-snapshot maps when provided (the /upload path);
    # otherwise fall back to standalone lookups (CLI / other callers).
    if site_ids is None:
        site_ids = _site_lookup()
    if product_ids is None:
        product_ids = _product_lookup()
    if rule_ids is None:
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
