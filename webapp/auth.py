"""
Optional Microsoft Entra ID (Azure AD) sign-in for the LeadRadar web app.

Implements the MSAL Authorization Code flow against an Entra ID app
registration (``AAD_TENANT_ID`` / ``AAD_CLIENT_ID`` / ``AAD_CLIENT_SECRET``),
so the app can be gated by a real Microsoft sign-in when run locally via
``uvicorn --reload`` — Easy Auth (used once the app is deployed as an
Azure Container App) only runs as a platform sidecar in Azure, it doesn't
exist under local uvicorn, so this fills that gap for local dev rather
than replacing Easy Auth in production.

When none of the ``AAD_*`` variables are set, sign-in is disabled and the
app is open — convenient for a local demo.

``current_user()`` checks the local session first (set by ``/auth/callback``
below), then falls back to Easy Auth's ``X-MS-CLIENT-PRINCIPAL`` header once
deployed (a base64 JSON claims blob — richer than the plain
``X-MS-CLIENT-PRINCIPAL-NAME`` header, since it also carries the display
name) — so the same call site works, with the same fields, in both contexts.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets

import msal
from dotenv import load_dotenv
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)

load_dotenv(".env")

AAD_TENANT_ID     = os.getenv("AAD_TENANT_ID", "")
AAD_CLIENT_ID     = os.getenv("AAD_CLIENT_ID", "")
AAD_CLIENT_SECRET = os.getenv("AAD_CLIENT_SECRET", "")
AAD_REDIRECT_URI  = os.getenv("AAD_REDIRECT_URI", "http://localhost:8000/auth/callback")

_AUTHORITY = f"https://login.microsoftonline.com/{AAD_TENANT_ID}"
# Deliberately empty: this only needs the ID token (name/preferred_username
# claims) to identify who's signed in, not a Graph access token. Requesting
# "User.Read" would ask for a delegated Graph permission, which corporate
# tenants commonly block from user self-consent — surfacing as a "needs
# admin approval" error despite never actually being used for anything here.
_SCOPES: list[str] = []

# Paths reachable without being signed in — the auth flow itself, plus static
# assets. "/favicon.ico" matters here even though the page declares its own
# icon under /static: browsers request this root-relative path speculatively
# regardless, and if it weren't public it would race the real sign-in's own
# /login redirect — each generating its own "state" and overwriting the
# other's in the session, so whichever response's redirect the browser is
# mid-flight on by the time it returns no longer matches. That race was the
# cause of "Invalid or expired sign-in attempt" succeeding only on retry.
PUBLIC_PATHS = ("/login", "/auth/callback", "/login-failed", "/static", "/favicon.ico", "/healthz")

# Cap on concurrently-pending sign-in attempts tracked per session — keeps the
# fix below (allowing more than one valid state) from growing unbounded if a
# browser somehow fires many parallel /login requests.
_MAX_PENDING_STATES = 5

router = APIRouter()


def auth_configured() -> bool:
    """False until AAD_TENANT_ID/CLIENT_ID/CLIENT_SECRET are all set in .env."""
    return bool(AAD_TENANT_ID and AAD_CLIENT_ID and AAD_CLIENT_SECRET)


def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        AAD_CLIENT_ID, authority=_AUTHORITY, client_credential=AAD_CLIENT_SECRET,
    )


# Claim types Easy Auth's X-MS-CLIENT-PRINCIPAL may carry, in priority order.
# Entra emits either the short OIDC name or the long WS-Fed URI depending on
# token version/tenant config, so both are checked.
_NAME_CLAIMS = (
    "name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
)
_EMAIL_CLAIMS = (
    "preferred_username",
    "email",
    "emails",
    "upn",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
)


def _first(claims: dict[str, str], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = claims.get(k)
        if v:
            return v
    return ""


def _initials(name: str | None, email: str | None) -> str:
    """'Jane Cooper' -> 'JC'. Falls back to the email's local part, then '?'."""
    source = (name or "").strip()
    if source:
        parts = source.split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[-1][0]).upper()
        return parts[0][:2].upper()
    if email:
        return email.split("@")[0][:2].upper()
    return "?"


def _decode_principal(b64: str) -> dict[str, str]:
    """Decode X-MS-CLIENT-PRINCIPAL into a {claim_type: value} dict (first value wins)."""
    try:
        claims_list = json.loads(base64.b64decode(b64)).get("claims", [])
    except Exception:
        logger.warning("Could not decode X-MS-CLIENT-PRINCIPAL header")
        return {}
    by_type: dict[str, str] = {}
    for c in claims_list:
        typ, val = c.get("typ"), c.get("val")
        if typ and val and typ not in by_type:
            by_type[typ] = val
    return by_type


