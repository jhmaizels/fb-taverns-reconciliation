"""
Offline tests for the team-facing Tennents price-file export.

Covers: the off-invoice/retro split by construct (standard uses the stored
per-site off-invoice; managed = all off-invoice; bespoke = flat retro), the
agreed rate coming from SKU_Master (not exceptions), no-rate SKUs shown as
RATE TBC, keg/pint maths (incl. multi-container keg-factor), a WSP change
flowing through to the keg price while the off-invoice holds, exception notes,
the Site_Prices parser round-trip, and that all three workbook builders emit
openable files. No Airtable network access.

    python test_tennents_price_export.py
"""
import io
import sys
import tempfile
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from openpyxl import Workbook, load_workbook

import tennents_price_export as tpe
from tennents_master import (
    SiteInfo,
    SitePrice,
    SkuException,
    SkuRate,
    TennentsMaster,
    keg_brl_factor,
    parse_master_workbook,
)

PASS = True


def _check(label, cond, detail=""):
    global PASS
    PASS &= bool(cond)
    print(f"  [{'ok' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")


def _near(a, b, tol=0.02):
    return a is not None and b is not None and abs(a - b) <= tol


# ---------- fixture ----------

def make_master() -> TennentsMaster:
    skus = [
        # 11G/22G multi-container, master brl_per_unit blank -> parse 11G
        SkuRate("090425", "09000X", "Tennent's Lager", "T.Lager 11G/22G", "11G / 22G",
                None, 4.0, 641.62, 302.00, True, "C&C", 12.33, 314.33),
        # 50L single, brl_per_unit present
        SkuRate("400889", "", "Jeffrey's", "Jeffrey's 50L", "50L",
                0.3055, 3.4, 538.39, 291.52, True, "C&C", 11.45, 302.97),
        # 50L/30L multi-container -> parse 50L
        SkuRate("GUI002", "GUI003", "Guinness", "Guinness 50L/30L", "50L / 30L",
                None, 4.1, 779.24, 249.00, True, "3rd party", 0.0, 249.00),
        # no agreed rate
        SkuRate("401211", "", "Tennent's", "T.Bavarian 50L", "50L",
                0.3055, 4.7, None, None, False, "C&C", 0.0, None),
    ]
    sites = [
        SiteInfo("11110001", "STANDARD ARMS", "Tenanted (TBC)", "Standard split"),
        SiteInfo("11110002", "MANAGED HOUSE", "MANAGED (confirmed)", "ALL OFF-INVOICE, zero retro"),
        SiteInfo("11110003", "BESPOKE BAR", "Tenanted (confirmed)",
                 "BESPOKE: flat £200/brl retro on all SKUs"),
    ]
    exceptions = [
        # open exception at STANDARD ARMS for Jeffrey's -> should surface as a note,
        # but the priced rate stays the AGREED 302.97 (not the loaded 291.54).
        SkuException("STANDARD ARMS", "11110001", "400889", "Jeffrey's 50L", 291.54, 302.97,
                     "FB under", 3.49, "open"),
    ]
    site_prices = [
        # STANDARD ARMS: T.Lager tenant gets £105.02/brl off-invoice; Jeffrey's full WSP (0)
        SitePrice("11110001", "STANDARD ARMS", "090425", "T.Lager", 105.02),
        SitePrice("11110001", "STANDARD ARMS", "400889", "Jeffrey's", 0.0),
        # A site-price given against the ALT code should still resolve to canonical
        SitePrice("11110001", "STANDARD ARMS", "GUI003", "Guinness", 60.00),
    ]
    return TennentsMaster("vTEST", "fixture", skus, sites, exceptions, site_prices)


def line(m, site_name, code):
    return tpe._price_line(m, tpe.find_site(m, site_name), m.find_sku(code))


