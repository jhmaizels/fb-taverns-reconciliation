"""
Supabase-backed auth core for the FB Taverns reconciliation service.

Migrates the app OFF the shared HTTP Basic password ONTO the SAME Supabase
project tenancy-master uses, with PER-USER drinks roles read from
`user_app_roles` (app=drinks).

Design decisions (operator-confirmed — build to these EXACTLY):

  D1  ROLE READ = USER'S OWN TOKEN + RLS, never service-role. This module holds
      ONLY the public anon key + Supabase URL. A user's drinks role is fetched
      from PostgREST with the USER'S OWN access token:
        GET {URL}/rest/v1/user_app_roles?user_id=eq.<uid>&app=eq.drinks&select=role
        apikey: <ANON>   Authorization: Bearer <user_access_token>
      The RLS policy user_app_roles_select (user_id = auth.uid() OR
      is_platform_admin()) lets a user read their OWN row, so no god-key is
      needed. The uid comes from validating the token first; the query filters
      by that same uid so the self-read returns exactly their row.

  D2  CUTOVER COMPLETE (2026-07-04). The legacy HTTP Basic dual-mode fallback
      has been REMOVED (pre-launch assessment P0: one shared password granted
      admin-equivalent, bypassing per-user roles). Auth is solely the hub's
      Supabase session; no Supabase session => not authorised.

Role mapping = three-tier by blast radius. ROLE_RANK = viewer:1, editor:2,
admin:3. See the integration contract for the route->minimum-role table.

Style: stdlib + `requests` (already pinned), f-strings, no new framework, no
new Python deps. Never raises raw secrets; all network calls are wrapped with
timeouts and fail safe (no session => not authorised, never => allowed).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import quote

import requests
from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse

logger = logging.getLogger("fbtaverns.auth")

# ---------------------------------------------------------------------------
# Environment (NO service-role key is ever read here)
# ---------------------------------------------------------------------------
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY") or ""

# ---------------------------------------------------------------------------
# External base-path mechanism (reverse-proxy mounting under /drinks).
#
# tenancy-master proxies /drinks/* to this service with a rewrite that STRIPS
# the /drinks prefix, so the FastAPI ROUTES stay at root (/lwc, /upload, ...).
# Only the browser-facing URLs this app EMITS (links, form actions, redirects,
# fetch targets, cookie Path, supabase redirectTo) get the prefix.
#
#   EXTERNAL_BASE_PATH = "" (standalone, default) or "/drinks" (under proxy)
#   PUBLIC_ORIGIN      = the public origin the browser sees under the proxy,
#                        e.g. "https://tenancy-master.onrender.com" — accepted
#                        by the login-CSRF same-origin guard in addition to the
#                        request's own derived origin.
# ---------------------------------------------------------------------------
EXTERNAL_BASE_PATH = (os.environ.get("EXTERNAL_BASE_PATH") or "").rstrip("/")
PUBLIC_ORIGIN = (os.environ.get("PUBLIC_ORIGIN") or "").rstrip("/")


def ext_url(path: str) -> str:
    """Prefix an internal (root-relative) path with EXTERNAL_BASE_PATH so it is
    correct for the browser under the proxy. `path` must begin with "/".
    With BASE unset this is the identity, so the app runs standalone unchanged."""
    return f"{EXTERNAL_BASE_PATH}{path}"

TENANCY_ADMIN_URL = (
    os.environ.get("TENANCY_ADMIN_URL")
    or "https://tenancy-master.onrender.com/admin"
)

# Legacy HTTP Basic dual-mode fallback REMOVED at cutover (2026-07-04,
# pre-launch assessment P0): the single-shared-password path granted full
# drinks-admin, bypassing the per-user Supabase role model. Auth is now
# solely via the hub's Supabase session. WEB_USERNAME/WEB_PASSWORD/
# LEGACY_BASIC_FALLBACK are no longer read; delete them from the Render env.


# ---------------------------------------------------------------------------
# HTTP request budget
# ---------------------------------------------------------------------------
_HTTP_TIMEOUT = 8  # seconds, per Supabase call


# ---------------------------------------------------------------------------
# Cookies — we own both reader and writer
# ---------------------------------------------------------------------------
COOKIE_AT = "fb_drinks_at"   # Supabase access token
COOKIE_RT = "fb_drinks_rt"   # Supabase refresh token

# Access tokens are short-lived (Supabase default ~1h); the refresh token is
# the long-lived credential. We don't set an explicit Max-Age on the access
# cookie (session cookie semantics) and give the refresh cookie 30 days.
_RT_MAX_AGE = 30 * 24 * 3600


# Cookie Path is scoped to the external base path so the drinks session cookies
# are NOT sent on sibling tenancy-master requests under the same host. set and
# clear MUST use the SAME path or the browser won't match (and so won't clear)
# the cookie. BASE unset => Path "/" (standalone, unchanged behaviour).
_COOKIE_PATH = EXTERNAL_BASE_PATH or "/"


def set_session_cookies(response: Response, at: str, rt: str) -> None:
    """Write both session cookies: HttpOnly, Secure, SameSite=Lax, Path=BASE."""
    response.set_cookie(
        COOKIE_AT, at,
        httponly=True, secure=True, samesite="lax", path=_COOKIE_PATH,
    )
    response.set_cookie(
        COOKIE_RT, rt,
        httponly=True, secure=True, samesite="lax", path=_COOKIE_PATH,
        max_age=_RT_MAX_AGE,
    )


def clear_session_cookies(response: Response) -> None:
    """Delete both session cookies (sign-out / failed refresh). Path MUST match
    set_session_cookies or the browser won't clear them."""
    response.delete_cookie(COOKIE_AT, path=_COOKIE_PATH, samesite="lax")
    response.delete_cookie(COOKIE_RT, path=_COOKIE_PATH, samesite="lax")


