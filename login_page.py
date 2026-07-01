"""
Self-contained HTML pages for the Supabase auth migration (Agent-LOGIN).

These three functions return complete HTML documents as strings, served by
webapp.py's OPEN endpoints:

  render_login_page(url, anon_key)    -> GET /login
  render_callback_page(url, anon_key) -> GET /auth/callback
  render_no_access_page(email, admin) -> rendered on 403 NO_ACCESS_DETAIL

Design (operator-confirmed — build to these EXACTLY):

  * supabase-js v2 loaded from a CDN as an ESM module (esm.sh). No npm/build step.
  * SUPABASE_URL + the *publishable* anon key are injected server-side. The anon
    key is public by design; nothing here holds a service-role key or any other
    secret.
  * Sign-in offers (a) Microsoft 365 via signInWithOAuth({provider:'azure'}),
    (b) email + password via signInWithPassword, and (c) a "set a password"
    sign-up toggle via signUp({...emailRedirectTo}).
  * All redirect URLs are built from location.origin at runtime so the pages are
    domain-portable (works on fb-taverns-reconcile.onrender.com or any host).
  * After a password sign-in (and after the OAuth PKCE exchange on the callback
    page) the browser reads the session and POSTs {access_token, refresh_token}
    to /auth/session, which sets HttpOnly cookies, then does a *relative*
    redirect.

Branding mirrors webapp.py's PAGE_HEAD: navy #324556 header, the FB Taverns
logo from /static/fb-taverns-logo.png, the blue #2c5aa0 accent buttons.

The value substitutions use a sentinel-replace approach (not str.format) so the
embedded JavaScript's own braces don't need escaping.
"""

from __future__ import annotations

from html import escape

# External base path under the reverse proxy (e.g. "/drinks"); "" standalone.
# These pages are SERVED at BASE/login & BASE/auth/callback, so
# window.location.origin is just scheme+host (no path) — every emitted/fetched
# URL must therefore carry BASE explicitly. Imported from the auth core so the
# whole app reads one source of truth.
from auth_supabase import EXTERNAL_BASE_PATH

# supabase-js v2 from a CDN, imported as an ESM module. Pinned to a major to
# avoid surprise breaking changes while staying patch-current.
_SUPABASE_JS_CDN = "https://esm.sh/@supabase/supabase-js@2"

# Shared CSS — a trimmed subset of webapp.py's PAGE_HEAD <style>, enough to keep
# the navy header + blue accents consistent on these standalone pages.
_BASE_CSS = """
  * { box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; color: #222; background: #f4f6f8; }
  .site-header { background: #324556; display: flex; align-items: center; justify-content: space-between; padding: 0.55em 1.5em; }
  .site-header .brand img { height: 44px; display: block; }
  .site-header .brand { display: inline-block; }
  .wrap { max-width: 420px; margin: 3em auto; padding: 0 1em; }
  .card { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 1.6em; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
  h1 { font-size: 1.4em; margin: 0 0 0.2em; color: #324556; }
  .sub { color: #666; margin: 0 0 1.4em; font-size: 0.95em; }
  label { display: block; margin-bottom: 0.35em; font-weight: 600; font-size: 0.9em; }
  input[type=email], input[type=password] { display: block; width: 100%; padding: 0.6em; margin-bottom: 1em; border: 1px solid #ccc; border-radius: 4px; font-size: 1em; }
  button { width: 100%; background: #2c5aa0; color: #fff; border: 0; padding: 0.75em 1.5em; border-radius: 4px; font-size: 1em; cursor: pointer; font-weight: 600; }
  button:hover { background: #1d3f74; }
  button:disabled { opacity: 0.6; cursor: default; }
  button.ms { background: #324556; display: flex; align-items: center; justify-content: center; gap: 0.6em; }
  button.ms:hover { background: #233240; }
  button.link { background: none; color: #2c5aa0; padding: 0; width: auto; font-weight: 600; font-size: 0.9em; }
  button.link:hover { background: none; text-decoration: underline; }
  .ms-glyph { display: inline-grid; grid-template-columns: 9px 9px; grid-template-rows: 9px 9px; gap: 2px; }
  .ms-glyph span { display: block; width: 9px; height: 9px; }
  .ms-glyph span:nth-child(1){background:#f25022} .ms-glyph span:nth-child(2){background:#7fba00}
  .ms-glyph span:nth-child(3){background:#00a4ef} .ms-glyph span:nth-child(4){background:#ffb900}
  .divider { display: flex; align-items: center; text-align: center; color: #999; font-size: 0.8em; margin: 1.4em 0; }
  .divider::before, .divider::after { content: ""; flex: 1; border-bottom: 1px solid #e0e0e0; }
  .divider span { padding: 0 0.8em; }
  .msg { padding: 0.7em 0.9em; border-radius: 4px; font-size: 0.9em; margin-bottom: 1em; display: none; }
  .msg.err { background: #fee; border: 1px solid #caa; color: #8a1f1f; display: block; }
  .msg.ok { background: #eef9ee; border: 1px solid #b6d9b6; color: #1f5a1f; display: block; }
  .msg.info { background: #eef4fb; border: 1px solid #c7d8f0; color: #1d3f74; display: block; }
  .toggle-row { text-align: center; margin-top: 1.2em; font-size: 0.9em; color: #666; }
  .foot { text-align: center; margin-top: 1.5em; color: #999; font-size: 0.8em; }
  a { color: #2c5aa0; }
  .spin { text-align: center; padding: 2em 0; color: #555; }
"""

