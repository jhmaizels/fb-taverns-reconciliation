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

import logging
import os
import tempfile
import time
from html import escape
from pathlib import Path

from datetime import date, timedelta
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

load_dotenv()

# Supabase auth core (per-user drinks roles, RLS-scoped; dual-mode legacy Basic
# fallback during cutover). See auth_supabase.py + the integration contract.
from auth_supabase import (  # noqa: E402
    DrinksPrincipal,
    EXTERNAL_BASE_PATH,
    FORBIDDEN_DETAIL,
    NO_ACCESS_DETAIL,
    PUBLIC_ORIGIN,
    ROLE_RANK,
    SUPABASE_ANON_KEY,
    SUPABASE_URL,
    TENANCY_ADMIN_URL,
    clear_session_cookies,
    ext_url,
    install_auth_handlers,
    require_drinks_role,
    set_session_cookies,
    validate_token,
)
from login_page import (  # noqa: E402
    render_callback_page,
    render_login_page,
    render_no_access_page,
)

from reconcile import (  # noqa: E402
    parse_lwc_sales,
    reconcile_lines,
    parse_fb_cost_file,
    build_master,
    load_master,
    _parse_date,
    Rule,
)
# master_export (which imports openpyxl) is imported lazily inside /export-master
# so it stays off the cold-start / health-check readiness path.
from support_parser import parse_support_request, validate_support_fields  # noqa: E402
from airtable_io import (  # noqa: E402
    create_site,
    delete_product,
    delete_site,
    end_all_product_rules,
    end_all_site_rules,
    update_product,
    load_master_snapshot,
    publish_patched_snapshot,
    refresh_master_cache_async,
    rename_site,
    load_rules_from_airtable,
    load_sites_from_airtable,
    upsert_file_record,
    write_mismatches,
    write_retro_findings,
    load_agreed_retros,
    get_active_master_info,
    upsert_pricing_rules,
    upsert_products_with_retros,
    load_tennents_agreements,
    replace_tennents_master,
    get_tennents_master_info,
    write_tennents_findings,
    BASE_ID,
)
from tennents import (  # noqa: E402
    parse_master as parse_tennents_master,
    parse_monthly as parse_tennents_monthly,
    reconcile as reconcile_tennents,
    render_summary_html as render_tennents_summary_html,
)
from summary import build_summary, render_summary_html  # noqa: E402
from retro import parse_lwc_retro, build_retro_summary, render_retro_summary_html  # noqa: E402
# Master editor (design docs/master-editor-design.md): master_changes is the
# single seam every edit flows through; master_pages holds the HTML bodies +
# form codec so the routes below stay thin.
import master_pages  # noqa: E402
from master_changes import (  # noqa: E402
    INCREASE_HARD_LIMIT_PCT,
    INCREASE_SANITY_BAND_PCT,
    MasterChange,
    apply_master_change,
    build_universal_increase,
    patch_snapshot_for_change,
    patch_snapshot_for_bulk_upsert,
    preview_master_change,
    validate_master_change,
)
from starlette.concurrency import run_in_threadpool  # noqa: E402

app = FastAPI(title="FB Taverns Reconciliation")
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)

# Register the auth core's redirect-as-exception handler. Without this, an
# unauthenticated GET raises _RedirectException -> unhandled 500 instead of a
# relative 303 to /login. Must be called once, right after app creation.
install_auth_handlers(app)

AIRTABLE_BASE_URL = f"https://airtable.com/{BASE_ID}"

# ---------- request + Airtable timing (INF-1) ----------
# The service had never been profiled. Log one line per request (method, path,
# status, ms) and rely on airtable_io's per-call _list_all/_batch lines (child
# logger "fbtaverns.airtable") to break the upload down by table. Own handler +
# propagate=False so logs land on stdout (Render captures it) without uvicorn
# double-logging. Only counts/timing/paths are logged — never field contents or
# the bearer token.
logger = logging.getLogger("fbtaverns")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


import threading

# Set once the boot master-cache warm-up SUCCEEDS. /healthz stays 503 until
# then (or a deadline) so Render's zero-downtime cutover waits for a WARM
# instance — otherwise the first /master hit on the new instance eats the ~30s
# inline rebuild and times out the hub proxy (the whole point of the warm-up).
_master_cache_ready = threading.Event()
# ...but never block a deploy forever on a persistent Airtable outage: after
# this many seconds /healthz reports ready regardless (accepting a possibly
# cold first hit over a stuck deploy).
_READINESS_DEADLINE_S = 60


@app.on_event("startup")
def _warm_master_cache() -> None:
    """Build the master snapshot in the background at boot so the FIRST page
    request after a deploy/restart doesn't do the ~30s inline Airtable rebuild
    (which times out the tenancy-hub proxy → bare 500). Post-boot expiries are
    handled by stale-while-revalidate in load_master_snapshot."""

    def _warm() -> None:
        try:
            load_master_snapshot()
            _master_cache_ready.set()
            logger.info("master-cache warm-up complete")
        except Exception:
            logger.warning("master-cache warm-up failed", exc_info=True)

    threading.Thread(target=_warm, daemon=True, name="master-cache-warmup").start()


@app.middleware("http")
async def _timing_middleware(request, call_next):
    # Direct-host bounce. The hub proxy STRIPS the /drinks prefix, so any request
    # that still carries EXTERNAL_BASE_PATH must have hit the OLD direct service
    # URL (a stale bookmark) — on which /drinks/<x> is a non-existent route (404).
    # Redirect it to the canonical proxied URL so it keeps working. Loop-safe:
    # proxied requests never carry the prefix, so this only fires on the direct host.
    if EXTERNAL_BASE_PATH and PUBLIC_ORIGIN:
        _p = request.url.path
        if _p == EXTERNAL_BASE_PATH or _p.startswith(EXTERNAL_BASE_PATH + "/"):
            _tgt = f"{PUBLIC_ORIGIN}{_p}"
            if request.url.query:
                _tgt = f"{_tgt}?{request.url.query}"
            return RedirectResponse(_tgt, status_code=307)

    start = time.perf_counter()
    response = await call_next(request)
    # Apply any session-cookie op the auth dependency staged on request.state.
    # Done HERE, on the real outgoing response, because routes that return their
    # own Response/HTMLResponse (e.g. /export-master, _error_page) drop cookies
    # set on the FastAPI-injected Response. scope["state"] is shared between the
    # dependency and this middleware (starlette 0.41.x), so the staged op is
    # visible after call_next regardless of how the route returned.
    rewrite = getattr(request.state, "drinks_cookie_rewrite", None)
    if rewrite:
        set_session_cookies(response, rewrite[0], rewrite[1])
    elif getattr(request.state, "drinks_clear_cookies", False):
        clear_session_cookies(response)
    # Never let an authed HTML page (pricing/tenant data, and dynamic grids that
    # change on every deploy) sit in a browser/proxy cache. Without this the app
    # sends NO Cache-Control, so a browser can serve a stale copy after a deploy —
    # "I don't see my change". Scoped to text/html so /export-master (xlsx) and
    # /version (json) keep their own semantics.
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    # Baseline security response headers (pre-launch assessment). The app is
    # served inside the hub via a same-origin rewrite (not an iframe), so
    # frame-ancestors 'none' is safe and blocks clickjacking; nosniff stops
    # content-type confusion; Referrer-Policy avoids leaking the (tokenless)
    # URLs. No broad CSP: the pages use small inline <script>/<style> the CSP
    # would have to allowlist — deferred to avoid breaking them at launch.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.0f}"
    logger.info("%s %s -> %s in %.0fms", request.method, request.url.path, response.status_code, elapsed_ms)
    return response


# Uploads are all small supplier/cost .xlsx files (a few MB); an xlsx is a
# zip, so cap the bytes we buffer into openpyxl to refuse an accidental huge
# or zip-bomb file before it can OOM the shared /drinks process (this service
# has an OOM/restart history). Pre-launch assessment P2.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


class _UploadTooLarge(Exception):
    pass


def _read_upload_capped(file: "UploadFile", cap: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read an UploadFile fully but refuse anything over `cap` bytes, reading
    in chunks so an oversize file is rejected without buffering all of it."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise _UploadTooLarge(
                f"file is over {cap // (1024 * 1024)} MB — a supplier/cost file is only a few MB"
            )
        chunks.append(chunk)
    return b"".join(chunks)


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    # Log the full traceback (Render captures stderr) so 500s are diagnosable.
    # The response is always the plain 500 — never surface tracebacks in-page.
    import traceback as _tb
    tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("unhandled error on %s %s:\n%s", request.method, request.url.path, tb)
    return PlainTextResponse("Internal Server Error", status_code=500)


# ---------- auth 403 rendering ----------
# require_drinks_role raises HTTPException(403, detail=NO_ACCESS_DETAIL) for a
# signed-in user with no drinks row, and HTTPException(403, detail=FORBIDDEN_DETAIL)
# for a role below the route minimum. Render friendly pages for those; everything
# else falls back to FastAPI's default HTTPException handling. Cookie rewrites that
# the dependency staged on a refreshed-but-unauthorised request are preserved by
# copying any Set-Cookie headers from exc.headers (the dependency does not set
# them on the exception, so this is a no-op today, but keeps the contract honest).
@app.exception_handler(StarletteHTTPException)
async def _auth_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 403 and exc.detail == NO_ACCESS_DETAIL:
        principal = getattr(request.state, "drinks", None)
        email = principal.email if principal else ""
        return HTMLResponse(render_no_access_page(email, TENANCY_ADMIN_URL), status_code=403)
    if exc.status_code == 403 and exc.detail == FORBIDDEN_DETAIL:
        # The dependency attaches the principal before the role check, so the
        # signed-in identity + sign-out render even on refusal.
        principal = getattr(request.state, "drinks", None)
        return HTMLResponse(
            f"""{render_head(principal.email if principal else "", principal.role if principal else "")}
<h1>Not allowed</h1>
<div class="result err">You don't have permission to perform this action. Ask an administrator to raise your drinks access level.</div>
<p><a href="{ext_url('/')}">Back</a></p>
{PAGE_FOOT}""",
            status_code=403,
        )
    # Default behaviour for all other HTTPExceptions (401, 404, etc.).
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None) or None)


