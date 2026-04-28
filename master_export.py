"""
Generate a wide-form FB cost-file Excel from the current Airtable state.

Output matches the layout of FB_Taverns_Cost_Price_File_Apr_26_v*.xlsx so the
file remains familiar to anyone who reads it. The export is read-only:
edits should happen in Airtable, then a fresh export can be regenerated.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from airtable_io import (  # noqa: E402
    _list_all,
    T,
    load_rules_from_airtable,
    load_sites_from_airtable,
)


def _gather_state(as_of: date | None = None):
    rules = load_rules_from_airtable()
    sites = load_sites_from_airtable()
    products = _list_all(
        T["Products"],
        fields=["product_code", "description", "supplier", "retro_per_keg", "retro_eligible"],
    )
    products_by_code: dict[str, dict] = {}
    for rec in products:
        f = rec["fields"]
        code = f.get("product_code")
        if not code:
            continue
        products_by_code[code] = {
            "name": f.get("description", "") or "",
            "retro_per_keg": float(f.get("retro_per_keg") or 0.0),
            "supplier": f.get("supplier", "") or "",
        }

    if as_of is None:
        active_rules = [r for r in rules if r.valid_to is None]
    else:
        active_rules = [
            r for r in rules
            if (r.valid_from or date.min) <= as_of
            and (r.valid_to is None or r.valid_to > as_of)
        ]

    return active_rules, sites, products_by_code


def build_master_xlsx_bytes(as_of: date | None = None) -> bytes:
    active_rules, sites, products_by_code = _gather_state(as_of)

    # site_ids: any site that currently has an active rule, sorted ascending
    site_ids = sorted({r.site_id for r in active_rules})
    site_name = lambda sid: (sites.get(sid) or {}).get("name", "") or sid

    # product_codes: anything with an active rule OR a retro on the master
    codes_active = {r.product_code for r in active_rules}
    codes_retro = {c for c, p in products_by_code.items() if p["retro_per_keg"] > 0}

    # fb_price per product: take from any active rule for that product (they're equal)
    product_fb: dict[str, float] = {}
    product_name: dict[str, str] = {}
    for r in active_rules:
        if r.product_code not in product_fb and r.fb_price:
            product_fb[r.product_code] = float(r.fb_price)
        if r.product_code not in product_name and r.product_desc:
            product_name[r.product_code] = r.product_desc
    for code, p in products_by_code.items():
        product_name.setdefault(code, p["name"])

    # Sort products alphabetically by name (case-insensitive); code as tiebreaker
    product_codes = sorted(
        codes_active | codes_retro,
        key=lambda c: (product_name.get(c, "").upper(), c),
    )

    # tenant prices: (site, product) -> price
    tenant: dict[tuple[str, str], float] = {}
    for r in active_rules:
        if r.tenant_price is not None:
            tenant[(r.site_id, r.product_code)] = float(r.tenant_price)

    # Build workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "FB Cost Price File"

    bold = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDE6F2")
    border = Border(*([Side(style="thin", color="CCCCCC")] * 4))
    centre = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Row 1: blank (in the cost file this holds account codes; we don't track those)
    # Row 2: headers
    headers = ["Product Code", "Product Name", "Price", "Retro P/Keg", "Net price"]
    for sid in site_ids:
        headers.append(f"{site_name(sid)} {sid}")
    ws.append([None] * len(headers))   # row 1
    ws.append(headers)                  # row 2
    for col_idx, _ in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=col_idx)
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = centre
        cell.border = border

    # Data rows
    for code in product_codes:
        fb = product_fb.get(code)
        retro = products_by_code.get(code, {}).get("retro_per_keg", 0.0) or 0.0
        net = fb - retro if fb is not None else None
        row = [code, product_name.get(code, ""), fb, retro if retro else None, net]
        for sid in site_ids:
            row.append(tenant.get((sid, code)))
        ws.append(row)

    # Number format: currency for cols 3..end; product code col stays as text
    for r in range(3, ws.max_row + 1):
        for c in range(3, ws.max_column + 1):
            ws.cell(row=r, column=c).number_format = '"£"#,##0.00'

    # Column widths
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 38
    for c in range(3, ws.max_column + 1):
        col_letter = get_column_letter(c)
        ws.column_dimensions[col_letter].width = 14

    # Freeze header + first two cols
    ws.freeze_panes = "C3"

    # Generated-on note in a second sheet so the wide format isn't disturbed
    info = wb.create_sheet("Info")
    info.append(["Generated", date.today().isoformat()])
    info.append(["As of", as_of.isoformat() if as_of else "today (active rules)"])
    info.append(["Active rules", len(active_rules)])
    info.append(["Sites covered", len(site_ids)])
    info.append(["Products listed", len(product_codes)])
    info.append([
        "Source of truth",
        "Airtable PricingRules + Products.retro_per_keg. Edit there, then re-export.",
    ])
    for c in (1, 2):
        info.column_dimensions[get_column_letter(c)].width = 30

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
