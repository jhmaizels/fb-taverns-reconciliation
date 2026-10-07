"""
Tennents Direct master price file — FB_Taverns_Tennents_Master.xlsx.

This workbook is the PRIMARY price file for the Tennents Direct estate
(operator direction 2026-07-14; supersedes the "Commercial Data" per-account
agreements). Its own README sheet is the spec; §4 is the reconciliation logic:

  For each Draught Pricing Report row: expected total discount = SKU_Master
  "CURRENT CORRECT Total Discount" unless a Site_SKU_Exceptions row overrides
  it (use "Loaded" value as expected-current until the exception status shows
  resolved). Tolerance ±£0.50/brl (rounding). Retro check: retro due must
  equal retro £/brl × barrels exactly. Managed sites: zero retro + full
  discount off-invoice is CORRECT (see Site_Master construct column).
  Gartocher: flat £200/brl retro construct — validate total discount, not
  the split.

Sheets parsed:
  README              -> version string (section "7. Version")
  SKU_Master          -> estate-wide per-SKU rates (SkuRate)
  Site_Master         -> sites, operating model, discount construct (SiteInfo)
  Site_SKU_Exceptions -> per-(site, SKU) overrides (SkuException)

Update rules (workbook README §5): the workbook is the editing surface — on
any change the operator bumps the version and re-uploads; the app replaces
the stored master wholesale. Never back-edit history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

REQUIRED_SHEETS = ("SKU_Master", "Site_Master", "Site_SKU_Exceptions")
# Site_Prices is OPTIONAL — the per-site tenant off-invoice layer that drives
# the team price file. Absent in the original v1.0 workbook; added by the
# seed. Parsing tolerates its absence so old workbooks still load.
SITE_PRICES_SHEET = "Site_Prices"

# SKU_Master internal consistency: base + hold should equal the CURRENT CORRECT
# total. £0.05 — tighter picks up spreadsheet float artefacts (same threshold
# as the old per-agreement master arithmetic check).
MASTER_ARITH_TOLERANCE = 0.05

# A brewers' barrel = 36 imperial gallons = 288 pints (36 × 8). Used to turn a
# £/brl figure into per-keg / per-pint prices for the team price file.
PINTS_PER_BRL = 288.0
LITRES_PER_BRL = 163.659            # 36 imp gal × 4.54609 L/gal
_LITRES_PER_GALLON = 4.54609


def sku_codes(s: "SkuRate") -> list[str]:
    """Primary + every alt code, upper-cased. alt_code may hold SEVERAL codes,
    "/"-separated — Tennents re-code containers (Heverlee 400217/401187), and the
    findings page's "Link to existing SKU" appends the new code there."""
    out = [str(s.sku_code).strip().upper()] if s.sku_code else []
    for c in str(s.alt_code or "").replace("\\", "/").split("/"):
        c = c.strip().upper()
        if c and c not in out:
            out.append(c)
    return out


_DESC_STOP = {"keg", "kegs", "the", "and", "a", "of", "x", "brl", "case"}


def _desc_tokens(text: str) -> set[str]:
    """Brand/product words only — sizes, ABVs and pack words are dropped so
    'Blackthorn Dry 5% 50L Keg' and 'Blackthorn Dry 50L Keg' compare equal."""
    out: set[str] = set()
    for t in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if not t or t in _DESC_STOP:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?%?|\d+l|\d+g|\d+lt|\d+ltr", t):
            continue
        out.add(t)
    return out


def suggest_sku(master: "TennentsMaster", desc: str) -> "SkuRate | None":
    """Best existing SKU for an unknown report description (Jaccard overlap on
    brand + product words) — preselects the 'Link to existing SKU' dropdown on
    the findings page. None when nothing shares a word."""
    want = _desc_tokens(desc)
    if not want:
        return None
    best, best_score = None, 0.0
    for s in master.skus:
        have = _desc_tokens(f"{s.brand} {s.product}")
        if not have:
            continue
        overlap = len(want & have)
        if not overlap:
            continue
        score = overlap / len(want | have)
        if score > best_score:
            best, best_score = s, score
    return best


