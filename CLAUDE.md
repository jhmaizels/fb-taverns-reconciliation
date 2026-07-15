# CLAUDE.md — fb-taverns-reconciliation

> Onboarding doc for a fresh Claude Code session. The original build session is
> lost; this file is the source of truth for how the system works. Function,
> table, and field names below are real — match them exactly.

## 1. Project overview

This is a **pricing reconciliation** system for **FB Taverns**, a pub/tavern
estate. Suppliers invoice tenants for draught products (kegs/barrels); the
estate maintains agreed **tenant prices**, **FB list prices**, and **retro
rebates** per (site, product). This tool checks that what suppliers actually
charged matches what was agreed, and flags every discrepancy as a typed
**Mismatch** for an operator to chase.

It reconciles three supplier/data flows:

- **LWC weekly sales** (England estate) — line-level delivery invoices vs the
  effective-dated pricing master. The primary flow.
- **LWC monthly retro** — per-keg retrospective rebate rates vs agreed retros.
- **Tennents Direct** (Scotland estate) — its own master + monthly draught
  pricing, joined by account + SKU.

Plus operator-facing **master-update** flows that mutate the pricing master,
and an LLM-assisted **tenant-support** override flow.

The point: catch wrong tenant prices, wrong FB/list prices, missing/unknown
items, managed-site margin leakage, retro under/overpayments, and LWC/Tennents
arithmetic errors — across a weekly/monthly operating cadence.

> Note: this repo is **separate** from `lp-bot` / `prediction_bot`. None of the
> lp-bot safety rules, Flask references, or EC2/Render-prod conventions from
> those projects apply here.

## 2. Stack & where it runs

- **Python 3.12** (Render pins `PYTHON_VERSION=3.12.7`).
- **Web framework: FastAPI + uvicorn** (NOT Flask). ASGI app object is
  `app` in `webapp.py`; started with `uvicorn webapp:app`.
- **Persistence: Airtable** (base `appyDA69D2YhdpsA4`) is the **system of
  record** — there is no SQL DB. Raw Airtable REST via `requests`.
- **Excel** I/O via pandas + openpyxl.
- **Anthropic SDK** (`anthropic`) for the NL tenant-support parser only.
- **Deploy: Render** web service named **`fb-taverns-reconcile`** (Blueprint in
  `render.yaml`, region frankfurt, plan starter). Deploys from the GitHub repo
  main branch.

Two run modes: (1) **local CLI** via `reconcile.py` (build-master / reconcile),
documented in `README.md`; (2) **cloud** via the Render web service.

## 3. Architecture & data flow

Thin layering. `webapp.py` is orchestration/UI only; all parsing, business
logic, and Airtable I/O live in imported modules.

Standard upload pipeline (every web upload route):

```
.xlsx upload → tempfile (Render has no persistent disk)
  → load master from Airtable
  → parse the xlsx
  → reconcile (in-memory)
  → write findings to Airtable
  → os.unlink tempfile (finally)
  → render HTML summary
```

Module map:

- `reconcile.py` — core LWC reconcile engine + the local CLI (`build-master`,
  `reconcile`).
- `airtable_io.py` — **the only** module that talks to Airtable. All reads and
  writes.
- `airtable_schema.json` — table-id map (generated, committed, never hand-edit).
- `webapp.py` — FastAPI app, routes, HTML.
- `retro.py` — LWC monthly retro flow.
- `tennents.py` — Tennents Direct reconciliation engine + HTML renderer
  (library, **no CLI**).
- `tennents_master.py` — parser + rate-resolution for the Tennents master
  workbook (`FB_Taverns_Tennents_Master.xlsx`), the primary Tennents price file.
- `support_parser.py` — NL → structured support rule via Claude tool-use.
- `summary.py` — builder/renderer for the main LWC weekly summary.
- `master_export.py` — read-only Airtable → wide-form FB cost Excel export.
- `setup_airtable.py` / `migrate_csv_to_airtable.py` — one-off bootstrap scripts.
- `setup_tennents_tables.py` / `load_tennents_master.py` — one-off bootstrap:
  create the three Tennents master tables (+ Files volume fields), then load
  the workbook from the CLI.

