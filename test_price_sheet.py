"""
Offline tests for price_sheet: reading a pub's price sheet whatever its
layout, finding the pub, and planning the changes that make the pub's prices
exactly the sheet's.

Run standalone:  python test_price_sheet.py
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from datetime import date

os.environ.setdefault("AIRTABLE_TOKEN", "test-token")
os.environ.setdefault("AIRTABLE_BASE_ID", "appTEST")

import price_sheet as ps  # noqa: E402
from reconcile import Rule  # noqa: E402

# Text as the Earl Grey side letter (20 Oct 2026) reads, flattened to one line —
# the hardest case: nothing but word order to go on.
EARL_GREY = (
    "FB TAVERNS  Pubs with Purpose  Earl Grey | Pricing side letter  20 October 2026  Private and confidential  "
    "For the attention of Noel Reid  Reid Hospitality Group Limited (company no. 17474986)  Ye Olde Earl Grey Inn, "
    "Kyo Heugh Road  West Kyo, Annfield Plain, Stanley, DH9 8TA  Dear Noel,  As agreed, your opening buying prices "
    "will match those applicable to your Queen’s Head account (site 833) for the same products and pack sizes.  "
    "Schedule 1 | Buying prices  Product code   Product   Pack   Buying price  12240204   Belhaven Best   11G   £125.13  "
    "16680008   Birra Moretti   22G   £502.46  15621274   Black Sheep Smooth  (FB Bitter)  50L   £180.36  "
    "19020003   Guinness Stout   30L   £131.67  19100003   Pilsner (FB Lager)   11G   £135.00  "
    "VAT treatment: as set out in the side letter."
)
KNOWN = {"12240204", "16680008", "15621274", "19020003", "19100003"}
SITES = {"819": {"name": "Earl Grey"}, "833": {"name": "Queens Head"}, "817": {"name": "Bellamys"}}


def test_reads_the_earl_grey_letter():
    lines = ps.lines_from_text(EARL_GREY, KNOWN)
    assert [(l.code, l.price) for l in lines] == [
        ("12240204", 125.13), ("16680008", 502.46), ("15621274", 180.36),
        ("19020003", 131.67), ("19100003", 135.0)], lines
    assert "Belhaven Best" in lines[0].desc and "FB Bitter" in lines[2].desc
    # the company number is 8 digits but has no price before the next code
    assert "17474986" not in {l.code for l in lines}


def test_a_row_split_over_two_lines_still_pairs():
    text = "15621274 Black Sheep Smooth\n(FB Bitter) 50L\n£180.36\n19100003 Pilsner 11G £135.00"
    assert [(l.code, l.price) for l in ps.lines_from_text(text, KNOWN)] == [("15621274", 180.36), ("19100003", 135.0)]


def test_finds_the_pub_and_the_pub_it_points_at():
    assert ps.guess_site(EARL_GREY, SITES)[0] == "819", "the heading names the pub; the Queen's Head comes later"
    assert ps.referenced_sites(EARL_GREY, SITES, exclude="819") == ["833"]


def test_excel_with_a_price_column():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Pricing for Bellamys"])
    ws.append(["Code", "Product", "Pack", "Price (£)", "Notes"])
    ws.append([12240204, "Belhaven Best", 11, 125.13, "x"])
    ws.append(["19100003", "FB Lager", 11, "£135.00", None])
    ws.append(["", "Total", "", 260.13, ""])
    buf = io.BytesIO()
    wb.save(buf)
    read = ps.read_sheet("prices.xlsx", buf.getvalue(), KNOWN)
    assert [(l.code, l.price) for l in read.lines] == [("12240204", 125.13), ("19100003", 135.0)]
    assert ps.guess_site(read.text, SITES)[0] == "817"


def test_csv_without_a_price_header_takes_the_last_number():
    raw = b"12240204,Belhaven Best 11G,11,125.13\n19020003,Guinness 30L,30,131.67\n"
    read = ps.read_sheet("p.csv", raw, KNOWN)
    assert [(l.code, l.price) for l in read.lines] == [("12240204", 125.13), ("19020003", 131.67)]


def test_word_table():
    doc = ('<w:document><w:body><w:p><w:r><w:t>Earl Grey prices</w:t></w:r></w:p><w:tbl><w:tr>'
           '<w:tc><w:p><w:r><w:t>12240204</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Belhaven Best</w:t></w:r></w:p></w:tc>'
           '<w:tc><w:p><w:r><w:t>£125.13</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
    read = ps.read_sheet("letter.docx", buf.getvalue(), KNOWN)
    assert [(l.code, l.price) for l in read.lines] == [("12240204", 125.13)]


def test_unreadable_formats_say_why():
    for name in ("old.xls", "pic.png"):
        try:
            ps.read_sheet(name, b"xx", KNOWN)
        except ValueError as exc:
            assert "xlsx" in str(exc) or "PDF" in str(exc)
        else:
            raise AssertionError(name)


def _w(code, price, status="tenanted", vt=None):
    return Rule(site_id="819", product_code=code, tenant_price=price, fb_price=100.0,
                valid_from=date(2026, 1, 1), valid_to=vt, status=status)


PRODUCTS = {"12240204": "Belhaven Best 11G", "16680008": "Birra Moretti 22G", "15621274": "FB Bitter 50L",
            "19020003": "Guinness 30L", "19100003": "FB Lager 11G", "11111111": "Old Ale 11G",
            "22222222": "Support Cider"}


def test_plan_replaces_the_pubs_prices():
    lines = ps.lines_from_text(EARL_GREY, KNOWN) + [ps.SheetLine("33333333", 99.0, "Mystery")]
    winners = {"12240204": _w("12240204", 120.0), "19020003": _w("19020003", 131.67),
               "11111111": _w("11111111", 150.0),
               "22222222": _w("22222222", 90.0, status="supported", vt=date(2026, 12, 1))}
    plan = ps.plan_sheet(site_id="819", site=SITES["819"], lines=lines, winners=winners,
                         products=PRODUCTS, compare={"12240204": 125.13, "16680008": 500.0})
    act = {r.code: r.action for r in plan.rows}
    assert act == {"12240204": "change", "16680008": "add", "15621274": "add", "19020003": "same",
                   "19100003": "add", "11111111": "remove", "22222222": "refused", "33333333": "refused"}, act
    assert {r.code for r in plan.to_write} == {"12240204", "16680008", "15621274", "19100003", "11111111"}
    removed = next(r for r in plan.rows if r.code == "11111111")
    assert removed.new is None and removed.current == 150.0
    assert next(r for r in plan.rows if r.code == "16680008").compare == 500.0


def test_plan_refuses_two_prices_and_flags_a_name_mismatch():
    lines = [ps.SheetLine("12240204", 125.13, "Belhaven Best"), ps.SheetLine("12240204", 126.0, "Belhaven"),
             ps.SheetLine("19020003", 131.67, "Carling")]
    plan = ps.plan_sheet(site_id="819", site=SITES["819"], lines=lines, winners={}, products=PRODUCTS)
    by = {r.code: r for r in plan.rows}
    assert by["12240204"].action == "refused" and "two prices" in by["12240204"].note
    assert by["19020003"].action == "add" and "check the code" in by["19020003"].note


def test_managed_pub_is_blocked():
    plan = ps.plan_sheet(site_id="819", site={"name": "X", "status": "managed"},
                         lines=[ps.SheetLine("12240204", 1.0)], winners={}, products=PRODUCTS)
    assert plan.blocked and plan.to_write == []


def test_lines_survive_the_form():
    lines = ps.lines_from_text(EARL_GREY, KNOWN)
    back = ps.decode_lines(ps.encode_lines(lines))
    assert [(l.code, l.price, l.desc) for l in back] == [(l.code, l.price, l.desc) for l in lines]


def test_preview_renders():
    lines = ps.lines_from_text(EARL_GREY, KNOWN)
    plan = ps.plan_sheet(site_id="819", site=SITES["819"], lines=lines,
                         winners={"11111111": _w("11111111", 150.0)}, products=PRODUCTS)
    html = ps.render_preview_page(plan=plan, lines=lines, sites=SITES, filename="letter.pdf",
                                  how="the sheet names Earl Grey", compare_site="833")
    assert "Sign off and make 6 changes" in html and "Queens Head now" in html
    none = ps.render_preview_page(plan=None, lines=lines, sites=SITES, filename="letter.pdf")
    assert "Which pub" in none and "Sign off" not in none


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