# ---------- row shapes ----------

@dataclass
class SkuRate:
    sku_code: str
    alt_code: str
    brand: str
    product: str
    container: str
    brl_per_unit: float | None
    abv: float | None
    wsp_per_brl: float | None
    contract_base_per_brl: float | None
    on_contract: bool
    supplier_type: str                    # "C&C" | "3rd party"
    hold_per_brl: float
    correct_total_per_brl: float | None   # None = no agreed rate yet (RATE TBC)
    source: str = ""
    notes: str = ""

    @property
    def implied_total(self) -> float | None:
        if self.contract_base_per_brl is None:
            return None
        return float(self.contract_base_per_brl) + float(self.hold_per_brl or 0)


@dataclass
class SiteInfo:
    account: str            # Tennents account number as string; may be "TBC"
    site_name: str
    operating_model: str    # raw text, e.g. "Tenanted (TBC)" / "MANAGED (confirmed)"
    discount_construct: str
    notes: str = ""

    @property
    def is_managed(self) -> bool:
        return "MANAGED" in (self.operating_model or "").upper()

    @property
    def flat_retro_per_brl(self) -> float | None:
        """Bespoke flat retro £/brl (Gartocher: £200) parsed from the construct."""
        m = re.search(r"flat\s*£\s*([\d.]+)\s*/\s*brl", self.discount_construct or "", re.I)
        return float(m.group(1)) if m else None

    @property
    def is_bespoke(self) -> bool:
        return "BESPOKE" in (self.discount_construct or "").upper()


@dataclass
class SkuException:
    site_name: str
    account: str                          # resolved via Site_Master; "" if unknown
    sku_code_raw: str                     # as in the sheet; may be compound "400751/400557"
    product: str
    loaded_total_per_brl: float | None    # expected-current until resolved
    correct_total_per_brl: float | None   # target rate once Tennents fix lands
    direction: str = ""
    impact_gbp: float | None = None
    status: str = ""
    # Explicit override (the Airtable `resolved` checkbox). None = derive from
    # the status text, so ticking the box in Airtable retires an exception
    # without a workbook re-upload.
    resolved_flag: bool | None = None

    @property
    def sku_codes(self) -> list[str]:
        return [c.strip() for c in str(self.sku_code_raw).split("/") if c.strip()]

    @property
    def resolved(self) -> bool:
        if self.resolved_flag is not None:
            return self.resolved_flag
        return "resolved" in (self.status or "").lower()


@dataclass
class SitePrice:
    """Per-(site, SKU) tenant OFF-INVOICE discount £/brl — FB's internal
    decision on how much of the agreed total discount is passed to the tenant
    on the invoice (the remainder is claimed back as the monthly Iona retro).

    Independent of the Tennents rate: the agreed WSP + total discount always
    come from SKU_Master, so a PINC flows straight through. This layer only
    carries the split. 0.0 (or a missing row) = tenant pays full WSP on invoice
    and the whole discount is retro. Managed sites store off = total (all
    off-invoice, zero retro). Sourced from each site's "Net & Invoice Pricing".
    """
    account: str
    site_name: str
    sku_code: str                         # canonical SKU code
    product: str = ""
    off_invoice_per_brl: float = 0.0
    notes: str = ""


@dataclass
class RateBasis:
    """Outcome of an expected-rate lookup for one (account, sku) pair."""
    basis: str                            # 'sku_master' | 'exception' | 'no_rate' | 'unknown_sku'
    expected: float | None                # expected-current total discount £/brl
    sku: SkuRate | None = None
    exception: SkuException | None = None


