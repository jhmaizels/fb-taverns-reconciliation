"""
FastAPI service for the FB Taverns reconciliation system.

Endpoints:
  GET  /            -> upload form (HTTP Basic auth)
  POST /upload      -> process an uploaded LWC weekly sales .xlsx
  GET  /healthz     -> liveness check (no auth) for Render

Designed for Render deployment via render.yaml. Reads the Airtable master,
runs reconciliation, pushes mismatches + a Files row back to Airtable.
"""

from __future__ import annotations

import os
import secrets
import tempfile
import traceback
from html import escape
from pathlib import Path
from typing import Annotated

from datetime import date
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

load_dotenv()

from reconcile import parse_lwc_sales, reconcile_lines, parse_fb_cost_file, build_master, load_master, _parse_date  # noqa: E402
from master_export import build_master_xlsx_bytes  # noqa: E402
from airtable_io import (  # noqa: E402
    load_rules_from_airtable,
    load_sites_from_airtable,
    upsert_file_record,
    write_mismatches,
    write_retro_findings,
    load_agreed_retros,
    get_active_master_info,
    upsert_pricing_rules,
    upsert_products_with_retros,
    BASE_ID,
)
from summary import build_summary, render_summary_html  # noqa: E402
from retro import parse_lwc_retro, build_retro_summary, render_retro_summary_html  # noqa: E402

app = FastAPI(title="FB Taverns Reconciliation")
security = HTTPBasic()

WEB_USERNAME = os.environ.get("WEB_USERNAME", "admin")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD")

AIRTABLE_BASE_URL = f"https://airtable.com/{BASE_ID}"