**Join key is `site_id`** throughout LWC (the LWC `SITE ID` column), NOT account
number. `ACCOUNT_BRIDGE` exists but is empty/diagnostic and is NOT applied in
reconcile. Tennents joins by `(account, sku_code)` instead.

## 4. The Airtable data model

Tables (ids from `airtable_schema.json`):

| Table | id | Key fields |
|---|---|---|
| Sites | `tblVBCkWzNj1R0Zdz` | site_id, name, status, country, notes |
| Products | `tblwpqsGjeOi6Tl1y` | product_code, description, supplier, retro_eligible, retro_per_keg |
| Files | `tblQ5oUQfQgAI2k8s` | file_name, supplier, received_at, line_count, parse_status, raw_hash, stored_path, period_month, barrels_total, tlager_barrels |
| PricingRules | `tblBonamwCgnfmX5G` | rule_key, site, product, tenant_price, fb_price, retro_pct, valid_from, valid_to, status, reason, source |
| Mismatches | `tbl6zLeJSip11hXYa` | mismatch_key, type, severity, file, site, product, rule, invoice_no, invoice_date, qty, delta_per_unit, delta_total, expected/actual_tenant_price, expected/actual_fb_price, status, notes |
| TennentsSkuMaster | (airtable_schema.json) | sku_code, alt_code, brand, product, container, brl_per_unit, abv, wsp_per_brl, contract_base_per_brl, on_contract, supplier_type, hold_per_brl, correct_total_per_brl, source, notes, version, source_file |
| TennentsSiteMaster | (airtable_schema.json) | account, site_name, operating_model, discount_construct, notes, version, source_file |
| TennentsSiteSkuExceptions | (airtable_schema.json) | exception_key, site_name, account, sku_code (raw, may be compound "400751/400557"), product, loaded_total_per_brl, correct_total_per_brl, direction, impact_gbp, status, resolved, version, source_file |
| TennentsAgreements | `tblxTZEY5H7WNfien` | **RETIRED 2026-07** (historical record only — the master workbook superseded the per-account Commercial Data agreements; nothing reads or writes this table) |

Key facts:

- **Link fields are arrays of record ids.** `PricingRules.site` / `.product`;
  `Mismatches.file` / `.site` / `.product` / `.rule`. Reads take `[0]`.
- **ALL findings (LWC, Tennents, retro) write to the single `Mismatches`
  table**, differentiated by the `type` singleSelect. There is no separate
  retro or Tennents findings table. Types: LWC (`wrong_tenant_price`,
  `wrong_fb_price`, `site_should_be_managed`, `unknown_site`,
  `product_not_on_master`, `tenant_price_missing`, `lwc_arithmetic_error`);
  Tennents (`tennents_wrong_discount`, `_exception_pending`,
  `_exception_resolved`, `_retro_arithmetic`, `_line_arithmetic`,
  `_managed_retro_split`, `_no_agreed_rate`, `_not_on_master`,
  `_new_customer`); retro (`retro_under_paid`, `_over_paid`,
  `_paid_not_on_master`). The pre-2026-07 Tennents types
  (`tennents_wrong_invoice`, `_wrong_fb_price`) are no longer produced but
  survive on historical rows.
- **PricingRules.rule_key** = `site|product|YYYY-MM-DD` (or `...|open` when no
  valid_from). This is the upsert/dedupe key.
- **Files dedupe by `raw_hash`** (sha256). Re-uploading the same file returns
  the existing record id (idempotent).
- `Mismatches.mismatch_key` uniqueness: LWC `fileid|NNNN|site|product|invoice|type`;
  Tennents `fileid|tennents|prefix|bits`; retro `fileid|retro|prefix|code`.
