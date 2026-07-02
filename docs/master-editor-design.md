# Master Editor — Build Design (v1)

**Repo:** `C:/dev/fb-taverns-reconciliation` · **Target doc path:** `docs/master-editor-design.md` · **Base commit:** `54f82f0` (working tree clean)

**Status correction up front:** the brief's "known bug to fix" — the dead `valid_from` close-pass guard in `upsert_pricing_rules` — is **already fixed and merged to main**. Commit `5ef92db` ("fix(master): fetch valid_from so the rule-closing guard goes live", merged via PR #1 `c8b1842`, ancestor of `54f82f0`) added `valid_from` to the close-pass projection at `airtable_io.py:482`, and the guard at `airtable_io.py:510-516` is live. A committed offline regression test exists: `test_upsert_close_guard.py` (4 cases; its fake `_list_all` honours the `fields=` projection, so re-dropping the field re-fails the test). This build **verifies and extends** that test; it does not re-fix the bug. See §2.4.

---

## 1. Scope

### 1.1 In scope (v1)

An in-app editor for the **LWC pricing master** (Airtable `PricingRules` + linked `Sites`/`Products`), in the app's existing server-rendered f-string style (no JS framework):

1. **Browse/search rules** — filterable grid over the cached master snapshot: site, product, free-text, status, and an "effective on date D" / open / ended / future view.
2. **Change a price effective from a date** — the standard forward change: close the current open rule at date D, create the successor from D. One call to the existing `upsert_pricing_rules(rules, close_keys_at_date=D)` (`airtable_io.py:462-555`).
3. **Fix a mistake in place (retro-correction)** — rewrite an existing rule's figures keeping its `rule_key`/`valid_from`. Explicitly labelled as history-rewriting.
4. **Add a rule** — new (site, product) rule; missing Sites/Products rows auto-created by `_ensure_sites_and_products` (`airtable_io.py:423-459`).
5. **End a rule** — set `valid_to` on an open rule with no successor (delist). Requires a **new** write primitive (§3.2) — nothing today closes a rule without upserting a replacement.
6. **Preview→confirm flow** for every mutation (POST-echo confirm page, no JS) — deliberately built as the phase-2 review screen (§1.3).
7. **Provenance** on every write: `source = "editor:<email>"`, required non-empty `reason`.