def check_auth(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> str:
    if not WEB_PASSWORD:
        raise HTTPException(503, "Server misconfigured: WEB_PASSWORD env var missing")
    ok_user = secrets.compare_digest(credentials.username.encode(), WEB_USERNAME.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), WEB_PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


PAGE_HEAD = """<!doctype html>
<html><head>
<meta charset="utf-8">
<title>FB Taverns Reconciliation</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 1080px; margin: 2em auto; padding: 0 1em; color: #222; }
  h1 { margin-bottom: 0.2em; }
  h2 { margin-top: 2em; padding-bottom: 0.3em; border-bottom: 2px solid #2c5aa0; color: #2c5aa0; }
  .sub { color: #666; margin-bottom: 2em; }
  form { background: #f6f6f6; border: 1px solid #ddd; padding: 1.5em; border-radius: 6px; max-width: 540px; }
  label { display: block; margin-bottom: 0.4em; font-weight: 600; }
  select, input[type=file] { display: block; width: 100%; padding: 0.5em; margin-bottom: 1em; box-sizing: border-box; }
  button { background: #2c5aa0; color: white; border: 0; padding: 0.7em 1.5em; border-radius: 4px; font-size: 1em; cursor: pointer; }
  button:hover { background: #1d3f74; }
  .result { background: #f6f9ff; border: 1px solid #c7d8f0; padding: 1.2em; border-radius: 6px; margin-top: 1em; max-width: 540px; }
  .err { background: #fee; border: 1px solid #caa; padding: 1.2em; border-radius: 6px; }
  .summary-row { display: flex; justify-content: space-between; padding: 0.3em 0; border-bottom: 1px dotted #ccc; }
  a.button { display: inline-block; padding: 0.5em 1em; background: #2c5aa0; color: white; text-decoration: none; border-radius: 4px; margin-top: 1em; margin-right: 0.5em; }
  pre { background: #fafafa; border: 1px solid #ddd; padding: 1em; overflow-x: auto; font-size: 0.85em; }
  table { border-collapse: collapse; width: 100%; margin: 0.6em 0 1em; font-size: 0.9em; }
  th, td { padding: 0.4em 0.6em; text-align: left; border-bottom: 1px solid #eee; }
  th { background: #f4f4f4; font-weight: 600; }
  td.r, th.r { text-align: right; font-variant-numeric: tabular-nums; }
  tr.neg td:last-child strong { color: #b00020; }
  tr.pos td:last-child strong { color: #1f7a1f; }
  details.block { background: #fff; border: 1px solid #ddd; border-radius: 6px; padding: 0.6em 1em; margin-bottom: 0.8em; }
  details.block > summary { cursor: pointer; padding: 0.4em 0; font-size: 1em; }
  .pill { display: inline-block; background: #eef; color: #335; padding: 0.1em 0.6em; border-radius: 10px; font-size: 0.8em; margin-left: 0.6em; font-weight: 400; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1em; max-width: 1080px; }
  .grid2 form h3 { margin-top: 0; color: #2c5aa0; }
  .master-banner { background: #fffbe7; border: 1px solid #e6d480; border-radius: 6px; padding: 0.7em 1em; margin: 0 0 1.5em; font-size: 0.92em; color: #4a3f10; }
  .master-banner .sep { color: #b09d50; margin: 0 0.4em; }
</style>
</head><body>
"""

PAGE_FOOT = "</body></html>"


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/version")
def version():
    """Returns the running git commit (Render injects RENDER_GIT_COMMIT)."""
    return {
        "commit": os.environ.get("RENDER_GIT_COMMIT", "unknown"),
        "branch": os.environ.get("RENDER_GIT_BRANCH", "unknown"),
    }


def _master_banner_html() -> str:
    try:
        info = get_active_master_info()
    except Exception:
        return ""
    sources = info.get("sources") or []
    src_text = sources[0] if sources else "<em>none</em>"
    if len(sources) > 1:
        src_text += f' <span class="pill">+{len(sources)-1} other source(s)</span>'
    vf = info.get("latest_valid_from") or "—"
    rules = info.get("active_rule_count", 0)
    retros = info.get("products_with_retro", 0)
    return (
        '<div class="master-banner">'
        f'<strong>Master:</strong> {src_text}'
        f' <span class="sep">·</span> effective from <strong>{vf}</strong>'
        f' <span class="sep">·</span> {rules} active rules'
        f' <span class="sep">·</span> {retros} products with retros'
        '</div>'
    )


@app.get("/", response_class=HTMLResponse)
def home(_user: str = Depends(check_auth)):
    today_iso = date.today().isoformat()
    return f"""{PAGE_HEAD}
<h1>FB Taverns Reconciliation</h1>
<p class="sub">Upload a supplier file to reconcile. Or update the pricing master.</p>
{_master_banner_html()}

<h2>Reconcile a supplier file</h2>
<div class="grid2">
  <form action="/upload" method="post" enctype="multipart/form-data">
    <h3>Weekly sales</h3>
    <p class="sub">LWC weekly sales report — checks tenant and FB pricing line by line.</p>
    <label for="ws-file">Weekly sales (.xlsx)</label>
    <input type="file" name="file" id="ws-file" accept=".xlsx" required>
    <input type="hidden" name="supplier" value="LWC">
    <button type="submit">Upload &amp; reconcile</button>
  </form>
  <form action="/upload-retro" method="post" enctype="multipart/form-data">
    <h3>Monthly retro</h3>
    <p class="sub">LWC Rate Per Keg — checks the per-keg retro paid against the agreed rate.</p>
    <label for="retro-file">Monthly retro (.xlsx)</label>
    <input type="file" name="file" id="retro-file" accept=".xlsx" required>
    <input type="hidden" name="supplier" value="LWC">
    <button type="submit">Upload &amp; reconcile</button>
  </form>
</div>

<h2>Pricing master</h2>
<div class="result" style="max-width: none">
  <p style="margin-top:0"><strong>Airtable is the master.</strong> The wide cost-file Excel is now a generated <em>view</em> of Airtable, not something you edit by hand. There are three update paths:</p>
  <ol>
    <li><strong>Single price change</strong> (new tenant at one site, 6-week support, single correction) — <a href="{AIRTABLE_BASE_URL}" target="_blank">edit in Airtable directly</a>. Adding a new <code>PricingRules</code> row auto-closes the prior open one.</li>
    <li><strong>Bulk update</strong> (annual RPI, full master refresh) — download the current master, edit it in Excel, upload below.</li>
    <li><strong>Just want a copy of the current master</strong> for review or to send to a supplier — download below.</li>
  </ol>
</div>

<div class="grid2" style="margin-top:1em">
  <form action="/export-master" method="get">
    <h3>Download current master</h3>
    <p class="sub">Wide-form Excel snapshot of every active rule, in the same layout as the FB cost file.</p>
    <button type="submit">Download master.xlsx</button>
  </form>
  <form action="/upload-master" method="post" enctype="multipart/form-data">
    <h3>Upload new master version</h3>
    <p class="sub">Use for bulk updates. Existing prices on the same site/product will be closed at the effective date and replaced.</p>
    <label for="vf">Effective from</label>
    <input type="date" name="valid_from" id="vf" value="{today_iso}" required>
    <label for="reason">What changed?</label>
    <input type="text" name="reason" id="reason" maxlength="200"
       placeholder="e.g. April 2026 RPI uplift v8 — fixes Fosters retro">
    <label for="m-file">Master file (.xlsx)</label>
    <input type="file" name="file" id="m-file" accept=".xlsx" required>
    <button type="submit">Upload master</button>
  </form>
</div>

<p style="margin-top:2em"><a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable base</a></p>
{PAGE_FOOT}"""


@app.post("/upload", response_class=HTMLResponse)
async def upload(
    file: UploadFile = File(...),
    supplier: str = Form("LWC"),
    _user: str = Depends(check_auth),
):
    original_name = file.filename or "uploaded.xlsx"
    if not original_name.lower().endswith(".xlsx"):
        return _error_page(f"File must be .xlsx (got {original_name!r})")

    suffix = Path(original_name).suffix or ".xlsx"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        rules = load_rules_from_airtable()
        if not rules:
            return _error_page("No pricing rules found in Airtable. Run build-master first.")
        sites = load_sites_from_airtable()
        active_site_ids = {r.site_id for r in rules if r.valid_to is None}

        lines = parse_lwc_sales(tmp_path)
        mismatches = reconcile_lines(lines, rules, sites)

        file_rec_id = upsert_file_record(
            tmp_path,
            supplier=supplier,
            line_count=len(lines),
            file_name_override=original_name,
        )
        mismatch_count = write_mismatches(mismatches, file_rec_id)
    except Exception:
        return _error_page(f"<pre>{traceback.format_exc()}</pre>")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    summary = build_summary(original_name, lines, mismatches, sites, active_site_ids=active_site_ids)
    summary_html = render_summary_html(summary)

    return f"""{PAGE_HEAD}
<h1>Reconciliation complete</h1>
<p class="sub">{original_name} &middot; <code>{file_rec_id}</code> in Airtable &middot; {mismatch_count} mismatches inserted</p>
{_master_banner_html()}
<p>
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable</a>
  <a class="button" href="/" style="background:#666">Upload another</a>
</p>
{summary_html}
{PAGE_FOOT}"""


@app.post("/upload-retro", response_class=HTMLResponse)
async def upload_retro(
    file: UploadFile = File(...),
    supplier: str = Form("LWC"),
    _user: str = Depends(check_auth),
):
    original_name = file.filename or "uploaded.xlsx"
    if not original_name.lower().endswith(".xlsx"):
        return _error_page(f"File must be .xlsx (got {original_name!r})")

    suffix = Path(original_name).suffix or ".xlsx"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        master = load_agreed_retros()
        if not any(v.get("agreed_retro", 0) > 0 for v in master.values()):
            return _error_page("No agreed retros found in Products. Run build-master with a cost file first.")

        lines = parse_lwc_retro(tmp_path)
        summary = build_retro_summary(original_name, lines, master)

        file_rec_id = upsert_file_record(
            tmp_path,
            supplier=supplier,
            line_count=len(lines),
            file_name_override=original_name,
        )
        n_findings = write_retro_findings(summary, file_rec_id)
    except Exception:
        return _error_page(f"<pre>{traceback.format_exc()}</pre>")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    summary_html = render_retro_summary_html(summary)
    return f"""{PAGE_HEAD}
<h1>Retro reconciliation complete</h1>
<p class="sub">{original_name} &middot; <code>{file_rec_id}</code> in Airtable &middot; {n_findings} findings inserted</p>
{_master_banner_html()}
<p>
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable</a>
  <a class="button" href="/" style="background:#666">Upload another</a>
</p>
{summary_html}
{PAGE_FOOT}"""


@app.get("/export-master")
def export_master(_user: str = Depends(check_auth)):
    """Download the current Airtable master as a wide-form Excel."""
    try:
        data = build_master_xlsx_bytes()
    except Exception:
        return _error_page(f"<pre>{traceback.format_exc()}</pre>")
    filename = f"FB_Taverns_Cost_Price_File_{date.today().isoformat()}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/upload-master", response_class=HTMLResponse)