- All writes go through `_batch` (chunks of `BATCH_SIZE=10`, `typecast:True`,
  0.25s sleep between chunks). `typecast:True` auto-creates new singleSelect
  options on the fly.
- The schema `base_id` in JSON is informational; `AIRTABLE_BASE_ID` env is what
  `DATA_URL` actually uses.

## 5. The reconciliation flows

### 5a. LWC weekly sales — `POST /upload`
- Inputs: LWC weekly sales `.xlsx` (line-level). `parse_lwc_sales` →
  `InvoiceLine` list. Line sheet picked by `_find_line_sheet` (prefers
  `FB_Taverns_Del_Date` / `Del_Date`, else first sheet with `Date` and not
  `Pivot`). Older `Diff. From Master` format is **unsupported** (hard exit).
- Master: `load_rules_from_airtable()` + `load_sites_from_airtable()`.
- Match: `_index_rules` builds `{(site_id, product_code): [rules sorted newest
  valid_from first]}`; `reconcile_lines` compares each line.
- Output: `write_mismatches` → Mismatches; `summary.build_summary` +
  `render_summary_html`. Local CLI writes `outputs/<stem>__mismatches.csv`.

### 5b. LWC monthly retro — `POST /upload-retro`
- Inputs: monthly "Rate Per Keg" `.xlsx` → `retro.parse_lwc_retro` →
  `RetroLine`. Master: `load_agreed_retros()` (Products.retro_per_keg).
- Match: `build_retro_summary` groups by product_code, compares each line's
  `rate_per_keg` vs `agreed_retro`. Threshold `RETRO_THRESHOLD=0.005` (½p/keg,
  tighter than LWC weekly).
- Output: `write_retro_findings` → Mismatches (under/over/paid-not-on-master
  only; multi-rate and agreed-not-delivered are diagnostic, NOT persisted).

### 5c. Tennents Direct monthly — `POST /upload-tennents`
- Master: the **`FB_Taverns_Tennents_Master.xlsx` workbook is the primary
  Tennents price file** (operator direction 2026-07-14). `load_tennents_master()`
  → `TennentsMaster` (SKU rates + sites + exceptions from the three tables).
  The workbook's own README sheet §4 is the reconciliation spec.
- Inputs: monthly Draught Pricing Report ("Data" sheet) →
  `tennents.parse_monthly` → `MonthlyReport` (Kegs>0 lines checkable;
  Kegs≤0 kept for volume only; raises on zero lines). Report discounts are
  NEGATIVE — normalised to positive file-wide.
- Match: `tennents.reconcile` checks **per line** (no mean-averaging):
  expected total discount from SKU_Master unless a Site_SKU_Exceptions row
  overrides (see §6), ±£0.50/brl; retro-due exactness; line arithmetic;
  managed/bespoke constructs. Join is account + raw SKU code (alt codes
  resolve via the SKU index).
- Output: `write_tennents_findings` → Mismatches; Files row gets
  `period_month`/`barrels_total`/`tlager_barrels` for the /tennents
  barrelage-vs-2,700 panel and the annual-retro claim-window alarm.

### 5d. Tenant-support override — `POST /add-support` (WRITE-TO-MASTER)
- Inputs: operator free text. `support_parser.parse_support_request` calls
  Claude (model `claude-opus-4-7`, forced tool `create_support_rule`, requires
  `ANTHROPIC_API_KEY`) → `{site_id, product_code, new_tenant_price, valid_from,
  valid_to, reason}`. `validate_support_fields` checks against master.
- Builds one `Rule(status='supported', tenant_price=new, retro_pct=0.0)`;
  `fb_price`/`product_desc` copied from the existing active standard rule so
  wrong_fb_price still fires inside the support window. Persisted with
  `close_keys_at_date=None` so the standard rule resumes after `valid_to`.

