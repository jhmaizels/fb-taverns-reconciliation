"""
Per-pub price feed for the tenancy hub's bar plans (GET /api/site-prices in
webapp.py). A bar plan change proposes which brands go on which taps; the hub
shows Jason each line's cost to the tenant and FB's margin per barrel (MPB)
before he approves it, so it reads one pub's prices from here. Pure snapshot ->
payload logic, testable offline (test_site_prices.py), thin-webapp rule.

Two price files, two joins, and the payload says which one a pub is on:
- LWC (England): rules by LWC site number. tenant_price is what the tenant pays
  per keg; FB's cost is fb_price less the retro (the PRODUCT-level fixed £/keg
  when known, else retro_pct x fb_price — exactly as deal_products.py does),
  and the margin is tenant price less that net cost. Per barrel only when the
  description names the container ("50L", "11G"); otherwise left out, never
  guessed.
- Tennent's (Scotland): products by account. The agreed total discount per
  barrel is split between the tenant's off-invoice discount and FB's retro;
  FB's margin per barrel IS the retro. Managed pubs take the whole discount
  off-invoice (retro 0); a bespoke flat-retro pub (Gartocher) keeps the flat
  figure; otherwise the stored split, and a product with no split on file is
  reported as such (off_invoice None) — a missing row is no decision, not £0.

Read-only. Nothing here writes to Airtable.
"""
from __future__ import annotations

import re
from datetime import date

from tennents_master import LITRES_PER_BRL, _LITRES_PER_GALLON, keg_brl_factor


def _r(v, places=2):
    return None if v is None else round(float(v), places)


def litres_from_description(desc: str) -> float | None:
    """Container size in litres named in an LWC description, or None. Takes the
    LAST size mentioned ("Carling 11G", "Madri Lager 50L Keg")."""
    found = re.findall(r"(\d+(?:\.\d+)?)\s*(L|G|GAL)\b", (desc or "").upper())
    if not found:
        return None
    val, unit = found[-1]
    return float(val) if unit == "L" else float(val) * _LITRES_PER_GALLON


def _active_rules(rules):
    """Newest active (valid_to is None) rule per (site_id, product_code)."""
    best: dict = {}
    for r in rules:
        if r.valid_to is not None:
            continue
        key = (str(r.site_id), r.product_code)
        cur = best.get(key)
        if cur is None or (r.valid_from or date.min) > (cur.valid_from or date.min):
            best[key] = r
    return best


def _retro_per_keg(rule, product_meta: dict):
    meta = product_meta.get(rule.product_code) or {}
    retro = meta.get("retro_per_keg")
    if retro is None and rule.fb_price is not None:
        retro = (rule.retro_pct or 0.0) * rule.fb_price
    return retro


def lwc_line(rule, product_meta: dict) -> dict:
    meta = product_meta.get(rule.product_code) or {}
    desc = meta.get("desc") or rule.product_desc or rule.product_code
    retro = _retro_per_keg(rule, product_meta)
    net_cost = (rule.fb_price - retro) if (rule.fb_price is not None and retro is not None) else None
    margin = (rule.tenant_price - net_cost) if (rule.tenant_price is not None and net_cost is not None) else None
    litres = litres_from_description(desc)
    brl = (litres / LITRES_PER_BRL) if litres else None
    return {
        "product_code": rule.product_code,
        "description": desc,
        "litres": _r(litres, 3),
        "tenant_price_keg": _r(rule.tenant_price),
        "fb_price_keg": _r(rule.fb_price),
        "retro_keg": _r(retro),
        "net_cost_keg": _r(net_cost),
        "margin_keg": _r(margin),
        "margin_per_brl": _r(margin / brl) if (margin is not None and brl) else None,
        "valid_from": rule.valid_from.isoformat() if rule.valid_from else None,
    }