def run():
    m = make_master()
    bpu_lager = keg_brl_factor(m.find_sku("090425"))   # 11G ≈ 0.3056
    bpu_gui = keg_brl_factor(m.find_sku("GUI002"))      # 50L ≈ 0.3055

    # ---- keg factor ----
    _check("11G keg factor ≈ 0.3056", _near(bpu_lager, 0.3056, 0.0005), f"{bpu_lager}")
    _check("50L/30L keg factor parses 50L ≈ 0.3055", _near(bpu_gui, 0.3055, 0.0005), f"{bpu_gui}")
    _check("brl_per_unit present is used", _near(keg_brl_factor(m.find_sku("400889")), 0.3055, 0.0005))

    # ---- standard site: stored off-invoice drives the split ----
    l = line(m, "STANDARD ARMS", "090425")
    _check("standard: agreed total 314.33", _near(l["total"], 314.33))
    _check("standard: off-invoice 105.02 from layer", _near(l["off"], 105.02))
    _check("standard: retro = total - off", _near(l["retro"], 314.33 - 105.02))
    _check("standard: net/brl = WSP - total", _near(l["net_brl"], 641.62 - 314.33))
    _check("standard: net/keg = (WSP-off)*bpu", _near(l["net_keg"], (641.62 - 105.02) * bpu_lager))
    _check("standard: net/pint = (WSP-off)/288", _near(l["net_pint"], (641.62 - 105.02) / 288.0, 0.001))

    # ---- off-invoice resolves via ALT code ----
    lg = line(m, "STANDARD ARMS", "GUI002")
    _check("alt-code site price resolves (GUI003 -> GUI002)", _near(lg["off"], 60.00))

    # ---- SKU with no stored site price -> off 0 (full WSP) ----
    lj = line(m, "STANDARD ARMS", "400889")
    _check("no stored off -> 0 (full WSP)", _near(lj["off"], 0.0))
    _check("full-WSP keg = WSP*bpu", _near(lj["net_keg"], 538.39 * 0.3055, 0.05))
    _check("open exception surfaces as a note", "Correction pending" in lj["note"], lj["note"])
    _check("priced rate is AGREED not loaded", _near(lj["total"], 302.97))

    # ---- managed site: all off-invoice, zero retro ----
    lm = line(m, "MANAGED HOUSE", "GUI002")
    _check("managed: off = total", _near(lm["off"], 249.00))
    _check("managed: retro = 0", _near(lm["retro"], 0.0))
    _check("managed: net/keg = (WSP-total)*bpu", _near(lm["net_keg"], (779.24 - 249.00) * bpu_gui))
    _check("managed: note flags it", "Managed" in lm["note"], lm["note"])

    # ---- bespoke: master does NOT model the split (README §4) — use stored
    # off (0 here), tenant on full WSP; construct surfaced as a note only ----
    lb = line(m, "BESPOKE BAR", "GUI002")
    _check("bespoke: off = stored (0, no split invented)", _near(lb["off"], 0.0))
    _check("bespoke: retro = total", _near(lb["retro"], 249.00))
    _check("bespoke: construct note", "Bespoke" in lb["note"], lb["note"])

    # ---- no agreed rate -> RATE TBC ----
    lt = line(m, "STANDARD ARMS", "401211")
    _check("no-rate: total None", lt["total"] is None)
    _check("no-rate: net/keg None", lt["net_keg"] is None)
    _check("no-rate: RATE TBC note", "RATE TBC" in lt["note"], lt["note"])

    # ---- WSP change flows through; off-invoice holds ----
    m.find_sku("090425").wsp_per_brl = 671.62      # +30 PINC
    m.reindex()
    l2 = line(m, "STANDARD ARMS", "090425")
    _check("PINC: off-invoice unchanged", _near(l2["off"], 105.02))
    _check("PINC: net/keg rose by 30*bpu", _near(l2["net_keg"], (671.62 - 105.02) * bpu_lager))
    _check("PINC: net/brl uses new WSP - total", _near(l2["net_brl"], 671.62 - 314.33))

    # ---- workbook builders emit openable files ----
    mb = tpe.build_master_workbook_bytes(m)
    wb = load_workbook(io.BytesIO(mb))
    _check("master workbook: a tab per site", len(wb.sheetnames) == 3, str(wb.sheetnames))
    _check("master workbook: tab named for site", "STANDARD ARMS" in wb.sheetnames)

    # grouping: products separated into labelled sections, in canonical order,
    # and the per-row Category column is gone (SKU Code is now first).
    ws = wb["STANDARD ARMS"]
    _check("header drops Category col (SKU Code first)",
           ws.cell(row=5, column=1).value == "SKU Code", str(ws.cell(row=5, column=1).value))
    colA = [ws.cell(row=rr, column=1).value for rr in range(6, ws.max_row + 1)]
    bands = [v for v in colA if v in set(tpe._CATEGORY_ORDER) | {tpe._OTHER}]
    _check("sections grouped in canonical order",
           bands == ["Standard Lager", "Premium Lager", "Ales & Stout"], str(bands))

    # ---- live formulas: editing Tenant Off-Invoice (I) recalculates Net Keg (G),
    # Net Pint (H) and the FB retro (J) in-sheet; New Net/Brl (F) tracks WSP/Total;
    # the retro column is now labelled "FB Retro" (Nick Madigan, Jul-2026). ----
    _check("retro column renamed to FB Retro",
           ws.cell(row=5, column=10).value == "FB Retro £/Brl", str(ws.cell(row=5, column=10).value))

    def _row_for(code):
        for rr in range(6, ws.max_row + 1):
            if ws.cell(row=rr, column=1).value == code:
                return rr
        return None

    rlag = _row_for("090425")                      # a priced row (off-invoice 105.02)
    _check("priced row located", rlag is not None, str(rlag))
    if rlag:
        g = ws.cell(row=rlag, column=7).value      # Net £/Keg
        h = ws.cell(row=rlag, column=8).value      # Net £/Pint
        jr = ws.cell(row=rlag, column=10).value    # FB Retro
        fb = ws.cell(row=rlag, column=6).value     # New Net £/Brl
        off = ws.cell(row=rlag, column=9).value    # Tenant Off-Invoice (input)
        _check("G Net/Keg is a live formula of D & I",
               isinstance(g, str) and g.startswith(f"=(D{rlag}-I{rlag})*"), str(g))
        _check("H Net/Pint is a live formula of D & I",
               isinstance(h, str) and h.startswith(f"=(D{rlag}-I{rlag})/"), str(h))
        _check("J FB retro = total - off (E-I)", jr == f"=E{rlag}-I{rlag}", str(jr))
        _check("F Net/Brl = WSP - total (D-E)", fb == f"=D{rlag}-E{rlag}", str(fb))
        _check("I off-invoice stays a numeric input", isinstance(off, (int, float)), str(off))
        # substitution guidance block (cols 11-14): FB cost/keg + tenant £/keg per RPB
        cost = ws.cell(row=rlag, column=11).value
        glo = ws.cell(row=rlag, column=12).value
        ghi = ws.cell(row=rlag, column=14).value
        _check("FB net cost/keg = (D-E)*bpu formula",
               isinstance(cost, str) and cost.startswith(f"=(D{rlag}-E{rlag})*"), str(cost))
        _check("guide @£150 = (D-E+150)*bpu formula",
               isinstance(glo, str) and glo.startswith(f"=(D{rlag}-E{rlag}+150)*"), str(glo))
        _check("guide @£250 = (D-E+250)*bpu (shown as-is, even above WSP)",
               isinstance(ghi, str) and ghi.startswith(f"=(D{rlag}-E{rlag}+250)*"), str(ghi))

    ghdr = [ws.cell(row=5, column=c).value for c in range(11, 16)]
    _check("guidance headers present + Notes shifted to col 15",
           ghdr == ["FB Net Cost £/Keg", "Tenant £/Keg @£150 RPB",
                    "Tenant £/Keg @£200 RPB", "Tenant £/Keg @£250 RPB", "Notes"], str(ghdr))

    rtbc = _row_for("401211")                      # RATE TBC row -> no formula, stays blank
    _check("TBC row located", rtbc is not None, str(rtbc))
    if rtbc:
        _check("TBC row: Net/Keg blank (no formula)",
               ws.cell(row=rtbc, column=7).value in (None, ""), str(ws.cell(row=rtbc, column=7).value))
        _check("TBC row: FB retro blank (no formula)",
               ws.cell(row=rtbc, column=10).value in (None, ""), str(ws.cell(row=rtbc, column=10).value))
        _check("TBC row: guidance block blank",
               all(ws.cell(row=rtbc, column=c).value in (None, "") for c in range(11, 15)), "not blank")

    sb = tpe.build_single_site_bytes(m, tpe.find_site(m, "MANAGED HOUSE"))
    wb1 = load_workbook(io.BytesIO(sb))
    _check("single-site workbook: one tab", len(wb1.sheetnames) == 1)

    zb = tpe.build_all_sites_zip_bytes(m)
    zf = zipfile.ZipFile(io.BytesIO(zb))
    _check("zip: one file per site", len(zf.namelist()) == 3, str(zf.namelist()))
    _check("zip: site name in filename", any("STANDARD ARMS" in n for n in zf.namelist()))

    _check("site choices listed", len(tpe.list_site_choices(m)) == 3)
    _check("find_site by account", tpe.find_site(m, "11110002").site_name == "MANAGED HOUSE")
    _check("find_site by name", tpe.find_site(m, "bespoke bar").account == "11110003")

    # ---- Site_Prices parser round-trip ----
    _roundtrip_parse()

    print("\n" + ("ALL PASS" if PASS else "FAILURES ABOVE"))
    return 0 if PASS else 1


