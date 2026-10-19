"""
Put back the July 2026 off-invoice deals the Site_Prices seed stored as £0.

seed_tennents_site_prices.py loaded each pub's tenant off-invoice from the
team's "FB Taverns - Master Price File - July 2026.xlsx" (Khristin Alhaddad,
29 Jul 2026), but passed a figure through ONLY when that sheet's own net keg
price reflected it; every other non-zero figure was stored as £0 and printed
as a review list ("confirm any the tenant should get + we add them"). On
these 45 lines the July sheet carried the figure in its Tenant Off-invoice
column while its net keg and Iona retro columns ignored it, so the seed
zeroed them, and nobody re-added them. The price file lists a product only
when its off-invoice is above £0, so they dropped off the pubs' files (Nick
Madigan: Corner House 14 Oct, Real McCoy and Hazel Arms 19 Oct 2026).
Tennents' September 2026 Draught Pricing Report shows the deals being
applied (Guinness at 23 pubs).

Pure planning here; the write is airtable_io.set_tennents_site_price, the
same one the bar plan form and the price grid use, one row at a time.
A row is restored only while the master still holds £0 (or no row) for it:
a figure set since July (bar plan page, price grid, workbook) is a later
decision and is left alone.
"""
from __future__ import annotations

from dataclasses import dataclass

from tennents_master import TennentsMaster, plan_bar_plan_change

# (July sheet = site name on the master, July SKU code as written, product, off-invoice £/brl)
JULY_ZEROED_DEALS: list[tuple[str, str, str, float]] = [
    ("1314 INN", "GUI002/GUI003", "Guinness Keg", 72.01),
    ("BELLS BAR", "4723", "Tennents Ember 80/-", 83.78),
    ("BELLS BAR", "GUI002/GUI003", "Guinness Keg", 134.20),
    ("FOXS BAR", "GUI002/GUI003", "Guinness Keg", 121.17),
    ("FOXS BAR", "MCE015", "McEwan's 80/-", 130.45),
    ("GRETNA INN", "STE025", "Stella Artois", 114.73),
    ("GRETNA INN", "GUI002/GUI003", "Guinness Keg", 94.30),
    ("HAZELWOOD BAR GLASGOW", "401152", "Tennents Special 70/-", 3.11),
    ("HAZELWOOD BAR GLASGOW", "GUI002/GUI003", "Guinness Keg", 88.11),
    ("KIMBERLEY TAVERN", "400557/400751", "Menabrea", 5.13),
    ("KIMBERLEY TAVERN", "GUI002/GUI003", "Guinness Keg", 51.19),
    ("OLD HARMONY BAR GLASGOW", "GUI002/GUI003", "Guinness Keg", 81.90),
    ("THE AMETHYST", "400557/400751", "Menabrea", 211.61),
    ("THE AMETHYST", "GUI002/GUI003", "Guinness Keg", 167.78),
    ("THE BELL ROCK", "400557/400751", "Menabrea", 166.18),
    ("THE BELL ROCK", "GUI002/GUI003", "Guinness Keg", 127.23),
    ("THE HAZEL ARMS", "STE025", "Stella Artois", 168.01),
    ("THE HAZEL ARMS", "GUI002/GUI003", "Guinness Keg", 88.11),
    ("THE RAMBLER", "400557/400751", "Menabrea", 101.50),
    ("THE RAMBLER", "GUI002/GUI003", "Guinness Keg", 88.18),
    ("WEAVERS", "INN008", "Innis and Gunn Lager (50L)", 42.72),
    ("WEAVERS", "GUI002/GUI003", "Guinness Keg", 21.18),
    ("WOODHILL BAR", "GUI002/GUI003", "Guinness Keg", 138.19),
    ("THE OLD GOVAN ARMS", "400557/400751", "Menabrea", 167.06),
    ("THE OLD GOVAN ARMS", "GUI002/GUI003", "Guinness Keg", 135.31),
    ("THE REAL MCCOY", "GUI002/GUI003", "Guinness Keg", 84.12),
    ("THE BRIDGE INN", "GUI002/GUI003", "Guinness Keg", 95.14),
    ("THE MOUNTBLOW BAR", "400557/400751", "Menabrea", 142.01),
    ("THE MOUNTBLOW BAR", "401152", "Tennents Special 70/-", 120.03),
    ("THE MOUNTBLOW BAR", "GUI002/GUI003", "Guinness Keg", 83.20),
    ("LA RONDE COATBRIDGE", "SAN002", "San Miguel", 29.97),
    ("LA RONDE COATBRIDGE", "GUI002/GUI003", "Guinness Keg", 187.03),
    ("THE LOCKHOUSE", "400557/400751", "Menabrea", 160.00),
    ("THE LOCKHOUSE", "GUI002/GUI003", "Guinness Keg", 65.03),
    ("SPARKY'S BAR & LOUNGE", "400557/400751", "Menabrea", 5.13),
    ("SPARKY'S BAR & LOUNGE", "SAN002", "San Miguel", 29.97),
    ("SPARKY'S BAR & LOUNGE", "401152", "Tennents Special 70/-", 142.78),
    ("STRATHSPEY", "GUI002/GUI003", "Guinness Keg", 107.18),
    ("RAMS HEAD AIRDRIE", "STE025", "Stella Artois", 560.07),
    ("RAMS HEAD AIRDRIE", "GUI002/GUI003", "Guinness Keg", 249.02),
    ("THE CORNER HOUSE MOTHERWELL", "SAN002", "San Miguel", 29.97),
    ("THE CORNER HOUSE MOTHERWELL", "401152", "Tennents Special 70/-", 142.78),
    ("THE CORNER HOUSE MOTHERWELL", "GUI002/GUI003", "Guinness Keg", 124.15),
    ("GARTOCHER BAR", "STE025", "Stella Artois", 360.05),
    ("RAM'S HEAD MARYHILL", "GUI002/GUI003", "Guinness Keg", 64.19),
]