@dataclass
class TennentsMaster:
    version: str
    source: str
    skus: list[SkuRate]
    sites: list[SiteInfo]
    exceptions: list[SkuException]
    site_prices: list[SitePrice] = field(default_factory=list)
    # True only when a Site_Prices sheet was actually present in the parsed
    # workbook. Lets replace_tennents_master preserve the stored off-invoice
    # layer when an OLDER workbook (no Site_Prices sheet) is re-uploaded, rather
    # than silently wiping it. site_prices=[] with this True means "explicitly
    # empty" (wipe); this False means "not provided" (leave the table alone).
    site_prices_present: bool = False

    _sku_index: dict[str, SkuRate] = field(default_factory=dict, repr=False)
    _site_by_account: dict[str, SiteInfo] = field(default_factory=dict, repr=False)
    _exception_index: dict[tuple[str, str], SkuException] = field(default_factory=dict, repr=False)
    _site_price_index: dict[tuple[str, str], SitePrice] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.reindex()

    def reindex(self) -> None:
        self._sku_index = {}
        for s in self.skus:
            for code in sku_codes(s):
                self._sku_index[code] = s

        self._site_by_account = {
            s.account: s for s in self.sites if s.account and s.account.upper() != "TBC"
        }

        # Exceptions are keyed by (account, RAW sku code) — NOT canonicalised.
        # Tennents loads rates per specific SKU code, and the workbook's own
        # convention is per-code: a mis-load on the 30L container (GUI003 at
        # Maryhill) says nothing about the 50L (GUI002), and an exception that
        # covers both containers lists both codes ("400751/400557"). Resolved
        # exceptions are dropped — per README §5 the override stops applying.
        self._exception_index = {}
        site_by_name = {s.site_name.strip().upper(): s for s in self.sites}
        for ex in self.exceptions:
            if not ex.account:
                site = site_by_name.get(ex.site_name.strip().upper())
                if site:
                    ex.account = site.account
            if ex.resolved or not ex.account or ex.account.upper() == "TBC":
                continue
            for code in ex.sku_codes:
                self._exception_index[(ex.account, str(code).strip().upper())] = ex

        # Site prices keyed (account, CANONICAL sku upper) — the file lists one
        # row per product, so alt/compound codes are canonicalised on the way in.
        self._site_price_index = {}
        for sp in self.site_prices:
            if not sp.account:
                site = site_by_name.get(sp.site_name.strip().upper())
                if site:
                    sp.account = site.account
            if sp.account:
                key = (sp.account, self.canonical_sku(sp.sku_code).strip().upper())
                self._site_price_index[key] = sp

    def off_invoice(self, account: str, sku_code: str) -> float:
        """Tenant off-invoice discount £/brl for (account, sku). 0.0 when none
        is stored — tenant pays full WSP on invoice, the whole discount is retro."""
        sp = self.site_price(account, sku_code)
        return float(sp.off_invoice_per_brl or 0.0) if sp else 0.0

    def site_price(self, account: str, sku_code: str) -> "SitePrice | None":
        """The stored Site_Prices row for (account, sku), or None when the file
        carries no split for it. Distinct from off_invoice() == 0: a row at £0
        is a RECORDED decision (tenant pays full WSP); no row is no decision."""
        return self._site_price_index.get(
            (str(account).strip(), self.canonical_sku(sku_code).strip().upper()))

    def canonical_sku(self, code: str) -> str:
        sku = self.find_sku(code)
        return sku.sku_code if sku else str(code).strip().upper()

    def find_sku(self, code: str) -> SkuRate | None:
        c = str(code).strip().upper()
        sku = self._sku_index.get(c)
        if sku is None and c.isdigit():
            # Tolerate leading-zero drift: Excel/pandas read "090425" as 90425,
            # so a code and its zero-stripped form must resolve to the same SKU
            # (the Site_Prices ↔ SKU_Master join depends on it).
            sku = self._sku_index.get(c.zfill(6)) or self._sku_index.get(c.lstrip("0"))
        return sku

    def site_for_account(self, account: str) -> SiteInfo | None:
        return self._site_by_account.get(str(account).strip())

    def resolve(self, account: str, sku_code: str) -> RateBasis:
        """Expected-current total discount for (account, sku) per README §4."""
        sku = self.find_sku(sku_code)
        ex = self._exception_index.get((str(account).strip(), str(sku_code).strip().upper()))
        if ex is not None:
            return RateBasis(basis="exception", expected=ex.loaded_total_per_brl, sku=sku, exception=ex)
        if sku is None:
            return RateBasis(basis="unknown_sku", expected=None)
        if sku.correct_total_per_brl is None:
            return RateBasis(basis="no_rate", expected=None, sku=sku)
        return RateBasis(basis="sku_master", expected=float(sku.correct_total_per_brl), sku=sku)

    def arithmetic_errors(self) -> list[SkuRate]:
        """SKU rows where contract base + hold ≠ CURRENT CORRECT total."""
        out = []
        for s in self.skus:
            if s.correct_total_per_brl is None or s.implied_total is None:
                continue
            if abs(float(s.correct_total_per_brl) - s.implied_total) > MASTER_ARITH_TOLERANCE:
                out.append(s)
        return out


