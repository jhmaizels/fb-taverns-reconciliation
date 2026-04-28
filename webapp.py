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
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

load_dotenv()

from reconcile import parse_lwc_sales, reconcile_lines  # noqa: E402
from airtable_io import (  # noqa: E402
    load_rules_from_airtable,
    load_sites_from_airtable,
    upsert_file_record,
    write_mismatches,
    write_retro_findings,
    load_agreed_retros,
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


@app.get("/", response_class=HTMLResponse)
def home(_user: str = Depends(check_auth)):
    return f"""{PAGE_HEAD}
<h1>FB Taverns Reconciliation</h1>
<p class="sub">Upload a supplier file. Results post to Airtable.</p>
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
<p>
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable</a>
  <a class="button" href="/" style="background:#666">Upload another</a>
</p>
{summary_html}
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
