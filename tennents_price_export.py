"""
Generate the team-facing Tennents per-site price file from the master.

Nick Madigan's manual task (Jul-2026) was: for each site, copy the off-invoice
discount per brl into the "Pricer" tab, recompute the net keg price, build one
master workbook (a tab per site) and save an individual copy into each site's
folder. This module does all of that from the reconciliation master, so it
stays in sync: WSP + agreed total discount come from SKU_Master (a PINC flows
straight through) and the per-site off-invoice comes from the Site_Prices
layer (TennentsMaster.site_prices). See [[tennents-pricing-position]].

Three outputs, all built from a single TennentsMaster:
  build_master_workbook_bytes(master)   -> one .xlsx, a tab per site
  build_single_site_bytes(master, site) -> one .xlsx, that site only
  build_all_sites_zip_bytes(master)     -> a .zip of the single-site files

Per line the price shown is the AGREED (correct) rate from SKU_Master — NOT any
mis-loaded exception value — because the price file tells the tenant what they
SHOULD pay; open exceptions are surfaced in the Notes column instead.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from tennents_master import (
    PINTS_PER_BRL,
    SiteInfo,
    SkuRate,
    TennentsMaster,
    keg_brl_factor,
)

# Column layout — mirrors the team's familiar "Net & Invoice" price file. Products
# are GROUPED into the sections below with a labelled header row + blank spacer,
# the way Nick's file reads, so the per-row Category column is not needed.
HEADERS = [
    "SKU Code", "Brand", "ABV %", "WSP £/Brl",
    "FB Taverns Total Discount", "New Net Price £/Brl",
    "Net Price £/Keg", "Net Price £/Pint",
    "Tenant Off-Invoice £/Brl", "FB Retro £/Brl",
    "In Range",
    # Substitution guidance (L..O): quick-price a product this site doesn't stock.
    "FB Net Cost £/Keg", "Tenant £/Keg @£150 RPB",
    "Tenant £/Keg @£200 RPB", "Tenant £/Keg @£250 RPB",
    "Notes",
]
_MONEY_COLS = {4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15}   # 1-indexed columns to format as currency
_ABV_COL = 3
_INRANGE_COL = 11   # "✓" when the site holds a Site_Prices row for the SKU (its range)

# Section order, matching the team's price file. Uncategorised SKUs fall into a
# trailing "Other" section (never hidden).
_CATEGORY_ORDER = ["Standard Lager", "Premium Lager", "Craft", "Ales & Stout", "Cider"]
_OTHER = "Other"

# Authoritative SKU -> section, taken from the team's file (correcting its
# off-by-one label placement: Caledonia Best is an ale, Drygate Pilsner is craft,
# Magners Ice Cold / Outcider are ciders). Keyed by primary AND alt codes so a
# lookup works whichever the master stored. Blackthorn (401176/401175) is mapped
# ahead of being added to the master.
_SKU_CATEGORY = {
    # Standard Lager
    "090425": "Standard Lager", "400889": "Standard Lager", "T00045238": "Standard Lager",
    # Premium Lager
    "401136": "Premium Lager", "400217": "Premium Lager", "400751": "Premium Lager",
    "400557": "Premium Lager", "401211": "Premium Lager", "STE025": "Premium Lager",
    "T00048477": "Premium Lager", "SAN002": "Premium Lager", "EST002": "Premium Lager",
    "T00043388": "Premium Lager", "KIN003": "Premium Lager",
    # Craft
    "401080": "Craft", "401079": "Craft", "DRY025": "Craft", "DIS009": "Craft",
    "T00044490": "Craft", "INN008": "Craft", "400248": "Craft", "INN005": "Craft",
    "JUB002": "Craft", "JUB003": "Craft",
    # Ales & Stout
    "400076": "Ales & Stout", "400745": "Ales & Stout", "004723": "Ales & Stout",
    "401152": "Ales & Stout", "004790": "Ales & Stout", "GUI002": "Ales & Stout",
    "GUI003": "Ales & Stout", "MCE005": "Ales & Stout", "MCE015": "Ales & Stout",
    "MCE008": "Ales & Stout", "T00045771": "Ales & Stout",
    # Cider
    "401172": "Cider", "401219": "Cider", "401173": "Cider", "401224": "Cider",
    "401178": "Cider", "401223": "Cider", "401174": "Cider", "401222": "Cider",
    "401188": "Cider", "401218": "Cider", "401176": "Cider", "401175": "Cider",
}

# Best-effort fallback for a SKU not in the map above (a future addition), so it
# still lands in a sensible section rather than "Other" where possible.
_CATEGORY_KEYWORDS = [
    ("Cider",         ("cider", "magners", "blackthorn", "outcider", "gaymers", "orchard", "olde english")),
    ("Ales & Stout",  ("stout", "guinness", "mcewan", "80/-", "70/-", "60/-", "bitter", "smooth", " ale")),
    ("Craft",         ("ipa", "pale", "drygate", "gladeye", "innis", "i&g", "jubel", "craft")),
    ("Premium Lager", ("heverlee", "menabrea", "bavarian", "stella", "mahou", "san miguel", "estrella", "kingfisher")),
    ("Standard Lager", ("lager", "pilsner")),
]


def _category(sku: SkuRate) -> str:
    for code in (sku.sku_code, sku.alt_code):
        if not code:
            continue
        u = str(code).strip().upper()
        for cand in (u, u.zfill(6), u.lstrip("0")):
            if cand in _SKU_CATEGORY:
                return _SKU_CATEGORY[cand]
    hay = f"{sku.brand} {sku.product}".lower()
    for label, keys in _CATEGORY_KEYWORDS:
        if any(k in hay for k in keys):
            return label
    return _OTHER


def _sanitize_tab(name: str, used: set[str]) -> str:
    """Excel tab name: ≤31 chars, none of []:*?/\\, unique."""
    clean = re.sub(r"[\[\]:*?/\\]", " ", name).strip()[:31] or "Site"
    base = clean
    i = 2
    while clean.upper() in used:
        suffix = f" {i}"
        clean = base[:31 - len(suffix)] + suffix
        i += 1
    used.add(clean.upper())
    return clean


def _price_line(master: TennentsMaster, site: SiteInfo, sku: SkuRate) -> dict:
    """Compute the price row for (site, sku). off-invoice/retro split follows the
    site construct: managed = all off-invoice (zero retro); bespoke flat retro =
    fixed retro; standard = the stored per-site off-invoice. Rate is the AGREED
    correct total from SKU_Master."""
    total = sku.correct_total_per_brl
    wsp = sku.wsp_per_brl
    note_bits: list[str] = []
    # In-range marker: does this site hold a Site_Prices row for the SKU? Presence
    # of the row = the product is on the site's Net & Invoice Pricing (its range),
    # independent of the £0-off default. Blank when the Site_Prices layer is absent.
    in_range = "✓" if (master.site_prices and master.has_site_price(site.account, sku.sku_code)) else ""

    if total is None:
        note_bits.append("RATE TBC — no agreed Tennents rate")
        return {
            "category": _category(sku), "code": sku.sku_code, "brand": sku.product or sku.brand,
            "abv": sku.abv, "wsp": wsp, "total": None, "net_brl": None,
            "net_keg": None, "net_pint": None, "off": None, "retro": None, "bpu": None,
            "in_range": in_range,
            "note": "; ".join(note_bits),
        }

    total = float(total)
    # off-invoice / retro split. Managed sites take the whole discount
    # off-invoice, zero retro (README §4 — a documented rule). Every other site
    # (incl. bespoke, whose split the master deliberately does NOT model — "flat
    # £200/brl retro: validate the total, not the split") uses the stored
    # per-site off-invoice, defaulting to 0 (tenant pays full WSP, all retro).
    if site.is_managed:
        off = total
    else:
        off = master.off_invoice(site.account, sku.sku_code)
    off = min(max(off, 0.0), total)      # never negative retro / over-100% off
    retro = total - off

    bpu = keg_brl_factor(sku)
    invoice_net_brl = (wsp - off) if wsp is not None else None
    row = {
        "category": _category(sku), "code": sku.sku_code, "brand": sku.product or sku.brand,
        "abv": sku.abv, "wsp": wsp, "total": total,
        "net_brl": (wsp - total) if wsp is not None else None,
        "net_keg": (invoice_net_brl * bpu) if invoice_net_brl is not None else None,
        "net_pint": (invoice_net_brl / PINTS_PER_BRL) if invoice_net_brl is not None else None,
        "off": off, "retro": retro, "bpu": bpu,
        "in_range": in_range,
        "note": "",
    }

    ex = master._exception_index.get((site.account, sku.sku_code.strip().upper()))
    if ex is None and sku.alt_code:
        ex = master._exception_index.get((site.account, sku.alt_code.strip().upper()))
    if ex is not None and ex.loaded_total_per_brl is not None:
        note_bits.append(f"Correction pending — currently loaded £{ex.loaded_total_per_brl:,.2f}/brl")
    if site.is_managed:
        note_bits.append("Managed: full discount off-invoice")
    elif site.is_bespoke:
        note_bits.append("Bespoke construct — retro per agreement; total validated")
    row["note"] = "; ".join(note_bits)
    return row


def _write_site_sheet(ws, master: TennentsMaster, site: SiteInfo, as_of: date) -> None:
    bold = Font(bold=True)
    title_font = Font(bold=True, size=13, color="1F3B57")
    sub_font = Font(size=9, color="555555")
    header_fill = PatternFill("solid", fgColor="DDE6F2")
    tbc_fill = PatternFill("solid", fgColor="FBE9D6")
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    centre = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws["A1"] = "FB Taverns — Tennents Scotland Price File"
    ws["A1"].font = title_font
    ws["A2"] = f"Site: {site.site_name}    |    Tennents account: {site.account}    |    Prices effective: {as_of.isoformat()}"
    ws["A2"].font = sub_font
    ws["A3"] = f"Operating model: {site.operating_model or '—'}    |    Discount construct: {site.discount_construct or '—'}"
    ws["A3"].font = sub_font

    header_row = 5
    for c, label in enumerate(HEADERS, start=1):
        cell = ws.cell(row=header_row, column=c, value=label)
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = centre
        cell.border = border

    # Group price lines by section (Standard Lager, Premium Lager, …) and write
    # each under a labelled band row with a blank spacer between, so the file
    # reads like the team's — products separated by type.
    lines_by_cat: dict[str, list[dict]] = {}
    for sku in master.skus:
        line = _price_line(master, site, sku)
        lines_by_cat.setdefault(line["category"] or _OTHER, []).append(line)
    ordered = [c for c in _CATEGORY_ORDER if c in lines_by_cat]
    ordered += [c for c in lines_by_cat if c not in _CATEGORY_ORDER]   # trailing "Other"

    section_fill = PatternFill("solid", fgColor="1F3B57")
    section_font = Font(bold=True, size=11, color="FFFFFF")

    r = header_row + 1
    for si, cat in enumerate(ordered):
        if si:                                    # blank spacer row between sections
            r += 1
        band = ws.cell(row=r, column=1, value=cat)
        band.font = section_font
        band.fill = section_fill
        for c in range(2, len(HEADERS) + 1):      # colour the whole band row
            ws.cell(row=r, column=c).fill = section_fill
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
        r += 1
        for line in lines_by_cat[cat]:
            values = [
                line["code"], line["brand"], line["abv"], line["wsp"],
                line["total"], line["net_brl"], line["net_keg"], line["net_pint"],
                line["off"], line["retro"],
                line["in_range"],                # K In Range
                None, None, None, None,          # L..O substitution guidance (written as formulas)
                line["note"],
            ]
            is_tbc = line["total"] is None
            # Write the derived money columns as LIVE Excel formulas of the input
            # cells (WSP=D, Total Discount=E, Tenant Off-Invoice=I) so the team can
            # edit the off-invoice in-sheet and have Net Keg/Pint and the FB retro
            # recalculate — Nick Madigan's bar-plan / new-site workflow (Jul-2026).
            # The K..N "substitution guidance" block prices a product this site
            # doesn't stock: FB net cost/keg, then the tenant keg price at a
            # £150/£200/£250 retained margin (RPB). Where the RPB exceeds the
            # product's total discount (E) the implied tenant price runs above WSP;
            # shown as-is (kept simple — James, Jul-2026). TBC rows stay blank.
            formulas: dict[int, str] = {}
            if not is_tbc:
                bpu = line["bpu"]
                formulas[10] = f"=E{r}-I{r}"                          # J  FB Retro = total - off-invoice
                if line["net_keg"] is not None:                      # WSP present -> all WSP-derived cols
                    formulas[6] = f"=D{r}-E{r}"                       # F  Net/Brl  = WSP - total discount
                    formulas[7] = f"=(D{r}-I{r})*{bpu!r}"            # G  Net/Keg  = (WSP - off) * keg factor
                    formulas[8] = f"=(D{r}-I{r})/{PINTS_PER_BRL!r}"  # H  Net/Pint = (WSP - off) / pints per brl
                    formulas[12] = f"=(D{r}-E{r})*{bpu!r}"           # L  FB net cost £/keg
                    formulas[13] = f"=(D{r}-E{r}+150)*{bpu!r}"       # M  tenant £/keg @ £150 RPB
                    formulas[14] = f"=(D{r}-E{r}+200)*{bpu!r}"       # N  tenant £/keg @ £200 RPB
                    formulas[15] = f"=(D{r}-E{r}+250)*{bpu!r}"       # O  tenant £/keg @ £250 RPB
            for c, v in enumerate(values, start=1):
                cell = ws.cell(row=r, column=c, value=formulas.get(c, v))
                cell.border = border
                if c in _MONEY_COLS and (isinstance(v, (int, float)) or c in formulas):
                    cell.number_format = '"£"#,##0.00'
                if c == _ABV_COL and isinstance(v, (int, float)):
                    cell.number_format = '0.0"%"'
                if c == _INRANGE_COL:
                    cell.alignment = centre
                if is_tbc:
                    cell.fill = tbc_fill
            r += 1

    warn = ("" if master.site_prices else
            "WARNING: the per-site off-invoice layer (Site_Prices) is NOT loaded — every site is "
            "shown at FULL WSP with no off-invoice applied. Seed/upload Site_Prices before using "
            "these figures. ")
    note = ws.cell(
        row=r + 1, column=1,
        value=(warn + f"Generated {as_of.isoformat()} from the Tennents master "
               f"({master.version or 'version n/a'}). WSP & agreed total discount from SKU_Master; "
               f"per-site off-invoice split from Site_Prices. Net £/Keg is on the invoice basis "
               f"(WSP − off-invoice); New Net £/Brl is after the FB retro. Net £/Brl, Net £/Keg, "
               f"Net £/Pint and FB Retro are live formulas — edit WSP, Total Discount or Tenant "
               f"Off-Invoice in-sheet and they recalculate. In Range (✓) = the product is on this "
               f"site's Net & Invoice Pricing (its current range as at the last master upload); "
               f"unticked rows are catalogue products the site doesn't currently buy. Substitution "
               f"guidance (right-hand block): FB Net Cost £/Keg is what FB pays; the @£150/£200/£250 RPB columns give the "
               f"tenant keg price at that retained margin. Typical RPB £150-250; when switching a "
               f"product the replacement's FB Retro must beat the retired product's — every switch "
               f"accretive. RATE TBC = no agreed Tennents rate yet — not priced."))
    note.font = Font(size=9, bold=bool(warn), color="B00020" if warn else "555555")
    ws.merge_cells(start_row=r + 1, start_column=1, end_row=r + 1, end_column=len(HEADERS))

    widths = [12, 26, 7, 12, 16, 14, 13, 13, 15, 14, 9, 15, 16, 16, 16, 40]
    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = f"A{header_row + 1}"


def _sites_in_scope(master: TennentsMaster) -> list[SiteInfo]:
    """Estate sites with a real account, alphabetical."""
    seen: set[str] = set()
    out: list[SiteInfo] = []
    for s in master.sites:
        if not s.account or s.account.upper() == "TBC" or s.account in seen:
            continue
        seen.add(s.account)
        out.append(s)
    return sorted(out, key=lambda s: s.site_name.upper())


def list_site_choices(master: TennentsMaster) -> list[tuple[str, str]]:
    """(account, site_name) for the per-site download picker."""
    return [(s.account, s.site_name) for s in _sites_in_scope(master)]


def find_site(master: TennentsMaster, account_or_name: str) -> SiteInfo | None:
    key = str(account_or_name).strip().upper()
    for s in _sites_in_scope(master):
        if s.account.upper() == key or s.site_name.strip().upper() == key:
            return s
    return None


def build_master_workbook_bytes(master: TennentsMaster, as_of: date | None = None) -> bytes:
    """One workbook, a tab per site (the 'master which has everything')."""
    as_of = as_of or date.today()
    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    for site in _sites_in_scope(master):
        ws = wb.create_sheet(_sanitize_tab(site.site_name, used))
        _write_site_sheet(ws, master, site, as_of)
    if not wb.sheetnames:                          # never leave an empty workbook
        wb.create_sheet("No sites")
    return _to_bytes(wb)


def build_single_site_bytes(master: TennentsMaster, site: SiteInfo, as_of: date | None = None) -> bytes:
    """One workbook, one site — the individual copy for a site folder."""
    as_of = as_of or date.today()
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(_sanitize_tab(site.site_name, set()))
    _write_site_sheet(ws, master, site, as_of)
    return _to_bytes(wb)


def build_all_sites_zip_bytes(master: TennentsMaster, as_of: date | None = None) -> bytes:
    """A .zip of one single-site workbook per site, named for the site — ready
    to drop into each site's 'Price Lists and Bar Plans' folder."""
    as_of = as_of or date.today()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for site in _sites_in_scope(master):
            safe = re.sub(r'[\\/:*?"<>|]', " ", site.site_name).strip()
            fname = f"{safe} - Tennents Price File {as_of.isoformat()}.xlsx"
            zf.writestr(fname, build_single_site_bytes(master, site, as_of))
    return buf.getvalue()


def _to_bytes(wb: Workbook) -> bytes:
    # openpyxl writes formulas without a cached result, so tell Excel to
    # recalculate on open — otherwise the live Net/Retro cells read blank/0 until
    # the sheet is touched.
    wb.calculation.fullCalcOnLoad = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
