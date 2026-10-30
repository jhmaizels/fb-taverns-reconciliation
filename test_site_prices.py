"""
Offline test for the bar plans price feed (site_prices.py): LWC and Tennent's
per-pub payloads and the site index. No Airtable, no FastAPI.

    python test_site_prices.py
"""
import sys
from datetime import date
from types import SimpleNamespace

from reconcile import Rule
from site_prices import (
    build_lwc_site_payload, build_site_index, build_tennents_site_payload, litres_from_description,
)
from tennents_master import LITRES_PER_BRL, SiteInfo, SitePrice, SkuRate, TennentsMaster


def sku(code, product, container="50L", wsp=300.0, total=250.0, brl=None):
    return SkuRate(sku_code=code, alt_code="", brand=product, product=product, container=container,
                   brl_per_unit=brl, abv=None, wsp_per_brl=wsp, contract_base_per_brl=None,
                   on_contract=True, supplier_type="C&C", hold_per_brl=0.0, correct_total_per_brl=total)


def main() -> int:
    ok = True

    def check(label, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            ok = False

    # --- litres -------------------------------------------------------------
    check("litres: 50L", litres_from_description("Madri Lager 50L Keg") == 50.0)
    check("litres: 11G", abs(litres_from_description("Carling 11G") - 11 * 4.54609) < 1e-9)
    check("litres: none named", litres_from_description("Mystery Keg") is None)

    # --- LWC ----------------------------------------------------------------
    snap = SimpleNamespace(
        sites={"817": {"name": "Bellamy's", "status": "tenanted", "country": "England"},
               "824": {"name": "Earl Grey", "status": "tenanted", "country": "England"}},
        rules=[
            Rule(site_id="817", product_code="A", product_desc="Madri Lager 50L Keg",
                 tenant_price=230.0, fb_price=130.0, retro_pct=0.1,
                 valid_from=date(2026, 4, 1), valid_to=None),
            Rule(site_id="817", product_code="A", product_desc="Madri Lager 50L Keg",
                 tenant_price=200.0, fb_price=120.0, retro_pct=0.1,
                 valid_from=date(2025, 4, 1), valid_to=None),
            Rule(site_id="817", product_code="B", product_desc="Old Ale 9G",
                 tenant_price=150.0, fb_price=90.0, retro_pct=0.0,
                 valid_from=date(2026, 1, 1), valid_to=date(2026, 3, 1)),
            Rule(site_id="824", product_code="C", product_desc="Carling 11G",
                 tenant_price=210.0, fb_price=110.0, retro_pct=0.0,
                 valid_from=date(2026, 4, 1), valid_to=None),
        ],
        products={"A": {"desc": "Madri Lager 50L Keg", "retro_per_keg": 9.0}},
        banner_info={"latest_valid_from": "2026-04-01"},
    )
    p = build_lwc_site_payload(snap, "817")
    check("lwc: site found", p["site"]["name"] == "Bellamy's" and p["supplier"] == "lwc")
    check("lwc: one active product, closed one dropped", [x["product_code"] for x in p["products"]] == ["A"])
    a = p["products"][0]
    check("lwc: newest rule wins", a["tenant_price_keg"] == 230.0)
    check("lwc: product retro preferred", a["retro_keg"] == 9.0 and a["net_cost_keg"] == 121.0)
    check("lwc: margin per keg", a["margin_keg"] == 109.0)
    check("lwc: margin per barrel from 50L", a["margin_per_brl"] == round(109.0 / (50 / LITRES_PER_BRL), 2))
    check("lwc: other pub's product offered, cost side only",
          [x["product_code"] for x in p["other_products"]] == ["C"]
          and p["other_products"][0]["tenant_price_keg"] is None
          and p["other_products"][0]["net_cost_keg"] == 110.0)
    check("lwc: unknown site is None", build_lwc_site_payload(snap, "999") is None)

    # --- Tennent's ----------------------------------------------------------
    master = TennentsMaster(
        version="v7", source="t",
        skus=[sku("100", "Tennent's Lager", "11G", brl=0.3056), sku("200", "Menabrea", "50L", total=None),
              sku("300", "Guinness", "50L", wsp=None)],
        sites=[SiteInfo("A1", "Hazel Arms", "Tenanted", "standard"),
               SiteInfo("M1", "Managed Bar", "MANAGED (confirmed)", "standard"),
               SiteInfo("G1", "Gartocher", "Tenanted", "BESPOKE flat £200/brl")],
        exceptions=[],
        site_prices=[SitePrice("A1", "Hazel Arms", "100", off_invoice_per_brl=109.70)],
    )
    t = build_tennents_site_payload(master, "A1")
    lager = t["products"][0]
    check("tn: stocked first", lager["sku_code"] == "100" and lager["stocked"])
    check("tn: retro = total - off", lager["retro_per_brl"] == round(250 - 109.70, 2))
    check("tn: net keg", lager["net_keg"] == round((300 - 109.70) * 0.3056, 2))
    guin = next(x for x in t["products"] if x["sku_code"] == "300")
    check("tn: no split on file is None, not £0", guin["off_invoice_per_brl"] is None and guin["retro_per_brl"] is None)
    mena = next(x for x in t["products"] if x["sku_code"] == "200")
    check("tn: RATE TBC has no retro", mena["retro_per_brl"] is None and "TBC" in mena["basis"])
    m = build_tennents_site_payload(master, "M1")["products"][0]
    check("tn: managed retro 0", m["retro_per_brl"] == 0.0)
    g = next(x for x in build_tennents_site_payload(master, "G1")["products"] if x["sku_code"] == "100")
    check("tn: flat retro kept", g["retro_per_brl"] == 200.0 and g["off_invoice_per_brl"] == 50.0)
    check("tn: unknown account None", build_tennents_site_payload(master, "ZZ") is None)

    # --- index --------------------------------------------------------------
    idx = build_site_index(snap, master)
    check("index: lwc sites", [s["site_id"] for s in idx["lwc"]] == ["817", "824"])
    check("index: tennents sites by name", [s["name"] for s in idx["tennents"]][0] == "Gartocher")

    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
