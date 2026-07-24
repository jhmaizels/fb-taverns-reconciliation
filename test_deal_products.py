"""
Offline test for the Deal Generator product feed (deal_products.py): the
snapshot -> payload builder and the bearer-token check. No Airtable, no
FastAPI — pure functions on a hand-built snapshot.

Run standalone (exit 0 = pass, 1 = fail):

    python test_deal_products.py
"""
import sys
from datetime import date
from types import SimpleNamespace

from deal_products import build_deal_products_payload, token_ok
from reconcile import Rule


def snap(rules, products=None, banner=None):
    return SimpleNamespace(
        rules=rules,
        products=products or {},
        banner_info=banner or {},
    )


def main() -> int:
    ok = True

    def check(label, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            ok = False

    # --- token_ok -----------------------------------------------------------
    check("token: match", token_ok("Bearer s3cret", "s3cret"))
    check("token: wrong value rejected", not token_ok("Bearer nope", "s3cret"))
    check("token: missing header rejected", not token_ok(None, "s3cret"))
    check("token: non-bearer rejected", not token_ok("Basic s3cret", "s3cret"))
    check("token: unconfigured server matches nothing", not token_ok("Bearer ", ""))
    try:
        rejected_not_crashed = not token_ok("Bearer café\U0001f37a", "s3cret")
    except TypeError:
        rejected_not_crashed = False
    check("token: non-ASCII header is a 401, not a TypeError 500", rejected_not_crashed)

    # --- payload ------------------------------------------------------------
    rules = [
        # Two rules for the same product at different sites: newest valid_from wins.
        Rule(site_id="101", product_code="15151307", product_desc="Madri Lager 50L Keg",
             tenant_price=230.0, fb_price=125.0, retro_pct=0.06983,
             valid_from=date(2025, 4, 1), valid_to=None),
        Rule(site_id="102", product_code="15151307", product_desc="Madri Lager 50L Keg",
             tenant_price=238.0, fb_price=131.30, retro_pct=0.06983,
             valid_from=date(2026, 4, 1), valid_to=None),
        # Closed rule: excluded even though newest.
        Rule(site_id="101", product_code="15530004", product_desc="Carling 11G",
             tenant_price=210.0, fb_price=999.0, retro_pct=0.0,
             valid_from=date(2026, 6, 1), valid_to=date(2026, 7, 1)),
        Rule(site_id="101", product_code="15530004", product_desc="Carling 11G",
             tenant_price=210.0, fb_price=114.60, retro_pct=0.08,
             valid_from=date(2026, 4, 1), valid_to=None),
        # No usable fb_price: excluded entirely.
        Rule(site_id="101", product_code="19999999", product_desc="Mystery Keg",
             tenant_price=100.0, fb_price=None, valid_from=date(2026, 4, 1), valid_to=None),
    ]
    products = {
        "15151307": {"desc": "Madri Lager 50L Keg", "retro_per_keg": 9.17},
        # Carling has NO product-level retro -> derived from retro_pct.
    }
    payload = build_deal_products_payload(
        snap(rules, products, {"latest_valid_from": "2026-04-01"})
    )

    check("as_of from banner", payload["as_of"] == "2026-04-01")
    codes = [p["product_code"] for p in payload["products"]]
    check("two products, sorted by code, mystery excluded", codes == ["15151307", "15530004"])

    madri = payload["products"][0]
    check("newest active rule's fb_price wins", madri["fb_price"] == 131.30)
    check("product-level retro preferred", madri["retro_gbp"] == 9.17)
    check("net = fb - retro", madri["net_price"] == round(131.30 - 9.17, 4))
    check("description from product meta", madri["description"] == "Madri Lager 50L Keg")

    carling = payload["products"][1]
    check("closed rule ignored (fb from open rule)", carling["fb_price"] == 114.60)
    check("retro derived from retro_pct when no product retro",
          carling["retro_gbp"] == round(0.08 * 114.60, 4))

    empty = build_deal_products_payload(snap([]))
    check("empty snapshot -> empty products, blank as_of",
          empty == {"as_of": "", "products": []})

    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