**Roles** (per operator's blast-radius tiering, `auth_supabase.py:151` `ROLE_RANK` viewer:1/editor:2/admin:3): grid = `viewer`; all mutations = `admin`, matching `/upload-master` and `/add-support` (`webapp.py:737, 808`).

### 1.2 Out of scope (v1)

- **Tennents master** — `TennentsAgreements` has no effective dating at all: flat `(account, sku_code)` rows, wipe-and-replace only (`tennents.py:66-124`, `airtable_io.py:678-738` `replace_tennents_master`). If it ever needs in-app editing it is a trivial flat-row CRUD, a separate later feature. Excluded per Reader 2 §3.
- **Product-level retro editing** (`Products.retro_per_keg`) — different shape: **not** effective-dated; a change retroactively alters what any re-run of a past month's retro file reports (`retro.py:103-106`, `airtable_io.py:622-634`). Defer; if pulled in later it is a flat overwrite via `upsert_products_with_retros` (`airtable_io.py:568-601`) behind a loud warning. Rule-level `retro_pct` *is* editable via the normal price-change action (it rides on the Rule).
- **Temporary supports** — stay on the existing `/add-support` LLM form (`webapp.py:805-916`); the grid links to it rather than duplicating the form.
- **Deleting rules** — not in v1. The delete pattern exists (`airtable_io.py:717`, `requests.delete` with `records[]`, `idempotent=False`) and can be lifted into `delete_pricing_rule` later if "undo mistaken rule" is demanded; for v1, "fix in place" + "end rule" cover the realistic mistakes.
- **Phase-2 email ingest** — seam only (§1.3).

### 1.3 Phase 2 (design-for, don't build)

Forward price-change emails → LLM extract → approval gate → the **same** write path, mirroring the tenancy `document_inbox` parked→review→confirm pattern (`C:/dev/tenancies/supabase/migrations/00009_document_inbox.sql`). The seam cut **now** (§3.1):

- All editor mutations are expressed as a `MasterChange` dataclass and applied by one pure function `apply_master_change(change, actor_email)`. Phase 2's "Approve" button calls the same function; the LLM extractor just emits a `MasterChange`.
- The preview/confirm page renders **from a `MasterChange`**, not raw form fields — it doubles as the phase-2 review screen with zero rework.

Phase 2 later adds (named, not built): an Airtable `ChangeInbox` table (id, source `email|manual`, `received_at`, raw excerpt, `message_id` unique for idempotent ingest, extracted MasterChange fields, confidence/errors, status `pending_review|applied|rejected`, `proposed_by/reviewed_by/reviewed_at/rejected_reason`, `applied_rule_keys`); `GET /master/inbox` + `GET /master/inbox/{id}` (= the preview page + Approve(admin)/Reject); a mailbox-poll ingest worker using `support_parser.parse_support_request` + `validate_support_fields` (`webapp.py:805-916` is the in-repo extract→validate→render-errors template, including missing-`ANTHROPIC_API_KEY` handling). Proposals may be created by `editor`/system; **Approve = admin**.

---

## 2. Editing model

### 2.1 Semantics the editor must preserve (load-bearing invariants)

- **Half-open intervals:** a rule matches when `valid_from <= invoice_date < valid_to`; missing `valid_from` → `date.min`, missing `valid_to` → `date.max` (`reconcile.py:589-615`, defaults at 611-613). Therefore *close old at D + open new at D* is gapless and overlap-free by construction; an invoice dated exactly D gets the **new** price.
- **Overlaps are legal and load-bearing:** `_index_rules` sorts newest-`valid_from`-first (`reconcile.py:595`) and `_lookup_rule` returns the first containing window. `/add-support` deliberately layers a bounded `supported` rule over the still-open standard rule with `close_keys_at_date=None` (`webapp.py:885`). **Do not add "no overlaps ever" validation** — it would break the support mechanism. Instead, show which rule *wins* on any date.
- **`rule_key` identity = `site|product|valid_from-iso`** (or `open`) (`airtable_io.py:402-404`), with **no** server-side uniqueness and no status/price in the key. Consequences:
  - Same `(site, product, valid_from)` push = in-place PATCH (`airtable_io.py:547-548`), which can silently clobber a standing rule if a support/new rule shares its `valid_from`. The editor must treat a key collision as an explicit "edit in place" or reject — never an accidental layer.
  - **Editing `valid_from` changes the key** → a naive upsert creates a new row and leaves the old one open. "Change effective date" must be modelled as end-the-old-row + create-new (v1: expose it as "end rule" then "add rule"; a combined action can come later).
- **"On the master" membership counts open-ended rules only** (`valid_to is None`): `reconcile.py:634-638`, banner `active_rule_count` (`airtable_io.py:344-345`), default export state (`master_export.py:46-47`). Ending a rule removes the key from membership *immediately*, even before `valid_to` passes — future deliveries flag `tenant_price_missing`/`product_not_on_master` (that's the desired delist signal; warn in the UI). The grid must distinguish **open** (valid_to empty) from **effective on D** (half-open containment, per `master_export.py:48-53`).
- **None-strip on write:** `upsert_pricing_rules` strips `None` fields (`airtable_io.py:546`), so the existing path can never *clear* a field (can't null `valid_to` to re-open a rule). Re-opening is out of v1 scope; if ever needed it's a new explicit-null PATCH primitive, not a tweak to the upsert.
- **`status` changes behaviour, not labels:** `managed` rules skip price checks and assert zero margin (`reconcile.py:719-734`). Status edits go through the same preview/confirm with a warning line.
- **`retro_pct` is stored to 10dp on purpose** (round-trips to source pennies — `reconcile.py:129-131`, CLAUDE.md §6): never round it. `fb_price` = LIST price (cost-file col 2), never Net.

### 2.2 The three edit operations, precisely

| Operation | When | Mechanics | History |
|---|---|---|---|
| **Change price from date D** | The price genuinely changed | `upsert_pricing_rules([new_rule], close_keys_at_date=D)` — close pass PATCHes the prior open rule's `valid_to=D` (scoped to this (site,product) via `keys_in_new`, `airtable_io.py:485,521`); new row created with `valid_from=D`, `valid_to=None`. Re-submitting the same D updates in place (same-key belt, `airtable_io.py:508`). | Preserved |
| **Fix a mistake (rewrite in place)** | The old figure was never true | Push a Rule with the **same** `(site,product,valid_from)`; upsert matches `by_key` → PATCH (`airtable_io.py:547-548`), no close. | **Rewritten** — labelled as such in the UI; prepend to `reason` rather than replace (Airtable update overwrites the cell): `"corrected by <email> <date>: was £180 — <reason>; " + old_reason` |
| **End rule at D** | Delist / repair an open-ended support | New primitive `end_pricing_rule` (§3.2): single-record PATCH `valid_to=D` + cache invalidation. Creates nothing. | Preserved |

Note the caveat for retro-corrections: already-persisted `Mismatches` rows are **not** recomputed, and `write_mismatches` always creates (no read-first dedup) while `Files` dedupes by `raw_hash` — re-uploading an affected weekly file produces duplicate mismatch rows. Surface this on the confirm page.

Also: keep the `/lwc` "Effective from = today − 14 days" default (`webapp.py:524-530`) **out** of the editor — that default exists for whole-file re-uploads; the editor's price-change default is **today**.

### 2.3 Validation invariants (enforced in `validate_master_change`, §3.1)

Derived from `validate_support_fields` (`support_parser.py:161-202`), the upsert guards, and matching semantics:

1. `site_id` exists in Sites; `product_code` exists in Products — **or** the add-rule form explicitly opted into create (auto-create defaults: Sites `status=tenanted, country=england`; Products `supplier=LWC`, `airtable_io.py:423-459` — show these defaults on the confirm page).
2. `tenant_price > 0`; `fb_price` if given `> 0`; float-parseable. New (not derived): warn — don't block — outside a £20–£500/keg sanity band.
3. Dates parse (`_parse_date`, `reconcile.py:85-103`); `valid_to` strictly `> valid_from` when both set (matches `support_parser.py:196-197`). Never write an inverted/empty interval.
4. `(site, product, valid_from)` collision with an existing `rule_key` → allowed **only** for the explicit fix-in-place op; rejected for price-change/add-rule ("a rule already starts on that date — edit it instead", link to it).
5. End-rule: target must be **open** (`valid_to` empty); `end_date > valid_from`; warn (don't block) if no successor rule exists for the key ("future deliveries will flag as missing — is this a delist?").
6. `reason` non-empty on **every** mutation.
7. Never round `retro_pct`; `fb_price` is LIST.
8. Overlaps allowed; the preview must state which rule wins on the effective date (newest `valid_from` first). Gaps allowed but warned (§2.1 membership).
9. `status ∈ {tenanted, managed, supported}` — validate **before** write because `_batch` uses `typecast=True` (`airtable_io.py`) which would silently mint a new select option from a typo.

### 2.4 The close-guard "fix" — verification, not re-fix

Verified in the working tree:
- Projection includes `valid_from`: `airtable_io.py:482` `existing = _list_all(table_id, fields=["rule_key", "valid_from", "valid_to", "site", "product"])`.
- Live guard `airtable_io.py:510-516`: skips closing any open rule whose `valid_from >= close_keys_at_date`.
- Same-key belt `airtable_io.py:508`; close scoped to `(site,product) in keys_in_new` `airtable_io.py:521`.

Build tasks:
1. **CI gate:** run `python test_upsert_close_guard.py` (exit 0) as part of the build's test step; do not modify the existing 4 cases.
2. **Extend** the same fake-`_list_all`/`_batch` harness with editor cases (§5.2). Its projection-honouring fake is exactly the rig that would catch a regression if anyone drops `valid_from` from the projection again.

---

## 3. Write actions

### 3.1 New module: `master_changes.py` (the phase-2 seam)

```python
@dataclass
class MasterChange:
    op: Literal["price_change", "fix_in_place", "end_rule", "add_rule"]
    site_id: str                      # normalised via _to_str_code
    product_code: str
    product_desc: str | None = None   # add_rule only (Products.description)
    tenant_price: float | None = None
    fb_price: float | None = None
    retro_pct: float | None = None    # 10dp, never rounded
    status: str = "tenanted"
    valid_from: date | None = None    # effective date for price_change/add_rule; key date for fix_in_place
    valid_to: date | None = None      # end date for end_rule
    reason: str = ""                  # required, non-empty
    source_note: str = ""             # e.g. "editor" now, "email:<message_id>" in phase 2

def validate_master_change(change: MasterChange, snap: MasterSnapshot) -> list[str]:
    """Returns blocking errors; warnings returned separately (list[str], list[str])."""

def preview_master_change(change: MasterChange, snap: MasterSnapshot) -> ChangePreview:
    """Pure: computes what will be closed/created/updated + winner-on-date + warnings.
    Renders the confirm page AND phase-2 review screen."""

def apply_master_change(change: MasterChange, actor_email: str) -> ChangeResult:
    """The ONLY apply path. Stamps source=f'{change.source_note}:{actor_email}',
    dispatches to upsert_pricing_rules / end_pricing_rule.
    Returns (created, updated, closed, rule_keys_touched)."""
```

Dispatch inside `apply_master_change`:
- `price_change` / `add_rule` → build one `Rule` (`reconcile.py:108-119`) and call `upsert_pricing_rules([rule], close_keys_at_date=change.valid_from)` **unchanged**. For `add_rule` on a genuinely new key the close pass finds nothing to close — same call is safe.
- `fix_in_place` → `upsert_pricing_rules([rule], close_keys_at_date=None)` with the target's existing `valid_from` (same key → PATCH). Reason-prepend handled here.
- `end_rule` → `end_pricing_rule(...)` (§3.2).

**Race note (Reader 1 §D):** no server-side uniqueness on `rule_key`; read-then-write dedup means concurrent admin edits could duplicate keys. Single Render starter process makes this unlikely; mitigation = `apply_master_change` re-reads the by_key state immediately before writing (which `upsert_pricing_rules` already does internally at `airtable_io.py:482-483`) and `end_pricing_rule` re-resolves + re-checks openness at apply time (§3.2). Do not build locking.

### 3.2 New primitive in `airtable_io.py`

```python
def end_pricing_rule(rule_key: str, valid_to: date, reason: str, source: str) -> str:
    """Targeted close of one rule; returns the record id. Raises ValueError if the
    rule is missing, already ended, or valid_to <= valid_from."""
```

Mechanics:
- Resolve `rule_key` → record id + current `valid_from`/`valid_to` via a **projected** `_list_all(T["PricingRules"], fields=["rule_key","valid_from","valid_to"])` filtered client-side (or `filterByFormula` on `rule_key` — one record, cheaper). Snapshot `Rule` objects don't carry the record id, so re-resolution is mandatory; it also serves as the apply-time openness re-check.
- Guards: record exists; `valid_to` currently empty (only open rules can be ended); `valid_to > valid_from`.
- Write: `_batch([{"id": rec_id, "fields": {"valid_to": valid_to.isoformat(), "reason": <prepend>, "source": source}}], "update", T["PricingRules"])` — Airtable PATCH is field-level merge (`airtable_io.py:135` uses `requests.patch`), so untouched fields (including any operator-added ad-hoc columns) survive.
- **Must** end with `invalidate_master_cache()` (`airtable_io.py:255`), same as `upsert_pricing_rules` at line 554 — this is the house rule for every master writer.

No other `airtable_io.py` changes. `upsert_pricing_rules` is reused as-is; do **not** touch its close pass.

### 3.3 Routes in `webapp.py`

All follow house rules: every href/action through `ext_url()` (`auth_supabase.py:76` — app is served under `EXTERNAL_BASE_PATH=/drinks` behind the hub proxy); auth via `Depends(require_drinks_role(...))` (`auth_supabase.py:432`); try/except → `logger.exception` + `_error_page` (`webapp.py:1077`); mutations are plain `<form method="post">`.

| Route | Method | Role | Does |
|---|---|---|---|
| `/master` | GET | viewer | Rule grid from `load_master_snapshot()` (cached — never `load_rules_from_airtable`). Filters via GET params (§4.1). |
| `/master/edit` | GET | admin | Single-rule page (`?rule_key=`): current values read-only + two forms — "Change price from a date" and "Fix a mistake (rewrites history)". 404 politely if the key no longer resolves against the snapshot. |
| `/master/end` | GET | admin | End-rule form (`?rule_key=`): `valid_to` date (default today) + required reason + delist warning. |
| `/master/add` | GET | admin | Add-rule form: site select from `snap.sites`, product select **or** free code+desc (explicit "create new product" tick), prices, `valid_from`, reason. |
| `/master/preview` | POST | admin | Parse form → `MasterChange` → `validate_master_change` → render `preview_master_change` as `.summary-row`s with all values as hidden inputs + one "Confirm change" button + Cancel link. Validation errors re-render the form with messages (visual precedent: `/add-support` parse-fail page, `webapp.py:836-845`). |
| `/master/apply` | POST | admin | Re-parse hidden inputs → re-validate → `apply_master_change(change, principal.email)` → result page (§4.3). Apply the `_is_cross_origin` check (`webapp.py:360`) — **all four mutating POSTs get it** (today only `/auth/*` are covered; master writes are exactly the blast radius CSRF matters for). |

Provenance: `source = f"editor:{principal.email}"`; handle `principal.legacy=True` (Basic-auth fallback, `auth_supabase.py:87-100`, email = Basic username — acceptable, being retired). No Airtable schema change needed (`source`/`reason`/`status` already exist, `airtable_io.py:530-539`); optional later hardening: additive `changed_by`/`changed_at` columns are safe because all reads use explicit projections (`_RULE_FIELDS` `airtable_io.py:184-187`; close-pass line 482).

### 3.4 Cache discipline (the 30s-inline-fetch trap)

`invalidate_master_cache()` sets the snapshot to None; the **next** `load_master_snapshot()` call takes a blocking ~30s 3-table inline rebuild — the exact mechanism of the 2026-07-01 hub-proxy-timeout 500 outage (fixed for steady-state by stale-while-revalidate + boot warm-up, `66658ff`). Therefore:

- `/master/apply`'s result page **must not** call `load_master_snapshot()`. Render the confirmation from the `ChangeResult` + the pre-write preview data already in hand (counts like `/upload-master` does at `webapp.py:788-796`), then fire `refresh_master_cache_async()` (`airtable_io.py:262-292`, generation-checked) so the background rebuild is already running when the user clicks "Back to master".
- The grid itself (`/master`) reads the cached snapshot — zero new Airtable read patterns; edits appear within ≤60s (`MASTER_CACHE_TTL`) or immediately once the kicked refresh publishes. State this on the result page ("changes may take up to a minute to appear in the list").
- `/export-master` reads fresh (`master_export.py` uses uncached loads) — exports reflect edits immediately; no action needed.

### 3.5 Ordering / partial-failure

`upsert_pricing_rules` closes **before** creating (`airtable_io.py:523-524` vs 552) with no transaction — a mid-flight failure can leave a closed rule with no successor. For single-rule editor ops, keep the existing order (changing it risks the shared upload path) but: surface `(created, updated, closed)` on the result page, and on any exception render an error page that says exactly which rule may have been closed and links to `/master/edit?rule_key=` for manual repair. `_batch` retry semantics already protect the common cases (429 always retried; 5xx retried only for idempotent PATCH; create POSTs fail fast to avoid dup rows, `airtable_io.py:65-101`).

---

## 4. UI

House style: `render_head(principal.email, principal.role)` + `PAGE_FOOT` (`webapp.py:275, 308`); existing CSS covers everything needed — `table` with `td.r` tabular-nums, `.pill`, `.support-tag`, `.master-banner`, `.grid2` form grid, `details.block`, `.result`/`.summary-row`, `.button`, `.help` (`webapp.py:217-272`). No new CSS beyond a couple of row classes (e.g. `tr.ended` muted).

### 4.1 `/master` — grid

- Entry points: third card in `/lwc`'s "Pricing master" section (`webapp.py:557-593`): "Browse & edit rules →"; keep `/lwc` as reconciliation home.
- Header: `_master_banner_html()` (sources / latest_valid_from / active_rule_count — recomputed with every snapshot, `airtable_io.py:330-363`; nothing extra to do).
- Filters (GET params, server-side over the in-memory snapshot): `?q=` free text over site name/id + product code/desc · `?site=` · `?status=` · `?show=active|effective_on=<date>|future|ended|all` (default `active` = `valid_to is None`; `effective_on` uses half-open containment per `master_export.py:48-53` — the two views are labelled distinctly, per §2.1).
- Columns: site · product (code + desc) · tenant £ · FB £ · retro % · valid_from · valid_to · status (`.pill` for `supported`/`future`) · source (truncated, `title=` full) · **Edit** / **End** links (admin only; hide for viewer).
- Scale (Reader 2 §7): design for **low hundreds** of active rules (~2 uploads + ~10 single edits/yr; ≤5-6 rows/key over years; master well under the 1,000-row free-tier concern which is Files/Mismatches-driven). Sort site,product; paginate with `?offset=` + "Next 200 →" links. No server-side pagination machinery — the snapshot is already module-cached.
- "Add a rule" and "Add a temporary support →" (link to `/add-support`) buttons at top (admin).

### 4.2 Edit / end / add forms

- `/master/edit`: read-only current-values block, then two clearly separated form cards: **(a) Change price from a date** (new tenant_price, optional fb_price/retro_pct, effective-from date default **today**, required reason) and **(b) Fix a mistake** ("rewrites history — the old figure is treated as never true", same fields minus effective date, required reason). Both POST to `/master/preview`.
- `/master/end`: valid_to date default today, required reason, `.help` block: "This removes the product from active membership immediately — future deliveries at this site will flag as missing. Use for genuine delists or to repair an open-ended support rule."
- `/master/add`: site `<select>` + product `<select>` populated from snapshot maps, with a free-entry alternative gated by an explicit "create new product/site" checkbox (confirm page shows the auto-create defaults from `airtable_io.py:423-459`).

### 4.3 Preview → confirm → result (no JS)

POST-echo confirm: `/master/preview` renders the parsed `MasterChange` as `.summary-row`s, e.g.:

> Rule **001 | PKEG1 | 2026-01-01** will be **closed at 2026-07-02** · New rule created **from 2026-07-02** at **£182.00** (FB £190.00, retro 12.5%) · On 2026-07-02 the **new rule wins** (newest valid_from) · Reason: "LWC list increase Jul-26"

plus any warnings (sanity band, gap/delist, retro-correction Mismatches caveat), hidden inputs carrying the full change, one **Confirm change** button → `/master/apply`, and a Cancel link. Result page: `(created, updated, closed)` counts + "Open Airtable" / "Back to master" buttons — mirroring `/upload-master`'s result (`webapp.py:788-796`). This preview page **is** the phase-2 review screen (§1.3).

### 4.4 Recent changes view

v1-cheap version: a `?show=recent` grid sort by Airtable `createdTime` (already read as record metadata; note it is creation-time-only — in-place fixes don't bump it, say so in the UI). Deep field-level history = Airtable's built-in revision history via the existing "Open Airtable base" deep-link. A proper changed_at column is the optional additive hardening in §3.3, not v1.

---

## 5. Risks + tests

### 5.1 What could corrupt the master

| Risk | Guard |
|---|---|
| Close-guard regression (future-dated rules wrongly closed) | Existing `test_upsert_close_guard.py` stays green in CI; editor reuses `upsert_pricing_rules` unchanged |
| `rule_key` collision silently clobbering a standing rule (key omits status/price) | Validation invariant 4: collision → explicit fix-in-place or reject |
| Inverted/empty intervals (`valid_to <= valid_from`) | Invariant 3 + `end_pricing_rule` guard |
| `typecast=True` minting a bogus status select option | Invariant 9: vocabulary whitelist pre-write |
| Ending an already-ended rule / stale rule_key after concurrent change | `end_pricing_rule` re-resolves + openness check at apply time |
| Editing `valid_from` orphaning the old row (key change) | v1 has no valid_from edit; docs + validation steer to end+add |
| Post-write 30s inline snapshot rebuild → proxy 500 | Result page renders from in-hand data + `refresh_master_cache_async()` kick (§3.4) |
| Close-without-successor on mid-flight failure | Counts surfaced + repair-link error page (§3.5) |
| CSRF on master writes | `_is_cross_origin` on all four mutating POSTs (§3.3) |
| Stale cache serving pre-edit data | Both write paths end in `invalidate_master_cache()` (upsert already does at `airtable_io.py:554`; `end_pricing_rule` by construction) |

### 5.2 Tests (all offline, extending the existing rig)

Extend `test_upsert_close_guard.py`'s fake `_list_all`/`_batch` harness (it honours `fields=` projection and records batched ops — exactly right). New file `test_master_editor.py` importing the same fakes (or factor the fakes into a small `_fakes.py` both import):

**`end_pricing_rule`:** closes an open rule (PATCH carries valid_to+reason+source, nothing else); raises on missing key; raises on already-ended; raises on `valid_to <= valid_from`; calls `invalidate_master_cache` (monkeypatch-count).

**Price-change via `apply_master_change`:** single-rule change closes exactly the one prior open rule for that (site,product) at D and creates the successor (asserts the `keys_in_new` scoping — other sites' open rules untouched); future-dated sibling for the same key is NOT closed (guard, re-asserted through the editor path); re-applying same effective date PATCHes in place (same-key belt); D == existing rule's valid_from → validation error for op=price_change, success for op=fix_in_place.

**Fix-in-place:** same key → update not create, zero closed; reason is prepended not replaced.

**Add-rule:** new key → create, zero closed; auto-create of missing site/product asserted via `_ensure_sites_and_products` fake.

**`validate_master_change` (pure, no fakes):** each invariant in §2.3 — table-driven.

**`preview_master_change` (pure):** winner-on-date correctness with an overlapping support rule (newest-valid_from-first, mirroring `reconcile.py:595`); gap warning; delist warning.

**Route smoke (FastAPI TestClient, Airtable mocked):** viewer can GET `/master`, gets 403-page on `/master/edit`; admin preview→apply round-trip carries the change intact through hidden inputs; cross-origin POST to `/master/apply` rejected.

Regression command for the build: `python test_upsert_close_guard.py && pytest test_master_editor.py` (match however the existing test is invoked in CI/deploy checks).

### 5.3 Explicitly NOT tested/changed

`upsert_pricing_rules` internals (unchanged), Tennents paths, retro reconciliation, live Airtable (all tests offline per house pattern).

---

## 6. Build plan

### Phase 0 — Foundation (½ day)

1. Verify guard status: read `airtable_io.py:482,510-516`, run `python test_upsert_close_guard.py` (exit 0). **Done if green — no code change.**
2. Factor the test fakes into an importable module (only if cleaner than importing from the test file).
3. Create `master_changes.py` with `MasterChange`, `validate_master_change`, `preview_master_change` (pure, no I/O) + table-driven tests. This is the phase-2 seam and is testable before any UI exists.

### Phase 1 — Write primitives (½–1 day)

4. `end_pricing_rule` in `airtable_io.py` (+ `invalidate_master_cache`) with tests.
5. `apply_master_change` dispatch (thin: builds `Rule` objects, calls existing/new primitives) with the §5.2 apply-path tests, including the through-the-editor close-guard re-assertions.

### Phase 2 — Routes + UI (1–1½ days)

6. `GET /master` grid (viewer) + `/lwc` link — read-only, ship-able alone.
7. `GET /master/edit|end|add` forms + `POST /master/preview` + `POST /master/apply` (admin, `_is_cross_origin`, provenance stamping, async-refresh kick, counts-based result page).
8. Route smoke tests.

### Phase 3 — QA + deploy (½ day)

9. Full offline suite green (old + new).
10. Deploy to Render (push main per existing flow), then live QA against the real base with a throwaway (site, product): add-rule → price-change → fix-in-place → end-rule; verify in Airtable UI + `/export-master` (fresh reads) + grid after cache refresh; verify no proxy 500 on the apply→result→back-to-grid path (the `66658ff` saga check).
11. Operator walkthrough of the three-operation model (change-from-date vs fix-in-place vs end) — the semantic distinction is the main user-error surface.

**Honest size: ~3–4 focused days.** No schema migration, no new dependencies, one new write primitive, everything else reuse.

### File list

| File | Change |
|---|---|
| `master_changes.py` | **new** — `MasterChange`, `validate_master_change`, `preview_master_change`, `apply_master_change` (phase-2 seam) |
| `airtable_io.py` | **add** `end_pricing_rule` (~30 lines); nothing else touched |
| `webapp.py` | **add** 6 `/master*` routes + `/lwc` card link + `_is_cross_origin` on the mutating POSTs |
| `test_master_editor.py` | **new** — §5.2 suite |
| `test_upsert_close_guard.py` | unchanged (kept green); optionally extended with editor-path guard cases if not covered in the new file |
| `docs/master-editor-design.md` | this document (`docs/` does not exist yet — create it) |
| `CLAUDE.md` (repo) | short "master editor" section: the three operations, cache discipline, phase-2 seam pointer |