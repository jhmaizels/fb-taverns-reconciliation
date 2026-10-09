"""
Offline tests for the Tennents price grid: plan_sku_rate_change and
plan_add_sku (tennents_master), set_tennents_sku_rate / add_tennents_sku and
a workbook re-upload keeping a grid-set total (airtable_io), and the grid
renderer (tennents_grid).

Run standalone:  python test_tennents_grid.py
"""
from __future__ import annotations

import sys

import airtable_io as aio  # noqa: E402
import tennents_grid  # noqa: E402
from tennents_master import SitePrice, plan_add_sku, plan_sku_rate_change  # noqa: E402
from test_tennents_bar_plan import make_master  # noqa: E402
from test_tennents_cache import FakeIO, _install  # noqa: E402

PASS = True


def _check(label, cond, detail=""):
    global PASS
    PASS &= bool(cond)
    print(f"  [{'ok' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")


def _refuses(label, fn, *args, **kw):
    try:
        fn(*args, **kw)
        _check(label, False, "accepted")
    except ValueError as e:
        _check(label, True, str(e))
        return str(e)


def test_plan_sku_rate_change():
    print("\n-- plan_sku_rate_change")
    m = make_master()
    c = plan_sku_rate_change(m, "09000X", "£320")
    _check("alt code resolves; total parsed", c.sku_code == "090425" and c.new_total == 320.0 and c.total_changed)
    _check("WSP left alone when blank", c.new_wsp is None and not c.wsp_changed)
    c = plan_sku_rate_change(m, "090425", "314.33", "610")
    _check("same total + new WSP: only the WSP changed", not c.total_changed and c.wsp_changed and c.new_wsp == 610.0)
    c = plan_sku_rate_change(m, "090425", "", "610")
    _check("blank total keeps the current one", c.new_total == 314.33 and not c.total_changed and c.wsp_changed)
    c = plan_sku_rate_change(m, "499999", "", "500")
    _check("blank total on a RATE-TBC row stays TBC", c.new_total is None and not c.total_changed and c.unchanged is False)
    c = plan_sku_rate_change(m, "090425", "314.33")
    _check("nothing changed", c.unchanged)
    c = plan_sku_rate_change(m, "090425", "250")
    _check("lowering within the off-invoices is warned", c.warnings and "shrinks" in c.warnings[0])
    msg = _refuses("total below a pub's off-invoice refused", plan_sku_rate_change, m, "090425", "99")
    _check("…naming the pub", msg is not None and "BELL ROCK" in msg and "£100.00" in msg)
    _refuses("total above the WSP refused", plan_sku_rate_change, m, "090425", "700")
    _refuses("negative refused", plan_sku_rate_change, m, "090425", "-1")
    _refuses("words refused", plan_sku_rate_change, m, "090425", "three hundred")
    _refuses("unknown product refused", plan_sku_rate_change, m, "123456", "300")


def test_plan_add_sku():
    print("\n-- plan_add_sku")
    m = make_master()
    k = plan_add_sku(m, " 401175 ", "  Blackthorn  Dry 50L ", "50L", "£293.49", "650")
    _check("row shaped", k.sku_code == "401175" and k.product == "Blackthorn Dry 50L" and k.container == "50L"
           and k.correct_total_per_brl == 293.49 and k.contract_base_per_brl == 293.49 and k.wsp_per_brl == 650.0
           and k.hold_per_brl == 0.0 and not k.on_contract, str(k))
    k = plan_add_sku(m, "401176", "Something", "", "", "")
    _check("blank total = RATE TBC", k.correct_total_per_brl is None and k.wsp_per_brl is None)
    _refuses("existing code refused", plan_add_sku, m, "090425", "X")
    _refuses("existing alt code refused", plan_add_sku, m, "09000X", "X")
    _refuses("same name + container refused", plan_add_sku, m, "401177", "T.Lager 50L", "50L")
    _refuses("compound code refused", plan_add_sku, m, "400751/400557", "X")
    _refuses("no name refused", plan_add_sku, m, "401178", "  ")
    _refuses("total over WSP refused", plan_add_sku, m, "401178", "X", "", "700", "600")