# ---------- price maths ----------

def keg_brl_factor(sku: SkuRate) -> float:
    """Barrels per keg for a SKU. Uses the master's brl_per_unit when set;
    otherwise, for a multi-container SKU, picks the container CLOSEST to the
    standard 50L keg — the one the price file quotes (e.g. '30L / 50L' Disco is
    quoted on 50L, '11G / 22G' Lager on the 11G ≈ 50L keg). Falls back to a 50L
    keg for a bare 'keg' with no stated size."""
    if sku.brl_per_unit:
        return float(sku.brl_per_unit)
    litres_opts = [
        float(val) if unit == "L" else float(val) * _LITRES_PER_GALLON
        for val, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(L|G)\b", (sku.container or "").upper())
    ]
    litres = min(litres_opts, key=lambda l: abs(l - 50.0)) if litres_opts else 50.0
    return litres / LITRES_PER_BRL


# ---------- parsing helpers ----------

def _num(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("£", "").replace(",", "")
    if not s or s.upper() in {"TBC", "N/A", "NA", "-", "—"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _text(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _account_str(v) -> str:
    """17591767 / 17591767.0 / 'TBC' -> normalised string."""
    n = _num(v)
    if n is not None and float(n).is_integer():
        return str(int(n))
    return _text(v)


def _find_col(df: pd.DataFrame, *prefixes: str) -> str | None:
    """First column whose stripped name starts with any prefix (case-insensitive).

    Headers embed dates that move with each PINC ('WSP £/brl (post 2-Mar-26)',
    '£ Impact Jun-26'), so exact-name matching would break on every version bump.
    """
    for col in df.columns:
        name = str(col).strip().upper()
        for p in prefixes:
            if name.startswith(p.upper()):
                return col
    return None


def _require_cols(df: pd.DataFrame, sheet: str, cols: dict[str, str | None]) -> None:
    missing = [label for label, col in cols.items() if col is None]
    if missing:
        raise ValueError(f"{sheet} sheet is missing expected column(s): {', '.join(missing)}")


# ---------- workbook parsing ----------

def parse_master_workbook(path: str, source_name: str = "") -> TennentsMaster:
    """Parse FB_Taverns_Tennents_Master.xlsx into a TennentsMaster."""
    book = pd.read_excel(path, sheet_name=None)
    missing = [s for s in REQUIRED_SHEETS if s not in book]
    if missing:
        raise ValueError(
            f"Not a Tennents master workbook — missing sheet(s): {', '.join(missing)}. "
            f"Expected FB_Taverns_Tennents_Master.xlsx with sheets {', '.join(REQUIRED_SHEETS)}."
        )

    version = ""
    if "README" in book:
        rd = book["README"]
        if rd.shape[1] >= 2:
            for _, row in rd.iterrows():
                if "version" in _text(row.iloc[0]).lower():
                    version = _text(row.iloc[1])
                    break

    # --- SKU_Master ---
    df = book["SKU_Master"]
    df.columns = [str(c).strip() for c in df.columns]
    c_code = _find_col(df, "SKU Code")
    c_alt = _find_col(df, "Alt Code")
    c_brand = _find_col(df, "Brand")
    c_prod = _find_col(df, "Product")
    c_cont = _find_col(df, "Container")
    c_bpu = _find_col(df, "Brl per Unit")
    c_abv = _find_col(df, "ABV")
    c_wsp = _find_col(df, "WSP")
    c_base = _find_col(df, "Contract Base Discount")
    c_onc = _find_col(df, "On Contract")
    c_sup = _find_col(df, "C&C")
    c_hold = _find_col(df, "50% Hold")
    c_tot = _find_col(df, "CURRENT CORRECT")
    c_src = _find_col(df, "Source")
    c_note = _find_col(df, "Status / Notes", "Status/Notes", "Notes")
    _require_cols(df, "SKU_Master", {
        "SKU Code": c_code, "Product": c_prod,
        "CURRENT CORRECT Total Discount": c_tot,
    })

    skus: list[SkuRate] = []
    for _, row in df.iterrows():
        code = _text(row[c_code])
        if not code:
            continue
        skus.append(SkuRate(
            sku_code=code,
            alt_code=_text(row[c_alt]) if c_alt else "",
            brand=_text(row[c_brand]) if c_brand else "",
            product=_text(row[c_prod]),
            container=_text(row[c_cont]) if c_cont else "",
            brl_per_unit=_num(row[c_bpu]) if c_bpu else None,
            abv=_num(row[c_abv]) if c_abv else None,
            wsp_per_brl=_num(row[c_wsp]) if c_wsp else None,
            contract_base_per_brl=_num(row[c_base]) if c_base else None,
            on_contract=_text(row[c_onc]).upper().startswith("Y") if c_onc else False,
            supplier_type=_text(row[c_sup]) if c_sup else "",
            hold_per_brl=_num(row[c_hold]) or 0.0 if c_hold else 0.0,
            correct_total_per_brl=_num(row[c_tot]),
            source=_text(row[c_src]) if c_src else "",
            notes=_text(row[c_note]) if c_note else "",
        ))
    if not skus:
        raise ValueError("SKU_Master sheet produced zero SKU rows.")

    # --- Site_Master ---
    df = book["Site_Master"]
    df.columns = [str(c).strip() for c in df.columns]
    c_site = _find_col(df, "Site")
    c_acct = _find_col(df, "Tennents Account")
    c_model = _find_col(df, "Operating Model")
    c_constr = _find_col(df, "Discount Construct")
    c_note = _find_col(df, "Notes")
    _require_cols(df, "Site_Master", {
        "Site": c_site, "Tennents Account": c_acct, "Discount Construct": c_constr,
    })

    sites: list[SiteInfo] = []
    for _, row in df.iterrows():
        name = _text(row[c_site])
        account = _account_str(row[c_acct])
        # Trailing commentary rows ("ACTION: …") have no account cell at all.
        if not name or not account:
            continue
        sites.append(SiteInfo(
            account=account,
            site_name=name,
            operating_model=_text(row[c_model]) if c_model else "",
            discount_construct=_text(row[c_constr]),
            notes=_text(row[c_note]) if c_note else "",
        ))
    if not sites:
        raise ValueError("Site_Master sheet produced zero site rows.")

    # --- Site_SKU_Exceptions ---
    df = book["Site_SKU_Exceptions"]
    df.columns = [str(c).strip() for c in df.columns]
    c_site = _find_col(df, "Site")
    c_sku = _find_col(df, "SKU")
    c_prod = _find_col(df, "Product")
    c_loaded = _find_col(df, "Loaded Total Discount")
    c_correct = _find_col(df, "Correct Total Discount")
    c_dir = _find_col(df, "Direction")
    c_impact = _find_col(df, "£ Impact")
    c_status = _find_col(df, "Status")
    _require_cols(df, "Site_SKU_Exceptions", {
        "Site": c_site, "SKU": c_sku, "Loaded Total Discount": c_loaded,
    })

    exceptions: list[SkuException] = []
    for _, row in df.iterrows():
        site_name = _text(row[c_site])
        sku_raw = _text(row[c_sku])
        # The legend row ("Amber = …") has no SKU cell.
        if not site_name or not sku_raw:
            continue
        exceptions.append(SkuException(
            site_name=site_name,
            account="",  # resolved against Site_Master in reindex()
            sku_code_raw=sku_raw,
            product=_text(row[c_prod]) if c_prod else "",
            loaded_total_per_brl=_num(row[c_loaded]),
            correct_total_per_brl=_num(row[c_correct]) if c_correct else None,
            direction=_text(row[c_dir]) if c_dir else "",
            impact_gbp=_num(row[c_impact]) if c_impact else None,
            status=_text(row[c_status]) if c_status else "",
        ))

    # --- Site_Prices (OPTIONAL — the per-site tenant off-invoice layer) ---
    site_prices: list[SitePrice] = []
    site_prices_present = SITE_PRICES_SHEET in book
    if SITE_PRICES_SHEET in book:
        df = book[SITE_PRICES_SHEET]
        df.columns = [str(c).strip() for c in df.columns]
        c_site = _find_col(df, "Site")
        c_acct = _find_col(df, "Tennents Account", "Account")
        c_sku = _find_col(df, "SKU")
        c_prod = _find_col(df, "Product")
        c_off = _find_col(df, "Off-Invoice", "Off Invoice", "Tenant Off")
        c_note = _find_col(df, "Notes")
        _require_cols(df, SITE_PRICES_SHEET, {
            "Site": c_site, "SKU": c_sku, "Off-Invoice Discount": c_off,
        })
        for _, row in df.iterrows():
            site_name = _text(row[c_site])
            sku = _text(row[c_sku])
            if not site_name or not sku:
                continue
            site_prices.append(SitePrice(
                account=_account_str(row[c_acct]) if c_acct else "",
                site_name=site_name,
                sku_code=sku,
                product=_text(row[c_prod]) if c_prod else "",
                off_invoice_per_brl=_num(row[c_off]) or 0.0,
                notes=_text(row[c_note]) if c_note else "",
            ))

    return TennentsMaster(
        version=version,
        source=source_name,
        skus=skus,
        sites=sites,
        exceptions=exceptions,
        site_prices=site_prices,
        site_prices_present=site_prices_present,
    )


# ---------- bar plan changes (one site, one product) ----------
#
# A "bar plan change" is the area manager agreeing a product's off-invoice
# split with one tenant (Nick Madigan, Scotland). It changes ONE Site_Prices
# row and nothing else: the agreed TOTAL stays on SKU_Master, so the retro is
# whatever the off-invoice leaves. Applied from /tennents/bar-plan, so a change
# no longer needs the master workbook edited and re-uploaded.

# Rows written from the bar plan page carry this prefix in source_file. They
# exist only in Airtable, so replace_tennents_master keeps them across a
# workbook re-upload (the findings: precedent for SKU rows).
BAR_PLAN_SOURCE_PREFIX = "bar plan:"

# Off-invoice £/brl tolerance for the monthly split check — the same ±£0.50
# rounding allowance the total-discount check uses.
SPLIT_TOLERANCE = 0.50


@dataclass
class BarPlanChange:
    """What a bar plan change would do, before it is written."""
    account: str
    site_name: str
    sku_code: str                 # canonical
    product: str
    total_per_brl: float
    current_off: float | None     # None = no Site_Prices row yet
    new_off: float
    wsp_per_brl: float | None
    keg_brl: float
    warnings: list[str] = field(default_factory=list)

    @property
    def new_retro(self) -> float:
        return round(self.total_per_brl - self.new_off, 2)

    @property
    def current_retro(self) -> float | None:
        return None if self.current_off is None else round(self.total_per_brl - self.current_off, 2)

    @property
    def removing(self) -> bool:
        return self.new_off == 0.0

    def net_keg(self, off: float | None) -> float | None:
        """Tenant's invoiced price per keg at an off-invoice figure (WSP − off) ×
        barrels per keg; None without a WSP or an off-invoice."""
        if self.wsp_per_brl is None or off is None:
            return None
        return round((float(self.wsp_per_brl) - off) * self.keg_brl, 2)


def plan_bar_plan_change(master: TennentsMaster, account: str, sku_code: str,
                         off_invoice) -> BarPlanChange:
    """Validate one bar plan change and describe it. Raises ValueError with a
    sentence for the operator when it cannot be applied.

    Refused: an unknown site or product; a managed site (it takes the whole
    discount off-invoice by rule, so it has no split to set); a product with no
    agreed total (RATE TBC — the retro would be unknowable); an off-invoice
    below £0 or above the agreed total (a negative retro). £0 is allowed and
    means "not sold here / tenant pays full WSP". A bespoke flat-retro site
    (Gartocher) is allowed but warned when the figure breaks its flat retro."""
    acct = str(account or "").strip()
    site = master.site_for_account(acct)
    if site is None:
        raise ValueError(f"No Tennents site with account {acct!r} on the master")
    if site.is_managed:
        raise ValueError(f"{site.site_name} is managed: it takes the whole discount off-invoice, "
                         "so there is no split to set")
    sku = master.find_sku(sku_code)
    if sku is None:
        raise ValueError(f"Product {str(sku_code).strip()!r} is not on SKU_Master — add it to the "
                         "master first (with its agreed total discount)")
    if sku.correct_total_per_brl is None:
        raise ValueError(f"{sku.sku_code} {sku.product} has no agreed total discount (RATE TBC) — "
                         "set the rate first, or the retro can't be worked out")
    try:
        off = float(str(off_invoice).replace("£", "").replace(",", "").strip())
    except (TypeError, ValueError):
        raise ValueError("Off-invoice must be a figure in £ per barrel (0 for not sold here)") from None
    if off != off or off in (float("inf"), float("-inf")):
        raise ValueError("Off-invoice must be a figure in £ per barrel")
    off = round(off, 2)
    total = round(float(sku.correct_total_per_brl), 2)
    if off < 0:
        raise ValueError("Off-invoice can't be negative")
    if off > total + 0.005:
        raise ValueError(f"Off-invoice £{off:,.2f}/brl is more than the agreed total £{total:,.2f}/brl "
                         f"for {sku.product or sku.sku_code} — FB's retro would be negative")

    existing = master.site_price(acct, sku.sku_code)
    warnings: list[str] = []
    flat = site.flat_retro_per_brl
    if flat is not None and off > 0 and abs((total - off) - flat) > 0.005:
        warnings.append(f"{site.site_name} is on a flat £{flat:,.2f}/brl retro; this leaves "
                        f"£{total - off:,.2f}/brl retro (off-invoice £{total - flat:,.2f} would keep the flat retro)")
    if sku.wsp_per_brl is None:
        warnings.append("No WSP on the master for this product, so the net keg price can't be shown")
    return BarPlanChange(
        account=acct, site_name=site.site_name, sku_code=sku.sku_code,
        product=sku.product or sku.brand, total_per_brl=total,
        current_off=(None if existing is None else round(float(existing.off_invoice_per_brl or 0.0), 2)),
        new_off=off, wsp_per_brl=sku.wsp_per_brl, keg_brl=keg_brl_factor(sku),
        warnings=warnings,
    )


def expected_off_invoice(master: TennentsMaster, account: str, sku_code: str,
                         total_charged: float) -> tuple[float | None, str]:
    """The off-invoice £/brl our file says Tennents should be giving the tenant
    on one delivery, with the basis. None = not judged:
      - managed site (whole discount off-invoice; its own check covers it);
      - bespoke flat-retro site: expected = the line's total − the flat retro;
      - otherwise the Site_Prices row; no row → None ("no split on file")."""
    site = master.site_for_account(account)
    if site is None or site.is_managed:
        return None, "managed" if site is not None else "unknown site"
    flat = site.flat_retro_per_brl
    if flat is not None:
        return round(total_charged - flat, 2), f"flat £{flat:,.2f}/brl retro"
    sp = master.site_price(account, sku_code)
    if sp is None:
        return None, "no split on file"
    return round(float(sp.off_invoice_per_brl or 0.0), 2), "our price file"
