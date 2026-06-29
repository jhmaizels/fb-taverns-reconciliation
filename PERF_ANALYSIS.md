# FB Taverns Reconciliation — Performance Analysis

Decision-ready consolidation across the three operator-named areas (**cold start**, **file upload time**, **reconciliation speed**) plus the cross-cutting Airtable I/O layer and infra/measurement. Every finding cites `file:line` evidence that was read directly. Where a number requires a real measurement, that is stated explicitly rather than invented.

Service shape (verified): FastAPI + single uvicorn worker on Render `starter` (`render.yaml:8`), Airtable-only persistence via raw `requests` (`airtable_io.py`), Excel via pandas/openpyxl. Render `starter` is a **paid, always-on** instance — it does **not** idle spin-down (that is the Free tier), so "cold start" here means deploy/boot + first-request, not idle wake-up.

---

## 1. TL;DR

- **Upload is the only area with real user-facing latency to win, and it is Airtable-network-bound, not CPU-bound.** A single `/upload` issues ~10 full-table paginated GETs — **Sites ×3, Products ×3, PricingRules ×3, Files ×1** (`webapp.py:322,325,331,337,353`) — of which ~6 are literal re-reads of data already in memory. **Highest-leverage upload fix: collapse the redundant master reads to one per-request snapshot (AIO-1 / INF-3).**
- **The write throttle wastes ~1.5–2.25s of pure dead time per upload.** `_batch` sleeps `time.sleep(0.25)` after *every* chunk including the last (`airtable_io.py:71`), and a weekly upload fires 6–9 separate `_batch` calls each typically one chunk. **Removing only the trailing sleep is the single cheapest real win (AIO-2 / INF-5).**
- **Reconciliation compute is NOT a bottleneck.** The matching core (`_index_rules`/`_lookup_rule`, `reconcile.py:591-617`) is already O(n_lines) with tiny constants (RS-4), and the summary builders are linear single-pass (RS-5). The pandas anti-patterns (`iterrows`+`to_dict`, RS-1/RS-2) are real CPU but ~40ms at 2000 rows — noise against a multi-second Airtable-bound request. **Top "fix": remove the dead `InvoiceLine.raw` allocation as hygiene, not for speed.**
- **Cold start has no user-latency lever; it is a deploy/boot + 512MB-RAM concern only.** Lazily importing `anthropic` (used ~10×/yr by `/add-support`, `support_parser.py:26`) and deferring `openpyxl` (`master_export.py:14-16`) trim boot import work but change zero steady-state latency (INF-6 / CS-2). **Top cold-start fix: lazy-import `anthropic`.**
- **The system has never been profiled** (`CLAUDE.md` s10). Every magnitude estimate below the "confirmed mechanism" line is an estimate. **Do INF-1 first**: ~20 lines of X-Process-Time middleware + per-`_list_all`/`_batch` timing, then read the breakdown from Render logs before investing M-effort work.
- **Two correctness/availability hazards ride alongside the perf work:** blocking `requests`/pandas on `async def` handlers starve the single worker and risk Render's 5s health-check → restart-mid-write (INF-2), and any Airtable `429`/`5xx` currently calls `sys.exit()` (`airtable_io.py:53,69`) killing the worker instead of erroring cleanly (AIO-3). Both should be fixed *with* the upload work, not after.

---

## 2. Measure first (do this before changing anything)

The system has never been profiled (`CLAUDE.md` s10; the fetch multipliers it quotes are diagnosis estimates, not measurements). Land instrumentation first so every other change is evidence-driven.

**INF-1 — request + Airtable timing (effort S, risk low, ~20 lines, near-zero overhead).**

1. One ASGI middleware in `webapp.py` (none registered today — `webapp.py:67`) that records `time.perf_counter()` delta, sets an `X-Process-Time` response header, and logs `method path status ms`.
2. Wrap `_list_all` (`airtable_io.py:42-59`) and `_batch` (`airtable_io.py:62-72`) to log **once per call** (`GET <table> <n_pages> <ms>` / `WRITE <table> <n_records> <ms>`) — not per page, to keep logs readable and overhead trivial.
3. Use stdlib `logging` at INFO so it lands in Render's log stream — no new dependency.

