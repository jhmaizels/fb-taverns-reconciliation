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
- `tennents.py` — Tennents Direct flow (library, **no CLI**).
- `support_parser.py` — NL → structured support rule via Claude tool-use.
- `summary.py` — builder/renderer for the main LWC weekly summary.
- `master_export.py` — read-only Airtable → wide-form FB cost Excel export.
- `setup_airtable.py` / `migrate_csv_to_airtable.py` — one-off bootstrap scripts.

**Join key is `site_id`** throughout LWC (the LWC `SITE ID` column), NOT account
number. `ACCOUNT_BRIDGE` exists but is empty/diagnostic and is NOT applied in
reconcile. Tennents joins by `(account, sku_code)` instead.

## 4. The Airtable data model

Tables (ids from `airtable_schema.json`):

| Table | id | Key fields |
|---|---|---|
| Sites | `tblVBCkWzNj1R0Zdz` | site_id, name, status, country, notes |
| Products | `tblwpqsGjeOi6Tl1y` | product_code, description, supplier, retro_eligible, retro_per_keg |
| Files | `tblQ5oUQfQgAI2k8s` | file_name, supplier, received_at, line_count, parse_status, raw_hash, stored_path |
| PricingRules | `tblBonamwCgnfmX5G` | rule_key, site, product, tenant_price, fb_price, retro_pct, valid_from, valid_to, status, reason, source |
| Mismatches | `tbl6zLeJSip11hXYa` | mismatch_key, type, severity, file, site, product, rule, invoice_no, invoice_date, qty, delta_per_unit, delta_total, expected/actual_tenant_price, expected/actual_fb_price, status, notes |
| TennentsAgreements | `tblxTZEY5H7WNfien` | agreement_key, account, customer_name, sku_code, sku_desc, tenant_invoice, fb_net_price, off_invoice_per_brl, retro_per_brl, total_per_brl, source |

Key facts:

- **Link fields are arrays of record ids.** `PricingRules.site` / `.product`;
  `Mismatches.file` / `.site` / `.product` / `.rule`. Reads take `[0]`.
- **ALL findings (LWC, Tennents, retro) write to the single `Mismatches`
  table**, differentiated by the `type` singleSelect. There is no separate
  retro or Tennents findings table. Types: LWC (`wrong_tenant_price`,
  `wrong_fb_price`, `site_should_be_managed`, `unknown_site`,
  `product_not_on_master`, `tenant_price_missing`, `lwc_arithmetic_error`);
  Tennents (`tennents_wrong_invoice`, `_wrong_fb_price`, `_wrong_discount`,
  `_not_on_master`, `_new_customer`); retro (`retro_under_paid`, `_over_paid`,
  `_paid_not_on_master`).
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
- Inputs: monthly Draught Pricing Report ("Data" sheet) →
  `tennents.parse_monthly` → `DeliveryLine` (filters Kegs>0; raises on zero).
  Master: `load_tennents_agreements()` → `Agreement` keyed `(account, sku_code)`.
- Match: `tennents.reconcile` aggregates deliveries per `(account, sku)` —
  **sums kegs/barrels, takes simple MEAN of rates** — then 3 per-key checks
  (invoice, FB net price, total discount) + 3 set-difference findings.
- Output: `write_tennents_findings` → Mismatches.

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
- **`POST /upload-tennents-master`**: `parse_tennents_master` →
  `replace_tennents_master` **WIPES ALL TennentsAgreements** then recreates.
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

**Tennents:** two tolerances — `TENNENTS_PRICE_TOLERANCE=0.05` (per-keg unit
prices: tenant invoice AND FB net), `TENNENTS_DISCOUNT_TOLERANCE=0.50` (per-Brl
discount). Discount section is the headline: `delta_total = delta_per_brl *
barrels`; positive Δ = tenant got LESS discount than master. FB-price mismatches
bucket by `(sku, round(expected,4), round(actual,4))` collapsing identical
errors across sites. Master arithmetic flags `|total - (off+retro)| > 0.05`.

**Severity (Tennents/retro):** Tennents invoice high if `|delta_per_unit*kegs|
>=50`; discount high `>=100` / medium `>=10` / low; fb-price/not-on-master/
new-customer medium. Retro under high `>=50` / medium `>=5` / low; over and
paid-not-on-master medium.

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
| `GET /tennents` | yes | Tennents landing | — |
| `POST /upload-tennents-master` | yes | **Tennents master WIPE+replace** | **YES** |
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
  - `POST /upload-tennents-master` — **DELETES ALL** TennentsAgreements then
    recreates.
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
  discount-mismatch type (not actual FB prices).
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
- Tennents `reconcile` averages rates as **simple mean** (sum/n), not
  keg-weighted — blended averages can sit just inside tolerance. Tennents
  account regex matches ANY parenthesised digits anywhere; SKU regex matches
  only the LAST parenthesised group at end-of-string — stray parens mis-extract.
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