def _roundtrip_parse():
    """Write a minimal master workbook WITH a Site_Prices sheet and parse it."""
    wb = Workbook()
    ws = wb.active
    ws.title = "README"
    ws.append(["Section", "Content"])
    ws.append(["7. Version", "vRT"])

    ws = wb.create_sheet("SKU_Master")
    ws.append(["SKU Code", "Alt Code", "Brand", "Product", "Container", "Brl per Unit",
               "ABV %", "WSP £/brl", "Contract Base Discount £/brl", "On Contract Schedule?",
               "C&C / 3rd Party", "50% Hold £/brl", "CURRENT CORRECT Total Discount £/brl",
               "Source", "Status / Notes"])
    ws.append(["090425", "09000X", "T.Lager", "T.Lager", "11G", 0.3056, 4.0, 641.62,
               302.0, "Y", "C&C", 12.33, 314.33, "src", "ok"])

    ws = wb.create_sheet("Site_Master")
    ws.append(["Site", "Tennents Account", "Operating Model", "Discount Construct", "Notes"])
    ws.append(["STANDARD ARMS", 11110001, "Tenanted", "Standard split", None])

    ws = wb.create_sheet("Site_SKU_Exceptions")
    ws.append(["Site", "SKU", "Product", "Loaded Total Discount £/brl",
               "Correct Total Discount £/brl", "Direction / Who Bears", "£ Impact", "Status"])

    ws = wb.create_sheet("Site_Prices")
    ws.append(["Site", "Tennents Account", "SKU Code", "Product",
               "Off-Invoice Discount £/Brl", "Notes"])
    # account left blank on purpose -> resolved from Site_Master by name
    ws.append(["STANDARD ARMS", None, "090425", "T.Lager", 105.02, ""])

    path = tempfile.mktemp(suffix=".xlsx")
    wb.save(path)
    m = parse_master_workbook(path, "rt")
    _check("parser: Site_Prices row loaded", len(m.site_prices) == 1, str(len(m.site_prices)))
    _check("parser: site_prices_present True when sheet exists", m.site_prices_present is True)
    _check("parser: account resolved from name", m.site_prices[0].account == "11110001",
           m.site_prices[0].account)
    _check("parser: off_invoice resolves", _near(m.off_invoice("11110001", "090425"), 105.02))
    _check("parser: off_invoice via alt code", _near(m.off_invoice("11110001", "09000X"), 105.02))

    # a workbook WITHOUT Site_Prices still parses (backward compatible)
    del m
    wb2 = load_workbook(path)
    del wb2["Site_Prices"]
    path2 = tempfile.mktemp(suffix=".xlsx")
    wb2.save(path2)
    m2 = parse_master_workbook(path2, "rt2")
    _check("parser: absent Site_Prices is fine", m2.site_prices == [])
    _check("parser: site_prices_present False when absent (preserve signal)",
           m2.site_prices_present is False)
    _check("parser: off_invoice defaults 0 with no layer", m2.off_invoice("11110001", "090425") == 0.0)


if __name__ == "__main__":
    raise SystemExit(run())