Required details (from the adversarial pass):
- **Use `try/finally`** around the timing: `_list_all`/`_batch` call `sys.exit()` on HTTP≥300 (`airtable_io.py:53,69`), so a success-only log loses the failure/slow-call timing.
- **Secret hygiene:** log table id + counts + ms only. Never log record fields, query params, or `HEADERS` (carries the bearer token).
- Optionally gate verbose timing behind `PERF_LOG=1`.

After landing it, run **one real `/upload` on Render** and read the per-table breakdown. This is the gate for INF-3/INF-4/INF-5 and confirms (a) which of the ~10 reads dominate, (b) how many pages each master table actually spans, and (c) how close `/healthz` comes to the 5s timeout during an upload.

**One thing you do not need to measure to act on:** the trailing-sleep removal (AIO-2/INF-5) and the redundant-fetch count (AIO-1/INF-3) are confirmed by code reading; their *direction* is certain even if absolute ms isn't.

---

## 3. Cold start

> Render `starter` is always-on (no idle spin-down), so nothing here changes steady-state request latency. These are **deploy/boot-time and RAM-headroom** items plus one reliability fix. Treat as a low-priority cleanup bundle, not a latency win.

| Fix | Evidence | Expected gain | Effort | Risk |
|---|---|---|---|---|
| INF-6 — lazy-import `anthropic` inside `parse_support_request` | `support_parser.py:26`; `webapp.py:41`,`496-525` | Removes the SDK from 100% of boots + every non-`/add-support` path; tens of MB RAM + a few-hundred-ms import off boot | S | low |
| CS-2 — defer `master_export`/openpyxl import to its use site | `master_export.py:14-16`; `webapp.py:40`,`419` | ~240ms (measured) off the **boot/health-check path**; *not* user latency (see prose) | S | low |
| CS-4 — read Airtable env vars defensively, validate at first use | `airtable_io.py:28-29`; `render.yaml:13-16`; `webapp.py:77` | Reliability: turns a missing-var boot crash-loop into a bootable process where `/healthz` passes and only Airtable routes 503 | S | low |
| CS-5 — call `load_dotenv()` once (keep `webapp.py:29`, drop `airtable_io.py:27`) | `webapp.py:29`; `airtable_io.py:27` | Hygiene; unmeasurable (one no-op fs scan at boot) | S | low |
| CS-3 — pin `anthropic==0.92.0` (drop `--workers 2`) | `render.yaml:7-8`; `requirements.txt:8` | Deploy-time determinism only, seconds of resolver variance; **no request-latency effect** | S | low |
| CS-6 — *do not* preload master in a startup hook (guardrail) | `webapp.py:142-144`; `airtable_io.py:42-59` | Zero — prevents a harmful regression that would re-couple boot to Airtable | S | low |

**Top items:**

**INF-6 (lazy-import `anthropic`)** is the one genuine cold-start lever. `anthropic` is imported unconditionally at `support_parser.py:26` and appears nowhere else; `/add-support` (`webapp.py:496-525`) is its sole consumer at ~10 calls/year. Move `import anthropic` into the body of `parse_support_request` (`support_parser.py:29`), after the existing `os.environ.get(ANTHROPIC_API_KEY)` check (`support_parser.py:46-47`) so the missing-key error path (`webapp.py:577-581`) is byte-identical. Correctness-safe; RAM relief is modest because pandas+numpy (which stay eager for the hot path) remain the dominant ~50–100MB consumers. **Correction carried from the adversarial pass:** the finding's "also defer master_export's pandas import" is a misread — `master_export.py` imports openpyxl (lines 14-16), not pandas; drop that sub-item. pandas enters via `reconcile.py:23` and stays eager.

**CS-2 (defer openpyxl)** is correct but its benefit is narrower than first claimed and was *measured*: the marginal `openpyxl` import is ~240ms warm / ~295ms cold, **larger** than the finding's "50–150ms". However the hot `/upload` path calls `pd.read_excel` (`reconcile.py:220,274,504`), which lazily triggers the real openpyxl import at request time regardless — confirmed empirically. So deferring `master_export` keeps openpyxl off the **path to readiness** (Render's deploy-boot `/healthz` probes never touch Excel, so they reach ready ~240ms sooner), but the first real upload still pays openpyxl either way. Frame it honestly as a deploy/readiness win, not a user-latency win. The ~240ms was measured on the Windows dev box against `openpyxl==3.1.5`; Render's Frankfurt Linux instance will differ — measure there to quote a prod number.