### 5e. Master updates (WRITE-TO-MASTER)
- **`POST /upload-master`** (LWC): `parse_fb_cost_file` → rules stamped
  `valid_from=vf`; `upsert_pricing_rules(rules, close_keys_at_date=vf)` (closes
  prior open rules for reappearing keys) + `upsert_products_with_retros`.
- **`POST /upload-tennents-master`**: `tennents_master.parse_master_workbook`
  → `replace_tennents_master` **WIPES the three Tennents master tables** then
  recreates them from the workbook. The workbook is the editing surface (its
  README §5): edit → bump version → re-upload. Exceptions can also be retired
  by ticking `resolved` in Airtable (no re-upload needed).
- **`GET /export-master`**: `master_export.build_master_xlsx_bytes()` →
  download (read-only).

## 6. Business rules that matter

**Rule/price selection (`_lookup_rule`):** key on `(site_id, product_code)`,
then pick the rule whose date range contains `invoice_date`, **half-open**:
`valid_from <= date < valid_to` (defaults: `valid_from`→date.min,
`valid_to`→date.max). Index pre-sorts **newest valid_from first**, so
overlapping rules resolve to **most-recent**. If `invoice_date` is None → newest
rule. A line dated exactly on a rule's `valid_to` falls into the next rule.

**Tolerances (LWC):**
- tenant_price: `tolerance` default **0.01** (1p/unit).
- fb_price: `fb_tolerance` default **0.05** (5p/unit, looser — LWC MASTER column
  is 2dp-rounded while list price may be sub-penny).
- LWC arithmetic check: fixed **0.02**.
- Comparisons are on absolute per-unit delta; `delta_total = delta_per_unit * qty`.

**Per-line checks, in order:**
1. `lwc_arithmetic_error` if `abs((unit-master)*qty - diff_master) > 0.02`.
2. Rule lookup. If **no rule**: managed site → `site_should_be_managed` (when
   `abs(unit-master)>tolerance`); else no active rules for site →
   `unknown_site` (medium); else product not in active master →
   `product_not_on_master` (medium); else → `tenant_price_missing` (medium).
   If rule found and `status=='managed'` → `site_should_be_managed` (skips price
   checks).
3. `wrong_tenant_price` if tenant_price set and `abs(unit-tenant_price)>tolerance`
   (if `status=='supported'`, a support_note is attached).
4. `wrong_fb_price` if fb_price set and `abs(master-fb_price)>fb_tolerance`.
   Steps 3 and 4 are independent — both can fire on one line.

**`fb_price` = LIST price** (FB cost file col 2), NOT Net price (col 4,
post-retro). Net price is deliberately ignored. `retro_pct = retro_per_keg /
list_price`, stored to **10dp on purpose** (so `retro_per_keg = retro_pct *
fb_price` round-trips to source pennies) — do not round it.

**Site status:** `tenanted` (default), `managed`, `supported`. **Managed**
sites expected to have zero margin (unit should equal MASTER); margin →
`site_should_be_managed`, using `tolerance` not `fb_tolerance`. Managed handling
appears in **two** places: no-rule + sites.csv/Sites.status `managed`, and a
matched rule with `status=='managed'`.

**Severity (`_severity`, £ on delta_total):** `<0.05` low, `<0.50` medium, else
high. unknown_site / product_not_on_master / tenant_price_missing are hardcoded
`medium` (no £ basis).

**"On the master" membership** counts only currently-active rules (`valid_to is
None`), so superseded/dropped products don't look present.

**Retro logic:** per-LINE classification — `under` where `(agreed-rate) >
threshold`, `over` where `(rate-agreed) > threshold`; the same product can
appear in BOTH. under total_delta is negative, over positive. If `agreed<=0`
and `rate>0` → `paid_not_on_master`. multi_rate only triggers on >1 distinct
NON-ZERO rate.

