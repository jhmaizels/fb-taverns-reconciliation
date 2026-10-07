"""
Offline tests for Tennents bar plan changes and the monthly split check:
plan_bar_plan_change's refusals and figures, expected_off_invoice, the
reconcile split check (section 6b), set_tennents_site_price, and a workbook
re-upload keeping a bar plan change.

Run standalone:  python test_tennents_bar_plan.py
"""
from __future__ import annotations

import sys

import airtable_io as aio  # noqa: E402
import tennents_master as tm  # noqa: E402
from tennents import DeliveryLine, MonthlyReport, reconcile, render_summary_html  # noqa: E402
from tennents_master import (  # noqa: E402
    SiteInfo, SitePrice, SkuRate, TennentsMaster, expected_off_invoice, plan_bar_plan_change,
)
from test_tennents_findings import FakeIO, _install  # noqa: E402  (also sets utf-8 stdout)

PASS = True


def _check(label, cond, detail=""):
    global PASS
    PASS &= bool(cond)
    print(f"  [{'ok' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")


def sku(code, product, total, wsp=606.0, alt=""):
    return SkuRate(code, alt, product.split()[0], product, "50L", 0.3055, 4.0, wsp, total, True, "C&C", 0.0, total)


def make_master(site_prices=None) -> TennentsMaster:
    skus = [
        sku("401211", "T.Bavarian Pilsner 50L", 350.0, wsp=778.70),
        sku("090425", "T.Lager 50L", 314.33, alt="09000X"),
        SkuRate("499999", "", "Stout", "Stout TBC", "50L", 0.3055, 4.0, None, None, False, "C&C", 0.0, None),
    ]
    sites = [
        SiteInfo("11110001", "BELL ROCK", "Tenanted (TBC)", "Standard split"),
        SiteInfo("11110002", "MANAGED HOUSE", "MANAGED (confirmed)", "ALL OFF-INVOICE"),
        SiteInfo("11110003", "GARTOCHER", "Tenanted (confirmed)", "BESPOKE: flat £200/brl retro on all SKUs"),
    ]
    if site_prices is None:
        site_prices = [SitePrice("11110001", "BELL ROCK", "090425", "T.Lager 50L", 100.0)]
    return TennentsMaster("vTEST", "fixture", skus, sites, [], site_prices=site_prices,
                          site_prices_present=True)


def test_prefix_kept_equal():
    print("\n-- prefix constant")
    _check("airtable_io and tennents_master agree", aio.BAR_PLAN_SOURCE_PREFIX == tm.BAR_PLAN_SOURCE_PREFIX)


def test_plan():
    print("\n-- plan_bar_plan_change")
    m = make_master()
    c = plan_bar_plan_change(m, "11110001", "401211", "£173")
    _check("new row: no current figure", c.current_off is None)
    _check("retro is the rest of the total", c.new_retro == 177.0, str(c.new_retro))
    _check("net keg = (WSP − off) × brl", c.net_keg(c.new_off) == round((778.70 - 173) * 0.3055, 2), str(c.net_keg(c.new_off)))
    c = plan_bar_plan_change(m, "11110001", "09000X", "118")
    _check("alt code resolves to canonical", c.sku_code == "090425")
    _check("existing row read", c.current_off == 100.0 and c.current_retro == 214.33, f"{c.current_off} {c.current_retro}")
    _check("0 allowed (not sold here)", plan_bar_plan_change(m, "11110001", "090425", "0").removing)

    def refuses(label, *args):
        try:
            plan_bar_plan_change(m, *args)
            _check(label, False, "accepted")
        except ValueError:
            _check(label, True)
    refuses("unknown site", "99999999", "401211", "100")
    refuses("managed site", "11110002", "401211", "100")
    refuses("unknown product", "11110001", "123456", "100")
    refuses("RATE TBC", "11110001", "499999", "100")
    refuses("not a number", "11110001", "401211", "lots")
    refuses("negative", "11110001", "401211", "-1")
    refuses("above the total", "11110001", "401211", "350.01")

    c = plan_bar_plan_change(m, "11110003", "401211", "173")
    _check("flat-retro site warned when it breaks the flat retro", any("flat £200.00" in w for w in c.warnings), str(c.warnings))
    c = plan_bar_plan_change(m, "11110003", "401211", "150")
    _check("flat-retro site not warned when it keeps it", not c.warnings, str(c.warnings))


