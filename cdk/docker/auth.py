"""
App-level Cognito authentication for the chatbot.

The chatbot is private. This module gates every page/API request behind a
Cognito-authenticated session using the OAuth authorization-code flow:

  1. An unauthenticated request is redirected to /auth/login, which redirects
     to the Cognito Hosted UI.
  2. After the user signs in, Cognito redirects back to /auth/callback with a
     short-lived authorization `code`.
  3. The app exchanges that code (server-side, using the confidential client
     secret) for Cognito tokens, verifies the ID token against the pool's
     JWKS, then issues the app's OWN signed, HTTP-only session cookie. Cognito
     tokens never reach the browser.
  4. Subsequent requests carry that cookie; a FastAPI dependency validates it.

Design notes:
  * The client SECRET is fetched at startup via DescribeUserPoolClient (task
    role grant), so it is never baked into the task definition / image.
  * Redirect URLs are derived from the FORWARDED host of the incoming request
    (CloudFront -> ALB), so the container env needs no CloudFront domain and
    there is no CDK resource cycle.
"""
import os
import time
from typing import Optional

import boto3
import requests
from fastapi import Request
from fastapi.responses import RedirectResponse, JSONResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from jose import jwt
from jose.utils import base64url_decode  # noqa: F401  (ensures crypto backend)

# --- Configuration (from container env) ----------------------------------
USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
COGNITO_DOMAIN = os.environ.get("COGNITO_DOMAIN", "")  # Hosted UI domain host
REGION = os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION", "us-east-1")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-secret")

# Auth is only ENABLED when the pool is configured. This lets the container
# run locally (no Cognito) without an auth wall, and keeps /healthz honest.
AUTH_ENABLED = bool(USER_POOL_ID and CLIENT_ID and COGNITO_DOMAIN)

SESSION_COOKIE = "chatbot_session"
SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours

_ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
_TOKEN_ENDPOINT = f"https://{COGNITO_DOMAIN}/oauth2/token"
_AUTHORIZE_ENDPOINT = f"https://{COGNITO_DOMAIN}/oauth2/authorize"
_LOGOUT_ENDPOINT = f"https://{COGNITO_DOMAIN}/logout"

_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="chatbot-session")

# Lazily-populated runtime state.
_client_secret: Optional[str] = None
_jwks: Optional[dict] = None


def _fetch_client_secret() -> str:
    """Read the confidential client's secret via the task role (not in env)."""
    global _client_secret
    if _client_secret is None:
        idp = boto3.client("cognito-idp", region_name=REGION)
        desc = idp.describe_user_pool_client(
            UserPoolId=USER_POOL_ID, ClientId=CLIENT_ID
        )
        _client_secret = desc["UserPoolClient"]["ClientSecret"]
    return _client_secret


def _get_jwks() -> dict:
    """Fetch (and cache) the pool's JSON Web Key Set for token verification."""
    global _jwks
    if _jwks is None:
        url = f"{_ISSUER}/.well-known/jwks.json"
        _jwks = requests.get(url, timeout=10).json()
    return _jwks


def init() -> None:
    """Warm the client secret + JWKS at startup so first login is fast."""
    if not AUTH_ENABLED:
        print("auth: Cognito not configured; running WITHOUT an auth wall")
        return
    _fetch_client_secret()
    _get_jwks()
    print(f"auth: Cognito auth ENABLED (pool {USER_POOL_ID})")


# --- Redirect-URL helpers -------------------------------------------------
def _base_url(request: Request) -> str:
    """
    Reconstruct the PUBLIC origin from forwarded headers.

    The public entry point is CloudFront over HTTPS, but the CloudFront->ALB
    origin leg is plain HTTP, so X-Forwarded-Proto arrives as "http". The
    OAuth redirect_uri must match the HTTPS callback registered on the Cognito
    client, so force https for any non-local host. (A localhost dev server
    stays http.)
    """
    host = request.headers.get("x-forwarded-host") or request.headers.get(
        "host", request.url.netloc
    )
    is_local = host.split(":")[0] in ("localhost", "127.0.0.1")
    proto = "http" if is_local else "https"
    return f"{proto}://{host}"


def _redirect_uri(request: Request) -> str:
    return f"{_base_url(request)}/auth/callback"


# --- Session cookie -------------------------------------------------------
def _issue_session(claims: dict) -> str:
    payload = {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "iat": int(time.time()),
    }
    return _serializer.dumps(payload)


def _read_session(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def current_user(request: Request) -> Optional[dict]:
    """The verified session user, or None. (None means unauthenticated.)"""
    if not AUTH_ENABLED:
        return {"sub": "local", "email": "local@dev"}
    return _read_session(request)


# --- OAuth flow -----------------------------------------------------------
def login(request: Request) -> RedirectResponse:
    """Redirect to the Cognito Hosted UI authorization endpoint."""
    if not AUTH_ENABLED:
        return RedirectResponse("/")
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": _redirect_uri(request),
    }
    qs = "&".join(f"{k}={requests.utils.quote(v, safe='')}" for k, v in params.items())
    return RedirectResponse(f"{_AUTHORIZE_ENDPOINT}?{qs}")


def callback(request: Request):
    """Exchange the authorization code for tokens, verify, set the cookie."""
    if not AUTH_ENABLED:
        return RedirectResponse("/")
    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"detail": "missing authorization code"}, status_code=400)

    secret = _fetch_client_secret()
    resp = requests.post(
        _TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": _redirect_uri(request),
        },
        auth=(CLIENT_ID, secret),  # confidential client -> HTTP Basic
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    if resp.status_code != 200:
        return JSONResponse(
            {"detail": f"token exchange failed: {resp.text}"}, status_code=401
        )
    tokens = resp.json()
    id_token = tokens["id_token"]
    access_token = tokens.get("access_token")

    # Verify the ID token signature + claims against the pool's JWKS.
    # Cognito ID tokens carry an `at_hash` claim bound to the access token, so
    # the access token must be supplied for jose to validate it.
    headers = jwt.get_unverified_header(id_token)
    key = next((k for k in _get_jwks()["keys"] if k["kid"] == headers["kid"]), None)
    if key is None:
        return JSONResponse({"detail": "unknown signing key"}, status_code=401)
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer=_ISSUER,
            access_token=access_token,
        )
    except Exception as ex:  # pylint: disable=broad-except
        return JSONResponse({"detail": f"invalid id token: {ex}"}, status_code=401)

    response = RedirectResponse("/")
    response.set_cookie(
        SESSION_COOKIE,
        _issue_session(claims),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


def logout(request: Request) -> RedirectResponse:
    """Clear the session cookie and sign out of the Hosted UI."""
    base = _base_url(request)
    if not AUTH_ENABLED:
        return RedirectResponse("/")
    params = {"client_id": CLIENT_ID, "logout_uri": f"{base}/"}
    qs = "&".join(f"{k}={requests.utils.quote(v, safe='')}" for k, v in params.items())
    response = RedirectResponse(f"{_LOGOUT_ENDPOINT}?{qs}")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
