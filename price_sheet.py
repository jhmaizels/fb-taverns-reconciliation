"""
Price sheet for one pub — read a pricing sheet (a side letter's schedule, a
price list), plan the changes that make the pub's LWC prices match it, and
show them for sign-off before anything is written.

The sheet REPLACES the pub's prices: every product on it is set to the
sheet's price, and every product the pub has a price for that is NOT on the
sheet is removed (a new bar top means the old prices no longer apply).

Sheets don't come in one layout (James, 21 Oct 2026: "it may not always be in
exactly the same format"), so the reader does not rely on columns:

  - PDF and Word: the text is read in order, and each product code is paired
    with the first £ amount after it, before the next code. A row the PDF
    splits over two lines still pairs up.
  - Excel and CSV: per row, the code cell and the price — the column headed
    "price" when there is one, else the last number in the row after the code.

A code is any code on the master's product list, or any 8-digit number (so a
product the master doesn't have yet is reported, not silently dropped). A code
with no price after it (a company number, say) is ignored.

Matching is by CODE, never by name (the Bellamy's lesson, 20 Oct 2026); the
preview shows the sheet's name beside the master's so a wrong code is visible
before sign-off.

Nothing here writes. webapp.py applies the plan through the grid's own cell
path (`_apply_lwc_cell`), so each change lands as a recorded change on the
price history page and can be undone there.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from html import escape
from urllib.parse import urlencode

from auth_supabase import ext_url

MAX_PRICE = 5000.0

_CODE_RE = re.compile(r"(?<![\d.,£])(\d{6,9})(?![\d.,])")
_MONEY_RE = re.compile(r"£\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?|(?<![\d.,])(\d+)\.(\d{2})(?![\d])")
_SITE_REF_RE = re.compile(r"\bsite\s*(?:no\.?|id|number)?\s*[:#]?\s*(\d{3})\b", re.I)


@dataclass
class SheetLine:
    code: str
    price: float
    desc: str = ""


@dataclass
class SheetRead:
    lines: list[SheetLine]
    text: str
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- reading

def _money(m: re.Match) -> float:
    if m.group(1) is not None:
        whole, frac = m.group(1), m.group(2) or "0"
    else:
        whole, frac = m.group(3), m.group(4)
    return round(float(whole.replace(",", "") + "." + frac.ljust(2, "0")), 2)


def _is_code(tok: str, known: set[str]) -> bool:
    return tok in known or len(tok) == 8


def _clean_desc(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip(" |-–:\t")
    return s[:80]


def lines_from_text(text: str, known: set[str]) -> list[SheetLine]:
    """Pair each product code with the first money value after it, before the
    next code. The words between the code and the price are the sheet's name
    for the product."""
    tokens: list[tuple[int, int, str, object]] = []
    for m in _CODE_RE.finditer(text):
        if _is_code(m.group(1), known):
            tokens.append((m.start(), m.end(), "code", m.group(1)))
    for m in _MONEY_RE.finditer(text):
        tokens.append((m.start(), m.end(), "money", _money(m)))
    tokens.sort()
    out: list[SheetLine] = []
    code, code_end = None, 0
    for start, end, kind, val in tokens:
        if kind == "code":
            code, code_end = val, end
        elif code is not None:
            out.append(SheetLine(code, val, _clean_desc(text[code_end:start])))
            code = None
    return out


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _num(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(",", "").lstrip("£").strip()
    try:
        return float(t)
    except ValueError:
        return None


def lines_from_rows(rows: list[list], known: set[str]) -> list[SheetLine]:
    """One product per row: the code cell and its price — the column headed
    "price" when the sheet has one, else the last number after the code."""
    out: list[SheetLine] = []
    price_col = None
    for row in rows:
        cells = [_cell_str(v) for v in row]
        lowered = [c.lower() for c in cells]
        hdr = [i for i, c in enumerate(lowered) if "price" in c and len(c) < 40]
        if hdr and not any(_CODE_RE.fullmatch(c) for c in cells):
            price_col = hdr[-1]
            continue
        ci = next((i for i, c in enumerate(cells)
                   if _CODE_RE.fullmatch(c) and _is_code(c, known)), None)
        if ci is None:
            continue
        price = None
        if price_col is not None and price_col < len(row) and price_col != ci:
            price = _num(row[price_col])
        if price is None:
            for v in reversed(row[ci + 1:]):
                p = _num(v)
                if p is not None:
                    price = round(p, 2)
                    break
        if price is None:
            continue
        desc = " ".join(c for i, c in enumerate(cells)
                        if i != ci and i != price_col and c and _num(row[i]) is None)
        out.append(SheetLine(cells[ci], round(price, 2), _clean_desc(desc)))
    return out


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"</w:tc>", "\t", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&apos;", "'"))


def _xlsx_rows(data: bytes) -> list[list]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    rows: list[list] = []
    for ws in wb.worksheets:
        rows.extend([list(r) for r in ws.iter_rows(values_only=True)])
        rows.append([])
    return rows


def read_sheet(filename: str, data: bytes, known: set[str]) -> SheetRead:
    """Read a price sheet of any of the supported kinds. Raises ValueError with
    a sentence for the person when the file can't be read at all."""
    name = (filename or "").lower()
    if data[:4] == b"%PDF" or name.endswith(".pdf"):
        try:
            text = _pdf_text(data)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"the PDF could not be read ({type(exc).__name__})") from exc
        res = SheetRead(lines_from_text(text, known), text)
        if not text.strip():
            res.problems.append("The PDF has no text in it — it looks like a scan. "
                                "Ask for the original file, or type the prices into a spreadsheet.")
        return res
    if name.endswith(".docx"):
        try:
            text = _docx_text(data)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("the Word file could not be read") from exc
        return SheetRead(lines_from_text(text, known), text)
    if name.endswith((".xlsx", ".xlsm")):
        try:
            rows = _xlsx_rows(data)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("the Excel file could not be read") from exc
        text = "\n".join(" ".join(_cell_str(v) for v in r) for r in rows)
        return SheetRead(lines_from_rows(rows, known), text)
    if name.endswith((".csv", ".txt")):
        raw = data.decode("utf-8-sig", "replace")
        if name.endswith(".csv"):
            rows = [list(r) for r in csv.reader(io.StringIO(raw))]
            return SheetRead(lines_from_rows(rows, known), raw)
        return SheetRead(lines_from_text(raw, known), raw)
    if name.endswith((".xls", ".doc")):
        raise ValueError("that is an old Office format — open it and save it as .xlsx or .docx, or as a PDF")
    raise ValueError("upload a PDF, Word (.docx), Excel (.xlsx) or CSV file")


