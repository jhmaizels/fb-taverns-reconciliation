"""
Fill in Tennents' own "FB TAVERNS - PRICER" for a pub, from the master.

David Simpson (Tennents, 9 Oct 2026): "When you are sending me pricing for FB,
could you use the attached please? It is formatted in a way for Pricing to be
able to load in to the system." So the file that goes to Tennents is THEIR
layout, not ours, and this module fills it in rather than asking Tennents to
load our team price file.

The template (pricer_template/FB_TAVERNS_PRICER_template.xlsx, Tennents' file
with its password removed) has three sheets:
  "BDM Copy"      — one row per product: Sku Code (B), Brand (C), ABV (D),
                    WSP/Brl (E), FB Taverns Total Discount (F), formulas for the
                    net prices (G-I), the TENANT OFF-INVOICE £/brl (J) and the
                    retro as F - J (K). Pubname label I2 (value J2), Date label
                    K2 (value L2).
  "Customer Copy" — the tenant-facing view; every figure references BDM Copy.
  "Comms"         — Tennents' own legacy sheet; untouched.

What we write: the pub name (J2), the date (L2) and column J for every product
row the master can match. Everything else stays exactly as Tennents sent it —
WSP and the total discount are Tennents' figures, and where the master disagrees
with them the check lists it rather than overwriting their numbers. A product
the pub sells that has no row on the pricer is listed too: Tennents cannot load
an off-invoice for a line their file does not carry (Kingfisher at Mallroad).

The off-invoice follows the team price file's rule (tennents_price_export):
managed pubs take the whole discount off-invoice, every other pub the stored
per-site figure; a product with no off-invoice above £0 is left blank, which is
how the template marks "not sold here".
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from xml.sax.saxutils import escape as _xml_escape
from dataclasses import dataclass, field
from datetime import date

from openpyxl import load_workbook

from tennents_master import SiteInfo, SkuRate, TennentsMaster, sku_codes

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "pricer_template", "FB_TAVERNS_PRICER_template.xlsx")

# Tennents send and expect the file under this open-password ("Password -
# fbtaverns", David Simpson, 9 Oct 2026). It is the file's own lock, not a
# credential to any system; TENNENTS_PRICER_PASSWORD overrides it, and an empty
# value sends the file unlocked.
DEFAULT_PASSWORD = "fbtaverns"

SHEET = "BDM Copy"
COL_CODE, COL_BRAND, COL_TOTAL, COL_OFF = 2, 3, 6, 10
FIRST_ROW = 6
PUBNAME_CELL, DATE_CELL = "J2", "L2"
TOLERANCE = 0.005


@dataclass
class PricerLine:
    row: int
    code: str
    brand: str
    sku: SkuRate | None
    off: float | None              # what goes in column J (None = left blank)
    tennents_total: float | None   # column F as Tennents sent it
    master_total: float | None


@dataclass
class PricerCheck:
    site_name: str
    account: str
    lines: list[PricerLine] = field(default_factory=list)
    # Products the pub has an off-invoice for that no pricer row carries.
    not_on_pricer: list[tuple[SkuRate, float]] = field(default_factory=list)
    # Pricer rows the master cannot place (left as Tennents sent them).
    unmatched_rows: list[PricerLine] = field(default_factory=list)

    @property
    def filled(self) -> list[PricerLine]:
        return [ln for ln in self.lines if ln.off is not None]

    @property
    def total_differs(self) -> list[PricerLine]:
        """Filled rows where Tennents' total discount is not the master's."""
        return [ln for ln in self.filled
                if ln.tennents_total is not None and ln.master_total is not None
                and abs(ln.tennents_total - ln.master_total) > TOLERANCE]

    @property
    def off_above_total(self) -> list[PricerLine]:
        """Filled rows whose off-invoice exceeds Tennents' total: the retro the
        file would show is negative."""
        return [ln for ln in self.filled
                if ln.tennents_total is not None and ln.off - ln.tennents_total > TOLERANCE]


def _norm(text) -> str:
    t = re.sub(r"\(.*?\)", " ", str(text or "")).lower()
    t = re.sub(r"\b(keg|50l|30l|11g|22g)\b", " ", t)
    return re.sub(r"[^a-z0-9]+", "", t)


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _match_sku(master: TennentsMaster, code, brand) -> SkuRate | None:
    """Pricer row → master SKU. By code first (a pricer cell may carry several,
    "400557/400751"); by product name only when exactly one SKU has it."""
    for c in str(code or "").replace("\\", "/").split("/"):
        c = c.strip()
        if c:
            sku = master.find_sku(c)
            if sku is not None:
                return sku
    want = _norm(brand)
    if not want:
        return None
    hits = {id(s): s for s in master.skus if want in (_norm(s.product), _norm(s.brand))}
    return next(iter(hits.values())) if len(hits) == 1 else None


def _off_for(master: TennentsMaster, site: SiteInfo, sku: SkuRate) -> float | None:
    total = sku.correct_total_per_brl
    if site.is_managed:
        return float(total) if total is not None else None
    off = master.off_invoice(site.account, sku.sku_code)
    return off if off > 0 else None


def _template_bytes() -> bytes:
    with open(TEMPLATE_PATH, "rb") as fh:
        return fh.read()


def check_pricer(master: TennentsMaster, site: SiteInfo, template: bytes | None = None) -> PricerCheck:
    """What the pricer for this pub will carry, and what it cannot."""
    wb = load_workbook(io.BytesIO(template or _template_bytes()))
    return _fill(master, site, wb[SHEET], write=False)


def _fill(master: TennentsMaster, site: SiteInfo, ws, write: bool) -> PricerCheck:
    check = PricerCheck(site_name=site.site_name, account=site.account)
    seen: set[str] = set()
    for r in range(FIRST_ROW, ws.max_row + 1):
        code, brand = ws.cell(r, COL_CODE).value, ws.cell(r, COL_BRAND).value
        if code in (None, "") and brand in (None, ""):
            continue
        sku = _match_sku(master, code, brand)
        line = PricerLine(
            row=r, code=str(code or "").strip(), brand=str(brand or "").strip(), sku=sku,
            off=None, tennents_total=_num(ws.cell(r, COL_TOTAL).value),
            master_total=_num(sku.correct_total_per_brl) if sku else None,
        )
        if sku is None:
            check.unmatched_rows.append(line)
        else:
            seen.add(sku.sku_code.upper())
            line.off = _off_for(master, site, sku)
        if write:
            ws.cell(r, COL_OFF).value = round(line.off, 2) if line.off is not None else None
        check.lines.append(line)

    for sku in master.skus:
        if sku.sku_code.upper() in seen:
            continue
        off = _off_for(master, site, sku)
        if off is not None and not site.is_managed:
            check.not_on_pricer.append((sku, off))
    return check


def _password() -> str:
    return os.environ.get("TENNENTS_PRICER_PASSWORD", DEFAULT_PASSWORD)


def _encrypt(data: bytes, password: str) -> bytes:
    if not password:
        return data
    from msoffcrypto.format.ooxml import OOXMLFile  # lazy: only on a download
    out = io.BytesIO()
    OOXMLFile(io.BytesIO(data)).encrypt(password, out)
    return out.getvalue()


def build_pricer_bytes(master: TennentsMaster, site: SiteInfo, as_of: date | None = None,
                       template: bytes | None = None, password: str | None = None
                       ) -> tuple[bytes, PricerCheck]:
    """Tennents' pricer for one pub, filled from the master, locked with the
    file password Tennents use. Returns the file and the check behind it."""
    as_of = as_of or date.today()
    wb = load_workbook(io.BytesIO(template or _template_bytes()))
    ws = wb[SHEET]
    check = _fill(master, site, ws, write=True)
    ws[PUBNAME_CELL] = site.site_name
    ws[DATE_CELL] = as_of
    ws[DATE_CELL].number_format = "dd/mm/yyyy"
    wb.calculation.fullCalcOnLoad = True
    buf = io.BytesIO()
    wb.save(buf)
    data = _with_cached_values(buf.getvalue(), _formula_values(wb))
    return _encrypt(data, _password() if password is None else password), check


# --- Cached formula results ------------------------------------------------
# openpyxl saves a formula with an EMPTY result, and only Excel with editing
# enabled recalculates it. A pricer downloaded from the app or opened from an
# email opens in Protected View, and Outlook, phones and loaders that read the
# stored values see the same thing: the net price per brl, per keg and per
# pint and the retro all blank (Nick Madigan, Corner House, 13 Nov 2026). So
# the file carries each result as well as the formula. The template's
# formulas are plain arithmetic on cells (E7-F7, ((E7-J7)/36)*11, (H7/88),
# 'BDM Copy'!H7); anything else (#REF!, a function) is left for Excel.

_REF = re.compile(r"(?:'([^']+)'!)?\$?([A-Z]{1,3})\$?(\d+)")
_SAFE = re.compile(r"^[0-9.eE+\-*/() ]+$")


def _formula_values(wb) -> dict[tuple[str, str], float]:
    """{(sheet, cell): result} for every formula the template's arithmetic
    can work out. A blank cell counts as 0, as in Excel; a division by zero
    or a reference to text gives no result."""
    memo: dict[tuple[str, str], float | None] = {}

    def value(sheet: str, coord: str, depth: int = 0) -> float | None:
        key = (sheet, coord)
        if key in memo:
            return memo[key]
        memo[key] = None  # cycle guard
        if sheet not in wb.sheetnames or depth > 20:
            return None
        v = wb[sheet][coord].value
        if v is None or v == "":
            out = 0.0
        elif isinstance(v, bool):
            out = None
        elif isinstance(v, (int, float)):
            out = float(v)
        elif isinstance(v, str) and v.startswith("="):
            out = evaluate(sheet, v[1:], depth)
        else:
            out = None
        memo[key] = out
        return out

    def evaluate(sheet: str, expr: str, depth: int) -> float | None:
        if "#" in expr:  # #REF! and the like: Excel's to show
            return None
        failed = False

        def sub(m):
            nonlocal failed
            ref_sheet = m.group(1) or sheet
            v = value(ref_sheet, f"{m.group(2)}{m.group(3)}", depth + 1)
            if v is None:
                failed = True
                return "0"
            return f"({v!r})"

        py = _REF.sub(sub, expr)
        if failed or not _SAFE.match(py):
            return None
        try:
            return float(eval(py, {"__builtins__": {}}, {}))  # digits and + - * / ( ) only
        except (ZeroDivisionError, ArithmeticError, SyntaxError):
            return None

    out: dict[tuple[str, str], float] = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    v = value(ws.title, c.coordinate)
                    if v is not None:
                        out[(ws.title, c.coordinate)] = round(v, 10)
    return out


def _sheet_paths(z: zipfile.ZipFile) -> dict[str, str]:
    """Sheet name -> its part in the package, through workbook.xml's rels."""
    wbx = z.read("xl/workbook.xml").decode("utf-8")
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    targets = {m.group(1): m.group(2) for m in
               re.finditer(r'<Relationship[^>]*?Id="([^"]+)"[^>]*?Target="([^"]+)"', rels)}
    targets.update({m.group(2): m.group(1) for m in
                    re.finditer(r'<Relationship[^>]*?Target="([^"]+)"[^>]*?Id="([^"]+)"', rels)})
    out = {}
    for m in re.finditer(r'<sheet\b[^>]*?name="([^"]+)"[^>]*?r:id="([^"]+)"', wbx):
        t = targets.get(m.group(2), "")
        out[m.group(1).replace("&amp;", "&").replace("&apos;", "'")] = (
            t.lstrip("/") if t.startswith("/") else "xl/" + t)
    return out


