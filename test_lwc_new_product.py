"""
Offline test for "Add a product at FB's cost" (24 Nov 2026): the planner
(master_changes.plan_add_lwc_product), the bar plan feed offering a product
no pub prices yet (site_prices.build_lwc_site_payload), and a bar plan price
for it taking FB's cost from the product (webapp._lwc_cell_change). No
Airtable.

    python test_lwc_new_product.py
"""
import os
import sys
from datetime import date
from types import SimpleNamespace

os.environ.setdefault("WEB_PASSWORD", "test")

from master_changes import plan_add_lwc_product
from reconcile import Rule
from site_prices import build_lwc_site_payload


def main() -> int:
    ok = True

    def check(label, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            ok = False

    def refused(label, fn, needle):
        try:
            fn()
        except ValueError as e:
            check(f"{label} (said: {e})", needle in str(e))
            return
        check(f"{label} (was not refused)", False)

    snap = SimpleNamespace(
        sites={"802": {"name": "Rose & Crown", "status": "tenanted", "country": "england"},
               "835": {"name": "Railway", "status": "tenanted", "country": "england"}},
        rules=[
            Rule(site_id="835", product_code="19406113", product_desc="Cruzcampo 50L Keg",
                 tenant_price=206.69, fb_price=113.55, retro_pct=0.0,
                 valid_from=date(2026, 11, 12), valid_to=None),
        ],
        product_ids={"19406113": "recA", "13550005": "recH"},
        products={
            "19406113": {"desc": "Cruzcampo 50L Keg", "retro_per_keg": 0.0},
            # Added at FB's cost: no rule anywhere yet.
            "13550005": {"desc": "HEINEKEN 11G KEG", "retro_per_keg": 0.0, "list_price": 137.0},
        },
        rule_ids={}, banner_info={"latest_valid_from": "2026-11-12"},
    )

    # --- planner ---------------------------------------------------------------
    plan = plan_add_lwc_product(snap, " 13550056 ", "HEINEKEN 0.0  20L KEG", "£59.00", "", "Robin Price 24 Nov")
    check("plan: code trimmed", plan["product_code"] == "13550056")
    check("plan: description spaces tidied", plan["description"] == "HEINEKEN 0.0 20L KEG")
    check("plan: price and no retro", plan["list_price"] == 59.0 and plan["retro_per_keg"] == 0.0)
    check("plan: net cost", plan["net_cost"] == 59.0)
    p2 = plan_add_lwc_product(snap, "19030124", "OLD MOUT BERRIES 30L", "90", "5.50")
    check("plan: retro read", p2["retro_per_keg"] == 5.5 and p2["net_cost"] == 84.5)
    refused("plan: refuses a product already on the master", lambda: plan_add_lwc_product(
        snap, "19406113", "Cruzcampo", "113.55"), "already on the master")
    refused("plan: refuses a code that is not LWC's number", lambda: plan_add_lwc_product(
        snap, "HEIN-11G", "Heineken", "137"), "LWC product code")
    refused("plan: refuses no description", lambda: plan_add_lwc_product(snap, "13550099", " ", "137"), "description")
    refused("plan: refuses no price", lambda: plan_add_lwc_product(snap, "13550099", "X 11G", ""), "required")
    refused("plan: refuses £0", lambda: plan_add_lwc_product(snap, "13550099", "X 11G", "0"), "more than £0")
    refused("plan: refuses words", lambda: plan_add_lwc_product(snap, "13550099", "X 11G", "abc"), "a number")
    refused("plan: refuses a silly price", lambda: plan_add_lwc_product(snap, "13550099", "X 11G", "13700"), "£5,000")
    refused("plan: refuses retro >= price", lambda: plan_add_lwc_product(snap, "13550099", "X 11G", "50", "50"), "retro")

    # --- the hub's bar plan feed -----------------------------------------------------
    pay = build_lwc_site_payload(snap, "802")
    others = {o["product_code"]: o for o in pay["other_products"]}
    check("feed: the new product is offered at a pub with no price", "13550005" in others)
    h = others.get("13550005") or {}
    check("feed: with FB's cost", h.get("fb_price_keg") == 137.0 and h.get("net_cost_keg") == 137.0)
    check("feed: and no tenant price", h.get("tenant_price_keg") is None)
    check("feed: description from the product", h.get("description") == "HEINEKEN 11G KEG")
    check("feed: a priced product still comes from its rule", (others.get("19406113") or {}).get("fb_price_keg") == 113.55)

    # Once a pub prices it, the rule is the source and the cost row is not used again there.
    snap.rules.append(Rule(site_id="802", product_code="13550005", product_desc="HEINEKEN 11G KEG",
                           tenant_price=228.0, fb_price=137.0, retro_pct=None,
                           valid_from=date(2026, 11, 25), valid_to=None))
    pay = build_lwc_site_payload(snap, "802")
    check("feed: priced at the pub -> in its own products",
          any(p["product_code"] == "13550005" and p["tenant_price_keg"] == 228.0 for p in pay["products"]))
    check("feed: and not also in other products",
          all(o["product_code"] != "13550005" for o in pay["other_products"]))
    snap.rules.pop()

    # --- a bar plan price for it -------------------------------------------------------
    try:
        import webapp
    except Exception as e:  # FastAPI etc. missing locally
        print(f"SKIP cell change (webapp not importable here: {e})")
    else:
        ch = webapp._lwc_cell_change(snap, "802", "13550005", 228.0, date(2026, 11, 25), note="bar plan")
        check("cell: a new price adds a rule", ch is not None and ch.op == "add_rule")
        check("cell: with FB's price from the product", ch is not None and ch.fb_price == 137.0)

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