**CS-4 (env-var robustness)** is a reliability item, not perf. `airtable_io.py:28-29` use `os.environ["..."]` bracket access at import; both vars are `sync: false` in `render.yaml:13-16` (dashboard-managed, easy to omit). A missing var raises `KeyError` before `app` is constructed, so `/healthz` is never reachable and an *initial* deploy crash-loops. Switch to `os.environ.get(...)` and validate at first use inside the Airtable primitives (raise 503, mirroring `check_auth` at `webapp.py:77`). Must still hard-fail when the token is genuinely missing — do not default it. Factual correction: Render does not retry-deploy endlessly against a healthy prior instance (it rolls back and keeps the old one); the crash-loop risk is specific to initial deploys / restart-on-exit.

---

## 4. File upload time

> **This is the area with real, winnable user-facing latency.** The upload is Airtable-network-bound: reconcile compute and Excel parse are noise against ~10 sequential paginated GETs plus throttled batch writes. Lead with the redundant-fetch consolidation and the trailing-sleep removal; pair with the event-loop-blocking fix.

| Fix | Evidence | Expected gain | Effort | Risk |
|---|---|---|---|---|
| AIO-1 / INF-3 — per-request master snapshot (collapse redundant reads) | `webapp.py:322,325,337,353`; `airtable_io.py:96,99,101,141,149,399,324,338` | Removes **6 of ~10** full-table GETs (Sites 3→1, Products 3→1, PricingRules 3→1) | M | medium |
| AIO-2 / INF-5 — skip the trailing `time.sleep(0.25)` after the last chunk | `airtable_io.py:65-71`,`443` | **~1.5–2.25s** per upload (6–9 `_batch` calls × 0.25s, mostly single-chunk) | S | low |
| INF-2 / UP-2 — run blocking upload body off the event loop | `webapp.py:306,363,431,669,715`; `airtable_io.py:51,67,71`; `render.yaml:8` | Availability: keeps `/healthz` responsive; removes restart-mid-write risk. No upload-latency change | S | low |
| AIO-3 — replace `sys.exit()` on 429/5xx with raise + bounded backoff | `airtable_io.py:48,51,52,69`; `CLAUDE.md:323,326` | Availability: turns a fatal worker-kill into a recoverable error | M | medium |
| INF-4 / AIO-5 — Files dedup via `filterByFormula` instead of full scan | `airtable_io.py:377-380`; `webapp.py:331,386,735` | Caps the only monotonically-growing read; **~0ms today**, future-proofing | S | low |
| UP-5 — stop re-reading Products + fix quadratic close-pass (`/upload-master`) | `webapp.py:464`; `airtable_io.py:200,232-233,287` | Products 3→2 safely; close-pass O(n²)→O(n) | M | medium (banner reuse unsafe — see prose) |
| UP-3 — hash uploaded bytes once in memory; drop the second disk pass | `webapp.py:319`; `airtable_io.py:272-277,376` | Removes one full-file disk read; negligible (single-digit-to-tens of ms), free | S | low |
| AIO-4 — add `fields=` projection to the 3 un-projected reads | `airtable_io.py:101,410,471,42` | Smaller payloads/parse on per-request reads; bounded minor win | S | low |

**Top items:**

**AIO-1 / INF-3 (per-request master snapshot) — the dominant upload win.** A populated `/upload` makes exactly 10 logical full-table GETs: `load_rules_from_airtable` reads Sites+Products+PricingRules (`airtable_io.py:96,99,101`); `load_sites_from_airtable` reads Sites again (`:78`); `upsert_file_record` scans Files (`:377`); `write_mismatches` re-reads all three via `_site_lookup`/`_product_lookup`/`_rule_lookup` (`:141,149,399`); the banner re-reads PricingRules+Products (`:324,338`). `_list_all` is serial, paginated `pageSize=100`, **with no read throttle**. Since `reconcile_lines` is purely in-memory (`reconcile.py:620-640`), these reads *are* the network cost of an upload. Build one snapshot up front and thread it through. **Mandatory amendments from the adversarial pass — the fix breaks without them:**

