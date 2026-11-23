"""Sign-in for Dieter's connector: OAuth 2.1 with PKCE, Dynamic Client Registration and refresh tokens.

How Claude connects (https://claude.com/docs/connectors/building/authentication):
1. It calls /mcp, gets a 401 pointing at /.well-known/oauth-protected-resource, and from there finds this
   authorization server's metadata (/.well-known/oauth-authorization-server).
2. It registers itself at /oauth/register (only Claude's callback and loopback addresses are accepted).
3. It sends the person to /oauth/authorize. They sign in to Dieter as usual if needed, then see "Allow Claude
   to use your Dieter?". Allow sends a one-time code back to Claude.
4. Claude swaps the code (plus its PKCE verifier) at /oauth/token for an access token (1 hour) and a refresh
   token (rotated on every use; reusing an old one cuts off that whole sign-in).

Only hashes of codes and tokens are stored.
"""

import base64
import hashlib
import json
import re
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core import ui

SCOPE = "dieter"
ACCESS_SECONDS = 60 * 60
REFRESH_SECONDS = 180 * 24 * 60 * 60
CODE_SECONDS = 10 * 60
CLAUDE_CALLBACKS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
LOOPBACK = re.compile(r"http://(localhost|127\.0\.0\.1)(:\d{1,5})?(/[^?#]*)?")

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT DEFAULT '',
    redirect_uris_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    scope TEXT DEFAULT '',
    expires_at REAL NOT NULL,
    used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    family TEXT NOT NULL,
    client_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    scope TEXT DEFAULT '',
    expires_at REAL NOT NULL,
    revoked INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_templates = ui.templates(Path(__file__).parent / "templates")


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pkce_ok(verifier, challenge):
    if not verifier or not re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return secrets.compare_digest(base64.urlsafe_b64encode(digest).decode().rstrip("="), challenge)


def redirect_allowed(uri):
    return uri in CLAUDE_CALLBACKS or bool(LOOPBACK.fullmatch(uri or ""))


def redirect_matches(registered, given):
    """Exact match, except a loopback address matches on any port (Claude Code picks a fresh one each time)."""
    if given in registered:
        return True
    if not LOOPBACK.fullmatch(given or ""):
        return False
    strip = lambda u: re.sub(r":\d+", "", u, count=1)  # noqa: E731
    return strip(given) in {strip(r) for r in registered if LOOPBACK.fullmatch(r)}