# ---------------------------------------------------------------------------
# Role ranks
# ---------------------------------------------------------------------------
ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}


def role_at_least(role: Optional[str], minimum: str) -> bool:
    """True iff `role` meets or exceeds `minimum` on the blast-radius ladder.
    Unknown / None role => False (fail safe)."""
    have = ROLE_RANK.get((role or "").strip().lower(), 0)
    need = ROLE_RANK.get((minimum or "").strip().lower(), 0)
    return have > 0 and need > 0 and have >= need


# ---------------------------------------------------------------------------
# Resolved-principal carrier (attached to request.state by the dependency)
# ---------------------------------------------------------------------------
@dataclass
class DrinksPrincipal:
    email: str          # signed-in email
    role: str           # one of viewer/editor/admin
    user_id: Optional[str] = None   # Supabase uid

    @property
    def is_admin(self) -> bool:
        return role_at_least(self.role, "admin")


# ---------------------------------------------------------------------------
# Supabase auth/token network helpers (all wrapped, fail safe)
# ---------------------------------------------------------------------------
def validate_token(access_token: str) -> Optional[dict]:
    """Validate a Supabase access token via GET {URL}/auth/v1/user.

    Returns a dict with at least {'id', 'email'} on success, else None
    (on 401, network error, or misconfiguration). Never raises."""
    if not access_token or not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {access_token}",
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("validate_token network error: %s", type(exc).__name__)
        return None

    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            return None
        uid = data.get("id")
        if not uid:
            return None
        return {"id": uid, "email": data.get("email") or "", "raw": data}
    if resp.status_code != 401:
        # Surface non-auth failures at WARN, without the token.
        logger.warning("validate_token unexpected status %s", resp.status_code)
    return None


def refresh_session(refresh_token: str) -> Optional[Tuple[str, str]]:
    """Exchange a refresh token for a fresh (access, refresh) pair via
    POST {URL}/auth/v1/token?grant_type=refresh_token.

    Returns (new_access, new_refresh) on success, else None. Never raises."""
    if not refresh_token or not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    try:
        resp = requests.post(
            f"{SUPABASE_URL}/auth/v1/token",
            params={"grant_type": "refresh_token"},
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Content-Type": "application/json",
            },
            json={"refresh_token": refresh_token},
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("refresh_session network error: %s", type(exc).__name__)
        return None

    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    new_at = data.get("access_token")
    new_rt = data.get("refresh_token")
    if not new_at or not new_rt:
        return None
    return new_at, new_rt


def get_drinks_role(user_id: str, access_token: str) -> Optional[str]:
    """Read the user's drinks role from user_app_roles via PostgREST, using the
    USER'S OWN token + RLS self-read (D1). NEVER uses a service-role key.

      GET {URL}/rest/v1/user_app_roles?user_id=eq.<uid>&app=eq.drinks&select=role
      apikey: <ANON>   Authorization: Bearer <user token>

    Returns the role string (viewer/editor/admin) or None if the user has no
    drinks row / on any error. Never raises."""
    if not user_id or not access_token or not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/user_app_roles",
            params={
                "user_id": f"eq.{user_id}",
                "app": "eq.drinks",
                "select": "role",
            },
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("get_drinks_role network error: %s", type(exc).__name__)
        return None

    if resp.status_code != 200:
        if resp.status_code not in (401, 403, 404):
            logger.warning("get_drinks_role unexpected status %s", resp.status_code)
        return None
    try:
        rows = resp.json()
    except ValueError:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    role = (rows[0] or {}).get("role")
    role = (role or "").strip().lower()
    return role if role in ROLE_RANK else None