async def upload_master(
    file: UploadFile = File(...),
    valid_from: str = Form(...),
    reason: str = Form(""),
    _user: str = Depends(check_auth),
):
    original_name = file.filename or "uploaded.xlsx"
    if not original_name.lower().endswith(".xlsx"):
        return _error_page(f"File must be .xlsx (got {original_name!r})")

    vf = _parse_date(valid_from)
    if vf is None:
        return _error_page(f"Could not parse 'Effective from' date: {valid_from!r}")

    suffix = Path(original_name).suffix or ".xlsx"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        # Push directly to Airtable WITHOUT writing to local CSV — the deployed
        # service has no persistent disk, and Airtable is the source of truth.
        rules, sites, products = parse_fb_cost_file(tmp_path)
        for r in rules:
            r.valid_from = vf
            if reason:
                r.reason = reason
            r.source = original_name
        rule_count = len(rules)
        product_count = len(products)
        retros_with_value = sum(1 for p in products.values() if (p.get("retro_per_keg") or 0) > 0)

        rules_created, rules_updated, rules_closed = upsert_pricing_rules(rules, close_keys_at_date=vf)
        products_created, products_updated = upsert_products_with_retros(products)
    except Exception:
        return _error_page(f"<pre>{traceback.format_exc()}</pre>")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return f"""{PAGE_HEAD}
<h1>Master uploaded</h1>
<p class="sub">{escape(original_name)} &middot; effective from {vf.isoformat()}</p>
{_master_banner_html()}
<div class="result">
  <div class="summary-row"><span>Rules in file</span><strong>{rule_count}</strong></div>
  <div class="summary-row"><span>Rules created</span><strong>{rules_created}</strong></div>
  <div class="summary-row"><span>Rules updated</span><strong>{rules_updated}</strong></div>
  <div class="summary-row"><span>Prior rules closed at {vf.isoformat()}</span><strong>{rules_closed}</strong></div>
  <div class="summary-row"><span>Products created</span><strong>{products_created}</strong></div>
  <div class="summary-row"><span>Products updated</span><strong>{products_updated}</strong></div>
  <div class="summary-row"><span>Products with retros</span><strong>{retros_with_value}</strong></div>
  {f'<div class="summary-row"><span>Reason</span><span>{escape(reason)}</span></div>' if reason else ""}
</div>
<p style="margin-top:1.5em">
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable</a>
  <a class="button" href="/" style="background:#666">Back to home</a>
</p>
{PAGE_FOOT}"""


def _error_page(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"""{PAGE_HEAD}
<h1>Error</h1>
<div class="result err">{message}</div>
<p><a href="/">Back</a></p>
{PAGE_FOOT}""",
        status_code=400,
    )
