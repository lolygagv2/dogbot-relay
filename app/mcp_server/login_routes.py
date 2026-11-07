"""Login + consent pages for the WIM-Z OAuth flow, and the /register override.

Mounted on the parent Starlette app *before* the SDK's auth routes so they take
precedence. Plain HTML (html.escape everywhere), no cookies: the `sid` created by
`WimzOAuthProvider.authorize` is the capability, with a per-session CSRF nonce.
"""
from __future__ import annotations

import html
import json
import logging
import secrets
import time
import uuid
from collections import defaultdict, deque
from typing import Any

from mcp.server.auth.provider import RegistrationError, construct_redirect_uri
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from app.auth import verify_password
from app.config import get_settings
from app.database import get_user_by_email
from app.mcp_server import oauth_db
from app.mcp_server.authz import ALL_SCOPES, OPT_IN_SCOPES, SCOPE_LABELS, normalize_scopes

logger = logging.getLogger(__name__)

_LOGIN_FAILS: dict[tuple[str, str], deque] = defaultdict(deque)
_LOGIN_WINDOW_S = 900
_LOGIN_MAX_FAILS = 5
_REGISTER_PER_HOUR = 20

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Frame-Options": "DENY",
             "Referrer-Policy": "no-referrer"}

_CSS = """
body{font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e8e8e8;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}
.card{background:#1a1d24;border:1px solid #2a2f3a;border-radius:14px;padding:28px 28px 22px;width:min(420px,92vw)}
h1{font-size:20px;margin:0 0 4px}.sub{color:#9aa3b2;font-size:14px;margin:0 0 18px}
label{display:block;font-size:13px;color:#c9d1dc;margin:12px 0 4px}
input[type=email],input[type=password]{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;
border:1px solid #2f3542;background:#0f1115;color:#fff;font-size:15px}
.row{display:flex;gap:10px;align-items:flex-start;margin:10px 0;padding:10px;border:1px solid #2a2f3a;border-radius:10px}
.row input{margin-top:4px}.row b{display:block;font-size:14px}.row span{font-size:13px;color:#9aa3b2}
.note{font-size:12px;color:#9aa3b2;margin:14px 0 6px;line-height:1.4}
.btns{display:flex;gap:10px;margin-top:16px}button{flex:1;padding:11px;border-radius:9px;border:0;font-size:15px;cursor:pointer}
.allow{background:#4f8cff;color:#fff}.deny{background:#2a2f3a;color:#e8e8e8}.err{color:#ff7b7b;font-size:13px;margin-top:8px}
"""


def _page(title: str, body: str) -> HTMLResponse:
    doc = f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" \
          f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><div class='card'>{body}</div></body></html>"
    return HTMLResponse(doc, headers=_NO_STORE)


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def _session_or_404(request: Request):
    sid = request.query_params.get("sid") or ""
    sess = oauth_db.get_login_session(sid) if sid else None
    return sid, sess


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _login_form(sid: str, csrf: str, client_name: str, error: str = "") -> HTMLResponse:
    err = f"<div class='err'>{html.escape(error)}</div>" if error else ""
    return _page("Sign in to WIM-Z", f"""
      <h1>Sign in to WIM-Z</h1>
      <p class='sub'>Authorizing <b>{html.escape(client_name)}</b></p>
      <form method='post' action='/authorize/login'>
        <input type='hidden' name='sid' value='{html.escape(sid)}'>
        <input type='hidden' name='csrf' value='{html.escape(csrf)}'>
        <label>Email</label><input type='email' name='email' autocomplete='username' required autofocus>
        <label>Password</label><input type='password' name='password' autocomplete='current-password' required>
        {err}
        <div class='btns'><button class='allow' type='submit'>Continue</button></div>
      </form>
      <p class='note'>This signs the assistant in as you. You choose what it may do on the next screen.</p>
    """)


async def login_get(request: Request) -> Response:
    sid, sess = _session_or_404(request)
    if not sess:
        return _page("WIM-Z", "<h1>Link expired</h1><p class='sub'>Start the connection again from your assistant.</p>")
    return _login_form(sid, sess["csrf"], sess["params"].get("client_name") or "an assistant")