def _header_html(base: str = "") -> str:
    """Navy header with the brand link + logo (logo src BASE-prefixed).

    The brand link is always "/": under the proxy that is the TEAM HUB portal
    (the FB logo returns to the hub everywhere); standalone it is the app root."""
    return (
        '<header class="site-header">'
        f'<a class="brand" href="/"><img src="{base}/static/fb-taverns-logo.png" alt="FB Taverns"></a>'
        "</header>"
    )


def _doc(title: str, body: str, head_extra: str = "", base: str = "") -> str:
    """Assemble a full HTML document with shared CSS + navy header."""
    return (
        "<!doctype html>\n"
        '<html lang="en"><head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>{_BASE_CSS}</style>\n"
        f"{head_extra}"
        "</head><body>\n"
        f"{_header_html(base)}\n"
        f"{body}\n"
        "</body></html>"
    )


def _inject(template: str, supabase_url: str, anon_key: str, base: str = "") -> str:
    """Replace the JS sentinels with JSON-safe string literals of url/anon
    key/base.

    Using sentinel replacement (not str.format / %) means the embedded JS braces
    need no escaping. The values are wrapped in JSON-quoted form so any stray
    quote can't break out of the string literal.
    """
    import json

    return (
        template
        .replace("__SUPABASE_URL_JSON__", json.dumps(supabase_url or ""))
        .replace("__SUPABASE_ANON_KEY_JSON__", json.dumps(anon_key or ""))
        .replace("__EXTERNAL_BASE_JSON__", json.dumps(base or ""))
    )