- The snapshot must hold the **raw `_list_all` records** (id + fields), not `load_rules_from_airtable`'s `Rule` list. `write_mismatches`'s rule map is keyed by `rule_key` (`airtable_io.py:398,681`), which the `Rule` dataclass (`reconcile.py:108-120`) does not carry. Build `rule_ids = {rec.rule_key → rec.id}` from the raw PricingRules snapshot, using the same `_rule_key` form (`airtable_io.py:133-135`) so links at `:681-683` resolve byte-identically.
- The snapshot field projection must be a **superset** of every consumer: PricingRules needs `rule_key, valid_from, valid_to, source, site, product, …` **and `createdTime`** (banner "uploaded at"); Products needs `product_code, description, **retro_per_keg**` (banner `products_with_retro`, `:338`); Sites needs the full `load_sites` field set (`site_id, name, status, country, notes`). Omit any → the banner counts silently regress.
- **Scope the snapshot to `/upload` only.** Do **not** thread it through `/upload-master` or `/add-support`, where `_ensure_sites_and_products` auto-creates rows mid-flow — a stale snapshot there would miss new rec-ids and break effective-dating / rule_key close. The banner on `/upload-master` reads *post-write* and must keep reflecting just-created/closed rules.

Realistic gain: **6 of 10 reads removed** (Sites 3→1, Products 3→1, PricingRules 3→1; Files stays 1×). On multi-page master tables that is the single biggest wall-clock saving and the largest cut to 5-req/s pressure. **Measurement caveat:** if each master table is a single page (≤100 rows) the saving is ~6 round-trips rather than seconds — confirm page counts via INF-1 before quoting an absolute number. A lower-risk **S-effort partial**: just have `write_mismatches` accept `site_ids`/`product_ids` from `load_rules` (saves Sites −1, Products −1) and leave the banner alone.

**AIO-2 / INF-5 (skip the trailing sleep) — the cheapest real win.** `_batch` (`airtable_io.py:65-71`) unconditionally `time.sleep(0.25)` after every chunk including the last, where it gates nothing; the Tennents delete loop repeats it (`:443`). A weekly LWC upload fires **6–9 separate `_batch` calls** (sites `:166`, products `:185`, rule close `:237`, create/update `:265-266`, retro `:304-305`, Files `:392`, mismatches `:695`), most carrying ≤10 records → one chunk → exactly one useless trailing sleep each. Removing trailing sleeps saves **~1.5–2.25s per upload** — larger than the per-call "~0.25s" framing because it is per-`_batch`-call. Fix: `if i + BATCH_SIZE < len(records): time.sleep(0.25)` (or sleep at loop top guarded by `i > 0`) in both loops. The inter-chunk ~4 req/s throttle is preserved, so the ~5 req/s base limit is still respected. **Do not** also flip to "reactive-only backoff" as INF-5(b) proposed: the inter-chunk spacing is the only thing keeping bursts under 5 req/s, and a real 429 imposes a ~30s lockout with no retry today — that belongs to AIO-3.

**INF-2 / UP-2 (off the event loop) — availability, not speed.** Every upload handler is `async def` (`webapp.py:306,363,431,669,715`) whose only `await` is `file.read()`, after which it runs blocking `requests` (`airtable_io.py:51,67`), pandas/openpyxl, and `time.sleep` (`:71`) directly on the single event-loop thread. A multi-second upload pins that thread; `/healthz` (already a plain `def`, `webapp.py:142-144`, so it runs in the threadpool) cannot be *dispatched* because the loop thread is blocked. Convert the heavy handlers to plain `def` (FastAPI runs them in its 40-thread pool) **or** wrap the post-read body in `await run_in_threadpool(...)`. **Mandatory detail:** a plain `def` handler must call `file.file.read()` (the sync `SpooledTemporaryFile`) — `await file.read()` won't compile in a non-async function. **Concurrency caveat:** true parallel uploads against one Airtable base (~5 req/s) raise 429 risk, and `_list_all`/`_batch` `sys.exit()` on ≥300 — a 429 would crash the worker rather than 400. So sequence this *after/with* AIO-1 (kill redundant reads) and AIO-3 (replace `sys.exit`).

---

## 5. Reconciliation speed