# ---------------------------------------------------------------- the pub

def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(the|ye|olde?)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def guess_site(text: str, sites: dict) -> tuple[str, str]:
    """(site_id, how) for the pub the sheet is for: the pub name that appears
    FIRST in the sheet (a side letter names its own pub in the heading, and
    any other pub — "to match the Queen's Head" — later). ("", "") if none."""
    body = " " + _norm(text) + " "
    best: tuple[int, str, str] | None = None
    for sid, info in sites.items():
        name = _norm((info or {}).get("name") or "")
        if len(name) < 4:
            continue
        pos = body.find(" " + name + " ")
        if pos >= 0 and (best is None or pos < best[0]):
            best = (pos, sid, (info or {}).get("name") or sid)
    if best is None:
        return "", ""
    return best[1], f'the sheet names "{best[2]}"'


def referenced_sites(text: str, sites: dict, exclude: str = "") -> list[str]:
    """Other pubs the sheet points at by number ("your Queen's Head account
    (site 833)") — the preview shows that pub's current prices alongside."""
    out: list[str] = []
    for m in _SITE_REF_RE.finditer(text):
        sid = m.group(1)
        if sid in sites and sid != exclude and sid not in out:
            out.append(sid)
    return out


# ---------------------------------------------------------------- the plan

@dataclass
class PlanRow:
    code: str
    sheet_desc: str
    master_desc: str
    current: float | None
    new: float | None
    action: str          # add | change | same | remove | refused
    note: str = ""
    compare: float | None = None

    @property
    def writes(self) -> bool:
        return self.action in ("add", "change", "remove")