# ---------------------------------------------------------------------------
# /login
# ---------------------------------------------------------------------------
def render_login_page(supabase_url: str, anon_key: str, base: str | None = None) -> str:
    """The sign-in page. Microsoft 365 + email/password + a sign-up toggle.

    `supabase_url` and `anon_key` are injected server-side (anon key is public).
    `base` is the external base path (e.g. "/drinks"); defaults to
    EXTERNAL_BASE_PATH. All same-origin URLs are built as origin + BASE + path
    at runtime so the page is correct under the proxy and standalone alike.
    """
    if base is None:
        base = EXTERNAL_BASE_PATH
    body = """
<div class="wrap">
  <div class="card">
    <h1>FB Taverns Reconciliation</h1>
    <p class="sub" id="sub">Sign in to continue.</p>

    <div class="msg" id="msg" role="alert"></div>

    <button type="button" class="ms" id="ms-btn">
      <span class="ms-glyph" aria-hidden="true"><span></span><span></span><span></span><span></span></span>
      Sign in with Microsoft
    </button>

    <div class="divider"><span>or</span></div>

    <form id="pw-form" autocomplete="on">
      <label for="email">Email</label>
      <input type="email" id="email" name="email" autocomplete="email" required>
      <label for="password">Password</label>
      <input type="password" id="password" name="password" autocomplete="current-password" required>
      <button type="submit" id="submit-btn">Sign in</button>
    </form>

    <div class="toggle-row">
      <span id="toggle-prompt">No password yet?</span>
      <button type="button" class="link" id="toggle-btn">Set a password</button>
    </div>
  </div>
  <div class="foot">FB Taverns &middot; secure sign-in</div>
</div>

<script type="module">
  import { createClient } from "__SUPABASE_JS_CDN__";

  const SUPABASE_URL = __SUPABASE_URL_JSON__;
  const ANON_KEY = __SUPABASE_ANON_KEY_JSON__;
  const BASE = __EXTERNAL_BASE_JSON__;   // external prefix ("" or "/drinks")
  const supabase = createClient(SUPABASE_URL, ANON_KEY);

  const msgEl = document.getElementById("msg");
  const subEl = document.getElementById("sub");
  const promptEl = document.getElementById("toggle-prompt");
  const toggleBtn = document.getElementById("toggle-btn");
  const submitBtn = document.getElementById("submit-btn");
  const pwInput = document.getElementById("password");
  const form = document.getElementById("pw-form");

  // Read next + any error surfaced by the callback redirect from the query.
  // A present `next` is already an EXTERNAL (/drinks/...) path from the server
  // redirect, so use it as-is; default to BASE + "/" when empty.
  const params = new URLSearchParams(window.location.search);
  let nextPath = params.get("next") || (BASE + "/");
  if (!nextPath.startsWith("/")) nextPath = BASE + "/";   // relative-only, no open redirect
  const errDesc = params.get("error_description") || params.get("error");

  function showMsg(text, kind) {
    msgEl.textContent = text;
    msgEl.className = "msg " + (kind || "info");
  }
  function clearMsg() { msgEl.className = "msg"; msgEl.textContent = ""; }

  if (errDesc) showMsg(errDesc, "err");

  // The redirect target after OAuth / after a password sign-in's cookie set.
  // origin is scheme+host only (no path), so prepend BASE explicitly.
  function callbackUrl() {
    return window.location.origin + BASE + "/auth/callback?next=" + encodeURIComponent(nextPath);
  }

  // Sign-in vs sign-up mode toggle ("set a password").
  let signupMode = false;
  function applyMode() {
    if (signupMode) {
      subEl.textContent = "Set a password for your account.";
      submitBtn.textContent = "Create account";
      pwInput.autocomplete = "new-password";
      promptEl.textContent = "Already have a password?";
      toggleBtn.textContent = "Sign in";
    } else {
      subEl.textContent = "Sign in to continue.";
      submitBtn.textContent = "Sign in";
      pwInput.autocomplete = "current-password";
      promptEl.textContent = "No password yet?";
      toggleBtn.textContent = "Set a password";
    }
  }
  toggleBtn.addEventListener("click", () => { signupMode = !signupMode; clearMsg(); applyMode(); });

  // (a) Microsoft 365 via Supabase 'azure' provider.
  document.getElementById("ms-btn").addEventListener("click", async () => {
    clearMsg();
    try {
      const { error } = await supabase.auth.signInWithOAuth({
        provider: "azure",
        options: {
          scopes: "openid profile email",
          redirectTo: callbackUrl(),
        },
      });
      if (error) showMsg(error.message || "Microsoft sign-in failed.", "err");
      // On success the browser is redirected to Microsoft, then back to /auth/callback.
    } catch (e) {
      showMsg("Microsoft sign-in failed. Please try again.", "err");
    }
  });

  // (b/c) email + password sign-in, or (c) sign-up to set a password.
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    clearMsg();
    const email = document.getElementById("email").value.trim();
    const password = pwInput.value;
    if (!email || !password) { showMsg("Enter your email and password.", "err"); return; }
    submitBtn.disabled = true;

    try {
      if (signupMode) {
        const { data, error } = await supabase.auth.signUp({
          email,
          password,
          options: { emailRedirectTo: callbackUrl() },
        });
        if (error) { showMsg(error.message || "Sign-up failed.", "err"); return; }
        // If email confirmation is required there is no session yet.
        if (data && data.session) {
          await postSessionAndGo(data.session);
        } else {
          showMsg("Check your email to confirm your account, then sign in.", "ok");
          signupMode = false; applyMode();
        }
      } else {
        const { data, error } = await supabase.auth.signInWithPassword({ email, password });
        if (error) { showMsg(error.message || "Sign-in failed.", "err"); return; }
        if (data && data.session) {
          await postSessionAndGo(data.session);
        } else {
          showMsg("Could not establish a session. Please try again.", "err");
        }
      }
    } catch (e) {
      showMsg("Something went wrong. Please try again.", "err");
    } finally {
      submitBtn.disabled = false;
    }
  });

  // POST the session tokens to the server (sets HttpOnly cookies) then redirect.
  async function postSessionAndGo(session) {
    const at = session.access_token;
    const rt = session.refresh_token;
    if (!at || !rt) { showMsg("Incomplete session returned. Please try again.", "err"); return; }
    try {
      const resp = await fetch(BASE + "/auth/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_token: at, refresh_token: rt, next: nextPath }),
      });
      if (!resp.ok) { showMsg("Could not start your session. Please try again.", "err"); return; }
      let dest = nextPath;
      try { const j = await resp.json(); if (j && typeof j.next === "string" && j.next.startsWith("/")) dest = j.next; } catch (e) {}
      window.location.assign(dest);
    } catch (e) {
      showMsg("Could not start your session. Please try again.", "err");
    }
  }

  applyMode();
</script>
"""
    body = body.replace("__SUPABASE_JS_CDN__", _SUPABASE_JS_CDN)
    body = _inject(body, supabase_url, anon_key, base)
    return _doc("FB Taverns — Sign in", body, base=base)