**Tennents (per the master workbook's own README §4 — the spec):**
- **Expected total discount** for a line = SKU_Master `correct_total_per_brl`
  **unless** an open Site_SKU_Exceptions row overrides it, in which case the
  exception's **Loaded** value is expected-current until the exception is
  resolved. Exceptions key on **(account, RAW sku code)** — a mis-load on the
  30L code says nothing about the 50L; a row covering both containers lists
  both codes (`"400751/400557"`). SKU-rate lookup, by contrast, resolves alt
  codes (`09000X` → `090425`).
- **Exception line outcomes:** actual ≈ Loaded → `tennents_exception_pending`
  (known state persists; NOT re-flagged as a mismatch; `(correct − actual) ×
  barrels` accrues as "short vs correct"); actual ≈ Correct →
  `tennents_exception_resolved` (Tennents' fix landed — mark resolved in the
  workbook and re-upload); neither → `tennents_wrong_discount` vs the Loaded
  value.
- **Tolerance** `TENNENTS_DISCOUNT_TOLERANCE=0.50` £/brl, inclusive (Δ of
  exactly 0.50 passes). `delta_per_brl = expected − actual` on positive-
  normalised values; **positive Δ = tenant/FB got LESS discount (short)**.
- **Retro exactness:** `Retro Due` must equal `retro £/brl × barrels` EXACTLY —
  `RETRO_EXACT_TOLERANCE=0.005` (to the penny). Only checked when the report
  has the column.
- **Line arithmetic:** `off + retro + AOD == total` per line (0.005).
- **Managed sites** (Site_Master operating model): zero retro + full discount
  off-invoice is **CORRECT** — never flagged. A retro split at a managed site →
  `tennents_managed_retro_split` (cash-timing review, not a value error).
- **Bespoke constructs** (e.g. Gartocher, flat £200/brl retro): only the TOTAL
  is validated — true for every site, since the master only carries totals; the
  OID/retro split is never checked against the master.
- **No agreed rate** (SKU_Master row with blank CURRENT CORRECT) →
  `tennents_no_agreed_rate` when delivered without an exception.
- Master data-quality (`base + hold ≠ CURRENT CORRECT`, >0.05) and WSP variance
  (implied gross vs WSP, >£1/brl) are **HTML-only** — never persisted.
- Volumes: barrels sum over ALL lines (returns net off); T.Lager identified by
  brand (`_is_tlager`, apostrophe-insensitive) with the 090425 code fallback.

**Severity (Tennents/retro):** Tennents discount high `>=100` / medium `>=10`
/ low (£ on delta_total); retro-arithmetic high `>=50` / medium `>=5` / low;
exception_pending low; everything else medium. Retro under high `>=50` /
medium `>=5` / low; over and paid-not-on-master medium.

## 7. Web routes (all auth via HTTP Basic except where noted)

| Route | Auth | Purpose | Writes master? |
|---|---|---|---|
| `GET /healthz` | no | Render liveness `{status: ok}` | — |
| `GET /version` | no | RENDER_GIT_COMMIT / _BRANCH | — |
| `GET /` | yes | Estate picker (LWC=England, Tennents=Scotland) | — |
| `GET /lwc` | yes | LWC landing + 4 forms (master Effective-from default = today−14d) | — |
| `POST /upload` | yes | LWC weekly reconcile | findings only |
| `POST /upload-retro` | yes | LWC monthly retro reconcile | findings only |
| `GET /export-master` | yes | Download master as .xlsx (read-only) | — |
| `POST /upload-master` | yes | **LWC master replace** | **YES** |
| `POST /add-support` | yes | **NL tenant-support rule** (LLM) | **YES** |
| `GET /tennents` | yes | Tennents landing (+ barrelage vs 2,700 + annual-retro claim alarm) | — |
| `GET /tennents/master` | yes | Read-only browse of the master workbook mirror | — |
| `POST /upload-tennents-master` | yes | **Tennents master workbook WIPE+replace** (admin) | **YES** |
| `POST /upload-tennents` | yes | Tennents monthly reconcile | findings only |

Auth: single shared credential, `WEB_USERNAME` (default `admin`) /
`WEB_PASSWORD`, constant-time compare. **If `WEB_PASSWORD` is unset the whole
app returns 503 (fail-closed).** Pages are hand-built f-strings (no template
engine).

## 8. Local dev & deploy

**Env vars (same names everywhere):**
- `AIRTABLE_TOKEN` (PAT `pat...`) — secret, sync:false in Render.
- `AIRTABLE_BASE_ID` (`appyDA69D2YhdpsA4`) — sync:false.
- `WEB_USERNAME` (default `admin`), `WEB_PASSWORD` (secret, sync:false).
- `ANTHROPIC_API_KEY` — only needed for `/add-support`.
- `PYTHON_VERSION=3.12.7`.

**Local CLI:**
```
python reconcile.py build-master ...        # FB cost xlsx + tenant folder → master_pricing.csv (+ sites.csv); --to-airtable optional
python reconcile.py reconcile sales.xlsx    # → outputs/<stem>__mismatches.csv; --use-airtable reads master from + writes findings to Airtable
```
Defaults: master `master_pricing.csv`, sites `sites.csv`. `--use-airtable`
ignores the CSV args. `parse_lwc_sales`/`_find_line_sheet` and missing master
call `sys.exit` (hard exit, not exception).

**Cloud deploy (`render.yaml`):** web service `fb-taverns-reconcile`, runtime
python, plan starter, region frankfurt. `buildCommand = pip install -r
requirements.txt`; `startCommand = uvicorn webapp:app --host 0.0.0.0 --port
$PORT`; `healthCheckPath = /healthz`. The three secrets are sync:false — **first
deploy is EXPECTED to fail until they are set in the Render dashboard.**

**One-time bootstrap:** `setup_airtable.py` (creates 5 tables via Metadata API,
writes `airtable_schema.json`) → `migrate_csv_to_airtable.py` (loads sites.csv +
master_pricing.csv). Both idempotent; use raw `requests`, BATCH_SIZE=10,
typecast, 0.25s sleeps.

## 9. CRITICAL / safety

- **Airtable base `appyDA69D2YhdpsA4` is the source of truth.** No SQL DB.
- **WRITE-TO-MASTER (riskier) flows that MUTATE the pricing master:**
  - `POST /upload-master` — closes + replaces LWC PricingRules.
  - `POST /add-support` — creates a PricingRule from an LLM parse.
  - `POST /upload-tennents-master` — **DELETES ALL** rows in the three
    Tennents master tables then recreates them from the uploaded workbook.
  There is **no dry-run / confirmation step** beyond the form submit. Treat
  these as the high-blast-radius operations.
- **Findings-only (safer) flows** that write only to the Mismatches table:
  `POST /upload`, `POST /upload-retro`, `POST /upload-tennents`.
- The Excel cost file (uploaded/downloaded via the master buttons) is the
  operator's single source of truth for prices; the three `AIRTABLE_*.md` docs
  (Form/Interface/Automation) are **SUPERSEDED** — do not treat them as current.
- Never echo or commit `AIRTABLE_TOKEN` / `WEB_PASSWORD` / `ANTHROPIC_API_KEY`.
  Real secrets live only in Render env / `.env`.
- `airtable_schema.json` is generated and committed — **never hand-edit**.

## 10. Known issues & gotchas

**Performance (from a prior Step-1 diagnosis — not yet profiled; Airtable key is
Render-only):** the reconcile is **NOT** an N+1-per-row pattern — reads are
bulk-loaded, matching is in-memory, writes are batched. The real inefficiency is
**redundant full-table Airtable fetches per upload**: Sites fetched ~3×,
Products ~2×, PricingRules ~2×, because `load_rules` + `load_sites` +
`write_mismatches` link-resolution each re-fetch independently. There is **no
caching of master tables across uploads**, plus the **0.25s/chunk write
throttle** and Airtable's own paginated, rate-limited API as the floor. Render
has shown intermittent **health-check timeouts (5s)** consistent with a long
upload blocking the worker.

**Other gotchas:**
- Errors in `_list_all` / `_batch` call **`sys.exit()`** (terminate process),
  not raise — but `replace_tennents_master`'s DELETE loop uses
  `raise_for_status()` instead (inconsistent).
- Airtable rate limit ~5 req/s. Writes/deletes throttled to ≤4 req/s; **reads
  are NOT throttled** (back-to-back paginated GETs) — large tables could brush
  the limit.
- `write_tennents_findings` **repurposes** the `expected_fb_price` /
  `actual_fb_price` columns to carry discount £/Brl values for the
  discount-shaped Tennents types (wrong_discount, exception_pending/resolved,
  no_agreed_rate) — not actual FB prices.
- `typecast:True` on every write auto-creates new singleSelect options — typos
  in a `type` value silently create a new option.
- `parse_fb_cost_file` is **positional**: header row index 1, data from row 2,
  cols 0–4 fixed (code/name/list/retro/net), sites from col 5; site_id is the
  first standalone 3-digit token `\b(\d{3})\b` per header cell (excludes e.g.
  `FW1615`). A site whose id isn't a standalone 3-digit token is missed.
- In `build_master`, tenant-folder prices **override** the FB cost tenant_price
  for matching keys but only mutate existing rules' tenant_price/desc/source;
  tenant-only rules get no fb_price/retro_pct.
- `valid_to` closing only closes rules whose key **reappears** in the new batch
  AND are currently open; products dropped from the new master are NOT closed
  (stay `valid_to=None`, remain "active" for membership).
- Tennents `reconcile` is **per line** (the old mean-averaging is gone), but
  findings are BUCKETED for display/persistence: discount mismatches by
  `(account, canonical sku, expected, actual)`, exception rows by
  `(account, exception's raw sku listing)`. Tennents account regex matches ANY
  parenthesised digits anywhere; SKU regex matches only the LAST parenthesised
  group at end-of-string — stray parens mis-extract.
- Tennents monthly reports carry discounts/Retro Due as **NEGATIVE** numbers;
  `parse_monthly` normalises sign file-wide (sum of totals < 0 → negate), so a
  future positive-convention export still parses. The master holds positives.
- The Tennents `version` string lives on every row of the three master tables
  (from the workbook's README sheet "7. Version" row); `get_tennents_master_info`
  reads it for the banner. The old TennentsAgreements table still exists but is
  dead code — do not resurrect `load_tennents_agreements`.
- LWC `ACCOUNT` column renamed `OUTLET` in Apr-2026+ files; both map to
  `account_name`. Older `Diff. From Master` workbook format unsupported.
- HTML: filenames in `/upload` and `/upload-retro` success pages are NOT
  `html.escape`d (latent reflected-HTML concern); error pages inject raw
  tracebacks in `<pre>` by design.
- `tennents.py` has **NO CLI** — no argparse/`__main__`; it's a library imported
  by `webapp.py`. Do not look for `python tennents.py`.
- `support_parser` hardcodes model `claude-opus-4-7`, ~$0.04/call, no caching
  (~10 calls/yr); missing `ANTHROPIC_API_KEY` raises KeyError (caught in webapp
  to tell the operator).
- `master_export` is read-only; sheet row 1 is intentionally blank, a second
  `Info` sheet carries as-of metadata, `freeze_panes='C3'`; net price =
  fb_price − retro_per_keg.
- README's pip install line (mentions `pyairtable`) is **out of sync** with
  `requirements.txt`; the code uses raw `requests`, not pyairtable.
- Airtable Free tier caps (~1,000 records/base, 100 automation runs/month) are
  exceeded within ~2 months of weekly files (per DEPLOY.md) — upgrade to Team.
- Three identically-named `render_summary_html` (tennents.py + summary.py) and
  three `_money`/`_money_neutral` copies exist — don't confuse imports.
- Unreachable `return 2` after `sys.exit` in the build-master valid_from guard.