def public_base(request):
    """https://askdieter.ai as Claude sees it (Cloud Run hands us plain http behind its proxy)."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    local = host.split(":")[0] in ("localhost", "127.0.0.1", "testserver")
    scheme = request.headers.get("x-forwarded-proto") or ("http" if local else "https")
    return f"{scheme}://{host}"


class OAuth:
    def __init__(self, db, get_user):
        self.db = db
        self.get_user = get_user  # user id -> user dict or None
        self._ready = False

    # ---------------------------------------------------------------- storage

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            conn.executescript(SCHEMA)
            conn.commit()
            self._ready = True
        return conn

    def _commit(self):
        self.db._commit()

    def client(self, client_id):
        row = self._conn().execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id or "",)).fetchone()
        if not row:
            return None
        return {"client_id": row["client_id"], "client_name": row["client_name"],
                "redirect_uris": json.loads(row["redirect_uris_json"])}

    def register_client(self, name, redirect_uris):
        client_id = "dieter-" + uuid.uuid4().hex
        self._conn().execute("INSERT INTO oauth_clients (client_id, client_name, redirect_uris_json) VALUES (?, ?, ?)",
                             (client_id, name[:100], json.dumps(redirect_uris)))
        self._commit()
        return client_id

    def new_code(self, client_id, user_id, redirect_uri, challenge, scope):
        code = secrets.token_urlsafe(32)
        self._conn().execute(
            "INSERT INTO oauth_codes (code_hash, client_id, user_id, redirect_uri, code_challenge, scope, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_hash(code), client_id, user_id, redirect_uri, challenge, scope, time.time() + CODE_SECONDS))
        self._commit()
        return code

    def issue(self, client_id, user_id, scope, family=None):
        family = family or uuid.uuid4().hex
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = time.time()
        conn = self._conn()
        for token, kind, seconds in ((access, "access", ACCESS_SECONDS), (refresh, "refresh", REFRESH_SECONDS)):
            conn.execute("INSERT INTO oauth_tokens (token_hash, kind, family, client_id, user_id, scope, expires_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (_hash(token), kind, family, client_id, user_id, scope, now + seconds))
        conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now - 24 * 60 * 60,))
        conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now - 60 * 60,))
        self._commit()
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_SECONDS,
                "refresh_token": refresh, "scope": scope}

    def user_for_token(self, token):
        """The active, non-guest user an access token belongs to, or None."""
        if not token:
            return None
        row = self._conn().execute(
            "SELECT user_id, expires_at, revoked FROM oauth_tokens WHERE token_hash = ? AND kind = 'access'",
            (_hash(token),)).fetchone()
        if not row or row["revoked"] or row["expires_at"] < time.time():
            return None
        return usable(self.get_user(row["user_id"]))

    def authenticate(self, request):
        header = request.headers.get("authorization") or ""
        scheme, _, token = header.partition(" ")
        return self.user_for_token(token.strip()) if scheme.lower() == "bearer" else None

    # ---------------------------------------------------------------- routes

    def router(self):
        router = APIRouter()

        @router.get("/.well-known/oauth-protected-resource")
        @router.get("/.well-known/oauth-protected-resource/mcp")
        def protected_resource(request: Request):
            base = public_base(request)
            return {"resource": base + "/mcp", "authorization_servers": [base], "scopes_supported": [SCOPE],
                    "bearer_methods_supported": ["header"], "resource_name": "Dieter"}

        @router.get("/.well-known/oauth-authorization-server")
        def authorization_server(request: Request):
            base = public_base(request)
            return {"issuer": base,
                    "authorization_endpoint": base + "/oauth/authorize",
                    "token_endpoint": base + "/oauth/token",
                    "registration_endpoint": base + "/oauth/register",
                    "scopes_supported": [SCOPE],
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "authorization_response_iss_parameter_supported": True}

        @router.post("/oauth/register")
        async def register(request: Request):
            try:
                data = await request.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                return _oauth_error("invalid_client_metadata", "Send the client details as JSON.")
            uris = data.get("redirect_uris")
            if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
                return _oauth_error("invalid_redirect_uri", "redirect_uris is required.")
            if not all(redirect_allowed(u) for u in uris):
                return _oauth_error("invalid_redirect_uri", "Dieter only connects to Claude.")
            name = str(data.get("client_name") or "Claude")
            client_id = self.register_client(name, uris)
            return JSONResponse({"client_id": client_id, "client_id_issued_at": int(time.time()),
                                 "client_name": name[:100], "redirect_uris": uris,
                                 "grant_types": ["authorization_code", "refresh_token"],
                                 "response_types": ["code"], "token_endpoint_auth_method": "none"},
                                status_code=201)

        @router.get("/oauth/authorize")
        def authorize_page(request: Request):
            params = dict(request.query_params)
            problem, client = self._check_authorize(params)
            if problem:
                return _page("oauth_problem.html", problem=problem, status_code=400)
            user = usable(getattr(request.state, "current_user", None))
            if not user:
                here = request.url.path + "?" + request.url.query
                return RedirectResponse(url="/login?next=" + quote(here, safe=""), status_code=303)
            return _page("oauth_allow.html", params=params, user=user, client=client,
                         destination=urlsplit(params["redirect_uri"]).hostname or params["redirect_uri"])

        @router.post("/oauth/authorize")
        async def authorize_decision(request: Request):
            params = {k: str(v) for k, v in (await request.form()).items()}
            problem, _ = self._check_authorize(params)
            if problem:
                return _page("oauth_problem.html", problem=problem, status_code=400)
            user = usable(getattr(request.state, "current_user", None))
            if not user:
                return _page("oauth_problem.html", problem="Sign in to Dieter first, then try connecting again.",
                             status_code=401)
            answer = {"state": params["state"]} if params.get("state") else {}
            answer["iss"] = public_base(request)
            if params.get("decision") != "allow":
                answer["error"] = "access_denied"
            else:
                answer["code"] = self.new_code(params["client_id"], user["id"], params["redirect_uri"],
                                               params["code_challenge"], SCOPE)
            joiner = "&" if "?" in params["redirect_uri"] else "?"
            return RedirectResponse(url=params["redirect_uri"] + joiner + urlencode(answer), status_code=303)

        @router.post("/oauth/token")
        async def token(request: Request):
            form = {k: str(v) for k, v in (await request.form()).items()}
            grant = form.get("grant_type")
            if grant == "authorization_code":
                return self._exchange_code(form)
            if grant == "refresh_token":
                return self._refresh(form)
            return _oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")

        return router

    def _check_authorize(self, params):
        """(problem shown to the person, client). Nothing is sent back to an unknown or mismatched redirect."""
        client = self.client(params.get("client_id"))
        if not client:
            return "Claude's connection request wasn't recognized. Remove the Dieter connector in Claude and add it again.", None
        if not redirect_matches(client["redirect_uris"], params.get("redirect_uri") or ""):
            return "That connection request doesn't match what Claude registered.", None
        if params.get("response_type") != "code" or not params.get("code_challenge") \
                or params.get("code_challenge_method") != "S256":
            return "That connection request is missing its security check (PKCE).", None
        return "", client

    def _exchange_code(self, form):
        code = form.get("code") or ""
        conn = self._conn()
        row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?", (_hash(code),)).fetchone()
        if not row or row["used"] or row["expires_at"] < time.time():
            return _oauth_error("invalid_grant", "That code is expired or already used.")
        if row["client_id"] != form.get("client_id") or row["redirect_uri"] != form.get("redirect_uri"):
            return _oauth_error("invalid_grant", "That code was issued to someone else.")
        if not pkce_ok(form.get("code_verifier") or "", row["code_challenge"]):
            return _oauth_error("invalid_grant", "PKCE check failed.")
        conn.execute("UPDATE oauth_codes SET used = 1 WHERE code_hash = ?", (row["code_hash"],))
        self._commit()
        if not usable(self.get_user(row["user_id"])):
            return _oauth_error("invalid_grant", "This Dieter account isn't active.")
        return _token_response(self.issue(row["client_id"], row["user_id"], row["scope"]))

    def _refresh(self, form):
        conn = self._conn()
        row = conn.execute("SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'",
                           (_hash(form.get("refresh_token") or ""),)).fetchone()
        if not row or row["client_id"] != form.get("client_id"):
            return _oauth_error("invalid_grant", "Unknown refresh token.")
        if row["revoked"]:
            # An old refresh token came back: someone may have copied it. End this whole sign-in.
            conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE family = ?", (row["family"],))
            self._commit()
            return _oauth_error("invalid_grant", "That refresh token was already used.")
        if row["expires_at"] < time.time() or not usable(self.get_user(row["user_id"])):
            return _oauth_error("invalid_grant", "Sign in again.")
        conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE family = ?", (row["family"],))
        self._commit()
        return _token_response(self.issue(row["client_id"], row["user_id"], row["scope"], family=row["family"]))


def usable(user):
    """A signed-in person who may connect Claude: active, and not the shared read-only guest."""
    if not user:
        return None
    user = dict(user)
    if user.get("status", "active") != "active" or user.get("role") == "guest" or not user.get("id"):
        return None
    return user


def _oauth_error(error, description, status_code=400):
    return JSONResponse({"error": error, "error_description": description}, status_code=status_code,
                        headers={"Cache-Control": "no-store"})


def _token_response(body):
    return JSONResponse(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _page(name, status_code=200, **context):
    return HTMLResponse(_templates.get_template(name).render(app=None, **context), status_code=status_code)