@dataclass
class SheetPlan:
    site_id: str
    site_name: str
    rows: list[PlanRow]
    blocked: str = ""

    @property
    def to_write(self) -> list[PlanRow]:
        return [] if self.blocked else [r for r in self.rows if r.writes]


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (s or "").lower())
            if w not in {"keg", "kegs", "cask", "pack", "smooth", "extra"}}


def _names_differ(sheet: str, master: str) -> bool:
    a, b = _words(sheet), _words(master)
    return bool(a and b and not (a & b))


def plan_sheet(*, site_id: str, site: dict, lines: list[SheetLine], winners: dict,
               products: dict, compare: dict | None = None) -> SheetPlan:
    """The changes that make the pub's prices exactly the sheet's.

    ``winners``: product_code -> the Rule the grid shows for this pub today.
    ``products``: product_code -> master description, for every product on the
    master (a code not in it can't be priced until the product is added).
    ``compare``: product_code -> price at the pub the sheet points at."""
    from reconcile import is_support_rule

    site_name = (site or {}).get("name") or site_id
    compare = compare or {}
    blocked = ""
    if (site or {}).get("status") == "managed":
        blocked = f"{site_name} is a managed pub — its prices are the FB price, not a tenant price list."

    by_code: dict[str, list[SheetLine]] = {}
    for ln in lines:
        by_code.setdefault(ln.code, []).append(ln)

    rows: list[PlanRow] = []
    for code, lns in by_code.items():
        prices = sorted({ln.price for ln in lns})
        sheet_desc = next((ln.desc for ln in lns if ln.desc), "")
        w = winners.get(code)
        cur = w.tenant_price if w is not None else None
        mdesc = products.get(code, "")
        row = PlanRow(code, sheet_desc, mdesc, cur, prices[0], "", compare=compare.get(code))
        if len(prices) > 1:
            row.action, row.new = "refused", None
            row.note = "the sheet gives this code two prices: " + ", ".join(f"£{p:,.2f}" for p in prices)
        elif code not in products:
            row.action = "refused"
            row.note = "not on the master's product list — add the product in the price file first"
        elif not (0 < row.new <= MAX_PRICE):
            row.action = "refused"
            row.note = f"£{row.new:,.2f} doesn't look like a keg price"
        elif w is not None and is_support_rule(w):
            row.action = "refused"
            row.note = (f"a support of £{cur:,.2f} runs until {w.valid_to:%d %b %Y} — change or end it in the grid"
                        if w.valid_to else "a support is running — change or end it in the grid")
        elif cur is not None and abs(cur - row.new) < 0.005:
            row.action = "same"
        else:
            row.action = "change" if w is not None else "add"
        if row.action != "refused" and _names_differ(sheet_desc, mdesc):
            row.note = f'check the code: the sheet says "{sheet_desc}", the master "{mdesc}"'
        rows.append(row)

    for code, w in winners.items():
        if code in by_code or w.tenant_price is None:
            continue
        row = PlanRow(code, "", products.get(code, "") or w.product_desc, w.tenant_price, None,
                      "remove", compare=compare.get(code))
        if is_support_rule(w):
            row.action = "refused"
            row.note = "not on the sheet, but a support is running — end it in the grid"
        rows.append(row)

    order = {"refused": 0, "change": 1, "add": 2, "remove": 3, "same": 4}
    rows.sort(key=lambda r: (order[r.action], r.master_desc or r.sheet_desc, r.code))
    return SheetPlan(site_id, site_name, rows, blocked)


def encode_lines(lines: list[SheetLine]) -> str:
    """The sheet's lines carried through the preview form, so sign-off works
    from what was shown without the file being uploaded again."""
    return "\n".join(f"{ln.code}\t{ln.price:.2f}\t{ln.desc.replace(chr(9), ' ')}" for ln in lines)