# ---------------------------------------------------------------------------
# SINGLE SIGN-ON: trust the hub's Supabase session.
#
# Under the tenancy-master proxy this app is same-origin with the hub, so the
# hub's @supabase/ssr session cookie (sb-<ref>-auth-token) is sent to /drinks
# and forwarded here by the proxy. Reading it lets a hub-signed-in user into
# drinks with NO separate drinks login and NO handoff route — their per-user
# drinks role still gates access. @supabase/ssr (0.6.x) stores the value as
# `base64-<base64url(JSON session)>`, chunked into `<name>.0`, `.1`, … at 3180
# chars. Never raises.
# ---------------------------------------------------------------------------
def _hub_cookie_name() -> str:
    """sb-<project-ref>-auth-token, ref = the SUPABASE_URL subdomain."""
    ref = ""
    try:
        ref = SUPABASE_URL.split("://", 1)[-1].split(".", 1)[0]
    except Exception:
        ref = ""
    return f"sb-{ref}-auth-token"


def _hub_access_token(request: Request) -> Optional[str]:
    """Return the access token from the hub's Supabase session cookie, or None."""
    try:
        base = _hub_cookie_name()
        if not base or base == "sb--auth-token":
            return None
        cookies = request.cookies
        if f"{base}.0" in cookies:
            parts, i = [], 0
            while f"{base}.{i}" in cookies:
                parts.append(cookies[f"{base}.{i}"])
                i += 1
            raw = "".join(parts)
        elif base in cookies:
            raw = cookies[base]
        else:
            return None
        if raw.startswith("base64-"):
            b = raw[len("base64-"):]
            decoded = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8")
        else:
            from urllib.parse import unquote
            decoded = unquote(raw)
        data = json.loads(decoded)
        tok = None
        if isinstance(data, dict):
            tok = data.get("access_token") or (data.get("currentSession") or {}).get("access_token")
        elif isinstance(data, list) and data:
            tok = data[0]
        return tok if isinstance(tok, str) and tok else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Internal: resolve a Supabase principal from the request cookies, refreshing
# if needed. Returns (principal_or_None, cookie_rewrites_or_None).
# cookie_rewrites is (new_at, new_rt) when a refresh happened — the dependency
# writes them onto the outgoing Response.
# ---------------------------------------------------------------------------
def _resolve_supabase(request: Request) -> Tuple[Optional[DrinksPrincipal], Optional[Tuple[str, str]]]:
    at = request.cookies.get(COOKIE_AT)
    rt = request.cookies.get(COOKIE_RT)
    cookie_rewrites: Optional[Tuple[str, str]] = None

    user = validate_token(at) if at else None

    if user is None and rt:
        # Access token missing/expired — try one refresh.
        refreshed = refresh_session(rt)
        if refreshed:
            new_at, new_rt = refreshed
            user = validate_token(new_at)
            if user is not None:
                cookie_rewrites = (new_at, new_rt)
                at = new_at

    # SINGLE SIGN-ON: if this app's own cookies didn't resolve a user, trust the
    # hub's Supabase session (same-origin cookie sent to /drinks). The user is
    # then validated + role-gated exactly as normal — `at` becomes their own
    # (hub) access token so the RLS self-read for the drinks role uses it.
    if user is None:
        hub_at = _hub_access_token(request)
        if hub_at:
            hub_user = validate_token(hub_at)
            if hub_user is not None:
                user = hub_user
                at = hub_at

    if user is None:
        return None, cookie_rewrites  # cookie_rewrites is None here

    role = get_drinks_role(user["id"], at)
    principal = DrinksPrincipal(
        email=user.get("email") or "",
        role=role or "",
        user_id=user["id"],
    )
    # role may be "" (no drinks row) — caller distinguishes "authed but no
    # access" from "below minimum".
    return principal, cookie_rewrites


# ---------------------------------------------------------------------------
# The dependency factory
# ---------------------------------------------------------------------------
# Sentinel detail strings the webapp layer can match on to render the right page
# instead of a bare JSON error. RedirectResponse is returned directly for GETs.
NO_ACCESS_DETAIL = "drinks:no-access"
FORBIDDEN_DETAIL = "drinks:forbidden"


