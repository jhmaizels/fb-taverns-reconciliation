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
import traceback
from html import escape
from pathlib import Path

from datetime import date, timedelta
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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
    role_at_least,
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
    load_master_snapshot,
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
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.0f}"
    logger.info("%s %s -> %s in %.0fms", request.method, request.url.path, response.status_code, elapsed_ms)
    return response


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
        return HTMLResponse(
            f"""{render_head("", "")}
<h1>Not allowed</h1>
<div class="result err">You don't have permission to perform this action. Ask an admin to raise your drinks access level.</div>
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
<title>FB Taverns Reconciliation</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; color: #222; }
  .container { max-width: 1080px; margin: 2em auto; padding: 0 1em; }
  .site-header { background: #324556; display: flex; align-items: center; justify-content: space-between; padding: 0.55em 1.5em; }
  .site-header .brand img { height: 44px; display: block; }
  .site-nav { display: flex; align-items: center; }
  .site-nav a { color: rgba(255,255,255,0.85); text-decoration: none; font-weight: 600; text-transform: uppercase; font-size: 0.8em; letter-spacing: 0.03em; padding: 0.45em 1.1em; border-left: 1px solid rgba(255,255,255,0.22); }
  .site-nav a:first-child { border-left: 0; }
  .site-nav a:hover { color: #fff; }
  .site-user { display: flex; align-items: center; gap: 0.6em; }
  .site-user .who { color: rgba(255,255,255,0.85); font-size: 0.8em; }
  .site-user a.admin-link { color: rgba(255,255,255,0.85); text-decoration: none; font-weight: 600; text-transform: uppercase; font-size: 0.8em; letter-spacing: 0.03em; }
  .site-user a.admin-link:hover { color: #fff; }
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
  .help { font-size: 0.85em; color: #555; margin: -0.4em 0 1em; line-height: 1.4; }
  .help strong { color: #2c5aa0; }
</style>
</head><body>
"""


def render_head(user_email: str = "", drinks_role: str = "") -> str:
    """Full page head + navy site header + opening <main>.

    Replaces the old PAGE_HEAD constant. Preserves the existing nav
    (Home / LWC / Tennents / Tenancy Hub) and adds, on the right:
      * the signed-in email (escaped),
      * a relative sign-out form (POST /auth/signout),
      * a conditional cross-domain Admin link (only for drinks 'admin').
    Called with empty strings for error / pre-auth pages — the nav still
    renders, just without identity or the Admin link.
    """
    user_block = ""
    if user_email or drinks_role:
        admin_link = ""
        if role_at_least(drinks_role, "admin"):
            admin_link = (
                f'<a class="admin-link" href="{escape(TENANCY_ADMIN_URL)}" '
                f'target="_blank" rel="noopener">Admin &#8599;</a>'
            )
        who = f'<span class="who">{escape(user_email)}</span>' if user_email else ""
        user_block = (
            '<div class="site-user">'
            f'{who}'
            f'{admin_link}'
            f'<form method="post" action="{ext_url("/auth/signout")}">'
            '<button type="submit" class="signout">Sign out</button>'
            '</form>'
            '</div>'
        )
    return f"""{HEAD_STYLE}<header class="site-header">
  <a class="brand" href="{ext_url('/')}"><img src="{ext_url('/static/fb-taverns-logo.png')}" alt="FB Taverns"></a>
  <nav class="site-nav">
    <a href="{ext_url('/')}">Home</a>
    <a href="{ext_url('/lwc')}">LWC</a>
    <a href="{ext_url('/tennents')}">Tennents</a>
    <a href="https://tenancy-master.onrender.com/tenancy" target="_blank" rel="noopener">Tenancy Hub &#8599;</a>
  </nav>
  {user_block}
</header>
<main class="container">
"""

PAGE_FOOT = "</main></body></html>"


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
    """Clear both session cookies and relative-redirect to /login."""
    # Same-origin guard so a cross-site page can't force-sign-out the user.
    if _is_cross_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
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
    src_text = sources[0] if sources else "<em>none uploaded yet</em>"
    if len(sources) > 1:
        src_text += f' <span class="pill">+{len(sources)-1} other source(s)</span>'
    vf = info.get("latest_valid_from") or "—"
    uploaded_at = info.get("latest_uploaded_at") or ""
    # Render upload date as just YYYY-MM-DD HH:MM
    if uploaded_at:
        uploaded_at = uploaded_at.replace("T", " ")[:16]
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

    return f"""{render_head(principal.email, principal.role)}
<h1>FB Taverns Reconciliation</h1>
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
    # Default "Effective from" to today minus 14 days. Master uploads are
    # almost always corrections to the current master — this default makes
    # the new master cover the most recent weekly LWC files (which span
    # the past ~7 days) without any manual date entry. Override with an
    # earlier date for full retroactive replacement, or a later one for
    # genuine future-dated changes.
    today_iso = (date.today() - timedelta(days=14)).isoformat()
    return f"""{render_head(principal.email, principal.role)}
<p class="sub" style="margin-top:0"><a href="{ext_url('/')}">← Back to estate picker</a></p>
<h1>LWC Reconciliation <span class="estate-tag">England</span></h1>
<p class="sub">Upload a supplier file to reconcile. Or update the pricing master.</p>
{_master_banner_html()}

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

<h2>Pricing master</h2>
<div class="result" style="max-width: none">
  <p style="margin-top:0">The <strong>FB Taverns Cost Price File</strong> Excel is the master. Update it on the left for everyday changes (RPI, new tenants, corrections). Use the right-hand form for <em>temporary</em> tenant support that overrides the master price for a window — when reconciliations land in that window, mismatches get tagged with the support context.</p>
</div>

<div class="grid2" style="margin-top:1em">
  <form action="{ext_url('/upload-master')}" method="post" enctype="multipart/form-data">
    <h3>Download current master</h3>
    <p class="sub">The version currently in force. <a href="{ext_url('/export-master')}">Download master.xlsx</a> — anyone with the link can use it.</p>
    <h3 class="second-h3">Upload new master version</h3>
    <p class="sub">Replaces the current master. Existing rules with the same site &amp; product are closed at the effective date and replaced with the new prices.</p>
    <label for="vf">Effective from</label>
    <input type="date" name="valid_from" id="vf" value="{today_iso}" required>
    <p class="help">Defaults to <strong>today − 14 days</strong> — covers the latest LWC weekly file so a fresh upload immediately flows through. Sales <em>before</em> this date still reconcile against the previous master.<br>
    &bull; <strong>Most uploads:</strong> leave the default. The new master picks up the most recent week's invoices.<br>
    &bull; <strong>Fully retroactive correction</strong> (replace the prior master entirely): set this to whatever date the prior master started — typically the 1st of a month.<br>
    &bull; <strong>Future-dated change</strong> (RPI agreed for a date that hasn't happened yet): set this to that date.</p>
    <label for="m-file">Master file (.xlsx)</label>
    <input type="file" name="file" id="m-file" accept=".xlsx" required>
    <button type="submit">Upload new version</button>
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

<p style="margin-top:2em"><a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable base</a></p>
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
            tmp.write(file.file.read())
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
        return _error_page(f"<pre>{traceback.format_exc()}</pre>")
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
  <a class="button" href="{ext_url('/')}" style="background:#666">Upload another</a>
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
            tmp.write(file.file.read())
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
    return f"""{render_head(principal.email, principal.role)}
<h1>Retro reconciliation complete</h1>
<p class="sub">{original_name} &middot; <code>{file_rec_id}</code> in Airtable &middot; {n_findings} findings inserted</p>
{_master_banner_html()}
<p>
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable</a>
  <a class="button" href="{ext_url('/')}" style="background:#666">Upload another</a>
</p>
{summary_html}
{PAGE_FOOT}"""


@app.get("/export-master")
def export_master(principal: DrinksPrincipal = Depends(require_drinks_role("viewer"))):
    """Download the current Airtable master as a wide-form Excel."""
    from master_export import build_master_xlsx_bytes  # lazy: keeps openpyxl off the boot path
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
            tmp.write(file.file.read())
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

        lookups: dict = {}
        rules_created, rules_updated, rules_closed = upsert_pricing_rules(
            rules, close_keys_at_date=vf, lookups_out=lookups
        )
        # Reuse the product map built above so we don't re-read the Products table.
        products_created, products_updated = upsert_products_with_retros(
            products, existing_by_code=lookups.get("product_ids")
        )
    except Exception:
        return _error_page(f"<pre>{traceback.format_exc()}</pre>")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

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
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable</a>
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
        # Build the lookup tables Claude needs to resolve site + product references
        site_records = load_sites_from_airtable()
        # Products: pull from Airtable directly so the LLM sees every code we know about
        from airtable_io import _list_all, T  # noqa: E402
        product_records = _list_all(T["Products"], fields=["product_code", "description"])
        products = {
            (rec["fields"].get("product_code") or ""): {
                "description": rec["fields"].get("description") or "",
            }
            for rec in product_records
            if rec["fields"].get("product_code")
        }
        # Filter sites to those with active rules so the LLM doesn't pick a retired one
        rules = load_rules_from_airtable()
        active_sites = {r.site_id for r in rules if r.valid_to is None}
        sites = {sid: info for sid, info in site_records.items() if sid in active_sites}

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
        return _error_page(f"<pre>{traceback.format_exc()}</pre>")

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


# ---------- Tennents Direct ----------

def _tennents_master_banner_html() -> str:
    try:
        info = get_tennents_master_info()
    except Exception:
        return ""
    sources = info.get("sources") or []
    src_text = sources[0] if sources else "<em>none uploaded yet</em>"
    if len(sources) > 1:
        src_text += f' <span class="pill">+{len(sources)-1} other source(s)</span>'
    uploaded_at = (info.get("latest_uploaded_at") or "").replace("T", " ")[:16]
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
            tmp.write(file.file.read())
            tmp_path = tmp.name
        agreements = parse_tennents_master(tmp_path)
        if not agreements:
            return _error_page("Master file produced zero agreements after parsing.")
        deleted, created = replace_tennents_master(agreements, source=original_name)
    except Exception:
        return _error_page(f"<pre>{traceback.format_exc()}</pre>")
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
            tmp.write(file.file.read())
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
        return _error_page(f"<pre>{traceback.format_exc()}</pre>")
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