def _raw_user_claims(request: Request) -> dict | None:
    """{'name': ..., 'email': ...} from the local session, or the Easy Auth headers."""
    session_user = request.session.get("user") if auth_configured() else None
    if session_user:
        return {"name": session_user.get("name"), "email": session_user.get("preferred_username")}

    principal = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    by_type   = _decode_principal(principal) if principal else {}
    name      = _first(by_type, _NAME_CLAIMS)
    email     = _first(by_type, _EMAIL_CLAIMS) or request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME") or ""
    if name or email:
        return {"name": name or None, "email": email or None}
    return None


def current_user(request: Request) -> dict | None:
    """
    Structured info about the signed-in caller for display purposes:
    ``{"name", "email", "display", "initials"}``, or ``None`` if not signed
    in. ``display`` is the full name when known, falling back to the email —
    use it anywhere a person's name should appear (e.g. comment authorship),
    so it reads like "Jane Cooper" rather than an email address.
    """
    claims = _raw_user_claims(request)
    if not claims:
        return None
    name, email = claims.get("name"), claims.get("email")
    if not (name or email):
        return None
    return {
        "name":     name,
        "email":    email,
        "display":  name or email,
        "initials": _initials(name, email),
    }


def signed_in_user(request: Request) -> str | None:
    """Plain string identity, for call sites that only need a truthy check or a fallback name."""
    user = current_user(request)
    return user["display"] if user else None


@router.get("/login")
async def login(request: Request):
    if not auth_configured():
        return RedirectResponse("/login-failed?error=AAD_TENANT_ID%2FCLIENT_ID%2FCLIENT_SECRET+not+set+in+.env")
    state = secrets.token_urlsafe(16)
    # A list, not a single overwritable value: concurrent /login hits (e.g. a
    # browser opening two tabs, or retrying) must each keep their own state
    # valid rather than the latest one clobbering the others' chance to complete.
    pending = request.session.get("auth_states", [])
    pending.append(state)
    request.session["auth_states"] = pending[-_MAX_PENDING_STATES:]
    auth_url = _msal_app().get_authorization_request_url(
        _SCOPES, state=state, redirect_uri=AAD_REDIRECT_URI,
    )
    return RedirectResponse(auth_url)


@router.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    pending = request.session.get("auth_states", [])
    if error or not state or state not in pending:
        detail = error_description or error or "Invalid or expired sign-in attempt"
        return RedirectResponse(f"/login-failed?error={detail}")
    request.session["auth_states"] = [s for s in pending if s != state]

    result = _msal_app().acquire_token_by_authorization_code(
        code, scopes=_SCOPES, redirect_uri=AAD_REDIRECT_URI,
    )
    if "error" in result:
        return RedirectResponse(f"/login-failed?error={result.get('error_description', result['error'])}")

    claims = result.get("id_token_claims", {})
    request.session["user"] = {
        "name":               claims.get("name"),
        "preferred_username": claims.get("preferred_username"),
    }
    return RedirectResponse("/")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    # X-MS-CLIENT-PRINCIPAL is injected by Azure Container Apps Easy Auth on
    # every authenticated request — its presence is the reliable signal that
    # we are running behind the platform auth layer, regardless of whether the
    # local MSAL vars (AAD_*) happen to be set.
    easy_auth = (
        request.headers.get("X-MS-CLIENT-PRINCIPAL")
        or request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
    )
    if easy_auth:
        return RedirectResponse("/.auth/logout?post_logout_redirect_uri=/")
    # Local dev: MSAL session — send through Microsoft's logout so the AAD
    # token is also revoked, not just the local cookie.
    tenant = AAD_TENANT_ID.strip()
    if tenant:
        base = AAD_REDIRECT_URI.rsplit("/auth/callback", 1)[0]
        return RedirectResponse(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/logout"
            f"?post_logout_redirect_uri={base}/"
        )
    # No auth configured at all — drop back to the login page.
    return RedirectResponse("/login")


@router.get("/login-failed", response_class=HTMLResponse)
async def login_failed(error: str = "Sign-in failed"):
    return HTMLResponse(f"""
    <html><body style="font-family:-apple-system,sans-serif;background:#F3F6FB;display:flex;
                        align-items:center;justify-content:center;height:100vh;margin:0;">
      <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:32px;
                  max-width:420px;text-align:center;">
        <div style="font-size:13px;font-weight:800;color:#0d1f45;text-transform:uppercase;
                    letter-spacing:1px;margin-bottom:8px;">Sign-in failed</div>
        <div style="font-size:12px;color:#64748b;margin-bottom:20px;">{error}</div>
        <a href="/login" style="display:inline-block;padding:8px 20px;background:#0d1f45;
                  color:#fff;border-radius:8px;font-size:12px;font-weight:700;text-decoration:none;">
          Try again
        </a>
      </div>
    </body></html>
    """)