RESTORE_NOTE = ("Restored from the July 2026 price file: the Site_Prices seed had stored £0 "
                "because the July sheet's net keg did not reflect it")


@dataclass
class RestoreLine:
    site_name: str           # as on the July sheet
    product: str
    july_code: str
    july_off: float
    status: str              # restore | already | changed | refused
    reason: str = ""
    account: str = ""
    sku_code: str = ""       # canonical, when resolved
    current_off: float | None = None
    change: object = None    # BarPlanChange when status == "restore"


def _resolve_sku(master: TennentsMaster, code: str):
    """The seed's lookup: each part of a compound code, with leading-zero drift."""
    for part in str(code).replace("\\", "/").split("/"):
        part = part.strip()
        if not part:
            continue
        for cand in (part, part.zfill(6), part.lstrip("0")):
            sku = master.find_sku(cand)
            if sku:
                return sku
    return None


def plan_july_restore(master: TennentsMaster) -> list[RestoreLine]:
    """Judge every zeroed July line against today's master."""
    by_name = {s.site_name.strip().upper(): s for s in master.sites}
    out: list[RestoreLine] = []
    for site_name, code, product, off in JULY_ZEROED_DEALS:
        line = RestoreLine(site_name=site_name, product=product, july_code=code, july_off=off,
                           status="refused")
        site = by_name.get(site_name.strip().upper())
        if site is None or not site.account or site.account.upper() == "TBC":
            line.reason = "No pub of this name with a Tennents account on the master"
            out.append(line)
            continue
        line.account = site.account
        sku = _resolve_sku(master, code)
        if sku is None:
            line.reason = f"Product {code} is not on SKU_Master"
            out.append(line)
            continue
        line.sku_code = sku.sku_code
        line.product = sku.product or sku.brand or product
        existing = master.site_price(site.account, sku.sku_code)
        cur = None if existing is None else round(float(existing.off_invoice_per_brl or 0.0), 2)
        line.current_off = cur
        if cur is not None and abs(cur - off) < 0.005:
            line.status = "already"
            out.append(line)
            continue
        if cur is not None and cur > 0.005:
            line.status = "changed"
            line.reason = "Set since July (bar plan, price grid or workbook) — left as it is"
            out.append(line)
            continue
        try:
            line.change = plan_bar_plan_change(master, site.account, sku.sku_code, f"{off:.2f}")
        except ValueError as e:
            line.reason = str(e)
            out.append(line)
            continue
        line.status = "restore"
        out.append(line)
    return out