def _seed(fake: FakeIO):
    fake.tables[aio.T["TennentsSkuMaster"]] = [
        {"id": "recS1", "fields": {"sku_code": "090425", "alt_code": "09000X", "product": "T.Lager 50L", "container": "50L",
                                   "brl_per_unit": 0.3055, "wsp_per_brl": 606.0, "contract_base_per_brl": 314.33,
                                   "hold_per_brl": 0.0, "correct_total_per_brl": 314.33, "version": "vTEST",
                                   "source_file": "wb.xlsx"}},
        {"id": "recS2", "fields": {"sku_code": "401211", "product": "T.Bavarian Pilsner 50L", "container": "50L",
                                   "brl_per_unit": 0.3055, "wsp_per_brl": 778.70, "contract_base_per_brl": 300.0,
                                   "hold_per_brl": 50.0, "correct_total_per_brl": 350.0, "version": "vTEST"}},
        {"id": "recS3", "fields": {"sku_code": "499999", "product": "Stout TBC", "container": "50L", "version": "vTEST"}},
    ]
    fake.tables[aio.T["TennentsSiteMaster"]] = [
        {"id": "recT1", "fields": {"account": "11110001", "site_name": "BELL ROCK", "operating_model": "Tenanted (TBC)",
                                   "discount_construct": "Standard split"}},
        {"id": "recT2", "fields": {"account": "11110002", "site_name": "MANAGED HOUSE", "operating_model": "MANAGED (confirmed)",
                                   "discount_construct": "ALL OFF-INVOICE"}},
    ]
    fake.tables[aio.T["TennentsSitePrices"]] = [
        {"id": "recP1", "fields": {"price_key": "11110001|090425", "account": "11110001", "site_name": "BELL ROCK",
                                   "sku_code": "090425", "off_invoice_per_brl": 100.0}},
    ]


def test_sku_writes_and_replace():
    print("\n-- set_tennents_sku_rate / add_tennents_sku, then a workbook re-upload")
    fake = FakeIO()
    _seed(fake)
    _install(fake)
    m = aio.load_tennents_master()
    n_reads = len(fake.sweeps)

    r = aio.set_tennents_sku_rate(m, plan_sku_rate_change(m, "09000X", "320", "610"), "james@x", "Tennents 7 Oct")
    row = fake.tables[aio.T["TennentsSkuMaster"]][0]["fields"]
    _check("patched by cached id, no read", r["action"] == "updated" and fake.patches[-1]["id"] == "recS1"
           and len(fake.sweeps) == n_reads)
    _check("total, base and WSP written", row["correct_total_per_brl"] == 320.0 and row["contract_base_per_brl"] == 320.0
           and row["wsp_per_brl"] == 610.0)
    _check("stamped price grid", row["source"].startswith(aio.GRID_SOURCE_PREFIX + "james@x"))
    _check("notes say what moved", "£314.33 → £320.00" in row["notes"] and "WSP £606.00 → £610.00" in row["notes"]
           and "Tennents 7 Oct" in row["notes"], row["notes"])
    m2 = aio.load_tennents_master()
    _check("cached master shows the new total", m2.find_sku("090425").correct_total_per_brl == 320.0
           and m2.find_sku("090425").wsp_per_brl == 610.0 and len(fake.sweeps) == n_reads)
    r = aio.set_tennents_sku_rate(m2, plan_sku_rate_change(m2, "090425", "320"), "james@x")
    _check("same figure again: nothing written", r["action"] == "already")
    r = aio.set_tennents_sku_rate(m2, plan_sku_rate_change(m2, "401211", "340"), "james@x")
    row = fake.tables[aio.T["TennentsSkuMaster"]][1]["fields"]
    _check("base nets the hold out (340 − 50)", row["contract_base_per_brl"] == 290.0 and row["correct_total_per_brl"] == 340.0)

    k = plan_add_sku(m2, "401175", "Blackthorn Dry 50L", "50L", "293.49", "650")
    r = aio.add_tennents_sku(m2, k, "james@x", "new line")
    made = fake.creates[-1]["fields"]
    _check("product created and stamped", r["action"] == "created" and made["sku_code"] == "401175"
           and made["source"].startswith(aio.GRID_SOURCE_PREFIX) and made["correct_total_per_brl"] == 293.49)
    m3 = aio.load_tennents_master()
    _check("cached master carries it", m3.find_sku("401175") is not None and m3.find_sku("401175").rec_id == fake.creates[-1]["id"])
    _refuses("duplicate code refused at the write", aio.add_tennents_sku, m3, k, "james@x")
    changes = aio.list_grid_changes(master=m3)
    _check("recent changes list the SKU edits", {c["sku_code"] for c in changes if c["kind"] == "sku"} == {"090425", "401211", "401175"})
    _check("renders with the new product", "401175" in tennents_grid.render_tennents_grid(m3, {}, True))

    # Workbook re-upload: an OLDER workbook total loses to the grid's; the
    # same total absorbs; a product the workbook lacks is re-created.
    wb = make_master()                      # 090425 at 314.33 / WSP 606, 401211 at 350, no 401175
    report: dict = {}
    aio.replace_tennents_master(wb, "wb.xlsx", report=report)
    rows = {f["fields"]["sku_code"]: f["fields"] for f in fake.tables[aio.T["TennentsSkuMaster"]]}
    _check("grid total wins over the older workbook", rows["090425"]["correct_total_per_brl"] == 320.0
           and rows["090425"]["wsp_per_brl"] == 610.0 and rows["090425"]["contract_base_per_brl"] == 320.0)
    _check("still stamped so the next re-upload keeps it too", str(rows["090425"].get("source", "")).startswith(aio.GRID_SOURCE_PREFIX))
    _check("401211 kept at 340 over the workbook's 350", rows["401211"]["correct_total_per_brl"] == 340.0)
    _check("product the workbook lacks re-created", "401175" in rows and rows["401175"]["correct_total_per_brl"] == 293.49)
    kept = report.get("sku_rates") or []
    _check("upload report names them", sorted(k["sku_code"] for k in kept) == ["090425", "401211"], str(kept))
    _check("no duplicate rows", len(fake.tables[aio.T["TennentsSkuMaster"]]) == 4)
    # Second pass with the workbook caught up on 090425: it absorbs (plain row).
    wb2 = make_master()
    for k2 in wb2.skus:
        if k2.sku_code == "090425":
            k2.correct_total_per_brl = 320.0
            k2.contract_base_per_brl = 320.0
            k2.wsp_per_brl = 610.0
    report = {}
    aio.replace_tennents_master(wb2, "wb2.xlsx", report=report)
    rows = {f["fields"]["sku_code"]: f["fields"] for f in fake.tables[aio.T["TennentsSkuMaster"]]}
    _check("workbook that caught up: plain workbook row", not str(rows["090425"].get("source", "")).startswith(aio.GRID_SOURCE_PREFIX)
           and rows["090425"]["correct_total_per_brl"] == 320.0)
    _check("401211 still kept", rows["401211"]["correct_total_per_brl"] == 340.0
           and [k["sku_code"] for k in report.get("sku_rates") or []] == ["401211"])