def require_drinks_role(minimum: str = "viewer"):
    """FastAPI dependency FACTORY.

    Usage:
        @app.get("/lwc")
        def lwc_home(principal: DrinksPrincipal = Depends(require_drinks_role("viewer"))):
            ...

    Behaviour:
      * Reads COOKIE_AT; validate_token. On failure, tries refresh_session via
        COOKIE_RT, re-validates, and rewrites cookies on success.
      * If a Supabase user resolved:
          - no drinks role  -> 403 with NO_ACCESS_DETAIL (webapp renders the
                               "no drinks access / pending" page).
          - role below `minimum` -> 403 with FORBIDDEN_DETAIL.
          - else: attach the principal to request.state.drinks and return it.
      * DUAL-MODE: if NO Supabase session resolved AND legacy fallback enabled
        AND valid Basic creds present -> allow as admin-equivalent.
      * Else: GET -> relative redirect to /login (with ?next=); non-GET -> 401.

    Fails safe: any unresolved/error path => not authorised.
    """
    if minimum not in ROLE_RANK:
        raise ValueError(f"unknown minimum role {minimum!r}")

    def _dependency(request: Request) -> DrinksPrincipal:
        principal, cookie_rewrites = _resolve_supabase(request)

        # Stage any freshly-refreshed session for the OUTGOING response. We do
        # NOT write cookies on the FastAPI-injected Response here: routes that
        # return their OWN Response/HTMLResponse (e.g. /export-master, the
        # _error_page paths) drop Set-Cookie headers placed on the injected
        # response. The timing middleware in webapp.py applies this staged
        # rewrite to the REAL outgoing response — scope["state"] is shared
        # between the dependency and the middleware, so this is visible there.
        # Staged before the role checks so a refreshed-but-unauthorised user
        # still gets the rotated cookie persisted (and won't re-refresh).
        if cookie_rewrites:
            request.state.drinks_cookie_rewrite = cookie_rewrites

        if principal is not None:
            # Authed Supabase user. Attach the principal BEFORE the role checks
            # so the 403 pages (no-access / forbidden) can show the signed-in
            # identity + a working sign-out even when access is refused.
            request.state.drinks = principal
            if not principal.role:
                # Signed in, but no drinks entitlement (GET and non-GET alike).
                raise HTTPException(status_code=403, detail=NO_ACCESS_DETAIL)
            if not role_at_least(principal.role, minimum):
                raise HTTPException(status_code=403, detail=FORBIDDEN_DETAIL)
            return principal

        # No Supabase session → not authorised (the legacy Basic fallback was
        # removed at cutover). If the request carried (now-stale) session cookies,
        # stage them for clearing so a dead/expired session doesn't trigger a
        # wasteful validate+refresh round-trip on every subsequent request. The
        # middleware clears them on the redirect (GET) / 401 (POST) response.
        if request.cookies.get(COOKIE_AT) or request.cookies.get(COOKIE_RT):
            request.state.drinks_clear_cookies = True

        if request.method == "GET":
            # No drinks session AND no hub session → the single sign-in lives at
            # the HUB. Send them to the team-hub root to sign in there; drinks has
            # no login of its own now (it trusts the hub session — see
            # _hub_access_token). Relative "/" so the Render proxy's internal host
            # isn't leaked, and standalone (no hub) falls back to its own login.
            target = "/" if EXTERNAL_BASE_PATH else f"{ext_url('/login')}"
            raise _RedirectException(target)
        raise HTTPException(status_code=401, detail="Not authenticated")

    return _dependency


# ---------------------------------------------------------------------------
# Redirect-as-exception plumbing
# ---------------------------------------------------------------------------
# FastAPI dependencies can't return a RedirectResponse to short-circuit a route
# whose return type is the principal. We raise a small exception and register an
# exception handler (install_auth_handlers) that turns it into a relative 303.
# ---------------------------------------------------------------------------
class _RedirectException(Exception):
    def __init__(self, location: str):
        self.location = location
        super().__init__(location)


def install_auth_handlers(app) -> None:
    """Register the redirect-exception handler on the FastAPI app. Call once at
    startup in webapp.py:  install_auth_handlers(app)."""

    @app.exception_handler(_RedirectException)
    async def _handle_redirect(_request: Request, exc: _RedirectException):  # noqa: ANN001
        # 303 so a POST that somehow lands here would GET /login; relative
        # Location keeps us off the Render internal host.
        return RedirectResponse(url=exc.location, status_code=303)