def _with_cached_values(data: bytes, values: dict[tuple[str, str], float]) -> bytes:
    """Write each formula's result into its cell's empty <v> in the saved
    package. Only cells openpyxl wrote as <f>..</f><v></v> are touched."""
    if not values:
        return data
    src = zipfile.ZipFile(io.BytesIO(data))
    paths = _sheet_paths(src)
    by_part: dict[str, dict[str, float]] = {}
    for (sheet, coord), v in values.items():
        if sheet in paths:
            by_part.setdefault(paths[sheet], {})[coord] = v
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            raw = src.read(item.filename)
            cells = by_part.get(item.filename)
            if cells:
                xml = raw.decode("utf-8")

                def put(m):
                    v = cells.get(m.group(2))
                    if v is None:
                        return m.group(0)
                    return f"{m.group(1)}<f>{m.group(3)}</f><v>{_xml_escape(repr(v))}</v></c>"

                xml = re.sub(r'(<c r="([A-Z]+\d+)"[^>]*>)<f>(.*?)</f><v\s*/?>(?:</v>)?</c>', put, xml)
                raw = xml.encode("utf-8")
            dst.writestr(item, raw)
    return buf.getvalue()


def pricer_filename(site: SiteInfo, as_of: date | None = None) -> str:
    as_of = as_of or date.today()
    safe = re.sub(r'[\x00-\x1f\x7f\\/:*?"<>|]', " ", site.site_name).strip() or "Site"
    return f"FB TAVERNS - PRICER - {safe} {as_of.isoformat()}.xlsx"


def build_pricer_zip_bytes(master: TennentsMaster, sites: list[SiteInfo], as_of: date | None = None,
                           password: str | None = None) -> bytes:
    """One pricer per pub in a .zip, for sending the whole estate at once."""
    as_of = as_of or date.today()
    template = _template_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for site in sites:
            data, _ = build_pricer_bytes(master, site, as_of, template, password)
            zf.writestr(pricer_filename(site, as_of), data)
    return buf.getvalue()
