"""
Offline tests for tennents_july_restore.plan_july_restore: which zeroed July
2026 deals are restored, left alone or refused against today's master.

Run standalone:  python test_tennents_july_restore.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("AIRTABLE_TOKEN", "test-token")
os.environ.setdefault("AIRTABLE_BASE_ID", "appTEST")

import tennents_july_restore as jr
from tennents_master import SiteInfo, SitePrice, SkuRate, TennentsMaster

PASS = True


def _check(label, cond, detail=""):
    global PASS
    PASS &= bool(cond)
    print(f"  [{'ok' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")


def sku(code, product, total, wsp=606.0, alt=""):
    return SkuRate(code, alt, product.split()[0], product, "50L", 0.3055, 4.0, wsp, total, True, "C&C", 0.0, total)


def make_master() -> TennentsMaster:
    skus = [
        sku("GUI002", "Guinness Keg", 300.0, alt="GUI003"),
        sku("400557", "Menabrea 30L", 250.0),
        sku("004723", "Tennents Ember 80/-", 200.0),
        sku("401152", "Tennents Special 70/-", 100.0),
    ]
    sites = [
        SiteInfo("11110001", "THE REAL MCCOY", "Tenanted (TBC)", "Standard split"),
        SiteInfo("11110002", "MANAGED HOUSE", "MANAGED (confirmed)", "ALL OFF-INVOICE"),
        SiteInfo("11110003", "BELLS BAR", "Tenanted (TBC)", "Standard split"),
    ]
    prices = [
        SitePrice("11110001", "THE REAL MCCOY", "GUI002", "Guinness Keg", 0.0),
        SitePrice("11110003", "BELLS BAR", "400557", "Menabrea 30L", 80.0),
        SitePrice("11110003", "BELLS BAR", "004723", "Tennents Ember 80/-", 83.78),
    ]
    return TennentsMaster("vTEST", "fixture", skus, sites, [], site_prices=prices, site_prices_present=True)


def test_plan():
    print("\n-- plan_july_restore")
    saved = jr.JULY_ZEROED_DEALS
    jr.JULY_ZEROED_DEALS = [
        ("THE REAL MCCOY", "GUI002/GUI003", "Guinness Keg", 84.12),       # £0 row -> restore
        ("bells bar", "GUI003", "Guinness Keg", 134.20),                   # no row, alt code, case -> restore
        ("BELLS BAR", "400557/400751", "Menabrea", 5.13),                  # set since July -> changed
        ("BELLS BAR", "4723", "Tennents Ember 80/-", 83.78),               # leading-zero drift, same -> already
        ("MANAGED HOUSE", "GUI002/GUI003", "Guinness Keg", 50.0),          # managed -> refused
        ("NOWHERE INN", "GUI002/GUI003", "Guinness Keg", 50.0),            # no pub -> refused
        ("THE REAL MCCOY", "STE025", "Stella Artois", 114.73),             # no product -> refused
        ("THE REAL MCCOY", "401152", "Tennents Special 70/-", 120.03),     # above total -> refused
    ]
    try:
        out = jr.plan_july_restore(make_master())
    finally:
        jr.JULY_ZEROED_DEALS = saved
    st = [ln.status for ln in out]
    _check("statuses", st == ["restore", "restore", "changed", "already", "refused", "refused", "refused", "refused"], str(st))
    a, b = out[0], out[1]
    _check("restore figure and retro", a.change.new_off == 84.12 and a.change.new_retro == round(300 - 84.12, 2),
           f"{a.change.new_off} {a.change.new_retro}")
    _check("£0 row read as current 0", a.current_off == 0.0)
    _check("alt code resolves to canonical", b.sku_code == "GUI002" and b.current_off is None, b.sku_code)
    _check("changed keeps its figure", out[2].current_off == 80.0 and out[2].change is None)
    _check("refusals carry a reason", all(ln.reason for ln in out[4:]))


def test_data():
    print("\n-- the July list")
    d = jr.JULY_ZEROED_DEALS
    _check("45 lines", len(d) == 45, str(len(d)))
    _check("all figures positive", all(off > 0 for *_, off in d))
    _check("no duplicate (pub, code)", len({(s, c) for s, c, *_ in d}) == len(d))


def test_routes():
    print("\n-- routes (offline)")
    import webapp
    from test_master_editor import FakeAuthClient
    m = make_master()
    saved = (jr.JULY_ZEROED_DEALS, webapp.load_tennents_master, webapp.set_tennents_site_price,
             webapp._tennents_master_banner_html)
    writes = []
    jr.JULY_ZEROED_DEALS = [("THE REAL MCCOY", "GUI002/GUI003", "Guinness Keg", 84.12),
                            ("BELLS BAR", "4723", "Tennents Ember 80/-", 83.78)]
    webapp.load_tennents_master = lambda *a, **k: m
    webapp._tennents_master_banner_html = lambda *a, **k: ""
    webapp.set_tennents_site_price = lambda master, change, actor, note="": (
        writes.append((change.account, change.sku_code, change.new_off, actor, note)) or {"action": "updated"})
    try:
        with FakeAuthClient("admin") as c:
            r = c.get("/tennents/july-restore")
            _check("preview 200", r.status_code == 200, str(r.status_code))
            _check("preview offers one restore", "Restore 1 deal to" in r.text)
            _check("preview writes nothing", writes == [])
            r = c.post("/tennents/july-restore/apply")
            _check("apply 200", r.status_code == 200, str(r.status_code))
            _check("one write, the restore row", len(writes) == 1 and writes[0][:3] == ("11110001", "GUI002", 84.12),
                   str(writes))
            _check("note says July", "July 2026" in writes[0][4] if writes else False)
            _check("result names the pub", "THE REAL MCCOY" in r.text and "Restored 1 of 1" in r.text)
        with FakeAuthClient("editor") as c:
            _check("editor refused", c.get("/tennents/july-restore").status_code in (302, 303, 401, 403))
    finally:
        (jr.JULY_ZEROED_DEALS, webapp.load_tennents_master, webapp.set_tennents_site_price,
         webapp._tennents_master_banner_html) = saved


def main() -> int:
    test_plan()
    test_data()
    test_routes()
    print("\nALL PASS" if PASS else "\nFAILURES")
    return 0 if PASS else 1


if __name__ == "__main__":
    sys.exit(main())
