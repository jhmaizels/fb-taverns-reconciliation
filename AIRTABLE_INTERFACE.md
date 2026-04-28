# Airtable Interface for managers: editing prices

The raw `PricingRules` table is fine for power users but it has 13 fields and
hundreds of rows — overwhelming for a non-technical area manager. An
**Interface** is a custom front-end you build on top of the base that hides
the noise and presents only what's needed.

You'll build three pages, in order:

1. **Browse current prices** — the manager picks a site, sees what every
   product costs the tenant right now, and clicks any row to see history.
2. **Update a price** — a one-screen form for adding a new effective-dated
   rule. The Automation closes the prior one automatically.
3. **Recent issues** — read-only list of mismatches grouped by file, so
   managers can chase up disputes.

You only build this **once**. Managers then bookmark the Interface URL.

---

## Before you start

- You need to be the **Owner** or **Creator** on the base.
- Set up the Automation first (see `AIRTABLE_AUTOMATION.md`) so the
  "Update a price" page doesn't leave the master in an inconsistent state.

In Airtable, click **Interfaces** in the top toolbar, then **Start building**.
Pick **Build an interface from scratch** (not a template).

Name it something obvious, e.g. **`FB Taverns — pricing & mismatches`**.

---

## Page 1 — Browse current prices

**Pick a layout**: choose **Record review** layout.

**Configure the source**:
- Table: **PricingRules**
- Filter (this is the magic that hides closed/historical rules):
  - `valid_to` → **is empty**
- Sort: by `site` → ascending, then `product` → ascending

**Layout the record**:
- **List on the left**: show `site` and `product` (the linked names). Group by `site`.
- **Detail panel on the right**: show
  - `tenant_price` (read-only)
  - `fb_price` (read-only)
  - `retro_pct` (read-only)
  - `valid_from`
  - `status`
  - `reason`
  - `source`

Add a section header at the top of the detail panel: **"Current price for this site / product"**.

Add a button labelled **"Add new price for this site / product"** that links to
the form on Page 2 (you'll wire it up after building Page 2).

---

## Page 2 — Update a price

This is the page area managers will use most. Make it idiot-proof.

**Pick a layout**: choose **Form** layout.
**Source table**: **PricingRules**.

**Fields to show on the form** (in this order):

| Field | Behaviour | Help text to show |
|---|---|---|
| `site` | Required. Dropdown of all sites. | "Which pub is this price for?" |
| `product` | Required. Dropdown of all products. | "Which drink is the price changing for?" |
| `tenant_price` | Required. Currency. | "What does the tenant pay per keg / case?" |
| `fb_price` | Optional. Currency. | "What FB Taverns pays. Leave blank if unchanged." |
| `valid_from` | Required. Date. | "When does this new price start? Use today for an immediate change." |
| `valid_to` | Optional. Date. | "Only fill this in for **temporary** support periods. e.g. a 6-week discount that ends on a specific date. Leave blank for normal price changes." |
| `status` | Required. Dropdown. Default `tenanted`. | "Almost always **tenanted**. Use **managed** if FB Taverns now operates the pub directly. Use **supported** for a temporary support price." |
| `reason` | Required. Single line of text. | "**Why** is this changing? One short sentence — your area manager needs this to make sense in 6 months." |

**Hide all the other fields** — `rule_key`, `source`, `created_at`, the linked Mismatches.

**Default values**:
- `status` → `tenanted`
- `valid_from` → today

**On submit**: send the user back to Page 1.

### Add example "reason" text on the form

Crucially — at the very top of Page 2, add a **Text** widget with this content:

> **About this form**
>
> Use this to record any pricing change for a site:
> - **A new tenant taking over** — pick the site & product, enter the new tenant price, set "valid from" to the handover date, status `tenanted`, reason e.g. *"New tenant Hannah Williams from 1 May 2026"*.
> - **A six-week support period on one product** — fill in both **valid from** and **valid to**, status `supported`, reason e.g. *"6-week launch support on Madri 50L while tenant builds volume"*.
> - **A site converting to managed** — set tenant price = FB price, status `managed`, reason e.g. *"Site now operated directly by FB Taverns from 1 June"*.
> - **A single-product correction** — just the one row, status `tenanted`, reason e.g. *"Carling 22G corrected to £164.30 per email from Doug Trotman 14 Mar"*.
>
> The system **automatically closes the prior price** for this site & product when you save. You don't need to do anything else.

---

## Page 3 — Recent issues

**Pick a layout**: choose **List** layout.
**Source table**: **Mismatches**.
**Group by**: `file`.
**Filter**: `status` is **open**.
**Sort**: by created time, descending.

**Fields to show in each row**:
- `type` (the mismatch type)
- `severity`
- `site`
- `product`
- `delta_total`
- `notes`

Lock everything read-only **except** the `status` field — managers can
acknowledge or resolve issues by clicking the dropdown.

Add a section header: **"Open issues from recent reconciliations"**.

---

## Sharing with managers

1. Top right of the Interface, click **Share**.
2. Add `jason.french@fbtaverns.com` (and any other managers).
3. Choose **Editor** access **at the Interface level**, but **Read-only** at
   the base level. Airtable's permissions system lets the Interface override:
   the manager can edit through the Interface form (which only writes to
   `PricingRules`), but if they tried to open the base directly they'd be
   read-only.
4. Copy the Interface URL and send it to them.

For the very first session with a manager, walk them through:
- "Click here to see current prices for your site"
- "Click here to update a price — fill in the form, click save, the system
  handles the rest"
- "If something looks wrong on the latest weekly reconciliation, find it on
  Page 3, change its status to **acknowledged** so we know you've seen it"

That's it. They never need to know what `valid_to` actually means or that
there's an Automation running underneath.
