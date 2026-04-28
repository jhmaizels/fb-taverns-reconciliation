# Manager UX: a single Airtable Form for price changes

For the realistic volume (~10 single-site edits/year + ~2 bulk uploads/year), a
single Airtable Form is the right level of UX. Five minutes to build, one URL
to share, no Interface complexity.

The full three-page Interface (`AIRTABLE_INTERFACE.md`) is a fine destination
later — but skip it for now.

This doc covers three small things, in order:

1. **The Form** for adding a new price (the thing managers actually use)
2. **An "Active prices" view** for browsing the current master (read-only)
3. **A "Recent issues" view** for triaging mismatches

You'll set all three up **once**. After that managers just bookmark URLs.

---

## 1. The Form for adding a new price

Setup, ~5 minutes.

1. Open the base → click **PricingRules** in the table tabs at the top.
2. Top-left, click the view-name dropdown → **+ Create new** → choose **Form**.
3. Name the form (left panel): **`Update a price`**.
4. The right panel shows every field on `PricingRules`. Drag them into the
   form / hide the rest. Use this exact order and help text:

   | Field | Required? | Help text shown under the field |
   |---|---|---|
   | `site` | ✅ | *Which pub is this price for?* |
   | `product` | ✅ | *Which drink is the price changing for?* |
   | `tenant_price` | ✅ | *What does the tenant pay per keg / case?* |
   | `valid_from` | ✅ | *When does this new price start? Use today for an immediate change.* |
   | `valid_to` | ❌ | *Only fill this in for **temporary** support periods (e.g. a 6-week discount that ends on a specific date). Leave blank for normal price changes.* |
   | `status` | ✅ | *Almost always **tenanted**. Use **managed** if FB Taverns now operates the pub directly. Use **supported** for a temporary support price.* |
   | `reason` | ✅ | *(see "Reason help text" below — paste the whole block in)* |
   | `fb_price` | ❌ | *Only fill if FB's cost has changed. Leave blank otherwise.* |

5. **Hide all the other fields** (rule_key, source, the linked
   Mismatches/PricingRules etc.).
6. **Defaults** (set in the field's settings on the form panel):
   - `status` → `tenanted`
   - `valid_from` → today (Airtable lets you pick "Today" as the default)
7. At the very top of the form, click **+ Add description** and paste the
   "About this form" block (see below).

### About this form (paste at the top)

```
Use this form to record any pricing change for a site:

• A new tenant taking over — pick the site and product, enter the new tenant
  price, set "valid from" to the handover date, status = tenanted, reason e.g.
  "New tenant Hannah Williams from 1 May 2026".

• A six-week support period on one product — fill in BOTH "valid from" and
  "valid to", status = supported, reason e.g. "6-week launch support on
  Madri 50L while tenant builds volume".

• A site converting to managed — set tenant price = FB price, status = managed,
  reason e.g. "Site now operated directly by FB Taverns from 1 June".

• A single-product correction — just the one row, status = tenanted, reason
  e.g. "Carling 22G corrected to £164.30 per email from Doug Trotman 14 Mar".

The system automatically closes the prior price for this site & product when
you save. You don't need to do anything else.
```

### Reason help text (paste under the `reason` field)

```
Why is this changing? One short sentence — your area manager needs this to
make sense in 6 months. Examples:
"New tenant Hannah Williams from 1 May 2026"
"6-week launch support on Madri 50L"
"Carling 22G corrected per email from Doug Trotman 14 Mar"
```

### Share the URL

8. Top-right of the form view → **Share form** → toggle on, copy URL.
9. Send it to Jason. He bookmarks it. Done.

The form URL looks like `https://airtable.com/embed/appyDA69D2YhdpsA4/shrXXXX...`
and works on phone or laptop without him needing an Airtable login.

---

## 2. An "Active prices" view (for browsing)

So managers can answer "what is the current tenant price at Bell 804 for
Carling 11G?" without you having to look it up.

1. Open `PricingRules` table.
2. Top-left view dropdown → **+ Create new** → **Grid view**.
3. Name it **`Active prices`**.
4. Add a filter: `valid_to` **is empty**.
5. Sort: by `site` ascending, then `product` ascending.
6. Hide noisy fields — keep only `site`, `product`, `tenant_price`, `fb_price`,
   `valid_from`, `status`, `source`.
7. Group by `site`.
8. Top-right → **Share view** → toggle on **Restricted view link** → copy URL.

Send Jason the URL. Read-only — he can scroll, search, see prices, but
can't edit. To change anything he uses the Form (above).

---

## 3. A "Recent issues" view (for mismatches review)

So Jason can see the latest reconciliation findings and acknowledge / resolve
them.

1. Open `Mismatches` table.
2. Create a new **Grid view** called **`Open issues`**.
3. Filter: `status` = `open`.
4. Sort: by created time descending.
5. Group by `file` (so each file's issues stay together).
6. Show fields: `type`, `severity`, `site`, `product`, `delta_total`, `notes`,
   `status`.
7. Hide everything else (mismatch_key, expected/actual prices, etc. — they
   show up in the row detail when clicked).
8. Share view → restricted link → copy URL.

For Jason to be able to flip the `status` dropdown (acknowledge / resolve),
he needs **Editor** access on the base — but only on Mismatches. The simpler
option: give him **Editor on the whole base** and rely on him not editing
anything else. He's not a programmer; he won't go rooting around in
PricingRules.

If you'd rather lock it down properly, the answer is **Airtable Interfaces** —
which we can build later if it ever matters. For one manager and ~10
edits/year, plain Editor is fine.

---

## What you actually send Jason

Three URLs, one short message:

> Two pricing tools. Bookmark these.
>
> **Update a price**: <form URL>
> One screen, fill in site / product / new price / when it starts / why.
> The system closes the prior price automatically.
>
> **See current prices**: <view URL>
> Read-only list of every active price by site.
>
> **Open issues**: <mismatches view URL>
> Latest reconciliation findings. When you've looked at one, change its
> status from `open` to `acknowledged` or `resolved`.

Plus a 10-minute call to walk through it the first time. After that he's
self-sufficient.

---

## What you do for the bulk RPI uploads

The form is for single edits. For the once-or-twice-a-year RPI cycle, you
download the current master from the web app, edit the Excel, and upload
it through `/upload-master`. That's already built.

So the full pricing workflow is:

| When | Who | Where |
|---|---|---|
| Single price change | Jason (or you) | The Form |
| Annual RPI / bulk update | You | The web app's `/upload-master` form |
| Reviewing mismatches | Jason | The "Open issues" view |
| Browsing current prices | Anyone | The "Active prices" view |
| Auditing history | You | The raw `PricingRules` table in Airtable |

No Excel cost-file editing. No "edit Airtable AND Excel" double-entry. Single
source of truth.
