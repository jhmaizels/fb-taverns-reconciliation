"""
One-off rollout of the white-label fixed tenant prices (operator, 2026-07-15).

FB Taverns white-labels three products with FIXED per-keg tenant prices:

    17910010  Appleshed Premium Cider 50L   ->  FB Cider   £145
    15621274  Black Sheep Smooth 50L Keg    ->  FB Bitter  £135
    19100003  Pilsner 11g                   ->  FB Lager   £135

(The same prices drive the findings page's suggested pricing —
summary.WHITE_LABEL_PRICES.)

This script ADDS a PricingRule at the fixed price for every active tenanted
site that does not already have an ACTIVE tenant price for the product.
Existing prices are never touched or closed — add-only where missing:

  - a site counts as active if it has at least one active rule (valid_to None);
  - sites whose Sites.status is 'managed' are skipped (managed sites buy at
    FB net — a fixed tenant price there would be wrong and would trip
    site_should_be_managed findings);
  - fb_price / retro_pct / product_desc are inherited from any existing active
    rule for the same product elsewhere on the estate, so margins and net
    prices render consistently. If the product is new to the master, the rule
    is tenant-only (no fb_price) until the next cost-file upload fills it in.

Usage:
    python rollout_whitelabel_prices.py            # dry run — prints the plan
    python rollout_whitelabel_prices.py --apply    # writes to Airtable
"""

from __future__ import annotations

import sys
from datetime import date

from reconcile import Rule
from summary import WHITE_LABEL_PRICES

# Descriptions used only if the product has no rule anywhere yet.
_FALLBACK_DESC = {
    "17910010": "Appleshed Premium Cider 50L",
    "15621274": "Black Sheep Smooth 50L Keg",
    "19100003": "Pilsner 11g",
}


def main(argv: list[str]) -> int:
    apply = "--apply" in argv

    from airtable_io import (
        load_rules_from_airtable,
        load_sites_from_airtable,
        upsert_pricing_rules,
    )

    rules = load_rules_from_airtable()
    sites = load_sites_from_airtable()

    active_rules = [r for r in rules if r.valid_to is None]
    active_site_ids = sorted({r.site_id for r in active_rules if r.site_id})
    managed = {sid for sid, info in sites.items() if (info.get("status") or "") == "managed"}

    today = date.today()
    to_create: list[Rule] = []
    report: list[str] = []

    for code, (label, price) in WHITE_LABEL_PRICES.items():
        product_rules = [r for r in active_rules if r.product_code == code]
        covered = {r.site_id for r in product_rules if r.tenant_price is not None}
        donor = next((r for r in product_rules if r.fb_price), None)
        desc = (donor.product_desc if donor and donor.product_desc
                else next((r.product_desc for r in product_rules if r.product_desc), "")) \
            or _FALLBACK_DESC.get(code, "")

        for sid in active_site_ids:
            if sid in covered:
                continue
            if sid in managed:
                report.append(f"  skip {sid} ({sites.get(sid, {}).get('name', '?')}): managed site")
                continue
            to_create.append(Rule(
                site_id=sid,
                product_code=code,
                product_desc=desc,
                tenant_price=float(price),
                fb_price=(donor.fb_price if donor else None),
                retro_pct=(donor.retro_pct if donor else 0.0),
                valid_from=today,
                valid_to=None,
                status="tenanted",
                reason=f"White-label rollout: {label} fixed £{price:.2f}/keg",
                source="operator direction 2026-07-15",
            ))
        n_new = sum(1 for r in to_create if r.product_code == code)
        print(f"{code} ({label} £{price:.0f}): {len(covered)} sites already priced, "
              f"{n_new} to add, donor fb_price="
              f"{f'£{donor.fb_price:.2f}' if donor and donor.fb_price else 'none'}")

    for line in sorted(set(report)):
        print(line)

    if not to_create:
        print("\nNothing to do — every active site already has a price for all three products.")
        return 0

    print(f"\n{len(to_create)} rules to create across {len(active_site_ids)} active sites.")
    if not apply:
        for r in to_create:
            print(f"  + {r.site_id} {r.product_code} £{r.tenant_price:.2f} ({r.product_desc})")
        print("\nDry run — re-run with --apply to write to Airtable.")
        return 0

    # close_keys_at_date=None: add-only — nothing existing is closed. The keys
    # we create are, by construction, for (site, product) pairs with no active
    # tenant price, so no same-key collision is possible.
    created, updated, closed = upsert_pricing_rules(to_create, close_keys_at_date=None)
    print(f"Airtable: {created} created, {updated} updated, {closed} closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