> **Compute is not the bottleneck.** The matching core and summary builders are already efficient; the only CPU findings are pandas iteration anti-patterns whose absolute cost is ~tens of ms against a multi-second, Airtable-bound upload. Proportion effort accordingly: take the dead-code removal as hygiene, treat the rest as optional polish, and do **not** touch the matching core or the Tennents simple-mean.

| Fix | Evidence | Expected gain | Effort | Risk |
|---|---|---|---|---|
| RS-1 — delete dead `InvoiceLine.raw` (`row.to_dict()` never read) | `reconcile.py:477,515,537`; `summary.py:107-239` | ~22ms at 2000 rows; dead-code hygiene | S | low |
| RS-1 — swap `iterrows()` → `itertuples`/column-arrays in `parse_lwc_sales` | `reconcile.py:515` | ~40ms→~3ms on the parse loop; immaterial to wall-clock | S | low |
| RS-2 — same swap in `retro.parse_lwc_retro` + `tennents.parse_monthly` | `retro.py:77`; `tennents.py:166` | ~1.5–3× parse-slice CPU; marginal vs I/O | S | low |
| RS-4 — *do not* "optimise" the matching core (guardrail) | `reconcile.py:591-617,635-641` | Zero — already O(n) with tiny constants | S | low |
| RS-5 — *do not* refactor summary builders / Tennents simple-mean (guardrail) | `summary.py:106-239`; `tennents.py:270-305,299-303` | Zero — linear single-pass; mean is business rule | S | low |
| RS-3 — calamine engine for `read_excel` (**needs measurement**) | `reconcile.py:220,274,502-504`; `retro.py:64-66`; `tennents.py:89,145` | 5–20× parse *only if* files reach many-thousand rows; ~0 today | M | medium |

**Top items:**

**RS-1 (dead `.raw` + iterrows).** `parse_lwc_sales` walks the frame with `df.iterrows()` (the slowest primitive) and stores `row.to_dict()` into `InvoiceLine.raw` (`reconcile.py:537`, declared `:477`) — and **nothing ever reads `.raw`** (confirmed by grep across `reconcile.py`/`summary.py`/`webapp.py`; `Mismatch.to_row` and `build_summary` read only typed scalar fields). Benchmarked on a 2000-row/12-col frame: current `iterrows`+`to_dict` 46.1ms → drop `to_dict` 24.2ms → `itertuples` no-dict 2.7ms (~17×). **Ship the `.raw` deletion regardless** — it's the bulk of the win and zero-risk. The `itertuples` swap is optional polish; if done, use `df.reindex(columns=[...all used cols...]).itertuples(index=False, name=None)` so optional columns absent from a given file don't `KeyError` (the current `row.get()` returns `None` for them), preserving the isna-skip (`:516`), float try/except (`:518-523`), `diff isna→0.0` (`:524`), `_to_str_code`, `_parse_date` guards verbatim. Either way the absolute saving (~40ms) is sub-noise against the Airtable-bound upload — justify it as hygiene, not latency.

**RS-2** applies the same swap to the two other upload-path parsers (`retro.parse_lwc_retro`, `tennents.parse_monthly`). No dead `.to_dict()` here (so smaller than RS-1), and the win is tens of ms — a code tidy, not a latency lever. **Implementation caveat:** `itertuples` mangles column names with spaces (`'Customer Name'`, `'Net Price per keg'`), so use `itertuples(index=False, name=None)` with positional unpacking or pre-extract columns as arrays; apply the `dropna(subset=['Customer Name','SKU'])` (`tennents.py:162`) and `df[df['Kegs']>0]` (`:163`) as vectorized masks **before** iterating, and keep the retro `qty<=0` skip (`:86-87`). **Scope correction:** drop `parse_tenant_pricing_folder` from scope entirely — it is CLI-only (`reconcile.py:378`) and never runs in the deployed service; `parse_master`/`parse_fb_cost_file` are infrequent build-master paths, no user-visible benefit.