async def login_post(request: Request) -> Response:
    form = await request.form()
    sid = str(form.get("sid") or "")
    sess = oauth_db.get_login_session(sid) if sid else None
    if not sess or not secrets.compare_digest(str(form.get("csrf") or ""), sess["csrf"]):
        return _page("WIM-Z", "<h1>Link expired</h1><p class='sub'>Start the connection again from your assistant.</p>")
    email = str(form.get("email") or "").strip().lower()
    password = str(form.get("password") or "")
    client_name = sess["params"].get("client_name") or "an assistant"

    key = (_client_ip(request), email)
    now = time.time()
    fails = _LOGIN_FAILS[key]
    while fails and fails[0] < now - _LOGIN_WINDOW_S:
        fails.popleft()
    if len(fails) >= _LOGIN_MAX_FAILS:
        return _login_form(sid, sess["csrf"], client_name, "Too many attempts. Try again in a few minutes.")

    user = get_user_by_email(email) if email else None
    if not user or not verify_password(password, user["hashed_password"]):
        fails.append(now)
        return _login_form(sid, sess["csrf"], client_name, "Email or password is incorrect.")

    oauth_db.set_login_session_user(sid, user["user_id"])
    return RedirectResponse(url=f"/authorize/consent?sid={sid}", status_code=303, headers=_NO_STORE)


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------

def _consent_form(sid: str, sess: dict) -> HTMLResponse:
    p = sess["params"]
    requested = set(normalize_scopes(p.get("scopes") or []))
    if requested == {"read"}:
        # client asked for nothing specific (most connector hosts): offer the standard set
        requested = set(ALL_SCOPES) - OPT_IN_SCOPES
    rows = []
    for sc in ALL_SCOPES:
        if sc == "read":
            continue
        checked = "checked" if (sc in requested and sc not in OPT_IN_SCOPES) else ""
        rows.append(f"<label class='row'><input type='checkbox' name='scope' value='{sc}' {checked}>"
                    f"<div><b>{html.escape(sc)}</b><span>{html.escape(SCOPE_LABELS[sc])}</span></div></label>")
    return _page("Allow access", f"""
      <h1>Allow <b>{html.escape(p.get('client_name') or 'this assistant')}</b> to use WIM-Z?</h1>
      <p class='sub'>Choose what it may do for your dogs.</p>
      <form method='post' action='/authorize/consent'>
        <input type='hidden' name='sid' value='{html.escape(sid)}'>
        <input type='hidden' name='csrf' value='{html.escape(sess['csrf'])}'>
        {''.join(rows)}
        <p class='note'>Read access to summaries, events and status is always included.
        Photos: see snapshots WIM-Z already took. Camera: ask WIM-Z to take a new snapshot. Never live video.
        Listen: WIM-Z records up to 15 seconds of sound and the assistant hears it; clips are deleted after 1 hour.
        This assistant can only widen quiet hours, never narrow them. Treat requests are limited by your daily cap.</p>
        <div class='btns'>
          <button class='deny' type='submit' name='action' value='deny'>Deny</button>
          <button class='allow' type='submit' name='action' value='allow'>Allow</button>
        </div>
      </form>
    """)


async def consent_get(request: Request) -> Response:
    sid, sess = _session_or_404(request)
    if not sess:
        return _page("WIM-Z", "<h1>Link expired</h1><p class='sub'>Start the connection again from your assistant.</p>")
    if not sess["user_id"]:
        return RedirectResponse(url=f"/authorize/login?sid={sid}", status_code=303, headers=_NO_STORE)
    return _consent_form(sid, sess)