def decode_lines(raw: str) -> list[SheetLine]:
    out: list[SheetLine] = []
    for line in (raw or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not _CODE_RE.fullmatch(parts[0].strip()):
            continue
        try:
            price = round(float(parts[1]), 2)
        except ValueError:
            continue
        out.append(SheetLine(parts[0].strip(), price, parts[2] if len(parts) > 2 else ""))
    return out


# ---------------------------------------------------------------- pages

_CSS = """<style>
table.sheet{border-collapse:collapse;width:100%;font-size:0.92em}
table.sheet th,table.sheet td{border-bottom:1px solid #e3e3e3;padding:0.35em 0.5em;text-align:left;vertical-align:top}
table.sheet td.r,table.sheet th.r{text-align:right;white-space:nowrap}
.sh-note{color:#666;font-size:0.9em}
.sh-warn{color:#8a5a00}
.sh-bad{background:#fdecea}
.sh-err{background:#fdecea;border:1px solid #f5c2c0;padding:0.6em 0.8em;border-radius:4px}
.sh-tag{display:inline-block;padding:0 0.45em;border-radius:3px;font-size:0.85em}
.sh-add{background:#e3f2fd}.sh-change{background:#fff3e0}.sh-remove{background:#fce4ec}
.sh-same{background:#eee;color:#666}.sh-refused{background:#fdecea}
</style>"""

_LABEL = {"add": "Add", "change": "Change", "remove": "Remove", "same": "No change", "refused": "Not done"}


def _m(v: float | None) -> str:
    return "—" if v is None else f"£{v:,.2f}"


def _site_options(sites: dict, selected: str, blank: str) -> str:
    opts = [f'<option value="">{escape(blank)}</option>']
    for sid in sorted(sites, key=lambda s: ((sites[s] or {}).get("name") or s).lower()):
        name = (sites[sid] or {}).get("name") or ""
        sel = " selected" if sid == selected else ""
        opts.append(f'<option value="{escape(sid)}"{sel}>{escape(name or sid)} ({escape(sid)})</option>')
    return "".join(opts)


def render_upload_page(sites: dict, error: str = "") -> str:
    err = f'<p class="sh-err">{escape(error)}</p>' if error else ""
    return f"""{_CSS}
<p class="sub" style="margin-top:0"><a href="{ext_url('/master')}">← Back to the price file</a></p>
<h1>Price sheet for one pub</h1>
<p>Upload a pub's price sheet (a side letter's schedule, a price list). Nothing changes yet: the next
page shows every change it would make to the pub's prices, and you sign them off there.</p>
<p>The sheet <strong>replaces</strong> the pub's prices. Products on the sheet are set to its price; any product
the pub has a price for that is <strong>not</strong> on the sheet is removed.</p>
{err}
<form method="post" action="{ext_url('/master/sheet/preview')}" enctype="multipart/form-data" style="max-width:40em">
  <label for="sheet-file">Price sheet (PDF, Word .docx, Excel .xlsx or CSV)</label>
  <input type="file" name="file" id="sheet-file" accept=".pdf,.docx,.xlsx,.xlsm,.csv,.txt" required>
  <label for="sheet-site">Pub</label>
  <select name="site_id" id="sheet-site">{_site_options(sites, "", "Find it on the sheet")}</select>
  <p class="help">Products are matched by their LWC product code, so the sheet must show the codes.
  Pub not in the list? <a href="{ext_url('/master/site/new')}">Add it to the price file</a> first.</p>
  <button type="submit">Read the sheet</button>
</form>
"""


def render_preview_page(*, plan: SheetPlan | None, lines: list[SheetLine], sites: dict, filename: str,
                        how: str = "", compare_site: str = "", problems: list[str] | None = None) -> str:
    carried = escape(encode_lines(lines))
    probs = "".join(f'<p class="sh-err">{escape(p)}</p>' for p in (problems or []))
    pick = f"""
<form method="post" action="{ext_url('/master/sheet/preview')}" style="max-width:none;margin:0.6em 0">
  <input type="hidden" name="lines" value="{carried}">
  <input type="hidden" name="filename" value="{escape(filename)}">
  <label for="sheet-site2" style="display:inline">Pub</label>
  <select name="site_id" id="sheet-site2" style="display:inline-block;width:auto;margin:0 0.5em">
    {_site_options(sites, plan.site_id if plan else "", "Choose the pub…")}</select>
  <button type="submit" style="margin:0">{"Change pub" if plan else "Use this pub"}</button>
</form>"""
    head = f"""{_CSS}
<p class="sub" style="margin-top:0"><a href="{ext_url('/master/sheet')}">← Upload a different sheet</a></p>
<h1>Check the price sheet{(" for " + escape(plan.site_name)) if plan else ""}</h1>
<p class="sh-note">{escape(filename)}: {len(lines)} priced product line{"s" if len(lines) != 1 else ""} read.
{("Pub chosen because " + escape(how) + ".") if (plan and how) else ""}</p>
{probs}"""
    if not lines:
        return head + ('<p class="sh-err">No product codes with prices were found on this sheet. The reader '
                       "pairs each LWC product code with the £ price after it, so the sheet has to show the "
                       "codes.</p>")
    if plan is None:
        return head + '<p class="sh-err">Which pub is this sheet for? It doesn\'t name one on the price file.</p>' + pick
    if plan.blocked:
        return head + f'<p class="sh-err">{escape(plan.blocked)}</p>' + pick

    cmp_name = (sites.get(compare_site) or {}).get("name") or compare_site
    cmp_th = f'<th class="r">{escape(cmp_name)} now</th>' if compare_site else ""
    body = []
    for r in plan.rows:
        cls = ' class="sh-bad"' if r.action == "refused" else ""
        note = f'<div class="{"sh-warn" if r.action != "refused" else ""} sh-note">{escape(r.note)}</div>' if r.note else ""
        cmp_td = ""
        if compare_site:
            differs = (r.compare is not None and r.new is not None and abs(r.compare - r.new) >= 0.005) \
                or (r.compare is None) != (r.new is None)
            mark = ' <span class="sh-warn" title="differs from the sheet">≠</span>' if differs else ""
            cmp_td = f'<td class="r">{_m(r.compare)}{mark}</td>'
        body.append(
            f"<tr{cls}><td>{escape(r.code)}</td>"
            f"<td>{escape(r.master_desc or '—')}<div class=\"sh-note\">{escape(r.sheet_desc)}</div>{note}</td>"
            f'<td class="r">{_m(r.current)}</td><td class="r">{_m(r.new)}</td>{cmp_td}'
            f'<td><span class="sh-tag sh-{r.action}">{_LABEL[r.action]}</span></td></tr>'
        )
    counts = {k: sum(1 for r in plan.rows if r.action == k) for k in _LABEL}
    summary = ", ".join(f"{counts[k]} {_LABEL[k].lower()}" for k in ("change", "add", "remove", "same", "refused")
                        if counts[k])
    n = len(plan.to_write)
    if n:
        sign = f"""
<form method="post" action="{ext_url('/master/sheet/apply')}" style="max-width:none;margin-top:1em">
  <input type="hidden" name="lines" value="{carried}">
  <input type="hidden" name="filename" value="{escape(filename)}">
  <input type="hidden" name="site_id" value="{escape(plan.site_id)}">
  <button type="submit">Sign off and make {n} change{"s" if n != 1 else ""}</button>
  <a href="{ext_url('/master')}?{urlencode({'site': plan.site_id})}">Cancel</a>
</form>
<p class="help">The changes take effect from today and each one is recorded on the
<a href="{ext_url('/master/history')}?{urlencode({'site': plan.site_id})}">price history</a> under your name,
where it can be undone.{" Lines marked Not done are left as they are." if counts["refused"] else ""}</p>"""
    else:
        sign = '<p class="help">Nothing to change: the pub\'s prices already match the sheet.</p>'
    return head + pick + f"""
<p><strong>{escape(summary)}.</strong> Read each line against the sheet before signing off. The grey line under
each product is the sheet's own wording.</p>
<table class="sheet"><thead><tr><th>Code</th><th>Product</th><th class="r">Now</th><th class="r">Sheet</th>{cmp_th}<th></th></tr></thead>
<tbody>{"".join(body)}</tbody></table>
{sign}"""