def test_expected():
    print("\n-- expected_off_invoice")
    m = make_master()
    _check("price file row", expected_off_invoice(m, "11110001", "090425", 314.33) == (100.0, "our price file"))
    _check("alt code", expected_off_invoice(m, "11110001", "09000X", 314.33)[0] == 100.0)
    _check("no row → not judged", expected_off_invoice(m, "11110001", "401211", 350)[0] is None)
    _check("managed → not judged", expected_off_invoice(m, "11110002", "090425", 314.33)[0] is None)
    _check("flat retro → total − flat", expected_off_invoice(m, "11110003", "090425", 314.33)[0] == 114.33)


def L(account="11110001", customer="BELL ROCK", sku="090425", off=100.0, retro=214.33, aod=0.0, total=None,
      barrels=0.3055) -> DeliveryLine:
    if total is None:
        total = off + retro + aod
    return DeliveryLine(account=account, customer_name=customer, sku_code=sku, sku_desc="desc",
                        kegs=1.0, barrels=barrels, invoice_price=100.0, off_per_brl=off,
                        retro_per_brl=retro, aod_per_brl=aod, total_per_brl=total,
                        retro_due=retro * barrels, net_price=None)


def R(*lines) -> MonthlyReport:
    return MonthlyReport(lines=list(lines), excluded_lines=[], period="2026-10", sign_normalized=True)


def test_split_check():
    print("\n-- reconcile split check")
    m = make_master()
    s = reconcile("t", m, R(L()))
    _check("matching split: nothing", not s.split_mismatches and not s.split_unrecorded)
    s = reconcile("t", m, R(L(off=100.40, retro=213.93)))
    _check("within ±£0.50: nothing", not s.split_mismatches)
    s = reconcile("t", m, R(L(off=118.0, retro=196.33), L(off=118.0, retro=196.33)))
    _check("split differs: one bucket", len(s.split_mismatches) == 1, str(s.split_mismatches))
    r = s.split_mismatches[0]
    _check("delta = actual − expected", r.delta_per_brl == 18.0, str(r.delta_per_brl))
    _check("FB retro short = delta × barrels", abs(r.retro_short_gbp - 18.0 * 0.611) < 0.01, str(r.retro_short_gbp))
    s = reconcile("t", m, R(L(off=90.0, retro=214.33, aod=10.0)))
    _check("AOD counts as off-invoice", not s.split_mismatches, str(s.split_mismatches))
    s = reconcile("t", m, R(L(off=118.0, retro=150.0)))
    _check("wrong total not double-flagged as a split", not s.split_mismatches and s.discount_mismatches)
    s = reconcile("t", m, R(L(sku="401211", off=173.0, retro=177.0)))
    _check("no split on file: listed as unrecorded", len(s.split_unrecorded) == 1 and not s.split_mismatches)
    s = reconcile("t", m, R(L(sku="401211", off=0.0, retro=350.0)))
    _check("no split on file, nothing off: not listed", not s.split_unrecorded)
    s = reconcile("t", m, R(L(account="11110002", customer="MANAGED HOUSE", off=314.33, retro=0.0)))
    _check("managed: not split-checked", not s.split_mismatches and not s.split_unrecorded)
    s = reconcile("t", m, R(L(account="11110003", customer="GARTOCHER", off=114.33, retro=200.0)))
    _check("flat retro kept: nothing", not s.split_mismatches)
    s = reconcile("t", m, R(L(account="11110003", customer="GARTOCHER", off=164.33, retro=150.0)))
    _check("flat retro broken: flagged", len(s.split_mismatches) == 1)
    s = reconcile("t", m, R(L(off=118.0, retro=196.33), L(sku="401211", off=173.0, retro=177.0)))
    html = render_summary_html(s)
    _check("section 6b rendered", "6b. Off-invoice split differs" in html and "has no split" in html)


