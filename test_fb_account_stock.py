"""Stock FB bought for a pub on its OWN LWC account (16 Nov 2026).

LWC books stock FB pays for (opening stock after a refurb, a support keg) on
account FF1031, FB TAVERNS FH LIMITED, with SITE ID 0. The pub is in the
DEL NAME column (LWC, Robin Price, 16 Nov 2026). Until then the reconciler
read only SITE ID, filed every such line as `line_without_site`, and the
draft email asked LWC which pub each invoice went to, though the file said.

Pins down, on rows copied from the 09.11.26 - 15.11.26 sales report:
 1. DEL POINT / DEL NAME are parsed off the real header row;
 2. FB-account lines are NOT `line_without_site` and are not in the email;
 3. they are listed as "Stock FB paid for", by pub, matched to our site;
 4. one billed above cost is flagged `fb_account_not_at_cost`;
 5. a siteless line on a TENANT's account is still `line_without_site`.

    python test_fb_account_stock.py
"""

import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from reconcile import Rule, is_fb_account_stock, parse_lwc_sales, reconcile_lines
from summary import build_summary, match_del_name, render_summary_html

# The header row of LWC's FB_Taverns_Del_Date sheet, as sent 16 Nov 2026.
HEADERS = [
    "DEPOT", "ACCOUNT\nNO", "SITE ID", "ACCOUNT", "ACCOUNT\nNAME", "ADDRESS\n1",
    "POST\nCODE", "DEL POINT", "DEL NAME", "PRODUCT\nCODE", "PRODUCT\nDESC",
    "INVOICE\nNO", "DATE", "QTY", "B'BRLS", "SALES", "UNIT", "MASTER",
    "DIFF. MASTER", "DIFF. + VAT",
]


def _row(depot, acct, site, outlet, holder, del_point, del_name, code, desc, inv,
         day, qty, unit, master):
    return [depot, acct, site, outlet, holder, "", "NA1 1NA", del_point, del_name,
            code, desc, inv, datetime(2026, 11, day), qty, 0.3, unit * qty, unit,
            master, (unit - master) * qty, (unit - master) * qty * 1.2]


ROWS = [
    # FB's own account: The Bell's opening stock and the Crook Hotel kegs.
    _row(17, "FF1031", 0, 0, "FB TAVERNS FH LIMITED", "0002", "THE SWISS BELL",
         12510001, "FOSTERS 11G KEG", 3453871, 10, 2, 93.15, 93.15),
    _row(17, "FF1031", 0, 0, "FB TAVERNS FH LIMITED", "0002", "THE SWISS BELL",
         19210004, "CARLSBERG 11G KEG", 3453871, 10, 2, 92.42, 92.42),
    _row(6, "FF1031", 0, 0, "FB TAVERNS FH LIMITED", "0009", "CROOK HOTEL",
         15550114, "COORS 22G KEG", 3462537, 13, 1, 247.69, 247.69),
    # Same account, billed ABOVE cost (made up: should never happen).
    _row(6, "FF1031", 0, 0, "FB TAVERNS FH LIMITED", "0009", "CROOK HOTEL",
         16680157, "BIRRA MORETTI 50L KEG", 3462537, 13, 1, 150.00, 137.52),
    # The Crook's tenant buying on their own account, priced as normal.
    _row(6, "FS4183", 821, "STEPHEN WILSON PUBS LTD T/A CROOK HOTEL",
         "STEPHEN WILSON PUBS LTD - 821", "0000", "STEPHEN WILSON PUBS LTD - 821",
         15530004, "CARLING 11G", 3460173, 13, 1, 175.00, 114.60),
    # A tenant-account line LWC booked without a site: still asked of LWC.
    _row(6, "FX9999", 0, 0, "SOMEONE LTD", "0001", "SOMEWHERE ARMS",
         15530004, "CARLING 11G", 3470000, 14, 1, 175.00, 114.60),
]

SITES = {
    "821": {"name": "STEPHEN WILSON PUBS LTD T/A CROOK HOTEL", "status": "tenanted"},
    "804": {"name": "THE BELL", "status": "tenanted"},
    "817": {"name": "BELLAMY'S CONSETT", "status": "tenanted"},
}
RULES = [
    Rule(site_id="821", product_code="15530004", product_desc="CARLING 11G",
         tenant_price=175.00, fb_price=114.60, retro_pct=0.0, valid_from=None,
         valid_to=None, source="test"),
]


def _lines():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wk.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "FB_Taverns_Del_Date"
        ws.append(HEADERS)
        for r in ROWS:
            ws.append(r)
        wb.save(p)
        return parse_lwc_sales(str(p))


def test_del_columns_parsed():
    lines = _lines()
    assert [l.del_name for l in lines[:3]] == ["THE SWISS BELL", "THE SWISS BELL", "CROOK HOTEL"]
    assert lines[0].del_point == "0002" or lines[0].del_point == "2", lines[0].del_point
    assert lines[0].account_no == "FF1031" and lines[0].site_id == "0"
    assert [is_fb_account_stock(l) for l in lines] == [True, True, True, True, False, False]


def test_fb_stock_is_not_asked_of_lwc():
    lines = _lines()
    ms = reconcile_lines(lines, RULES, SITES)
    types = sorted(m.type for m in ms)
    assert types == ["fb_account_not_at_cost", "line_without_site"], types
    over = next(m for m in ms if m.type == "fb_account_not_at_cost")
    assert over.line.del_name == "CROOK HOTEL"
    assert round(over.delta_total, 2) == 12.48 and over.severity == "high"
    stray = next(m for m in ms if m.type == "line_without_site")
    assert stray.line.account_no == "FX9999"

    s = build_summary("wk.xlsx", lines, ms, SITES, {"821", "804"})
    # Only the tenant-account stray is a siteless line; it shows its DEL NAME.
    assert [r.account_no for r in s.lines_without_site] == ["FX9999"]
    assert s.lines_without_site[0].site_name == "SOMEWHERE ARMS"

    # Four FB-account lines, matched to our sites by DEL NAME.
    assert len(s.fb_paid_stock) == 4
    by = {(r.del_name, r.product_code): r for r in s.fb_paid_stock}
    assert by[("THE SWISS BELL", "12510001")].site_id == "804"
    assert by[("CROOK HOTEL", "15550114")].site_id == "821"
    assert round(sum(r.total for r in s.fb_paid_stock), 2) == round(
        2 * 93.15 + 2 * 92.42 + 247.69 + 150.00, 2)

    html = render_summary_html(s)
    assert "Stock FB paid for" in html
    assert "THE SWISS BELL total" in html and "CROOK HOTEL total" in html
    cfg = json.loads(re.search(
        r'<script id="findings-config" type="application/json">(.*?)</script>', html, re.S
    ).group(1))
    invoices = [r["invoice"] for r in cfg["email"]["siteless_lines"]]
    assert invoices == ["3470000"], invoices  # FB's own invoices are not asked about


def test_match_del_name_needs_one_clear_match():
    names = {"821": "STEPHEN WILSON PUBS LTD T/A CROOK HOTEL", "804": "THE BELL",
             "817": "BELLAMY'S CONSETT", "812": "THE VICTORIA INN",
             "835": "THE RAILWAY SPENNYMOOR", "834": "T/A THE RAILWAY CROSSINGS"}
    assert match_del_name("CROOK HOTEL", names) == "821"
    assert match_del_name("THE SWISS BELL", names) == "804"
    assert match_del_name("THE RAILWAY", names) == ""     # two Railways: no guess
    assert match_del_name("THE GARDENERS", names) == ""   # nothing like it
    assert match_del_name("", names) == ""


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if failed else 0)