HEAD_STYLE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='3' fill='%23324556'/%3E%3Ctext x='8' y='12' font-size='10' text-anchor='middle' fill='white' font-family='sans-serif'%3EF%3C/text%3E%3C/svg%3E">
<title>FB Taverns — Drinks Reconciliation</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; color: #222; }
  .container { max-width: 1080px; margin: 2em auto; padding: 0 1em; }
  .site-header { background: #324556; display: flex; align-items: center; justify-content: space-between; padding: 0.55em 1.5em; }
  .site-header .brand img { height: 44px; display: block; }
  .site-user { display: flex; align-items: center; gap: 0.6em; }
  .site-user .who { color: rgba(255,255,255,0.85); font-size: 0.8em; }
  .site-user form { background: none; border: 0; padding: 0; margin: 0; max-width: none; }
  .site-user button.signout { background: rgba(255,255,255,0.12); color: #fff; border: 1px solid rgba(255,255,255,0.3); padding: 0.3em 0.8em; border-radius: 4px; font-size: 0.78em; cursor: pointer; }
  .site-user button.signout:hover { background: rgba(255,255,255,0.25); }
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
  .card-link { display: block; padding: 1.5em; background: #f6f9ff; border: 1px solid #c7d8f0; border-radius: 8px; text-decoration: none; color: #222; transition: background 0.1s; }
  .card-link:hover { background: #eaf1fa; }
  .card-link h2 { color: #2c5aa0; border-bottom: none; padding-bottom: 0; }
  .card-tag { display: inline-block; background: #e0e8f0; color: #2c5aa0; padding: 0.1em 0.6em; border-radius: 10px; font-size: 0.5em; font-weight: 600; vertical-align: middle; margin-left: 0.5em; }
  .card-meta { background: #fff; padding: 0.7em 1em; border-radius: 4px; font-size: 0.9em; }
  .card-cta { color: #2c5aa0; font-weight: 600; }
  .estate-tag { display: inline-block; background: #e0e8f0; color: #2c5aa0; padding: 0.15em 0.7em; border-radius: 12px; font-size: 0.4em; font-weight: 600; vertical-align: middle; margin-left: 0.5em; }
  .grid2 form > h3 + h3, .grid2 form .second-h3 { margin-top: 1.5em; padding-top: 1em; border-top: 1px solid #ddd; }
  textarea { width: 100%; padding: 0.5em; box-sizing: border-box; font-family: inherit; font-size: 0.95em; min-height: 80px; }
  .support-tag { display: inline-block; background: #fff4cf; color: #8a6500; padding: 0.05em 0.5em; border-radius: 8px; font-size: 0.7em; font-weight: 700; margin-left: 0.5em; vertical-align: middle; }
  tr.support-note td { background: #fffbe7; color: #4a3f10; border-bottom: 1px solid #eee; padding-top: 0.2em; padding-bottom: 0.4em; font-size: 0.85em; }
  .master-banner { background: #fffbe7; border: 1px solid #e6d480; border-radius: 6px; padding: 0.7em 1em; margin: 0 0 1.5em; font-size: 0.92em; color: #4a3f10; }
  .master-banner .sep { color: #b09d50; margin: 0 0.4em; }
  tr.ended td { color: #999; }
  .help { font-size: 0.85em; color: #555; margin: -0.4em 0 1em; line-height: 1.4; }
  .help strong { color: #2c5aa0; }
  /* --- pricing-master pivot: products (rows) x sites (cols), Excel-style --- */
  .pivot-wide { width: 96vw; max-width: 96vw; position: relative; left: 50%; transform: translateX(-50%); }
  .pivot-toolbar { display: flex; align-items: center; gap: 0.7em; flex-wrap: wrap; margin: 0.6em 0; }
  .pivot-toolbar .grow { flex: 1 1 auto; }
  .pivot-toolbar form { background: none; border: 0; padding: 0; margin: 0; max-width: none; display: flex; gap: 0.5em; align-items: center; }
  .pivot-toolbar input[type=search] { width: 260px; padding: 0.45em 0.6em; margin: 0; box-sizing: border-box; }
  button.toggle { background: #33691e; }
  button.toggle:hover { background: #274f16; }
  .pivot-wrap { overflow: auto; max-height: 78vh; border: 1px solid #ddd; border-radius: 6px; margin: 0.4em 0 1em; }
  table.pivot { border-collapse: separate; border-spacing: 0; width: auto; min-width: 100%; font-size: 0.84em; margin: 0; }
  table.pivot th, table.pivot td { border-bottom: 1px solid #ececec; border-right: 1px solid #ececec; padding: 0.35em 0.6em; background: #fff; vertical-align: top; }
  table.pivot thead th { position: sticky; top: 0; z-index: 3; background: #eef2f7; text-align: right; vertical-align: bottom; font-weight: 600; }
  table.pivot thead th.site { max-width: 120px; white-space: normal; line-height: 1.15; }
  table.pivot thead th.site .sid { display: block; font-weight: 400; color: #789; font-size: 0.82em; }
  table.pivot .sticky-col { position: sticky; z-index: 2; background: #fff; text-align: left; white-space: normal; }
  table.pivot .sticky-col.c1 { left: 0; width: 72px; min-width: 72px; max-width: 72px; box-sizing: border-box; overflow-wrap: anywhere; }
  table.pivot .sticky-col.c2 { left: 72px; min-width: 180px; max-width: 240px; box-shadow: 1px 0 0 #ddd; }
  table.pivot thead th.sticky-col { z-index: 5; background: #eef2f7; }
  table.pivot td.pcode, table.pivot .pcode { color: #567; font-size: 0.9em; font-variant-numeric: tabular-nums; }
  table.pivot td.num, table.pivot th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  table.pivot td.pinfo { color: #444; background: #fbfcfe; }
  table.pivot tbody tr:hover td { background: #f2f7ff; }
  table.pivot tbody tr:hover td.sticky-col { background: #f2f7ff; }
  table.pivot .cell-margin { display: none; }
  table.pivot.show-margin .cell-price { display: none; }
  table.pivot.show-margin .cell-margin { display: block; }
  .cell-margin .pct { display: block; font-size: 0.82em; color: #777; font-weight: 400; }
  .cell-neg { color: #b00020; }
  .cell-warn { color: #8a6500; }
  .cell-pos { color: #1f7a1f; }
  .pivot-empty { color: #cbd2da; }
  table.pivot tr.section td { background: #e8edf3; font-weight: 600; color: #2c5aa0; font-size: 0.95em; padding: 0.25em 0.6em; }
  /* in-grid editing: each cell is a one-field form (Enter submits) */
  table.pivot form.cellf { background: none; border: 0; padding: 0; margin: 0; max-width: none; }
  table.pivot input.cell-input { width: 84px; text-align: right; font-variant-numeric: tabular-nums; padding: 0.25em 0.4em; margin: 0; border: 1px solid #c9d6e4; border-radius: 3px; font-size: 1em; box-sizing: border-box; display: inline-block; }
  table.pivot input.cell-input:focus { border-color: #2c5aa0; outline: 2px solid #dcebff; background: #fbfdff; }
  table.pivot td.edit-cell { padding: 0.2em 0.35em; }
  /* single-site view: stay inside the normal page column, table hugs content */
  .pivot-single .pivot-wrap { display: inline-block; max-width: 100%; }
  .pivot-single table.pivot { min-width: 0; }
  /* top scrollbar mirroring the bottom one (JS-synced; hidden when no overflow) */
  .pivot-topscroll { overflow-x: auto; overflow-y: hidden; margin: 0.4em 0 0; }
  .pivot-topscroll > div { height: 1px; }
  /* edit mode: site headers open the rename form */
  table.pivot a.site-head { color: inherit; text-decoration: none; border-bottom: 1px dashed #9ab0c8; }
  table.pivot a.site-head:hover { color: #2c5aa0; border-bottom-color: #2c5aa0; }
</style>
</head><body>
"""


def render_head(user_email: str = "", drinks_role: str = "") -> str:
    """Full page head + navy site header + opening <main>.

    Replaces the old PAGE_HEAD constant. The header is deliberately minimal:
    the FB logo (always returns to the HUB portal — "/" when proxied under
    EXTERNAL_BASE_PATH, the app root when standalone) plus, on the right,
    the signed-in email (escaped) and a relative sign-out form
    (POST /auth/signout). No duplicate nav links, no cross-app links —
    the hub portal is the crossroads (admin is reached from its Admin tile).
    Called with empty strings for error / pre-auth pages — the header still
    renders, just without identity.
    """
    user_block = ""
    if user_email or drinks_role:
        who = f'<span class="who">{escape(user_email)}</span>' if user_email else ""
        user_block = (
            '<div class="site-user">'
            f'{who}'
            f'<form method="post" action="{ext_url("/auth/signout")}">'
            '<button type="submit" class="signout">Sign out</button>'
            '</form>'
            '</div>'
        )
    # Proxied: the logo goes to the hub portal at the PUBLIC root. Standalone:
    # ext_url("/") is the app's own root (identity with no base path).
    brand_href = "/" if EXTERNAL_BASE_PATH else ext_url("/")
    return f"""{HEAD_STYLE}<header class="site-header">
  <a class="brand" href="{brand_href}"><img src="{ext_url('/static/fb-taverns-logo.png')}" alt="FB Taverns"></a>
  {user_block}
</header>
<main class="container">
"""

PAGE_FOOT = "</main></body></html>"


_PROCESS_START = time.time()


@app.get("/healthz")
def healthz():
    """Readiness check for Render's zero-downtime cutover: 200 only once the
    master cache is warm (or the deadline has passed), so traffic isn't routed
    to an instance that would eat the ~30s inline rebuild on its first hit.
    Liveness is implied — the process answering at all means it's up."""
    warm = _master_cache_ready.is_set()
    if warm or (time.time() - _PROCESS_START) > _READINESS_DEADLINE_S:
        return {"status": "ok", "warm": warm}
    return JSONResponse({"status": "warming"}, status_code=503)


@app.get("/version")
def version():
    """Running git commit (Render injects RENDER_GIT_COMMIT) + process vitals.

    up_s resets to ~0 whenever the process restarts — the tell for a crash/OOM
    kill (a burst of proxy-level 500s followed by up_s ~0 = the process died
    mid-request). rss_mb is the peak resident memory (ru_maxrss, KB on Linux)."""
    rss_mb = None
    try:
        import resource
        rss_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        pass
    return {
        "commit": os.environ.get("RENDER_GIT_COMMIT", "unknown"),
        "branch": os.environ.get("RENDER_GIT_BRANCH", "unknown"),
        "up_s": round(time.time() - _PROCESS_START, 1),
        "rss_mb": rss_mb,
    }


# ---------------------------------------------------------------------------
# Auth endpoints — all OPEN (no auth dependency). The login/callback pages run
# supabase-js client-side (PKCE) and POST the resulting tokens to /auth/session,
# which validates them and sets HttpOnly cookies. Sign-out clears the cookies
# and relative-redirects to /login. SUPABASE_URL + anon key are injected
# server-side into the login/callback HTML (anon key is public by design).
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_page():
    """Self-contained login page (M365 + email/password + sign-up toggle)."""
    return HTMLResponse(render_login_page(supabase_url=SUPABASE_URL, anon_key=SUPABASE_ANON_KEY))


@app.get("/auth/callback", response_class=HTMLResponse)
def auth_callback():
    """OAuth/PKCE callback landing page. supabase-js completes the exchange
    client-side, then POSTs tokens to /auth/session and relative-redirects."""
    return HTMLResponse(render_callback_page(supabase_url=SUPABASE_URL, anon_key=SUPABASE_ANON_KEY))


def _is_cross_origin(request: Request) -> bool:
    """True iff the request carries an Origin header that differs from BOTH our
    own derived origin AND the configured PUBLIC_ORIGIN. Used to block login-CSRF
    (forged token POST to /auth/session) and forced-signout from another site. A
    same-origin fetch/form (or a non-browser client with no Origin header)
    returns False. Trusts Render's X-Forwarded-*.

    Under the tenancy-master proxy the browser Origin is the PUBLIC host
    (https://tenancy-master.onrender.com), NOT this drinks service host, so the
    request's own derived origin won't match. PUBLIC_ORIGIN (env) is accepted as
    an additional allowed origin so legitimate proxied logins aren't wrongly
    blocked. Genuinely foreign origins are still rejected."""
    origin = request.headers.get("origin")
    if not origin:
        return False
    origin = origin.rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    own_origin = f"{proto}://{host}".rstrip("/")
    if origin == own_origin:
        return False
    if PUBLIC_ORIGIN and origin == PUBLIC_ORIGIN:
        return False
    return True


@app.post("/auth/session")
async def auth_session(request: Request):
    """Receive {access_token, refresh_token, next} from the callback page,
    validate the access token, set HttpOnly cookies. Never logs token values."""
    # Login-CSRF guard: a cross-site page must not be able to mint a session
    # cookie in this user's browser by POSTing attacker-supplied tokens.
    if _is_cross_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    access_token = (body.get("access_token") or "").strip() if isinstance(body, dict) else ""
    refresh_token = (body.get("refresh_token") or "").strip() if isinstance(body, dict) else ""
    if not access_token or not refresh_token:
        raise HTTPException(status_code=400, detail="Missing tokens")

    # Confirm the token is real before trusting it (do not set cookies on junk).
    if validate_token(access_token) is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    nxt = body.get("next") if isinstance(body, dict) else None
    safe_next = nxt if isinstance(nxt, str) and nxt.startswith("/") else "/"

    resp = JSONResponse({"ok": True, "next": safe_next})
    set_session_cookies(resp, access_token, refresh_token)
    return resp


@app.post("/auth/signout")
def auth_signout(request: Request):
    """Clear the drinks session cookies, then end the WHOLE session.

    Proxied (EXTERNAL_BASE_PATH set): after clearing our cookies, 307-redirect
    to the HUB's /auth/signout — 307 preserves the POST method so the hub's
    signout handler actually runs, kills the hub Supabase session, and then
    redirects to its login. Standalone: keep the original behaviour — 303 to
    our own /login."""
    # Same-origin guard so a cross-site page can't force-sign-out the user.
    if _is_cross_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    if EXTERNAL_BASE_PATH:
        resp = RedirectResponse(url="/auth/signout", status_code=307)
    else:
        resp = RedirectResponse(url=ext_url("/login"), status_code=303)
    clear_session_cookies(resp)
    return resp


def _master_banner_html(info: dict | None = None) -> str:
    # A cosmetic banner must NEVER take the page down: wrap the whole thing.
    # The narrow try below only caught get_active_master_info() *raising* — if it
    # returned None / a non-dict, the info.get(...) calls threw and 500'd every
    # page that shows the banner (e.g. /lwc), while the estate picker (which
    # doesn't render it) was fine.
    try:
        if info is None:
            info = get_active_master_info()
        if not isinstance(info, dict):
            return ""
        return _render_master_banner(info)
    except Exception:
        return ""


def _render_master_banner(info: dict) -> str:
    sources = info.get("sources") or []
    # sources[0] is an uploaded FILENAME (Rule.source) — attacker-influenceable,
    # so escape it. The <em>/<span> literals below are trusted markup we build.
    src_text = escape(sources[0]) if sources else "<em>none uploaded yet</em>"
    if len(sources) > 1:
        src_text += f' <span class="pill">+{len(sources)-1} other source(s)</span>'
    vf = escape(str(info.get("latest_valid_from") or "—"))
    uploaded_at = str(info.get("latest_uploaded_at") or "")
    # Render upload date as just YYYY-MM-DD HH:MM
    if uploaded_at:
        uploaded_at = escape(uploaded_at.replace("T", " ")[:16])
    rules = info.get("active_rule_count", 0)
    retros = info.get("products_with_retro", 0)
    upload_chip = (
        f' <span class="sep">·</span> uploaded <strong>{uploaded_at}</strong>'
        if uploaded_at else ""
    )
    return (
        '<div class="master-banner">'
        f'<strong>Current master:</strong> {src_text}'
        f'{upload_chip}'
        f' <span class="sep">·</span> effective from <strong>{vf}</strong>'
        f' <span class="sep">·</span> {rules} active rules'
        f' <span class="sep">·</span> {retros} products with retros'
        '</div>'
    )


@app.get("/", response_class=HTMLResponse)
def index(principal: DrinksPrincipal = Depends(require_drinks_role("viewer"))):
    """Two-card index — pick a supplier estate."""
    try:
        lwc_info = get_active_master_info()
    except Exception:
        lwc_info = {}
    try:
        ten_info = get_tennents_master_info()
    except Exception:
        ten_info = {}

    lwc_master = (lwc_info.get("sources") or ["—"])[0]
    lwc_rules = lwc_info.get("active_rule_count", 0)
    ten_master = (ten_info.get("sources") or ["—"])[0]
    ten_count = ten_info.get("agreement_count", 0)
    ten_customers = ten_info.get("customer_count", 0)

    # Proxied under the hub: "/" is the Team Hub portal (same origin), the
    # same target as the header logo. Standalone (no base path) there is no
    # hub to return to, so no link.
    hub_link = (
        '<p class="sub" style="margin-top:0"><a href="/">← Back to Team Hub</a></p>'
        if EXTERNAL_BASE_PATH else ""
    )
    return f"""{render_head(principal.email, principal.role)}
{hub_link}<h1>FB Taverns Reconciliation</h1>
<p class="sub">Pick the supplier estate to reconcile against. Each has its own master and reconciliation flow.</p>
<div class="grid2">
  <a href="{ext_url('/lwc')}" class="card-link">
    <h2 style="margin-top:0">LWC <span class="card-tag">England</span></h2>
    <p>Weekly sales report + monthly retro statement. Per-site, per-product tenant pricing with FB cost and retro.</p>
    <p class="card-meta">
      <strong>Master:</strong> <code>{escape(lwc_master)}</code><br>
      <strong>{lwc_rules}</strong> active pricing rules
    </p>
    <p><span class="card-cta">Open LWC reconciliation →</span></p>
  </a>
  <a href="{ext_url('/tennents')}" class="card-link">
    <h2 style="margin-top:0">Tennents Direct <span class="card-tag">Scotland</span></h2>
    <p>Monthly draught pricing report combining invoice, discount and retro data. Per-(customer, SKU) discount agreements.</p>
    <p class="card-meta">
      <strong>Master:</strong> <code>{escape(ten_master)}</code><br>
      <strong>{ten_count}</strong> agreements across <strong>{ten_customers}</strong> customers
    </p>
    <p><span class="card-cta">Open Tennents reconciliation →</span></p>
  </a>
</div>
{PAGE_FOOT}"""


@app.get("/lwc", response_class=HTMLResponse)
def lwc_home(principal: DrinksPrincipal = Depends(require_drinks_role("viewer"))):
    # The ONLINE price file is the master (operator direction 2026-07-03):
    # the Excel-upload path is retired from this page — /upload-master stays
    # routable for a bulk re-import emergency, but day-to-day everything is
    # edited in the grid and reconciliations check against it directly.
    return f"""{render_head(principal.email, principal.role)}
<p class="sub" style="margin-top:0"><a href="{ext_url('/')}">← Back to estate picker</a></p>
<h1>LWC Reconciliation <span class="estate-tag">England</span></h1>

<h2 style="margin-top:1em">Pricing master</h2>
<div class="result" style="max-width: none">
  <p style="margin-top:0">The <strong>online price file</strong> is the master. Open it to browse prices and
  margins, and (admins) edit directly in the grid — prices, products, sites, the annual increase. Every change
  takes effect from the day it's made, with history kept underneath, and reconciliations check supplier files
  against it.</p>
  <p style="margin-bottom:0"><a class="button" href="{ext_url('/master')}" style="margin-top:0">Open price file</a></p>
</div>

<div class="grid2" style="margin-top:1em">
  <form action="{ext_url('/export-master')}" method="get">
    <h3>Download to Excel</h3>
    <p class="sub">A fresh <code>master.xlsx</code> generated from the price file <em>as it stands right now</em> —
    every grid edit included, in the familiar cost-file layout.</p>
    <button type="submit">Download master.xlsx</button>
  </form>
  <form action="{ext_url('/add-support')}" method="post">
    <h3>Tenant support</h3>
    <p class="sub">A temporary support price for one site &amp; product. Reconciliations during the support window flag the mismatch but tag it with the support context, so you can see why LWC is charging the standard price.</p>
    <label for="support-text">Describe the support in plain English</label>
    <textarea name="text" id="support-text" required
      placeholder="e.g. Bell 804, reducing price of Moretti 22g to £200 for six weeks starting from today"></textarea>
    <p class="help">Include site, product, new price, when it starts, and how long it runs.<br>
    &bull; <em>"Castle Gate 820, Coors 11G to £150 from 1 May for 8 weeks"</em><br>
    &bull; <em>"Bay Horse 816, Madri 50L at £180 from today until end of June"</em><br>
    &bull; <em>"Lady Jane 805, Carling 22G dropped to £155 from 15 May for one month"</em></p>
    <button type="submit">Add support</button>
  </form>
</div>

<h2>Reconcile a supplier file</h2>
<div class="grid2">
  <form action="{ext_url('/upload')}" method="post" enctype="multipart/form-data">
    <h3>Weekly sales</h3>
    <p class="sub">LWC weekly sales report — checks tenant and FB pricing line by line.</p>
    <label for="ws-file">Weekly sales (.xlsx)</label>
    <input type="file" name="file" id="ws-file" accept=".xlsx" required>
    <input type="hidden" name="supplier" value="LWC">
    <button type="submit">Upload &amp; reconcile</button>
  </form>
  <form action="{ext_url('/upload-retro')}" method="post" enctype="multipart/form-data">
    <h3>Monthly retro</h3>
    <p class="sub">LWC Rate Per Keg — checks the per-keg retro paid against the agreed rate.</p>
    <label for="retro-file">Monthly retro (.xlsx)</label>
    <input type="file" name="file" id="retro-file" accept=".xlsx" required>
    <input type="hidden" name="supplier" value="LWC">
    <button type="submit">Upload &amp; reconcile</button>
  </form>
</div>
{PAGE_FOOT}"""


@app.post("/upload", response_class=HTMLResponse)
def upload(
    file: UploadFile = File(...),
    supplier: str = Form("LWC"),
    principal: DrinksPrincipal = Depends(require_drinks_role("editor")),
):
    original_name = file.filename or "uploaded.xlsx"
    if not original_name.lower().endswith(".xlsx"):
        return _error_page(f"File must be .xlsx (got {original_name!r})")

    suffix = Path(original_name).suffix or ".xlsx"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(_read_upload_capped(file))
            tmp_path = tmp.name

        # One coherent master read (Sites/Products/PricingRules fetched once),
        # reused for reconcile, the mismatch link-resolution, and the banner —
        # instead of re-fetching each table ~3x across this request.
        snap = load_master_snapshot()
        rules = snap.rules
        if not rules:
            return _error_page("No pricing rules found in Airtable. Run build-master first.")
        sites = snap.sites
        active_site_ids = {r.site_id for r in rules if r.valid_to is None}

        lines = parse_lwc_sales(tmp_path)
        mismatches = reconcile_lines(lines, rules, sites)

        file_rec_id = upsert_file_record(
            tmp_path,
            supplier=supplier,
            line_count=len(lines),
            file_name_override=original_name,
        )
        mismatch_count = write_mismatches(
            mismatches, file_rec_id,
            site_ids=snap.site_ids, product_ids=snap.product_ids, rule_ids=snap.rule_ids,
        )
    except Exception:
        logger.exception("request failed")
        return _error_page("Something went wrong processing this request — the details have been logged. Try again, and if it recurs contact the administrator.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    summary = build_summary(original_name, lines, mismatches, sites, active_site_ids=active_site_ids)
    summary_html = render_summary_html(summary)

    return f"""{render_head(principal.email, principal.role)}
<h1>Reconciliation complete</h1>
<p class="sub">{original_name} &middot; <code>{file_rec_id}</code> in Airtable &middot; {mismatch_count} mismatches inserted</p>
{_master_banner_html(snap.banner_info)}
<p>
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable</a>
  <a class="button" href="{ext_url('/lwc')}" style="background:#666">Upload another</a>
</p>
{summary_html}
{PAGE_FOOT}"""


@app.post("/upload-retro", response_class=HTMLResponse)
def upload_retro(
    file: UploadFile = File(...),
    supplier: str = Form("LWC"),
    principal: DrinksPrincipal = Depends(require_drinks_role("editor")),
):
    original_name = file.filename or "uploaded.xlsx"
    if not original_name.lower().endswith(".xlsx"):
        return _error_page(f"File must be .xlsx (got {original_name!r})")

    suffix = Path(original_name).suffix or ".xlsx"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(_read_upload_capped(file))
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
        logger.exception("request failed")
        return _error_page("Something went wrong processing this request — the details have been logged. Try again, and if it recurs contact the administrator.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    summary_html = render_retro_summary_html(summary)
    return f"""{render_head(principal.email, principal.role)}
<h1>Retro reconciliation complete</h1>
<p class="sub">{original_name} &middot; <code>{file_rec_id}</code> in Airtable &middot; {n_findings} findings inserted</p>
{_master_banner_html()}
<p>
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable</a>
  <a class="button" href="{ext_url('/lwc')}" style="background:#666">Upload another</a>
</p>
{summary_html}
{PAGE_FOOT}"""


@app.get("/export-master")
def export_master(principal: DrinksPrincipal = Depends(require_drinks_role("viewer"))):
    """Download the current online master as a wide-form Excel. Built from
    the cached snapshot (sub-second) — the previous fresh-sweep build took
    ~30s+ and timed out the hub proxy as a bare Internal Server Error."""
    from master_export import build_master_xlsx_bytes_from_snapshot  # lazy: keeps openpyxl off the boot path
    try:
        snap = load_master_snapshot()
        data = build_master_xlsx_bytes_from_snapshot(snap)
    except Exception:
        logger.exception("request failed")
        return _error_page("Something went wrong processing this request — the details have been logged. Try again, and if it recurs contact the administrator.")
    filename = f"FB_Taverns_Cost_Price_File_{date.today().isoformat()}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/upload-master", response_class=HTMLResponse)
def upload_master(
    file: UploadFile = File(...),
    valid_from: str = Form(...),
    reason: str = Form(""),
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
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
            tmp.write(_read_upload_capped(file))
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

        # Snapshot BEFORE the write (normally cache-warm) so the grid can be
        # patched + re-published afterwards — a plain invalidate would leave
        # the operator's next /master click doing the ~30s inline sweep that
        # times out the hub proxy.
        try:
            snap_before = load_master_snapshot()
        except Exception:
            snap_before = None

        lookups: dict = {}
        rules_created, rules_updated, rules_closed = upsert_pricing_rules(
            rules, close_keys_at_date=vf, lookups_out=lookups
        )
        # Reuse the product map built above so we don't re-read the Products table.
        products_created, products_updated = upsert_products_with_retros(
            products, existing_by_code=lookups.get("product_ids")
        )
    except Exception:
        logger.exception("request failed")
        return _error_page("Something went wrong processing this request — the details have been logged. Try again, and if it recurs contact the administrator.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Instant grid: mirror the whole-file upsert onto the cached snapshot and
    # re-publish, then reconcile from Airtable in the background.
    try:
        if snap_before is not None:
            from dataclasses import replace as _dc_replace
            patched = patch_snapshot_for_bulk_upsert(snap_before, rules, vf)
            new_sites = dict(patched.sites)
            for sid, sname in (sites or {}).items():
                cur = new_sites.get(sid)
                if cur is None:
                    new_sites[sid] = {"name": sname or "", "status": "tenanted",
                                      "country": "england", "notes": "",
                                      "_rec_id": "pending-refresh"}
                elif sname and not cur.get("name"):
                    new_sites[sid] = {**cur, "name": sname}
            new_products = dict(getattr(patched, "products", {}) or {})
            for code, info in (products or {}).items():
                new_products[code] = {
                    "desc": info.get("name") or (new_products.get(code) or {}).get("desc") or "",
                    "retro_per_keg": float(info.get("retro_per_keg") or 0.0),
                }
            publish_patched_snapshot(
                _dc_replace(patched, sites=new_sites, products=new_products)
            )
    except Exception:
        logger.exception("snapshot patch failed — grid catches up on refresh")
    refresh_master_cache_async()

    return f"""{render_head(principal.email, principal.role)}
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
  <a class="button" href="{ext_url('/master')}">View price grid</a>
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank" style="background:#666">Open Airtable</a>
  <a class="button" href="{ext_url('/')}" style="background:#666">Back to home</a>
</p>
{PAGE_FOOT}"""


@app.post("/add-support", response_class=HTMLResponse)
def add_support(
    text: str = Form(...),
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """Parse a natural-language support request and create the rule in Airtable."""
    text = (text or "").strip()
    if not text:
        return _error_page("Support description is empty.")

    try:
        # Lookup tables Claude needs to resolve site + product references. Read
        # from the SWR-cached master snapshot (≤60s stale, refreshed on every
        # write) — NOT ~5 fresh full-table Airtable sweeps, which together with
        # the LLM call below pushed this one POST toward the 30s hub-proxy
        # timeout. Sites/products/rules don't change second-to-second, so a
        # cache-fresh view is right here.
        snap = load_master_snapshot()
        rules = snap.rules
        products = {
            code: {"description": (info or {}).get("desc") or ""}
            for code, info in (getattr(snap, "products", {}) or {}).items()
        }
        # Filter sites to those with active rules so the LLM doesn't pick a retired one
        active_sites = {r.site_id for r in rules if r.valid_to is None}
        sites = {sid: info for sid, info in snap.sites.items() if sid in active_sites}

        parsed = parse_support_request(text, sites, products)
        errors = validate_support_fields(parsed, sites, products)
        if errors:
            err_html = "".join(f"<li>{escape(e)}</li>" for e in errors)
            parsed_html = "".join(
                f"<div class='summary-row'><span>{escape(k)}</span><span>{escape(str(v))}</span></div>"
                for k, v in parsed.items()
            )
            return _error_page(
                f"<p>The description couldn't be parsed cleanly. Reword and try again, or fix in Airtable directly.</p>"
                f"<ul>{err_html}</ul>"
                f"<h3>What Claude returned:</h3><div class='result'>{parsed_html}</div>"
            )

        # Build a single Rule and push to Airtable. close_keys_at_date=None
        # because a support rule LAYERS ON TOP of the standard rule — the
        # standard rule must remain active so it resumes after valid_to.
        sid = parsed["site_id"]
        code = parsed["product_code"]
        vf = _parse_date(parsed["valid_from"])
        vt = _parse_date(parsed["valid_to"])
        new_price = float(parsed["new_tenant_price"])
        reason = (parsed.get("reason") or "").strip()

        # FB price comes from the existing standard rule for the same product,
        # so reconciliation can still flag wrong_fb_price mismatches inside the
        # support window.
        existing_fb = next(
            (r.fb_price for r in rules
             if r.product_code == code and r.valid_to is None and r.fb_price is not None),
            None,
        )
        existing_desc = next(
            (r.product_desc for r in rules if r.product_code == code and r.product_desc),
            "",
        )

        rule = Rule(
            site_id=sid,
            product_code=code,
            product_desc=existing_desc,
            tenant_price=new_price,
            fb_price=existing_fb,
            retro_pct=0.0,
            valid_from=vf,
            valid_to=vt,
            status="supported",
            reason=reason,
            source=f"support_form ({text[:80]})",
        )

        from airtable_io import upsert_pricing_rules  # noqa: E402
        created, updated, _closed = upsert_pricing_rules([rule], close_keys_at_date=None)
    except KeyError as e:
        return _error_page(
            f"<p>Server is missing a required setting: <code>{escape(str(e))}</code>.</p>"
            f"<p>Set <code>ANTHROPIC_API_KEY</code> in the Render environment to enable natural-language parsing.</p>"
        )
    except Exception:
        logger.exception("request failed")
        return _error_page("Something went wrong processing this request — the details have been logged. Try again, and if it recurs contact the administrator.")

    site_name = (sites.get(sid) or {}).get("name", "")
    product_desc = products.get(code, {}).get("description", existing_desc)

    return f"""{render_head(principal.email, principal.role)}
<h1>Support added</h1>
<p class="sub">Standard rule remains active — this support layers on top until it ends.</p>
{_master_banner_html()}
<div class="result">
  <div class="summary-row"><span>Site</span><strong>{escape(sid)} {escape(site_name)}</strong></div>
  <div class="summary-row"><span>Product</span><strong>{escape(code)} {escape(product_desc)}</strong></div>
  <div class="summary-row"><span>Support tenant price</span><strong>£{new_price:,.2f}</strong></div>
  <div class="summary-row"><span>Valid from</span><strong>{vf.isoformat() if vf else '?'}</strong></div>
  <div class="summary-row"><span>Valid to</span><strong>{vt.isoformat() if vt else '?'}</strong></div>
  <div class="summary-row"><span>Reason</span><span>{escape(reason)}</span></div>
  <div class="summary-row"><span>Original description</span><span><em>{escape(text)}</em></span></div>
  <div class="summary-row"><span>Airtable</span><span>created={created} updated={updated}</span></div>
</div>
<p style="margin-top:1.5em">
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable</a>
  <a class="button" href="{ext_url('/')}" style="background:#666">Back to home</a>
</p>
{PAGE_FOOT}"""


# ---------- Pricing-master editor (/master*) ----------
# Design: docs/master-editor-design.md §3.3-§3.5, §4. Grid = viewer; every
# mutation (and the preview) = admin, matching /upload-master and /add-support.
# All mutating POSTs carry the _is_cross_origin guard — master writes are
# exactly the blast radius CSRF matters for. Every mutation flows through
# preview_master_change (validates) -> apply_master_change (the ONLY apply
# path, stamps source="editor:<email>").

_GENERIC_ERR = (
    "Something went wrong processing this request — the details have been "
    "logged. Try again, and if it recurs contact the administrator."
)


def _master_not_found_page(principal: DrinksPrincipal, rule_key: str) -> HTMLResponse:
    """Polite 404 when a rule_key no longer resolves against the snapshot
    (e.g. edited or removed since the grid page was rendered)."""
    return HTMLResponse(
        f"""{render_head(principal.email, principal.role)}
<h1>Rule not found</h1>
<div class="result err">No pricing rule matches <code>{escape(rule_key)}</code>.
It may have been changed or removed since the list was loaded (the list is cached for up to a minute).</div>
<p><a href="{ext_url('/master')}">Back to master</a></p>
{PAGE_FOOT}""",
        status_code=404,
    )


@app.get("/master", response_class=HTMLResponse)
def master_grid(
    request: Request,
    principal: DrinksPrincipal = Depends(require_drinks_role("viewer")),
):
    """The master over the CACHED snapshot (never a fresh Airtable sweep — §3.4).
    Default view is the Excel-style pivot (products × sites); ?view=list is the
    detailed per-rule grid with history/filters/pagination."""
    try:
        params = dict(request.query_params)
        snap = load_master_snapshot()
        if params.get("view") == "list":
            # The upload-provenance banner stays on the detailed list only —
            # the grid is the source of truth now, so it's noise there.
            banner = _master_banner_html(snap.banner_info)
            body = master_pages.render_master_grid(
                snap, params, is_admin=principal.is_admin, banner_html=banner
            )
        else:
            body = master_pages.render_master_pivot(
                snap, params, is_admin=principal.is_admin
            )
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    return f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}"


@app.get("/master/edit", response_class=HTMLResponse)
def master_edit(
    rule_key: str = "",
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """Single-rule page: current values + the two edit forms (change-from-date
    vs fix-in-place — the semantic split is the main user-error surface)."""
    try:
        snap = load_master_snapshot()
        rule = master_pages.find_rule(snap, rule_key)
        if rule is None:
            return _master_not_found_page(principal, rule_key)
        body = master_pages.render_edit_page(snap, rule)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    return f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}"


@app.get("/master/end", response_class=HTMLResponse)
def master_end(
    rule_key: str = "",
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    try:
        snap = load_master_snapshot()
        rule = master_pages.find_rule(snap, rule_key)
        if rule is None:
            return _master_not_found_page(principal, rule_key)
        body = master_pages.render_end_page(snap, rule)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    return f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}"


@app.get("/master/add", response_class=HTMLResponse)
def master_add(
    site_id: str = "",
    product_code: str = "",
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """site_id/product_code (optional GET params) prefill the selects — the
    pivot's edit mode links blank cells here."""
    try:
        snap = load_master_snapshot()
        body = master_pages.render_add_page(snap, site_id=site_id, product_code=product_code)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    return f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}"


@app.get("/master/cell", response_class=HTMLResponse)
def master_cell(
    site_id: str = "",
    product_code: str = "",
    fsite: str = "",
    fq: str = "",
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """The Excel-like single-cell editor the pivot's edit mode links to:
    amend / remove / add ONE price. fsite/fq return the operator to the same
    filtered grid view after saving."""
    try:
        snap = load_master_snapshot()
        if site_id not in snap.sites or product_code not in snap.product_ids:
            return _master_not_found_page(principal, f"{site_id}|{product_code}")
        body = master_pages.render_cell_page(snap, site_id, product_code, fsite=fsite, fq=fq)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    return f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}"


@app.get("/master/site", response_class=HTMLResponse)
def master_site(
    site_id: str = "",
    fsite: str = "",
    fq: str = "",
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """Set/correct a site's display name (linked from the grid's edit-mode
    site headers — auto-created sites have no name)."""
    try:
        snap = load_master_snapshot()
        if site_id not in snap.sites:
            return _master_not_found_page(principal, site_id)
        body = master_pages.render_site_name_page(snap, site_id, fsite=fsite, fq=fq)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    return f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}"


@app.post("/master/site/apply")
async def master_site_apply(
    request: Request,
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """Site settings ops: rename (default) · end_all (end every current price —
    'site leaves the estate') · delete (record removal, only with NO history)."""
    if _is_cross_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    form = await request.form()
    site_id = (form.get("site_id") or "").strip()
    fsite = (form.get("fsite") or "").strip()
    fq = (form.get("fq") or "").strip()
    name = (form.get("name") or "").strip()
    do = (form.get("do") or "rename").strip()

    try:
        snap = await run_in_threadpool(load_master_snapshot)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    if site_id not in snap.sites:
        return _master_not_found_page(principal, site_id)

    def _rerender(errors: list[str]) -> HTMLResponse:
        body = master_pages.render_site_name_page(
            snap, site_id, fsite=fsite, fq=fq, errors=errors
        )
        return HTMLResponse(
            f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}",
            status_code=400,
        )

    from dataclasses import replace as _dc_replace
    today = date.today()

    if do == "end_all":
        try:
            n = await run_in_threadpool(
                end_all_site_rules, site_id, today,
                "site removed from master",
                f"site-remove:{principal.email}",
            )
        except ValueError as exc:
            return _rerender([str(exc)])
        except Exception:
            logger.exception("site end_all failed")
            return _error_page(_GENERIC_ERR)
        logger.info("site %s removed from master by %s (%d rules ended)", site_id, principal.email, n)
        # patch: close this site's open rules in the cached snapshot
        try:
            rules = [
                _dc_replace(
                    r,
                    valid_to=(
                        today if (r.valid_from is None or r.valid_from < today)
                        else r.valid_from + timedelta(days=1)
                    ),
                )
                if (r.site_id == site_id and r.valid_to is None) else r
                for r in snap.rules
            ]
            publish_patched_snapshot(_dc_replace(snap, rules=rules))
        except Exception:
            logger.exception("snapshot patch failed — grid catches up on refresh")
        refresh_master_cache_async()
        return RedirectResponse(
            ext_url("/master") + "?" + urlencode([("edit", "1"), ("saved", "1")]),
            status_code=303,
        )

    if do == "delete":
        try:
            await run_in_threadpool(delete_site, site_id)
        except ValueError as exc:
            return _rerender([str(exc)])
        except Exception:
            logger.exception("site delete failed")
            return _error_page(_GENERIC_ERR)
        logger.info("site %s deleted by %s", site_id, principal.email)
        try:
            sites = {k: v for k, v in snap.sites.items() if k != site_id}
            site_ids = {k: v for k, v in snap.site_ids.items() if k != site_id}
            publish_patched_snapshot(_dc_replace(snap, sites=sites, site_ids=site_ids))
        except Exception:
            logger.exception("snapshot patch failed — grid catches up on refresh")
        refresh_master_cache_async()
        return RedirectResponse(
            ext_url("/master") + "?" + urlencode([("edit", "1"), ("saved", "1")]),
            status_code=303,
        )

    # default: rename
    if not name:
        return _rerender(["the site name must not be empty"])
    try:
        await run_in_threadpool(rename_site, site_id, name)
    except ValueError as exc:
        return _rerender([str(exc)])
    except Exception:
        logger.exception("site rename failed")
        return _error_page(_GENERIC_ERR)

    # Same instant-grid pattern as the cell editor: patch the cached snapshot
    # (new name in the sites dict) and re-publish, then reconcile async.
    try:
        sites = dict(snap.sites)
        sites[site_id] = {**sites[site_id], "name": name}
        publish_patched_snapshot(_dc_replace(snap, sites=sites))
    except Exception:
        logger.exception("snapshot patch failed — grid catches up on refresh")
    refresh_master_cache_async()

    back = [("edit", "1"), ("saved", "1")]
    if fsite:
        back.append(("site", fsite))
    if fq:
        back.append(("q", fq))
    return RedirectResponse(
        ext_url("/master") + "?" + urlencode(back), status_code=303
    )


@app.get("/master/site/new", response_class=HTMLResponse)
def master_site_new(
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    body = master_pages.render_site_new_page()
    return f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}"


@app.post("/master/site/create")
async def master_site_create(
    request: Request,
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    if _is_cross_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    form = await request.form()
    site_id = (form.get("site_id") or "").strip()
    name = (form.get("name") or "").strip()

    def _rerender(errors: list[str]) -> HTMLResponse:
        body = master_pages.render_site_new_page(errors=errors, site_id=site_id, name=name)
        return HTMLResponse(
            f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}",
            status_code=400,
        )

    if not site_id or not name:
        return _rerender(["both the site id and the name are required"])
    try:
        rec_id = await run_in_threadpool(create_site, site_id, name)
    except ValueError as exc:
        return _rerender([str(exc)])
    except Exception:
        logger.exception("site create failed")
        return _error_page(_GENERIC_ERR)
    logger.info("site %s (%s) created by %s", site_id, name, principal.email)

    # patch the cached snapshot so the new site is selectable immediately,
    # then land on its (empty) column in edit mode ready for prices.
    try:
        from dataclasses import replace as _dc_replace
        snap = await run_in_threadpool(load_master_snapshot)
        if site_id not in snap.sites:
            sites = dict(snap.sites)
            sites[site_id] = {
                "name": name, "status": "tenanted", "country": "england",
                "notes": "", "_rec_id": rec_id,
            }
            site_ids = {**snap.site_ids, site_id: rec_id}
            publish_patched_snapshot(_dc_replace(snap, sites=sites, site_ids=site_ids))
    except Exception:
        logger.exception("snapshot patch failed — grid catches up on refresh")
    refresh_master_cache_async()
    return RedirectResponse(
        ext_url("/master") + "?" + urlencode(
            [("edit", "1"), ("site", site_id), ("saved", "1")]
        ),
        status_code=303,
    )


@app.get("/master/product", response_class=HTMLResponse)
def master_product(
    product_code: str = "",
    fsite: str = "",
    fq: str = "",
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """Product settings (linked from the grid's edit-mode code/name cells):
    edit code + name, end its prices everywhere, or delete an unused record."""
    try:
        snap = load_master_snapshot()
        if product_code not in snap.product_ids:
            return _master_not_found_page(principal, product_code)
        body = master_pages.render_product_page(snap, product_code, fsite=fsite, fq=fq)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    return f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}"


@app.post("/master/product/apply")
async def master_product_apply(
    request: Request,
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """Product settings ops: save (code/name rename — a code change also
    rewrites the stored rule_keys) · end_all (delist the line estate-wide) ·
    delete (record removal, only with NO history)."""
    if _is_cross_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    form = await request.form()
    product_code = (form.get("product_code") or "").strip()
    fsite = (form.get("fsite") or "").strip()
    fq = (form.get("fq") or "").strip()
    do = (form.get("do") or "save").strip()
    new_code = (form.get("new_code") or "").strip()
    new_desc = (form.get("new_desc") or "").strip()

    try:
        snap = await run_in_threadpool(load_master_snapshot)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    if product_code not in snap.product_ids:
        return _master_not_found_page(principal, product_code)

    def _rerender(errors: list[str]) -> HTMLResponse:
        body = master_pages.render_product_page(
            snap, product_code, fsite=fsite, fq=fq, errors=errors
        )
        return HTMLResponse(
            f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}",
            status_code=400,
        )

    from dataclasses import replace as _dc_replace
    today = date.today()

    def _back() -> RedirectResponse:
        back = [("edit", "1"), ("saved", "1")]
        if fsite:
            back.append(("site", fsite))
        if fq:
            back.append(("q", fq))
        return RedirectResponse(
            ext_url("/master") + "?" + urlencode(back), status_code=303
        )

    if do == "end_all":
        try:
            n = await run_in_threadpool(
                end_all_product_rules, product_code, today,
                "product removed from master",
                f"product-remove:{principal.email}",
            )
        except ValueError as exc:
            return _rerender([str(exc)])
        except Exception:
            logger.exception("product end_all failed")
            return _error_page(_GENERIC_ERR)
        logger.info("product %s removed from master by %s (%d rules ended)",
                    product_code, principal.email, n)
        try:
            rules = [
                _dc_replace(
                    r,
                    valid_to=(
                        today if (r.valid_from is None or r.valid_from < today)
                        else r.valid_from + timedelta(days=1)
                    ),
                )
                if (r.product_code == product_code and r.valid_to is None) else r
                for r in snap.rules
            ]
            publish_patched_snapshot(_dc_replace(snap, rules=rules))
        except Exception:
            logger.exception("snapshot patch failed — grid catches up on refresh")
        refresh_master_cache_async()
        return _back()

    if do == "delete":
        try:
            await run_in_threadpool(delete_product, product_code)
        except ValueError as exc:
            return _rerender([str(exc)])
        except Exception:
            logger.exception("product delete failed")
            return _error_page(_GENERIC_ERR)
        logger.info("product %s deleted by %s", product_code, principal.email)
        try:
            products = {k: v for k, v in (getattr(snap, "products", {}) or {}).items()
                        if k != product_code}
            product_ids = {k: v for k, v in snap.product_ids.items() if k != product_code}
            publish_patched_snapshot(
                _dc_replace(snap, products=products, product_ids=product_ids)
            )
        except Exception:
            logger.exception("snapshot patch failed — grid catches up on refresh")
        refresh_master_cache_async()
        return _back()

    # default: save (code / name / product-level FB list price / retro)
    if not new_code or not new_desc:
        return _rerender(["both the product code and the name are required"])

    def _opt_float(name: str) -> float | None:
        s = (form.get(name) or "").strip()
        if not s:
            return None
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    new_fb = _opt_float("new_fb")
    new_retro = _opt_float("new_retro")
    open_rules = [
        r for r in snap.rules
        if r.product_code == product_code and r.valid_to is None
    ]
    cur_fbs = {round(r.fb_price, 4) for r in open_rules if r.fb_price is not None}
    cur_fb = next(iter(cur_fbs)) if len(cur_fbs) == 1 else None
    cur_retro = (
        (getattr(snap, "products", {}) or {}).get(product_code) or {}
    ).get("retro_per_keg") or 0.0

    fb_changed = new_fb is not None and (
        cur_fb is None or abs(new_fb - cur_fb) > 0.005 or len(cur_fbs) > 1
    )
    # Retro: blank means "keep"; 0 means "no retro".
    retro_final = new_retro if new_retro is not None else cur_retro
    retro_changed = new_retro is not None and abs(new_retro - cur_retro) > 0.005
    if new_fb is not None and new_fb <= 0:
        return _rerender(["the FB list price must be positive"])
    if new_retro is not None and new_retro < 0:
        return _rerender(["the retro must not be negative"])
    fb_for_net = new_fb if new_fb is not None else cur_fb
    if fb_for_net is not None and retro_final >= fb_for_net > 0:
        return _rerender([
            f"retro £{retro_final:.2f}/keg is at or above the FB list price "
            f"£{fb_for_net:.2f} — the net price would be zero or negative"
        ])

    try:
        rewritten = await run_in_threadpool(
            update_product, product_code, new_code, new_desc,
            retro_final if retro_changed else None,
        )
    except ValueError as exc:
        return _rerender([str(exc)])
    except Exception:
        logger.exception("product update failed")
        return _error_page(_GENERIC_ERR)
    logger.info("product %s -> %s (%s) by %s; %d rule keys rewritten",
                product_code, new_code, new_desc, principal.email, rewritten)

    # FB list / retro change: re-date every current tenanted price for this
    # product from today with the new figures — tenant prices unchanged, so
    # only the cost side (and therefore margins) moves. History kept.
    successors: list = []
    if fb_changed or retro_changed:
        for r in open_rules:
            if (r.status or "tenanted") != "tenanted":
                continue
            if r.valid_from is not None and r.valid_from > today:
                continue
            fb = new_fb if new_fb is not None else r.fb_price
            retro_pct = (retro_final / fb) if (fb and retro_final) else 0.0
            successors.append(Rule(
                site_id=r.site_id, product_code=new_code, product_desc=new_desc,
                tenant_price=r.tenant_price, fb_price=fb, retro_pct=retro_pct,
                valid_from=today, valid_to=None, status="tenanted",
                reason=f"product edit: list/retro updated (was fb {r.fb_price}, retro £{cur_retro:g})",
                source=f"product-edit:{principal.email}",
            ))
        if successors:
            try:
                await run_in_threadpool(upsert_pricing_rules, successors, today)
            except Exception:
                logger.exception("product fb/retro rules rewrite failed")
                return _error_page(_GENERIC_ERR)

    try:
        products = dict(getattr(snap, "products", {}) or {})
        info = products.pop(product_code, {}) or {}
        products[new_code] = {**info, "desc": new_desc, "retro_per_keg": retro_final}
        product_ids = dict(snap.product_ids)
        product_ids[new_code] = product_ids.pop(product_code, "pending-refresh")
        rules = [
            _dc_replace(r, product_code=new_code, product_desc=new_desc)
            if r.product_code == product_code else r
            for r in snap.rules
        ]
        rule_ids = {}
        for k, v in snap.rule_ids.items():
            parts = k.split("|")
            if len(parts) == 3 and parts[1] == product_code:
                parts[1] = new_code
                rule_ids["|".join(parts)] = v
            else:
                rule_ids[k] = v
        patched = _dc_replace(
            snap, products=products, product_ids=product_ids,
            rules=rules, rule_ids=rule_ids,
        )
        if successors:
            patched = patch_snapshot_for_bulk_upsert(patched, successors, today)
        publish_patched_snapshot(patched)
    except Exception:
        logger.exception("snapshot patch failed — grid catches up on refresh")
    refresh_master_cache_async()
    return _back()


@app.get("/master/increase", response_class=HTMLResponse)
def master_increase(
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    body = master_pages.render_increase_page()
    return f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}"


def _parse_increase_form(form) -> tuple[float | None, date | None, list[str]]:
    errors: list[str] = []
    pct = None
    try:
        pct = float((form.get("pct") or "").strip())
    except (TypeError, ValueError):
        errors.append("enter the increase as a percentage, e.g. 3.5")
    vf = _parse_date((form.get("valid_from") or "").strip())
    if vf is None:
        errors.append("enter the effective date")
    if pct is not None:
        if pct == 0:
            errors.append("a 0% increase changes nothing")
        elif abs(pct) > INCREASE_HARD_LIMIT_PCT:
            errors.append(
                f"{pct:+g}% is outside the ±{INCREASE_HARD_LIMIT_PCT:.0f}% hard limit — "
                "check the figure (3.5 means +3.5%)"
            )
    return pct, vf, errors


@app.post("/master/increase/preview", response_class=HTMLResponse)
async def master_increase_preview(
    request: Request,
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """Pure preview — WRITES NOTHING. Shows counts + examples, then the
    confirm button carries pct+date to /master/increase/apply."""
    if _is_cross_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    form = await request.form()
    pct, vf, errors = _parse_increase_form(form)
    if errors:
        body = master_pages.render_increase_page(
            errors=errors, pct=str(form.get("pct") or ""), vf=str(form.get("valid_from") or "")
        )
        return HTMLResponse(
            f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}",
            status_code=400,
        )
    try:
        snap = await run_in_threadpool(load_master_snapshot)
        _rules, stats = build_universal_increase(snap, pct, vf, principal.email)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    warnings = []
    lo, hi = INCREASE_SANITY_BAND_PCT
    if not (lo <= pct <= hi):
        warnings.append(
            f"{pct:+g}% is outside the usual {lo:+.0f}%…{hi:+.0f}% band — double-check before confirming"
        )
    if stats["n_rules"] == 0:
        warnings.append("no current prices match — nothing would change")
    body = master_pages.render_increase_preview_page(pct, vf, stats, warnings)
    return f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}"


@app.post("/master/increase/apply", response_class=HTMLResponse)
async def master_increase_apply(
    request: Request,
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """Apply the universal increase: recompute server-side from a fresh
    snapshot (never trust echoed figures beyond pct+date), bulk-upsert with
    the close-at-date pass, patch + republish the cached snapshot."""
    if _is_cross_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    form = await request.form()
    pct, vf, errors = _parse_increase_form(form)
    if errors or form.get("confirm") != "1":
        return _error_page(master_pages.errors_html(errors or ["missing confirmation"]))
    try:
        snap = await run_in_threadpool(load_master_snapshot)
        new_rules, stats = build_universal_increase(snap, pct, vf, principal.email)
        # Idempotence guard FIRST: refuse if the affected prices are not the ones
        # the operator previewed — catches a DOUBLE SUBMIT of Apply (the first
        # run already moved every price, so a second would compound the increase)
        # and any concurrent edit between preview and apply. Checked before the
        # empty-set check so a double submit gets the clear "already applied"
        # message (a re-run's new_rules is empty because the successors are now
        # dated on the effective date and skipped).
        if (form.get("state") or "") != stats["checksum"]:
            return _error_page(
                "<p>The master's prices have changed since this preview — most "
                "likely the increase was <strong>already applied</strong> (a double "
                "submit), or someone edited a price in between.</p>"
                "<p><strong>Nothing was written.</strong> Check the grid, and re-run "
                "the preview if an increase is still needed.</p>"
            )
        if not new_rules:
            return _error_page("<p>No current prices match — nothing to apply.</p>")
        created, updated, closed = await run_in_threadpool(
            upsert_pricing_rules, new_rules, vf
        )
    except Exception:
        logger.exception("universal increase apply failed")
        return _error_page(_GENERIC_ERR)
    logger.info(
        "universal increase %+g%% by %s: %d created, %d updated, %d closed",
        pct, principal.email, created, updated, closed,
    )
    try:
        publish_patched_snapshot(patch_snapshot_for_bulk_upsert(snap, new_rules, vf))
    except Exception:
        logger.exception("snapshot patch failed — grid catches up on refresh")
    refresh_master_cache_async()
    grid = ext_url("/master")
    return f"""{render_head(principal.email, principal.role)}
<h1>Increase applied</h1>
<div class="result" style="max-width:640px">
  <div class="summary-row"><span>Increase</span><span><strong>{pct:+g}%</strong> effective {vf.isoformat()}</span></div>
  <div class="summary-row"><span>Successor prices created</span><span>{created}</span></div>
  <div class="summary-row"><span>Updated in place (same-day)</span><span>{updated}</span></div>
  <div class="summary-row"><span>Prior prices closed</span><span>{closed}</span></div>
  <div class="summary-row"><span>Retro</span><span>fixed £/keg — unchanged</span></div>
</div>
<p style="margin-top:1.5em">
  <a class="button" href="{grid}">Back to price grid</a>
  <a class="button" href="{grid}?view=list" style="background:#666">Detailed list</a>
</p>
{PAGE_FOOT}"""


@app.post("/master/cell/apply")
async def master_cell_apply(
    request: Request,
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """Apply ONE cell edit (save/remove/add a price) and bounce straight back
    to the grid — the Excel-like flow. The client sends ONLY the new price and
    the cell identity; everything else (fb/retro/status, which op applies) is
    derived server-side from the current winner. After a successful write the
    cached snapshot is PATCHED and re-published so the grid shows the change
    instantly — a plain invalidate would leave the next page read doing the
    ~30s inline sweep that times out the hub proxy."""
    if _is_cross_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    form = await request.form()
    site_id = (form.get("site_id") or "").strip()
    product_code = (form.get("product_code") or "").strip()
    fsite = (form.get("fsite") or "").strip()
    fq = (form.get("fq") or "").strip()
    do = (form.get("do") or "save").strip()
    tp_raw = (form.get("tenant_price") or "").strip()

    try:
        snap = await run_in_threadpool(load_master_snapshot)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    if site_id not in snap.sites or product_code not in snap.product_ids:
        return _master_not_found_page(principal, f"{site_id}|{product_code}")

    def _rerender(errors: list[str], status_code: int = 400) -> HTMLResponse:
        body = master_pages.render_cell_page(
            snap, site_id, product_code, fsite=fsite, fq=fq,
            errors=errors, tenant_val=tp_raw,
        )
        return HTMLResponse(
            f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}",
            status_code=status_code,
        )

    winner = master_pages.pivot_winner_for(snap, site_id, product_code)
    today = date.today()

    back = [("edit", "1")]
    if fsite:
        back.append(("site", fsite))
    if fq:
        back.append(("q", fq))

    def _back_to_grid(saved: bool) -> RedirectResponse:
        qs = ([("saved", "1")] if saved else []) + back
        return RedirectResponse(
            ext_url("/master") + "?" + urlencode(qs), status_code=303
        )

    # Excel semantics from the in-grid cells: an emptied cell means "remove
    # this price"; an empty cell that never had a price is a no-op.
    if do != "delete" and not tp_raw:
        if winner is None:
            return _back_to_grid(saved=False)
        do = "delete"

    if do == "delete":
        if winner is None:
            return _rerender(["there is no current price to remove"])
        # Delist from today. A rule that STARTED today ends tomorrow instead
        # (valid_to must be after valid_from) — it bills today only.
        vt = today
        if winner.valid_from is not None and winner.valid_from >= today:
            vt = winner.valid_from + timedelta(days=1)
        change = MasterChange(
            op="end_rule", site_id=site_id, product_code=product_code,
            valid_from=winner.valid_from, valid_to=vt,
            reason=f"grid: price removed (was {_fmt_price(winner.tenant_price)})",
            source_note="grid",
        )
    else:
        try:
            tp = float(tp_raw)
        except (TypeError, ValueError):
            return _rerender(["enter the price in £, e.g. 182.50"])
        # Excel-like: re-entering the same price is a no-op, not an error.
        if (
            winner is not None and winner.tenant_price is not None
            and abs(tp - winner.tenant_price) < 0.005
        ):
            return _back_to_grid(saved=False)
        if winner is None:
            # New price at this site: inherit the product-level FB list price
            # (when consistent across sites) and retro so margins stay honest.
            fbs = {
                round(r.fb_price, 4) for r in snap.rules
                if r.product_code == product_code and r.valid_to is None
                and r.fb_price is not None
            }
            fb = fbs.pop() if len(fbs) == 1 else None
            retro_per_keg = (
                (getattr(snap, "products", {}) or {}).get(product_code) or {}
            ).get("retro_per_keg") or 0.0
            retro_pct = (retro_per_keg / fb) if (fb and retro_per_keg) else None
            change = MasterChange(
                op="add_rule", site_id=site_id, product_code=product_code,
                tenant_price=tp, fb_price=fb, retro_pct=retro_pct,
                status="tenanted", valid_from=today,
                reason="grid: price added", source_note="grid",
            )
        elif winner.valid_from == today:
            # Second edit the same day: same rule_key -> in-place fix. fb/retro
            # None keep the stored values (the write path skips them).
            change = MasterChange(
                op="fix_in_place", site_id=site_id, product_code=product_code,
                tenant_price=tp, fb_price=None, retro_pct=None,
                status=winner.status or "tenanted", valid_from=today,
                reason=f"grid: price corrected (was {_fmt_price(winner.tenant_price)})",
                source_note="grid",
            )
        else:
            change = MasterChange(
                op="price_change", site_id=site_id, product_code=product_code,
                tenant_price=tp, fb_price=winner.fb_price,
                retro_pct=winner.retro_pct or None,
                status=winner.status or "tenanted", valid_from=today,
                reason=f"grid: price change (was {_fmt_price(winner.tenant_price)})",
                source_note="grid",
            )

    errors, _warnings = validate_master_change(change, snap)
    if errors:
        return _rerender(errors)
    try:
        await run_in_threadpool(apply_master_change, change, principal.email)
    except ValueError as exc:
        # Apply-time refusal (state moved / duplicate submit) — nothing written.
        return _rerender([f"refused at apply time: {exc}"], status_code=409)
    except Exception:
        logger.exception("grid cell apply failed")
        return _error_page(_GENERIC_ERR)

    # Instant-grid flow: publish a patched snapshot, then reconcile from
    # Airtable in the background.
    try:
        publish_patched_snapshot(
            patch_snapshot_for_change(snap, change, principal.email)
        )
    except Exception:
        logger.exception("snapshot patch failed — grid catches up on refresh")
    refresh_master_cache_async()
    return _back_to_grid(saved=True)


def _fmt_price(v: float | None) -> str:
    return "—" if v is None else f"£{v:,.2f}"


@app.post("/master/preview", response_class=HTMLResponse)
async def master_preview(
    request: Request,
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """POST-echo confirm page: parse form -> MasterChange -> pure preview.
    WRITES NOTHING. Blocking errors render without a Confirm button (400)."""
    if _is_cross_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    form = await request.form()
    change, parse_errors = master_pages.parse_master_change_form(form)
    if change is None or parse_errors:
        return _error_page(master_pages.errors_html(parse_errors or ["missing operation"]))
    try:
        # run_in_threadpool: a post-write cache miss rebuilds inline (~30s) and
        # must not block the event loop (the 66658ff proxy-timeout saga).
        snap = await run_in_threadpool(load_master_snapshot)
        preview = preview_master_change(change, snap)
        body = master_pages.render_preview_page(change, preview)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    return HTMLResponse(
        f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}",
        status_code=400 if preview.errors else 200,
    )


@app.post("/master/apply", response_class=HTMLResponse)
async def master_apply(
    request: Request,
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    """Re-parse the confirm page's hidden inputs, RE-VALIDATE server-side
    (never trust the echoed form), then apply via apply_master_change with
    provenance stamped from the signed-in principal.

    Double-submit safety = idempotent-ish apply semantics (§3.5/§2.2): a
    re-confirmed price_change PATCHes the same key in place; a re-confirmed
    end_rule refuses inside end_pricing_rule ("already ended") and lands on
    the friendly refusal page below with nothing written.
    """
    if _is_cross_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    form = await request.form()
    change, parse_errors = master_pages.parse_master_change_form(form)
    if change is None or parse_errors:
        return _error_page(master_pages.errors_html(parse_errors or ["missing operation"]))
    try:
        snap = await run_in_threadpool(load_master_snapshot)
        # preview_master_change runs validate_master_change internally; the
        # pre-write preview is also what the result page renders from (§3.4).
        preview = preview_master_change(change, snap)
    except Exception:
        logger.exception("request failed")
        return _error_page(_GENERIC_ERR)
    if preview.errors:
        return _error_page(master_pages.errors_html(preview.errors))
    try:
        result = await run_in_threadpool(
            apply_master_change, change, principal.email
        )
    except ValueError as exc:
        # Apply-time refusal from the primitives (state moved since the
        # preview, or a double submit) — nothing was written.
        return _error_page(
            "<p>The change was refused at apply time — the rule's state has "
            "changed since the preview (or this is a duplicate submission):</p>"
            f"<p><strong>{escape(str(exc))}</strong></p>"
            "<p>Nothing was written. Reload the rule and try again if it still needs changing.</p>"
        )
    except Exception:
        logger.exception("master apply failed")
        # A price_change/end closes BEFORE it creates, so a create-batch failure
        # can leave a close already committed. Invalidate + rebuild the cache so
        # the "check & repair" link reflects what actually landed (not a stale
        # pre-close snapshot for up to the TTL), mirroring the success path.
        from airtable_io import invalidate_master_cache  # noqa: E402
        invalidate_master_cache()
        refresh_master_cache_async()
        return _error_page(master_pages.render_apply_failure(change, preview))
    # Retro is product-level (operator decision 2026-07-04): a per-row retro
    # edit propagates UP — set Products.retro_per_keg (which the retro
    # reconciliation + export read) and reflow every site's net price — so the
    # grid and the reconciliation can never disagree. Only when the retro was
    # explicitly provided AND actually changes the product £/keg. Best-effort:
    # a reflow failure must not fail the (already-committed) rule edit.
    try:
        if change.op in ("price_change", "fix_in_place", "add_rule") and change.retro_pct is not None:
            fb = change.fb_price
            if fb is None:
                w = master_pages.pivot_winner_for(snap, change.site_id, change.product_code)
                fb = w.fb_price if w else None
            if fb:
                new_retro_gbp = round(change.retro_pct * fb, 2)
                cur = (getattr(snap, "products", {}) or {}).get(change.product_code) or {}
                if abs(new_retro_gbp - float(cur.get("retro_per_keg") or 0.0)) > 0.005:
                    from airtable_io import set_product_retro  # noqa: E402
                    await run_in_threadpool(
                        set_product_retro, change.product_code, new_retro_gbp,
                        date.today(), f"retro-propagate:{principal.email}",
                    )
    except Exception:
        logger.exception("retro propagation failed — product-level retro may be stale")

    # §3.4: result page renders from in-hand data only; kick the background
    # snapshot rebuild so "Back to master" doesn't eat the 30s inline fetch.
    refresh_master_cache_async()
    body = master_pages.render_result_page(change, preview, result)
    return f"{render_head(principal.email, principal.role)}{body}{PAGE_FOOT}"


# ---------- Tennents Direct ----------

def _tennents_master_banner_html() -> str:
    try:
        info = get_tennents_master_info()
    except Exception:
        return ""
    sources = info.get("sources") or []
    # sources[0] = uploaded filename (attacker-influenceable) — escape it.
    src_text = escape(sources[0]) if sources else "<em>none uploaded yet</em>"
    if len(sources) > 1:
        src_text += f' <span class="pill">+{len(sources)-1} other source(s)</span>'
    uploaded_at = escape((str(info.get("latest_uploaded_at") or "")).replace("T", " ")[:16])
    upload_chip = (
        f' <span class="sep">·</span> uploaded <strong>{uploaded_at}</strong>'
        if uploaded_at else ""
    )
    return (
        '<div class="master-banner">'
        f'<strong>Current master:</strong> {src_text}'
        f'{upload_chip}'
        f' <span class="sep">·</span> {info.get("agreement_count", 0)} agreements'
        f' <span class="sep">·</span> {info.get("customer_count", 0)} customers'
        '</div>'
    )


@app.get("/tennents", response_class=HTMLResponse)
def tennents_home(principal: DrinksPrincipal = Depends(require_drinks_role("viewer"))):
    return f"""{render_head(principal.email, principal.role)}
<p class="sub" style="margin-top:0"><a href="{ext_url('/')}">← Back to estate picker</a></p>
<h1>Tennents Direct Reconciliation <span class="estate-tag">Scotland</span></h1>
<p class="sub">Upload the monthly draught pricing report. Or update the discount-agreement master.</p>
{_tennents_master_banner_html()}

<h2>Reconcile a monthly file</h2>
<form action="{ext_url('/upload-tennents')}" method="post" enctype="multipart/form-data" style="max-width: 540px">
  <h3 style="margin-top:0; color: #2c5aa0">Monthly draught pricing report</h3>
  <p class="sub">e.g. <code>FB Taverns Draught Pricing Report - January.xlsx</code>. The <code>Data</code> tab is the per-delivery line items.</p>
  <label for="ten-file">Monthly file (.xlsx)</label>
  <input type="file" name="file" id="ten-file" accept=".xlsx" required>
  <button type="submit">Upload &amp; reconcile</button>
</form>

<h2>Discount-agreement master</h2>
<div class="result" style="max-width: none">
  <p style="margin-top:0">The <strong>FB Taverns - Commercial Data</strong> Excel is the master. The <code>FB Taverns Discount</code> tab holds (Customer, SKU) discount agreements. Re-upload to replace the master wholesale.</p>
</div>
<form action="{ext_url('/upload-tennents-master')}" method="post" enctype="multipart/form-data" style="max-width: 540px; margin-top: 1em">
  <h3 style="margin-top:0; color: #2c5aa0">Upload new master</h3>
  <p class="sub">Replaces every Tennents agreement currently in the system. Old agreements are deleted; the new file's rows take their place.</p>
  <label for="ten-master-file">Commercial Data file (.xlsx)</label>
  <input type="file" name="file" id="ten-master-file" accept=".xlsx" required>
  <button type="submit">Replace master</button>
</form>

<p style="margin-top:2em"><a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable base</a></p>
{PAGE_FOOT}"""


@app.post("/upload-tennents-master", response_class=HTMLResponse)
def upload_tennents_master(
    file: UploadFile = File(...),
    principal: DrinksPrincipal = Depends(require_drinks_role("admin")),
):
    original_name = file.filename or "uploaded.xlsx"
    if not original_name.lower().endswith(".xlsx"):
        return _error_page(f"File must be .xlsx (got {original_name!r})")

    suffix = Path(original_name).suffix or ".xlsx"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(_read_upload_capped(file))
            tmp_path = tmp.name
        agreements = parse_tennents_master(tmp_path)
        if not agreements:
            return _error_page("Master file produced zero agreements after parsing.")
        deleted, created = replace_tennents_master(agreements, source=original_name)
    except Exception:
        logger.exception("request failed")
        return _error_page("Something went wrong processing this request — the details have been logged. Try again, and if it recurs contact the administrator.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    customer_count = len({a.account for a in agreements})
    return f"""{render_head(principal.email, principal.role)}
<p class="sub" style="margin-top:0"><a href="{ext_url('/tennents')}">← Back to Tennents</a></p>
<h1>Tennents master replaced</h1>
{_tennents_master_banner_html()}
<div class="result">
  <div class="summary-row"><span>File</span><code>{escape(original_name)}</code></div>
  <div class="summary-row"><span>Agreements deleted</span><strong>{deleted}</strong></div>
  <div class="summary-row"><span>Agreements created</span><strong>{created}</strong></div>
  <div class="summary-row"><span>Distinct customers</span><strong>{customer_count}</strong></div>
</div>
<p style="margin-top:1.5em">
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable</a>
  <a class="button" href="{ext_url('/tennents')}" style="background:#666">Back to Tennents</a>
</p>
{PAGE_FOOT}"""


@app.post("/upload-tennents", response_class=HTMLResponse)
def upload_tennents(
    file: UploadFile = File(...),
    principal: DrinksPrincipal = Depends(require_drinks_role("editor")),
):
    original_name = file.filename or "uploaded.xlsx"
    if not original_name.lower().endswith(".xlsx"):
        return _error_page(f"File must be .xlsx (got {original_name!r})")

    suffix = Path(original_name).suffix or ".xlsx"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(_read_upload_capped(file))
            tmp_path = tmp.name

        agreements = load_tennents_agreements()
        if not agreements:
            return _error_page("No Tennents master loaded yet — upload Commercial Data first.")
        lines = parse_tennents_monthly(tmp_path)
        summary = reconcile_tennents(original_name, agreements, lines)
        file_rec_id = upsert_file_record(
            tmp_path,
            supplier="Tennents",
            line_count=len(lines),
            file_name_override=original_name,
        )
        n_findings = write_tennents_findings(summary, file_rec_id)
    except Exception:
        logger.exception("request failed")
        return _error_page("Something went wrong processing this request — the details have been logged. Try again, and if it recurs contact the administrator.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    summary_html = render_tennents_summary_html(summary)
    return f"""{render_head(principal.email, principal.role)}
<p class="sub" style="margin-top:0"><a href="{ext_url('/tennents')}">← Back to Tennents</a></p>
<h1>Tennents reconciliation complete</h1>
<p class="sub">{escape(original_name)} &middot; <code>{file_rec_id}</code> in Airtable &middot; {n_findings} findings inserted</p>
{_tennents_master_banner_html()}
<p>
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable</a>
  <a class="button" href="{ext_url('/tennents')}" style="background:#666">Upload another</a>
</p>
{summary_html}
{PAGE_FOOT}"""


def _error_page(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"""{render_head("", "")}
<h1>Error</h1>
<div class="result err">{message}</div>
<p><a href="{ext_url('/')}">Back</a></p>
{PAGE_FOOT}""",
        status_code=400,
    )