**RS-4 / RS-5 are deliberate non-findings (guardrails).** `_index_rules` builds one dict keyed by `(site_id, product_code)`, sorts each tiny bucket newest-`valid_from`-first once, then `reconcile_lines` makes a single pass with O(1) set membership and a short scan over only that key's handful of effective-dated versions (`reconcile.py:591-617,635-641`). No quadratic behaviour, no repeated full-rule scan. `build_summary`/`build_retro_summary`/`tennents.reconcile` are linear single passes. **Do not** "optimise" these: a naive bisect on `valid_from` alone would break the half-open `vf ≤ on_date < vt` interval (buckets are `valid_from`-DESC and the predicate also tests `valid_to`), and changing the Tennents simple-mean (`tennents.py:299-303`, documented in `CLAUDE.md:344-345`) to keg-weighted would flip which rows breach tolerance — a business-rule change, not a perf win.

**RS-3 (calamine engine) — needs measurement, deprioritise.** The `ExcelFile`/`_find_line_sheet` pattern is already optimal (one open, one sheet). The only lever is swapping openpyxl→calamine, which is 5–20× faster **only if** weekly files reach many thousands of rows — for which there is no evidence. Do not implement without (a) an INF-1 profile showing `read_excel` dominating and (b) a realistic large sample workbook. If ever adopted, it must cover **all six** `read_excel` sites (the finding missed `reconcile.py:220,274`), and be gated behind a golden-file diff test — calamine/openpyxl differ on blank-cell/date coercion and header strings, and a silent parse regression would corrupt every comparison.

---

## 6. Cross-cutting: Airtable I/O layer

The shared backbone (`airtable_io.py`) is where the upload latency actually lives. Five coordinated changes, ordered by leverage:

**(1) Per-request master snapshot — AIO-1.** *Removes:* Sites 3→1, Products 3→1, PricingRules 3→1 (6 of ~10 reads). See §4 for the mandatory raw-record + superset-projection + `/upload`-only-scope amendments. This is the single biggest read-side win and the largest reduction in 5-req/s burst pressure.

**(2) Cross-upload master cache (TTL) — AIO-6.** *Removes:* the remaining master reads across a *burst* of uploads / repeated index+banner renders (collapse to one fetch per TTL window). Render `starter` stays warm, so a module-level dict + timestamp persists between operator sessions. **Load-bearing correctness constraint:** put the cache in the **high-level accessors** (`load_sites_from_airtable`, `load_rules_from_airtable`, `get_active_master_info`) **only — never at `_list_all` level**, because the write paths read the tables they mutate via `_list_all`/`_*_lookup` directly (`airtable_io.py:200,141,149,287`); a stale `_list_all` cache there would break effective-dating / rule_key close / typecast auto-create. **Invalidate on every master-mutating handler:** after `upsert_pricing_rules` + `upsert_products_with_retros` (`/upload-master`), after `upsert_pricing_rules` (`/add-support` — note it reads `load_rules` at `webapp.py:520` *before* writing, so a missed invalidation would attach a stale `fb_price`), and after `replace_tennents_master`. Keep TTL ≤300s. Do **not** cache Files or Mismatches. AIO-6 and AIO-1 overlap — build the per-request snapshot first (or fold both), since the accessor cache subsumes most of AIO-1's intra-request dedup.