def test_set_site_price():
    print("\n-- set_tennents_site_price")
    m = make_master()
    tid = aio.T["TennentsSitePrices"]
    fake = FakeIO([])
    fake.tables[tid] = [{"id": "recP1", "fields": {"price_key": "11110001|90425", "account": "11110001",
                                                   "site_name": "BELL ROCK", "sku_code": "90425",
                                                   "off_invoice_per_brl": 100.0, "notes": "seeded"}}]
    _install(fake)
    r = aio.set_tennents_site_price(m, plan_bar_plan_change(m, "11110001", "090425", "118"), "james@x", "Nick 7 Oct")
    row = fake.tables[tid][0]["fields"]
    _check("updated in place (leading-zero drift matched)", r["action"] == "updated" and len(fake.tables[tid]) == 1)
    _check("figure written", row["off_invoice_per_brl"] == 118.0)
    _check("stamped bar plan", row["source_file"].startswith(aio.BAR_PLAN_SOURCE_PREFIX + "james@x"))
    _check("notes carry before → after and keep the old", "£100.00 → £118.00" in row["notes"] and "Nick 7 Oct" in row["notes"]
           and row["notes"].endswith("seeded"), row["notes"])
    r = aio.set_tennents_site_price(m, plan_bar_plan_change(m, "11110001", "090425", "118"), "james@x")
    _check("same figure again: nothing written", r["action"] == "already")
    r = aio.set_tennents_site_price(m, plan_bar_plan_change(m, "11110001", "401211", "173"), "james@x")
    _check("new row created", r["action"] == "created" and len(fake.tables[tid]) == 2)
    _check("listed as a bar plan change", len(aio.list_bar_plan_changes()) == 2)


def test_replace_keeps_bar_plan():
    print("\n-- replace_tennents_master keeps bar plan rows")
    tid = aio.T["TennentsSitePrices"]
    fake = FakeIO([])
    stamp = aio.BAR_PLAN_SOURCE_PREFIX + "james@x 2026-10-07"
    fake.tables[tid] = [
        {"id": "r1", "fields": {"price_key": "11110001|090425", "account": "11110001", "site_name": "BELL ROCK",
                                "sku_code": "090425", "off_invoice_per_brl": 118.0, "source_file": stamp}},
        {"id": "r2", "fields": {"price_key": "11110001|401211", "account": "11110001", "site_name": "BELL ROCK",
                                "sku_code": "401211", "off_invoice_per_brl": 173.0, "source_file": stamp}},
        {"id": "r3", "fields": {"price_key": "11110003|090425", "account": "11110003", "site_name": "GARTOCHER",
                                "sku_code": "090425", "off_invoice_per_brl": 114.33, "source_file": stamp}},
    ]
    _install(fake)
    wb = make_master(site_prices=[
        SitePrice("11110001", "BELL ROCK", "90425", "T.Lager 50L", 100.0),      # older figure, drifted code
        SitePrice("11110003", "GARTOCHER", "090425", "T.Lager 50L", 114.33),    # workbook caught up
    ])
    report: dict = {}
    deleted, created, preserved = aio.replace_tennents_master(wb, "wb.xlsx", report=report)
    rows = {(f["fields"]["account"], f["fields"]["sku_code"].lstrip("0")): f["fields"] for f in fake.tables[tid]}
    _check("page figure wins over an older workbook", rows[("11110001", "90425")]["off_invoice_per_brl"] == 118.0)
    _check("row absent from the workbook re-created", rows[("11110001", "401211")]["off_invoice_per_brl"] == 173.0)
    _check("workbook that caught up: plain workbook row",
           not str(rows[("11110003", "90425")].get("source_file", "")).startswith(aio.BAR_PLAN_SOURCE_PREFIX))
    _check("no duplicate rows", len(fake.tables[tid]) == 3, str(len(fake.tables[tid])))
    kept = report.get("bar_plan") or []
    _check("report lists the two kept", sorted(k["action"] for k in kept) == ["created", "kept_over_workbook"], str(kept))
    _check("preserved counts them", preserved == 2, str(preserved))


def main() -> int:
    test_prefix_kept_equal()
    test_plan()
    test_expected()
    test_split_check()
    test_set_site_price()
    test_replace_keeps_bar_plan()
    print("\nALL PASS" if PASS else "\nFAILURES")
    return 0 if PASS else 1


if __name__ == "__main__":
    sys.exit(main())