# ---------------------------------------------------------------------------
# /auth/callback
# ---------------------------------------------------------------------------
def render_callback_page(supabase_url: str, anon_key: str, base: str | None = None) -> str:
    """The OAuth/PKCE landing page.

    supabase-js (detectSessionInUrl) completes the code exchange in the browser,
    then we read the session and POST {access_token, refresh_token} to
    BASE/auth/session, then redirect (relative) to `next`. On error, redirect
    back to BASE/login?error_description=...
    """
    if base is None:
        base = EXTERNAL_BASE_PATH
    body = """
<div class="wrap">
  <div class="card">
    <div class="spin" id="spin">Signing you in…</div>
    <div class="msg" id="msg" role="alert"></div>
  </div>
</div>

<script type="module">
  import { createClient } from "__SUPABASE_JS_CDN__";

  const SUPABASE_URL = __SUPABASE_URL_JSON__;
  const ANON_KEY = __SUPABASE_ANON_KEY_JSON__;
  const BASE = __EXTERNAL_BASE_JSON__;   // external prefix ("" or "/drinks")
  // detectSessionInUrl lets supabase-js complete the PKCE exchange from the URL.
  const supabase = createClient(SUPABASE_URL, ANON_KEY, {
    auth: { detectSessionInUrl: true, flowType: "pkce" },
  });

  const spin = document.getElementById("spin");
  const msgEl = document.getElementById("msg");

  // next from query; relative-only. A present `next` is already EXTERNAL
  // (/drinks/...); default to BASE + "/" when empty.
  const params = new URLSearchParams(window.location.search);
  let nextPath = params.get("next") || (BASE + "/");
  if (!nextPath.startsWith("/")) nextPath = BASE + "/";

  function bounceToLogin(desc) {
    const q = new URLSearchParams();
    q.set("next", nextPath);
    if (desc) q.set("error_description", desc);
    window.location.assign(BASE + "/login?" + q.toString());   // relative
  }

  function fail(desc) {
    spin.style.display = "none";
    msgEl.className = "msg err";
    msgEl.textContent = desc || "Sign-in failed.";
    setTimeout(() => bounceToLogin(desc), 1800);
  }

  (async () => {
    // Surface provider errors passed back in the query/fragment.
    const hash = new URLSearchParams((window.location.hash || "").replace(/^#/, ""));
    const providerErr = params.get("error_description") || params.get("error")
      || hash.get("error_description") || hash.get("error");
    if (providerErr) { fail(providerErr); return; }

    try {
      // Give supabase-js a moment to process the URL, then read the session.
      let session = null;
      const got = await supabase.auth.getSession();
      session = got && got.data ? got.data.session : null;

      // If detectSessionInUrl hasn't settled yet, retry briefly.
      for (let i = 0; i < 10 && !session; i++) {
        await new Promise(r => setTimeout(r, 150));
        const g = await supabase.auth.getSession();
        session = g && g.data ? g.data.session : null;
      }

      if (!session || !session.access_token || !session.refresh_token) {
        fail("Could not complete sign-in. Please try again.");
        return;
      }

      const resp = await fetch(BASE + "/auth/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          access_token: session.access_token,
          refresh_token: session.refresh_token,
          next: nextPath,
        }),
      });
      if (!resp.ok) { fail("Could not start your session. Please try again."); return; }

      let dest = nextPath;
      try { const j = await resp.json(); if (j && typeof j.next === "string" && j.next.startsWith("/")) dest = j.next; } catch (e) {}
      window.location.assign(dest);   // relative
    } catch (e) {
      fail("Could not complete sign-in. Please try again.");
    }
  })();
</script>
"""
    body = body.replace("__SUPABASE_JS_CDN__", _SUPABASE_JS_CDN)
    body = _inject(body, supabase_url, anon_key, base)
    return _doc("FB Taverns — Signing in", body, base=base)