async def consent_post(request: Request) -> Response:
    form = await request.form()
    sid = str(form.get("sid") or "")
    sess = oauth_db.get_login_session(sid) if sid else None
    if not sess or not sess["user_id"] or not secrets.compare_digest(str(form.get("csrf") or ""), sess["csrf"]):
        return _page("WIM-Z", "<h1>Link expired</h1><p class='sub'>Start the connection again from your assistant.</p>")
    p = sess["params"]
    redirect_uri = p["redirect_uri"]
    state = p.get("state")
    oauth_db.delete_login_session(sid)

    if str(form.get("action")) != "allow":
        url = construct_redirect_uri(redirect_uri, error="access_denied",
                                     error_description="The user denied access", state=state)
        return RedirectResponse(url=url, status_code=302, headers=_NO_STORE)

    ticked = [str(v) for v in form.getlist("scope")]
    granted = normalize_scopes([sc for sc in ticked if sc in ALL_SCOPES])
    code = secrets.token_urlsafe(32)
    oauth_db.save_code(code, sess["client_id"], sess["user_id"], granted, p["code_challenge"], redirect_uri,
                       bool(p.get("redirect_uri_provided_explicitly")), p.get("resource"))
    logger.info(f"[OAUTH] consent granted user={sess['user_id']} client={sess['client_id']} scopes={granted}")
    return RedirectResponse(url=construct_redirect_uri(redirect_uri, code=code, state=state), status_code=302,
                            headers=_NO_STORE)


# ---------------------------------------------------------------------------
# /register override (RFC 7591) — always returns a client_secret
# ---------------------------------------------------------------------------

_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
         "Access-Control-Allow-Headers": "Content-Type, mcp-protocol-version"}


def _reg_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status, headers=_CORS)


async def register(request: Request) -> Response:
    """Mirror of the SDK's RegistrationHandler with one deliberate difference: public clients
    (`token_endpoint_auth_method: none`) also receive a `client_secret` in the response, because
    at least one connector host rejects secretless registrations. The stored auth method stays
    `none`, so the token endpoint never demands that secret (PKCE protects the exchange)."""
    from app.mcp_server.server import provider  # late import: avoids a cycle at module load

    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS)
    ip = _client_ip(request)
    if oauth_db.registrations_since(ip, 3600) >= _REGISTER_PER_HOUR:
        return _reg_error("invalid_client_metadata", "too many registrations from this address", 429)
    try:
        meta = OAuthClientMetadata.model_validate_json(await request.body())
    except ValidationError as e:
        return _reg_error("invalid_client_metadata", str(e)[:300])

    method = meta.token_endpoint_auth_method or "client_secret_post"
    if method == "private_key_jwt":
        return _reg_error("invalid_client_metadata", "token_endpoint_auth_method 'private_key_jwt' is not supported")
    if "authorization_code" not in meta.grant_types:
        return _reg_error("invalid_client_metadata", "grant_types must include 'authorization_code'")
    if "code" not in meta.response_types:
        return _reg_error("invalid_client_metadata", "response_types must include 'code'")
    if meta.scope is None:
        meta.scope = " ".join(ALL_SCOPES)
    else:
        bad = set(meta.scope.split()) - set(ALL_SCOPES)
        if bad:
            return _reg_error("invalid_client_metadata", f"Requested scopes are not valid: {', '.join(sorted(bad))}")

    issued_at = int(time.time())
    info = OAuthClientInformationFull.model_validate({
        **meta.model_dump(),
        "token_endpoint_auth_method": method,
        "client_id": str(uuid.uuid4()),
        "client_id_issued_at": issued_at,
        "client_secret": secrets.token_hex(32),
        "client_secret_expires_at": 0,
    })
    try:
        await provider.register_client(info)
    except RegistrationError as e:
        return _reg_error(e.error, e.error_description or "")
    # remember who registered (rate limiting)
    try:
        with oauth_db.db_connection() as conn:
            conn.execute("UPDATE oauth_clients SET created_ip = ? WHERE client_id = ?", (ip, info.client_id))
            conn.commit()
    except Exception:  # pragma: no cover
        pass
    body = info.model_dump(mode="json", exclude_none=True)
    return JSONResponse(body, status_code=201, headers={**_CORS, **_NO_STORE})


routes = [
    Route("/authorize/login", login_get, methods=["GET"]),
    Route("/authorize/login", login_post, methods=["POST"]),
    Route("/authorize/consent", consent_get, methods=["GET"]),
    Route("/authorize/consent", consent_post, methods=["POST"]),
    Route("/register", register, methods=["POST", "OPTIONS"]),
]
