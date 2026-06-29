"""
Parse natural-language tenant support requests into structured fields via Claude.

Example input:
  "Bell 804, reducing price of Moretti 22g to £200 for six weeks starting today"

Returns:
  {
    "site_id": "804",
    "product_code": "16680008",
    "new_tenant_price": 200.00,
    "valid_from": "2026-04-28",
    "valid_to": "2026-06-09",
    "reason": "Six-week support on Birra Moretti 22G at Bell 804 from 28 April",
  }

Uses tool use for guaranteed structured output. ~$0.04 per call on Opus 4.7,
which at the expected ~10 calls/year is rounding error — no caching needed.
"""

from __future__ import annotations

import os
from datetime import date


def parse_support_request(
    text: str,
    sites: dict[str, dict],
    products: dict[str, dict],
) -> dict:
    """
    Parse a natural-language support request via Claude.

    sites    : {site_id: {"name": ..., ...}}
    products : {product_code: {"description": ..., ...}}

    Raises:
      KeyError              — ANTHROPIC_API_KEY not set
      anthropic.APIError    — API failure (rate limit, server error, etc.)
      RuntimeError          — LLM returned an unexpected response shape
      ValueError            — parsed fields fail validation
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise KeyError("ANTHROPIC_API_KEY environment variable is not set")

    # Imported lazily: the Anthropic SDK is only needed by the /add-support
    # route (~10 calls/year), so keeping it out of module import means it loads
    # on demand rather than on every cold start / non-support request path.
    import anthropic

    client = anthropic.Anthropic()
    today = date.today().isoformat()

    sites_list = "\n".join(
        f"  {sid} {info.get('name', '')}".rstrip()
        for sid, info in sorted(sites.items())
    )
    products_list = "\n".join(
        f"  {code} {info.get('description', '')}".rstrip()
        for code, info in sorted(products.items())
    )

    tool = {
        "name": "create_support_rule",
        "description": (
            "Create a tenant support rule from a parsed natural-language description. "
            "All fields are required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "site_id": {
                    "type": "string",
                    "description": (
                        "3-digit site_id from the Available sites list (e.g. '804'). "
                        "Match the user's text to the closest site by name."
                    ),
                },
                "product_code": {
                    "type": "string",
                    "description": (
                        "Product code from the Available products list. "
                        "Match the user's text to the closest product by name "
                        "(e.g. 'Moretti 22g' -> '16680008' BIRRA MORETTI 22G KEG)."
                    ),
                },
                "new_tenant_price": {
                    "type": "number",
                    "description": "The new tenant price per keg/case in pounds (positive number).",
                },
                "valid_from": {
                    "type": "string",
                    "description": (
                        f"Effective start date in YYYY-MM-DD format. Today is {today}. "
                        "Resolve relative dates ('today', 'next Monday', '1 May') correctly."
                    ),
                },
                "valid_to": {
                    "type": "string",
                    "description": (
                        f"End date in YYYY-MM-DD format. Today is {today}. "
                        "If the user says 'for six weeks', that's 42 days from valid_from."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short human-readable summary capturing site, product, "
                        "and the duration/intent of the support."
                    ),
                },
            },
            "required": [
                "site_id",
                "product_code",
                "new_tenant_price",
                "valid_from",
                "valid_to",
                "reason",
            ],
        },
    }

    user_message = f"""Parse this tenant support request into structured fields.

Today's date: {today}

Available sites:
{sites_list}

Available products:
{products_list}

Description from operator:
{text!r}

Use the create_support_rule tool to return the parsed fields. Match site and
product references to the closest IDs in the lists above (fuzzy match is fine
— "Moretti 22g" should match BIRRA MORETTI 22G KEG). Resolve relative dates
("today", "next Monday", "for six weeks") into ISO YYYY-MM-DD strings."""

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        tools=[tool],
        tool_choice={"type": "tool", "name": "create_support_rule"},
        messages=[{"role": "user", "content": user_message}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "create_support_rule":
            return block.input

    raise RuntimeError(
        f"LLM did not return a create_support_rule tool call. "
        f"stop_reason={response.stop_reason!r}"
    )


def validate_support_fields(
    fields: dict,
    sites: dict[str, dict],
    products: dict[str, dict],
) -> list[str]:
    """Check parsed fields against current master state. Returns list of error messages (empty if valid)."""
    errors: list[str] = []
    sid = str(fields.get("site_id", "")).strip()
    code = str(fields.get("product_code", "")).strip()

    if not sid:
        errors.append("site_id is missing")
    elif sid not in sites:
        errors.append(f"site_id {sid!r} is not in the master")

    if not code:
        errors.append("product_code is missing")
    elif code not in products:
        errors.append(f"product_code {code!r} is not in the master")

    try:
        price = float(fields.get("new_tenant_price", 0))
        if price <= 0:
            errors.append(f"new_tenant_price must be positive (got {price})")
    except (TypeError, ValueError):
        errors.append(f"new_tenant_price is not a number: {fields.get('new_tenant_price')!r}")

    from reconcile import _parse_date  # type: ignore

    vf = _parse_date(fields.get("valid_from"))
    vt = _parse_date(fields.get("valid_to"))
    if vf is None:
        errors.append(f"valid_from could not be parsed: {fields.get('valid_from')!r}")
    if vt is None:
        errors.append(f"valid_to could not be parsed: {fields.get('valid_to')!r}")
    if vf and vt and vt <= vf:
        errors.append(f"valid_to ({vt}) must be after valid_from ({vf})")

    if not (fields.get("reason") or "").strip():
        errors.append("reason is empty")

    return errors