# ---------------------------------------------------------------------------
# 403 — authenticated but no drinks role
# ---------------------------------------------------------------------------
def render_no_access_page(email: str, tenancy_admin_url: str, base: str | None = None) -> str:
    """The "signed in but no drinks entitlement" page (403 NO_ACCESS_DETAIL).

    Shows the signed-in email, a clear ask-an-admin message, a link to the
    tenancy admin users page, and a sign-out form so a wrong-account user can
    switch. `base` (external prefix) defaults to EXTERNAL_BASE_PATH; the
    (email, tenancy_admin_url) signature is preserved for existing callers.
    """
    if base is None:
        base = EXTERNAL_BASE_PATH
    safe_email = escape(email or "")
    safe_admin = escape(tenancy_admin_url or "")
    email_line = (
        f'<p class="sub">Signed in as <strong>{safe_email}</strong>.</p>'
        if safe_email else ""
    )
    body = f"""
<div class="wrap">
  <div class="card">
    <h1>No access yet</h1>
    {email_line}
    <div class="msg info" style="display:block">
      Your account doesn't yet have access to drinks reconciliation.
      Ask an administrator to grant you a Drinks role.
    </div>
    <p style="font-size:0.92em;color:#555;">
      An administrator can grant access under
      <a href="{safe_admin}">Admin &rarr; Users &amp; roles</a> on the Team Hub.
    </p>
    <form method="post" action="{base}/auth/signout" style="margin-top:1.4em;">
      <button type="submit">Sign out</button>
    </form>
  </div>
  <div class="foot">FB Taverns &middot; access is managed centrally</div>
</div>
"""
    return _doc("FB Taverns — No access", body, base=base)
