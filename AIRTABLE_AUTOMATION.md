# Airtable Automation: auto-close prior pricing rules

When a manager adds a new row to `PricingRules` with a fresh `valid_from`, this
Automation finds any prior open rule for the same site + product and sets its
`valid_to` to the new date. That keeps the master internally consistent
without anyone having to remember to close the previous rule by hand.

You only need to set this up **once** as the base owner. Managers then just add
rows in the Interface (see `AIRTABLE_INTERFACE.md`) and the Automation handles
the rest invisibly.

---

## Setup steps (one-time, ~5 minutes)

1. Open the Airtable base **FB Taverns Reconciliation**.
2. Click **Automations** in the top toolbar.
3. Click **Create automation**.
4. Rename it (top-left) to something obvious, e.g. **`Auto-close prior pricing rule`**.

### Configure the trigger

5. Under **Trigger**, choose **When a record is created**.
6. Pick the table: **PricingRules**.
7. Click **Test trigger** — it'll show the most recent record. You don't need to do anything with it; it's just confirming the trigger is wired up.

### Add the action

8. Under **Actions**, click **+ Add advanced logic or action** → **Run a script**.
9. In the script editor, scroll down to **Input variables** and add **two** variables:

   | Variable name | Source |
   |---|---|
   | `recordId` | Record (from step 1: When record created) → **Airtable record ID** |
   | `validFrom` | Record (from step 1) → **valid_from** field |

10. **Replace** all the default code in the script panel with the script in the
   "Script" section below.
11. Click **Test** to run it against the most-recent record. You should see "No
    prior rule to close" or "Closed N rule(s)" — both are valid responses
    depending on the test record.
12. Click **Turn on** (top right). Done.

---

## Script

Copy this whole block into the script editor:

```javascript
const config = input.config();
const newRecordId = config.recordId;
const newValidFrom = config.validFrom;

if (!newValidFrom) {
    output.text('New rule has no valid_from — skipping.');
    return;
}

const table = base.getTable('PricingRules');
const newRecord = await table.selectRecordAsync(newRecordId, {
    fields: ['site', 'product', 'valid_from'],
});
if (!newRecord) {
    output.text('New record not found.');
    return;
}

const siteLink = newRecord.getCellValue('site');
const productLink = newRecord.getCellValue('product');
if (!siteLink || siteLink.length === 0 || !productLink || productLink.length === 0) {
    output.text('New rule missing site or product link — skipping.');
    return;
}
const siteId = siteLink[0].id;
const productId = productLink[0].id;

// Find every other rule for the same (site, product) that is currently open
// (valid_to is empty) and dated earlier than the new rule.
const all = await table.selectRecordsAsync({
    fields: ['site', 'product', 'valid_from', 'valid_to'],
});

const toClose = [];
for (const rec of all.records) {
    if (rec.id === newRecordId) continue;
    const s = rec.getCellValue('site');
    const p = rec.getCellValue('product');
    if (!s || s.length === 0 || s[0].id !== siteId) continue;
    if (!p || p.length === 0 || p[0].id !== productId) continue;
    if (rec.getCellValue('valid_to')) continue;
    const vf = rec.getCellValueAsString('valid_from');
    if (!vf || vf >= newValidFrom) continue;
    toClose.push({ id: rec.id, fields: { valid_to: newValidFrom } });
}

if (toClose.length === 0) {
    output.text('No prior open rule to close.');
    return;
}

// Airtable allows up to 50 records per updateRecordsAsync call.
for (let i = 0; i < toClose.length; i += 50) {
    await table.updateRecordsAsync(toClose.slice(i, i + 50));
}
output.text(`Closed ${toClose.length} prior rule(s) at ${newValidFrom}.`);
```

---

## How it actually works (in plain English)

When a manager adds, say:

> A new row for **Bell 804** + **Carling 11G**, valid from **2026-05-15**, tenant price **£189.50**

…the Automation:

1. Looks at every existing rule for **Bell 804** + **Carling 11G**.
2. Finds the one that's currently active (no `valid_to`) and dated earlier than 2026-05-15.
3. Sets that rule's `valid_to` to 2026-05-15.

Result: the old price is automatically retired the moment the new one starts.
Reconciliations dated before 2026-05-15 still find the old price; on or after,
they find the new one. No one had to remember.

---

## Limits to know about

- **Free tier**: 100 automation runs/month. A manager editing one or two prices a
  week is well under that. If you hit the cap, upgrade to Team (~£20/seat/mo)
  or wait for the month to reset.
- **The Automation only runs when a row is _created_**, not when an existing row
  is _edited_. That's intentional: the right way to change a price is to add a
  new effective-dated row, not edit history. If a manager edits a closed rule
  by mistake, that doesn't trigger anything — but it shouldn't be done.
- **One race condition to be aware of**: if two managers create a new rule for
  the same (site, product) at the same instant, both Automations will fire and
  both will try to close the prior rule. Airtable handles this gracefully (the
  second update is a no-op since `valid_to` is already set), but the second
  rule will end up "open" alongside the first. In practice this is vanishingly
  rare, and the Reconciliations CSV report would surface the overlap if it ever
  caused a real mismatch.
