"""
JSON product/cost feed for the tenancy hub's Deal Generator (GET
/api/deal-products in webapp.py). Pure snapshot -> payload logic lives here so
it is testable offline (test_deal_products.py), matching the thin-webapp rule.

Semantics follow the master everywhere else in this repo:
- fb_price is the LIST price (cost file col 2), never the net.
- retro is the PRODUCT-level fixed pounds/keg (Products.retro_per_keg) when
  known, else derived from the rule's retro_pct (retro_pct * fb_price
  round-trips to source pennies by design).
- net = fb_price - retro_per_keg, exactly as master_export computes it.
- "Active" membership = rules with valid_to is None, same as everywhere else;
  per product the newest valid_from wins.
"""
from __future__ import annotations

import secrets
from datetime import date


def token_ok(authorization_header: str | None, expected: str | None) -> bool:
    """Constant-time bearer check. No token configured => nothing matches.
    Compared as bytes: compare_digest raises TypeError on non-ASCII str, which
    would turn a hostile header into a 500 instead of a 401."""
    if not expected or not authorization_header:
        return False
    if not authorization_header.startswith("Bearer "):
        return False
    supplied = authorization_header[len("Bearer "):]
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def build_deal_products_payload(snap) -> dict:
    """MasterSnapshot -> {"as_of", "products": [...]} for the Deal Generator.

    One entry per product that has an ACTIVE rule with a usable fb_price;
    tenant_price is deliberately omitted (the Deal Generator prices NEW deals
    from cost + target GP; per-site current prices are not its input).
    """
    best: dict = {}
    for r in snap.rules:
        if r.valid_to is not None:
            continue
        if r.fb_price is None or r.fb_price <= 0:
            continue
        cur = best.get(r.product_code)
        if cur is None or (r.valid_from or date.min) > (cur.valid_from or date.min):
            best[r.product_code] = r

    product_meta = getattr(snap, "products", {}) or {}
    products = []
    for code in sorted(best):
        r = best[code]
        meta = product_meta.get(code) or {}
        retro = meta.get("retro_per_keg")
        if retro is None:
            retro = (r.retro_pct or 0.0) * r.fb_price
        retro = round(float(retro), 4)
        products.append(
            {
                "product_code": code,
                "description": meta.get("desc") or r.product_desc or code,
                "fb_price": round(float(r.fb_price), 4),
                "retro_gbp": retro,
                "net_price": round(float(r.fb_price) - retro, 4),
            }
        )

    info = snap.banner_info if isinstance(getattr(snap, "banner_info", None), dict) else {}
    as_of = info.get("latest_valid_from") or ""
    return {"as_of": str(as_of), "products": products}