def test_render():
    print("\n-- render_tennents_grid")
    m = make_master(site_prices=[
        SitePrice("11110001", "BELL ROCK", "090425", "T.Lager 50L", 100.0, source_file="bar plan:james@x 2026-10-07"),
        SitePrice("11110003", "GARTOCHER", "090425", "T.Lager 50L", 0.0),
    ])
    html = tennents_grid.render_tennents_grid(m, {}, is_admin=True, banner_html="<b>BANNER</b>")
    _check("banner and title", "BANNER" in html and "Tennents price grid" in html)
    heads = html[html.index("<thead>"):html.index("</thead>")]
    _check("tenanted pubs as columns, managed left out", "BELL ROCK" in heads and "GARTOCHER" in heads and "MANAGED HOUSE" not in heads)
    _check("off-invoice cell and its retro / net keg", "£100.00" in html and "£214.33" in html
           and f"£{(606.0 - 100.0) * 0.3055:,.2f}/keg" in html)
    _check("page-set cell marked", 'cell-support' in html and "set on the page: james@x" in html)
    _check("£0 reads as not sold", html.count('pivot-empty">·') >= 1)
    _check("RATE TBC row", "RATE TBC" in html)
    _check("edit button for admin, no inputs in read mode", "Edit prices" in html and 'name="off_invoice"' not in html)
    _check("Gartocher's bespoke construct in its header", "flat £200/brl" in html)
    html_v = tennents_grid.render_tennents_grid(m, {}, is_admin=False)
    _check("viewer gets no edit button", "Edit prices" not in html_v and 'id="grid-save"' not in html_v)

    html_m = tennents_grid.render_tennents_grid(m, {"managed": "1"}, is_admin=True)
    _check("managed pub shown on request, all off", "MANAGED HOUSE" in html_m and "all off" in html_m)

    html_e = tennents_grid.render_tennents_grid(m, {"edit": "1", "site": "11110001", "q": "lager"}, is_admin=True)
    _check("edit mode: off-invoice input with prev value and the pub/product identity",
           'name="off_invoice" class="cell-input" value="100.00" data-prev="100.00"' in html_e
           and 'name="account" value="11110001"' in html_e and 'name="sku_code" value="090425"' in html_e)
    _check("edit mode: WSP and Total inputs in one form", 'name="wsp"' in html_e and 'name="total" form="pcell-0"' in html_e)
    heads_e = html_e[html_e.index("<thead>"):html_e.index("</thead>")]
    _check("edit mode: product filter kept, one pub", 'pcell-1' not in html_e and "GARTOCHER" not in heads_e and "BELL ROCK" in heads_e)
    _check("edit mode: buttons", "+ Add product" in html_e and "+ Add pub" in html_e and "grid-save" in html_e)
    _check("filters ride the cell forms", 'name="fsite" value="11110001"' in html_e and 'name="fq" value="lager"' in html_e)
    html_t = tennents_grid.render_tennents_grid(m, {"edit": "1", "q": "stout"}, is_admin=True)
    _check("RATE TBC row: cells disabled until the total is set", "disabled" in html_t and "set the product" in html_t)
    html_v2 = tennents_grid.render_tennents_grid(m, {"edit": "1"}, is_admin=False)
    _check("viewer's ?edit=1 ignored", 'name="off_invoice"' not in html_v2)


if __name__ == "__main__":
    test_plan_sku_rate_change()
    test_plan_add_sku()
    test_sku_writes_and_replace()
    test_render()
    print("\nALL PASS" if PASS else "\nFAILURES")
    sys.exit(0 if PASS else 1)