**(3) Field projection — AIO-4.** *Removes:* nothing, but trims payload/parse on the 3 un-projected reads (`load_rules`' PricingRules `:101`, `load_tennents_agreements` `:410`, `get_tennents_master_info` `:471`). Bounded minor win (record counts are capped; the `~1000` figure in `CLAUDE.md` is the Airtable **per-base** Free-tier cap, not per-table). Projection must include **every** field the constructor reads — PricingRules reads link fields `site`/`product` first and would silently empty the master if either is omitted. `createdTime` is record metadata, returned regardless. Bundle with AIO-1.

**(4) `filterByFormula` for the Files dedup — AIO-5 / INF-4.** *Removes:* the unbounded full-table Files scan (`airtable_io.py:377-380`), replacing it with `filterByFormula={raw_hash}='<hash>'` returning 0–1 rows. This is the **only read whose cost grows forever** (one row per upload). **~0ms today** (Files is sub-one-page), pure future-proofing — sequence it *after* the master-read dedup. Add an optional `formula`/`filter_formula` param to `_list_all` (effort S, not M). `raw_hash` is a hexdigest (`[0-9a-f]{64}`) so no injection surface, but escape defensively and take `records[0]` (not assert exactly one) to preserve "return first match" behaviour. Idempotency (same hash → same rec id, no new row) is byte-for-byte preserved. Do **not** use `filterByFormula` for the active-rule banner — `NOT({valid_to})` still server-side scans and offset-paginates; caching (item 2) removes that read entirely.

**(5) Batch/throttle tuning — AIO-2 + AIO-3.** *Removes:* the trailing `time.sleep(0.25)` (≈1.5–2.25s/upload, §4) while keeping inter-chunk spacing. Separately, replace `sys.exit()` on HTTP≥300 (`airtable_io.py:53,69`) with a raised exception + bounded backoff that honours `Retry-After` **on 429/5xx only** (a 422 bad-field must still fail fast — silently retrying it would mask schema/typo errors and fight `typecast:True` auto-create). Align `replace_tennents_master`'s `raise_for_status` (`:441`) onto the same path. The CLI (`reconcile.py:883-929`) treats a raised error like the current hard exit. **Severity is unconfirmed:** `sys.exit` raises `SystemExit` (a `BaseException`, not caught by `except Exception` at `webapp.py:338`), but whether it reliably kills the multi-request uvicorn worker vs. failing one request is version-dependent — a 5-min staging repro (inject a 429) settles it. Sequence AIO-3 *after* AIO-1, since the read dedup is the real rate-cliff remover and this is defense-in-depth.

---

## 7. Prioritized roadmap (single ordered list)

### Quick wins — S effort, confirmed, ship first

1. **INF-1 — instrumentation.** *Why:* never profiled; gates everything below. *Impact:* 0ms but turns guesses into numbers. *Effort:* S. *Risk:* low. *Guardrail:* `try/finally` timing; log table-id/counts/ms only, never record contents or `HEADERS`.
2. **AIO-2 / INF-5 — skip trailing `time.sleep(0.25)`.** *Why:* pure dead time, 6–9× per upload. *Impact:* ~1.5–2.25s/upload. *Effort:* S. *Risk:* low. *Guardrail:* keep inter-chunk spacing (≤5 req/s); don't switch to reactive-only throttle here.
3. **INF-2 / UP-2 — move blocking upload body off the event loop.** *Why:* prevents 5s health-check starvation → restart-mid-write. *Impact:* availability (no latency change). *Effort:* S. *Risk:* low. *Guardrail:* use `file.file.read()` in sync handlers; sequence with #5/AIO-3 to avoid exposing 429s under new concurrency.
4. **RS-1 (dead `.raw` deletion only).** *Why:* removes an unused per-row dict allocation. *Impact:* ~22ms/2000 rows (hygiene). *Effort:* S. *Risk:* low. *Guardrail:* confirmed nothing reads `.raw`.
5. **INF-6 — lazy-import `anthropic`.** *Why:* off 100% of boots; SDK used ~10×/yr. *Impact:* boot import + RAM headroom. *Effort:* S. *Risk:* low. *Guardrail:* keep the missing-`ANTHROPIC_API_KEY` error path identical.
6. **CS-2 — defer openpyxl import; CS-4 — defensive env vars; CS-5 — single `load_dotenv`; CS-3 — pin `anthropic`.** *Why:* boot/readiness cleanup + a config-crash safety net. *Impact:* deploy/readiness only. *Effort:* S each. *Risk:* low. *Guardrail:* CS-4 must still hard-fail when token genuinely missing.

### Medium — measure-then-build, the real upload latency tier

7. **AIO-1 / INF-3 — per-request master snapshot.** *Why:* the dominant upload latency driver (6 of ~10 reads). *Impact:* large (validate page counts via #1). *Effort:* M. *Risk:* medium. *Guardrail:* raw records + superset projection (incl. `createdTime`, `retro_per_keg`); `/upload`-only scope; `rule_key`-keyed rule map.
8. **AIO-6 — cross-upload TTL master cache.** *Why:* collapses repeated master reads in bursts/index renders. *Impact:* meaningful in steady-state. *Effort:* M. *Risk:* medium. *Guardrail:* cache at accessor level only; invalidate on all 3 master-write handlers; never cache Files/Mismatches.
9. **AIO-3 — replace `sys.exit` with raise + 429/5xx backoff.** *Why:* turns a fatal worker-kill into a recoverable error. *Impact:* availability. *Effort:* M. *Risk:* medium. *Guardrail:* 429/5xx only — 422 fails fast; honour `Retry-After`. Confirm severity on staging.
10. **UP-5 — `/upload-master` Products 3→2 + linearize close-pass.** *Why:* slowest write route. *Impact:* ~0.3–1s + O(n²)→O(n). *Effort:* M. *Risk:* medium. *Guardrail:* banner must read post-write — do **not** reuse the pre-write snapshot for it; preserve effective-dating close guards byte-for-byte.

### Larger / lower-priority / future-proofing

11. **AIO-4 — field projection on 3 reads** (bundle into AIO-1). *Impact:* minor payload trim. *Effort:* S. *Guardrail:* include every constructor-read field (esp. PricingRules `site`/`product`).
12. **INF-4 / AIO-5 — Files dedup via `filterByFormula`.** *Why:* the only read that grows forever. *Impact:* ~0ms today, caps future growth. *Effort:* S. *Guardrail:* `records[0]` not assert-one; same-hash → same rec id.
13. **UP-3 — hash bytes once in memory.** *Impact:* negligible-but-free disk-read removal. *Effort:* S. *Guardrail:* hash identical raw bytes so Files dedup key is unchanged.
14. **RS-1/RS-2 itertuples swaps.** *Impact:* tens of ms (polish). *Effort:* S. *Guardrail:* `itertuples(index=False, name=None)` + reindex for optional/space-named columns; preserve all per-row guards.
15. **RS-3 — calamine engine. *Measure first.*** *Impact:* 5–20× parse *only* on many-thousand-row files (unproven). *Effort:* M. *Risk:* medium. *Guardrail:* cover all 6 `read_excel` sites; gate behind a golden-file diff test.

---

## 8. Rejected / needs-measurement

**Needs measurement before committing:**

- **RS-3 (calamine engine swap)** — conditional win that only materialises if weekly files reach many thousands of rows; no evidence they do. Gate on an INF-1 profile + a real large sample + golden-file diff test. Misses 2 of 6 `read_excel` sites as written.
- **Absolute magnitudes for AIO-1 / INF-3** — the "several seconds" / "6 round-trips" range depends on how many pages Sites/Products/PricingRules actually span on the live base (≤100 rows → single page → smaller win). Confirm via INF-1 before quoting a number.
- **AIO-3 severity** — whether a real 429-`sys.exit` currently kills the uvicorn worker or merely fails one request is version-dependent; a 5-min staging repro settles it.

**Sub-proposals rejected (do not chase):**

- **CS-3 `--workers 2`** — `starter` is 512MB / 0.5 vCPU; two pandas workers risk OOM and 0.5 vCPU gives no real parallelism. Keep one worker; fix health-check starvation via INF-2 instead.
- **INF-6 "defer master_export pandas"** — misread; `master_export.py` imports openpyxl, not pandas. Only the `anthropic` lazy-import is the real lever.
- **INF-5(b) "reactive-only throttle"** — removing proactive inter-chunk spacing can exceed 5 req/s; a 429 imposes a ~30s lockout with no retry today. Keep spacing; build the 429 wrapper (AIO-3) instead.
- **AIO-1 "compute banner from rules in memory"** — `products_with_retro` is a Products-table count (`retro_per_keg`) with no in-memory rules equivalent, and `latest_uploaded_at` needs `createdTime` the `Rule` dataclass drops. Widen projections or leave `get_active_master_info` as one read.
- **UP-5 "reuse pre-write snapshot for the banner"** — the `/upload-master` banner must reflect just-created/closed rules; reusing the pre-write snapshot is a visible regression.

**Deliberate non-findings (guardrails to prevent harmful "optimisation"):**

- **RS-4 — matching core** is already O(n) with tiny constants; a bisect rewrite would break the half-open effective-dating interval. Leave it.
- **RS-5 — summary builders + Tennents simple-mean.** Linear single-pass; the simple-mean is a documented business rule (`CLAUDE.md:344-345`) — changing it flips tolerance breaches.
- **CS-6 — no startup master preload.** Keeping `/healthz` Airtable-free is correct; a startup hook would re-couple boot to Airtable (CS-4 crash-loop risk). No keep-warm cron needed (starter is always-on).
