"""
Offline tests for price_history: reading the change trail off the LWC rules
and the Tennent's notes, which change can be undone, and the roll-back plan.

Run standalone:  python test_price_history.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from types import SimpleNamespace

import price_history as ph
from reconcile import Rule
from tennents_master import SiteInfo, SitePrice, SkuRate, TennentsMaster

TODAY = date(2026, 10, 20)
D = date.fromisoformat


def _rule(code, price, vf, vt=None, *, site="817", reason="", source="", status="tenanted", fb=100.0):
    return Rule(site_id=site, product_code=code, product_desc=f"desc {code}", tenant_price=price,
                fb_price=fb, retro_pct=0.1, valid_from=D(vf) if vf else None,
                valid_to=D(vt) if vt else None, status=status, reason=reason, source=source)


def _snap(rules, created=None):
    return SimpleNamespace(rules=rules, sites={"817": {"name": "Bellamys"}, "819": {"name": "Earl Grey"}},
                           rule_created=created or {})


def _cell(events, code, site="817"):
    return [e for e in events if e.product_code == code and e.site_id == site]


def test_bellamys_add_then_wrong_correction():
    """Add at charged at 14:59, then a wrong figure typed over it at 17:29 the
    same day: two changes, and only the second can be undone, back to £87."""
    r = _rule("12300006", 150.0, "2026-10-20",
              reason="corrected by j@x.com 2026-10-20: was £87.00 — grid: price corrected (was £87.00); "
                     "findings: accepted at charged", source="grid:j@x.com")
    ev = ph.lwc_events(_snap([r], {"817|12300006|2026-10-20": "2026-10-20T13:59:00.000Z"}), TODAY)
    c = _cell(ev, "12300006")
    assert [(e.before, e.after) for e in sorted(c, key=lambda e: e.order)] == [(None, 87.0), (87.0, 150.0)], c
    last = [e for e in c if e.latest][0]
    assert last.undoable and last.before == 87.0 and last.who == "j@x.com"
    assert not [e for e in c if not e.latest][0].undoable


def test_price_change_reads_the_closed_predecessor():
    old = _rule("A", 180.0, "2026-01-01", "2026-10-20")
    new = _rule("A", 199.5, "2026-10-20", source="grid:j@x.com")
    c = [e for e in _cell(ph.lwc_events(_snap([old, new]), TODAY), "A") if e.on == TODAY]
    assert len(c) == 1 and (c[0].before, c[0].after) == (180.0, 199.5) and c[0].undoable
    assert c[0].on == TODAY and c[0].who == "j@x.com"


def test_cost_only_successor_is_not_a_price_change():
    old = _rule("A", 180.0, "2026-01-01", "2026-10-20", fb=100.0)
    new = _rule("A", 180.0, "2026-10-20", fb=110.0)
    assert [e for e in _cell(ph.lwc_events(_snap([old, new]), TODAY), "A") if e.on == TODAY] == []


def test_removed_cell_and_undo_target():
    r = _rule("B", 120.0, "2026-01-01", "2026-10-20", reason="grid: price removed (was £120.00)",
              source="grid:j@x.com")
    c = [e for e in _cell(ph.lwc_events(_snap([r]), TODAY), "B") if e.after is None]
    assert len(c) == 1 and c[0].before == 120.0 and c[0].on == TODAY and c[0].undoable


def test_removed_on_its_first_day():
    r = _rule("B", 120.0, "2026-10-20", "2026-10-21")
    c = [e for e in _cell(ph.lwc_events(_snap([r]), TODAY), "B") if e.after is None]
    assert c and c[0].on == TODAY


def test_supports_are_left_out_and_block_undo():
    std_old = _rule("C", 180.0, "2026-01-01", "2026-10-20")
    std_new = _rule("C", 190.0, "2026-10-20")
    sup = _rule("C", 150.0, "2026-10-01", "2026-12-01", status="supported")
    ev = ph.lwc_events(_snap([std_old, std_new, sup]), TODAY)
    c = [e for e in _cell(ev, "C") if e.on == TODAY]
    assert len(c) == 1 and c[0].after == 190.0
    assert not c[0].undoable, "the grid shows the support, not the price this change set"
    items = ph.plan_rollback(ev, TODAY)
    assert len(items) == 1 and items[0].blocked


def test_superseded_change_is_not_undoable():
    a = _rule("E", 100.0, "2026-01-01", "2026-10-10")
    b = _rule("E", 110.0, "2026-10-10", "2026-10-20")
    c_ = _rule("E", 120.0, "2026-10-20")
    c = sorted(_cell(ph.lwc_events(_snap([a, b, c_]), TODAY), "E"), key=lambda e: e.order)
    assert [(e.before, e.after, e.undoable) for e in c] == [
        (None, 100.0, False), (100.0, 110.0, False), (110.0, 120.0, True)]


def test_rollback_to_a_date_puts_back_the_first_before():
    a = _rule("E", 100.0, "2026-01-01", "2026-10-10")
    b = _rule("E", 110.0, "2026-10-10", "2026-10-20")
    c_ = _rule("E", 120.0, "2026-10-20")
    other = _rule("F", 50.0, "2026-01-01", "2026-10-20", site="819")
    other2 = _rule("F", 55.0, "2026-10-20", site="819")
    ev = ph.lwc_events(_snap([a, b, c_, other, other2]), TODAY)
    items = ph.plan_rollback(ev, D("2026-10-05"))
    by = {(i.site_id, i.product_code): i for i in items}
    assert by[("817", "E")].target == 100.0 and by[("817", "E")].changes == 2
    assert by[("819", "F")].target == 50.0
    only = ph.plan_rollback(ev, D("2026-10-15"), site="817")
    assert [(i.product_code, i.target) for i in only] == [("E", 110.0)]
    assert ph.plan_rollback(ev, D("2026-10-21")) == []


def test_filter_window_and_search():
    old = _rule("A", 180.0, "2026-01-01", "2026-10-20")
    new = _rule("A", 199.5, "2026-10-20", source="grid:j@x.com")
    ev = ph.lwc_events(_snap([old, new]), TODAY)
    assert len(ph.filter_events(ev, since=TODAY)) == 1
    assert ph.filter_events(ev, since=TODAY, site="819") == []
    assert len(ph.filter_events(ev, since=TODAY, q="j@x")) == 1


# ---------------- Tennent's ----------------

def _master():
    skus = [SkuRate("401211", "", "T.", "T.Bavarian Pilsner", "50L", 0.3055, 4.0, 778.70, 350.0, True, "C&C",
                    0.0, 350.0, notes="Price grid 2026-10-12 by n@x.com: total £340.00 → £350.00/brl, "
                                      "WSP £770.00 → £778.70/brl."),
            SkuRate("090425", "", "T.", "T.Lager", "50L", 0.3055, 4.0, 606.0, 314.33, True, "C&C", 0.0, 314.33,
                    notes="Price grid 2026-10-12 by n@x.com: total RATE TBC → £314.33/brl.")]
    sites = [SiteInfo("11110001", "BELL ROCK", "Tenanted (TBC)", "Standard split")]
    prices = [SitePrice("11110001", "BELL ROCK", "090425", "T.Lager 50L", 120.0,
                        notes="Bar plan change 2026-10-19 by j@x.com: off-invoice £100.00 → £120.00/brl. | "
                              "Bar plan change 2026-10-02 by j@x.com: off-invoice none → £100.00/brl. Restored."),
              SitePrice("11110001", "BELL ROCK", "401211", "", 173.0, notes="from the workbook")]
    return TennentsMaster("vTEST", "fixture", skus, sites, [], site_prices=prices, site_prices_present=True)


def test_tennents_site_price_chain():
    ev = ph.tennents_events(_master())
    c = sorted([e for e in ev if e.kind == ph.TN_OFF], key=lambda e: e.order)
    assert [(e.on, e.before, e.after) for e in c] == [(D("2026-10-02"), 0.0, 100.0), (D("2026-10-19"), 100.0, 120.0)]
    assert c[-1].undoable and not c[0].undoable and c[-1].site_name == "BELL ROCK"


def test_tennents_product_figures():
    ev = ph.tennents_events(_master())
    tot = [e for e in ev if e.kind == ph.TN_TOTAL and e.product_code == "401211"]
    wsp = [e for e in ev if e.kind == ph.TN_WSP]
    assert [(e.before, e.after, e.undoable) for e in tot] == [(340.0, 350.0, True)]
    assert [(e.before, e.after, e.undoable) for e in wsp] == [(770.0, 778.7, True)]
    tbc = [e for e in ev if e.kind == ph.TN_TOTAL and e.product_code == "090425"]
    assert tbc and tbc[0].before is None and not tbc[0].undoable, "RATE TBC can't be put back"
    items = ph.plan_rollback(ev, D("2026-10-01"))
    blocked = [i for i in items if i.blocked]
    assert len(blocked) == 1 and blocked[0].product_code == "090425"
    off = [i for i in items if i.kind == ph.TN_OFF]
    assert off and off[0].target == 0.0


def test_pages_render():
    ev = ph.tennents_events(_master())
    html = ph.render_history_page(master="tennents", events=ev, sites=[("11110001", "BELL ROCK")],
                                  params={"days": "3650"}, is_admin=True)
    assert "Undo: put back £100.00" in html and "Roll back to an earlier date" in html
    viewer = ph.render_history_page(master="tennents", events=ev, sites=[], params={"days": "3650"},
                                    is_admin=False)
    assert "Undo:" not in viewer and "Roll back to an earlier date" not in viewer
    page = ph.render_rollback_page(master="tennents", items=ph.plan_rollback(ev, D("2026-10-01")),
                                   since=D("2026-10-01"), site="", site_label="")
    assert "Put back 3 prices" in page, page


TESTS = [v for k, v in dict(globals()).items() if k.startswith("test_")]


def main() -> int:
    failures = 0
    for t in TESTS:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}\n      {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"ok    {t.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