def build_lwc_site_payload(snap, site_id: str) -> dict | None:
    """One LWC pub's priced products, plus the estate's other products (no
    tenant price at this pub — a proposed brand the pub does not stock yet).
    None when the site number is not on the master."""
    sid = str(site_id).strip()
    site = (snap.sites or {}).get(sid)
    if site is None:
        return None
    product_meta = getattr(snap, "products", {}) or {}
    active = _active_rules(snap.rules)
    here = [lwc_line(r, product_meta) for (s, _), r in active.items() if s == sid]
    here.sort(key=lambda p: p["description"].upper())
    stocked = {p["product_code"] for p in here}

    newest: dict = {}
    for (s, code), r in active.items():
        if code in stocked or r.fb_price is None or r.fb_price <= 0:
            continue
        cur = newest.get(code)
        if cur is None or (r.valid_from or date.min) > (cur.valid_from or date.min):
            newest[code] = r
    others = []
    for code, r in newest.items():
        line = lwc_line(r, product_meta)
        # Another pub's tenant price is not this pub's: cost side only.
        for k in ("tenant_price_keg", "margin_keg", "margin_per_brl", "valid_from"):
            line[k] = None
        others.append(line)
    others.sort(key=lambda p: p["description"].upper())

    info = snap.banner_info if isinstance(getattr(snap, "banner_info", None), dict) else {}
    return {
        "supplier": "lwc",
        "as_of": str(info.get("latest_valid_from") or ""),
        "site": {"site_id": sid, "name": site.get("name", ""), "status": site.get("status", ""),
                 "country": site.get("country", "")},
        "products": here,
        "other_products": others,
    }


def tennents_line(master, site, sku) -> dict:
    total = sku.correct_total_per_brl
    keg_brl = keg_brl_factor(sku)
    flat = site.flat_retro_per_brl
    sp = master.site_price(site.account, sku.sku_code)
    if total is None:
        off, basis = None, "no agreed rate (RATE TBC)"
    elif site.is_managed:
        off, basis = float(total), "managed: whole discount off-invoice"
    elif flat is not None:
        off, basis = max(float(total) - flat, 0.0), f"flat £{flat:,.2f}/brl retro"
    elif sp is not None:
        off, basis = float(sp.off_invoice_per_brl or 0.0), "our price file"
    else:
        off, basis = None, "no split on file"
    retro = (float(total) - off) if (total is not None and off is not None) else None
    wsp = sku.wsp_per_brl
    return {
        "sku_code": sku.sku_code,
        "product": sku.product or sku.brand or sku.sku_code,
        "brand": sku.brand or "",
        "container": sku.container or "",
        "brl_per_keg": _r(keg_brl, 4),
        "wsp_per_brl": _r(wsp),
        "total_discount_per_brl": _r(total),
        "off_invoice_per_brl": _r(off),
        "retro_per_brl": _r(retro),
        "net_keg": _r((float(wsp) - off) * keg_brl) if (wsp is not None and off is not None) else None,
        "stocked": sp is not None,
        "basis": basis,
    }


def build_tennents_site_payload(master, account: str) -> dict | None:
    """One Tennent's pub's split for every product on the master. Products the
    pub has a stored split for come first (stocked); the rest are what a bar
    plan could bring in. None when the account is not on the master."""
    site = master.site_for_account(str(account).strip())
    if site is None:
        return None
    lines = [tennents_line(master, site, s) for s in master.skus if s.sku_code]
    lines.sort(key=lambda p: (not p["stocked"], p["product"].upper()))
    return {
        "supplier": "tennents",
        "as_of": master.version or "",
        "site": {"account": site.account, "name": site.site_name, "managed": site.is_managed,
                 "flat_retro_per_brl": site.flat_retro_per_brl},
        "products": lines,
    }


def build_site_index(snap, master) -> dict:
    """Every pub on either price file, so the hub can link its own sites."""
    lwc = [
        {"site_id": str(sid), "name": s.get("name", ""), "status": s.get("status", ""),
         "country": s.get("country", "")}
        for sid, s in sorted((snap.sites or {}).items(), key=lambda kv: str(kv[0]))
    ] if snap is not None else []
    tennents = [
        {"account": s.account, "name": s.site_name, "managed": s.is_managed}
        for s in sorted(master.sites, key=lambda s: s.site_name.upper())
        if s.account and s.account.upper() != "TBC"
    ] if master is not None else []
    return {"lwc": lwc, "tennents": tennents}
