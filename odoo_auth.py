"""
Multi-tenant "log in with your Odoo" OAuth provider for Odoo MCP Multi.

This turns the server into a PUBLIC connector: any claude.ai user can add the
URL, and the OAuth login page asks for *their* Odoo URL / database / username /
API key instead of a GitHub or Google account. Credentials are validated
against Odoo, encrypted, stored under a per-user subject, and attached to each
request's access token so the tools can call that user's Odoo.

Flow (standard OAuth 2.1 + PKCE, as claude.ai expects):

    claude.ai ─GET /.well-known/…──► discovers this authorization server
    claude.ai ─POST /register─────► dynamic client registration
    claude.ai ─GET /authorize─────► we park the request as a "transaction"
                                     and redirect the browser to /login?txn=…
    user      ─POST /login────────► Odoo creds validated via XML-RPC,
                                     encrypted + stored, auth code issued,
                                     redirect back to claude.ai with ?code=
    claude.ai ─POST /token────────► code → access token (+ refresh token)
    claude.ai ─POST /mcp──────────► bearer token → user's Odoo creds → tools

Storage is any `key-value` AsyncKeyValue (Redis in production, memory for local
tests). Everything written is Fernet-encrypted with MCP_SECRET_KEY; tokens are
stored by SHA-256 hash so the store never holds a usable token.
"""

from __future__ import annotations

import hashlib
import html
import secrets
import time
import xmlrpc.client
from typing import Any
from urllib.parse import urlparse

import anyio
from key_value.aio.adapters.pydantic import PydanticAdapter
from key_value.aio.protocols.key_value import AsyncKeyValue
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, BaseModel
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from fastmcp.server.auth.auth import AccessToken, OAuthProvider

SCOPE = "odoo"
TXN_TTL = 15 * 60          # how long the login page stays valid
CODE_TTL = 5 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 3600
LOGIN_ATTEMPTS = 10        # failed logins per IP per LOGIN_WINDOW
LOGIN_WINDOW = 15 * 60


# --------------------------------------------------------------------------- #
# Stored models
# --------------------------------------------------------------------------- #

class OdooCredentials(BaseModel):
    url: str
    database: str
    username: str
    api_key: str
    uid: int
    readonly: bool = False
    display_name: str = ""
    created_at: float = 0.0


class Transaction(BaseModel):
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    state: str | None = None
    code_challenge: str
    scopes: list[str] = []
    resource: str | None = None
    created_at: float


class StoredCode(BaseModel):
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    expires_at: float
    subject: str
    resource: str | None = None


class StoredAccess(BaseModel):
    client_id: str
    scopes: list[str]
    expires_at: int
    subject: str
    refresh_hash: str | None = None


class StoredRefresh(BaseModel):
    client_id: str
    scopes: list[str]
    expires_at: int
    subject: str
    access_hash: str | None = None


class Counter(BaseModel):
    n: int = 0


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def build_storage(url: str | None, secret: str) -> AsyncKeyValue:
    """Redis when a URL is given, in-memory otherwise. Always encrypted."""
    if url:
        from key_value.aio.stores.redis import RedisStore
        base: AsyncKeyValue = RedisStore(url=url)
    else:
        from key_value.aio.stores.memory import MemoryStore
        base = MemoryStore()
    return FernetEncryptionWrapper(
        key_value=base,
        source_material=secret,
        salt="odoo-mcp-storage",
        raise_on_decryption_error=False,
    )


# --------------------------------------------------------------------------- #
# Odoo credential check (runs in a worker thread; xmlrpc is blocking)
# --------------------------------------------------------------------------- #

def _check_odoo(url: str, db: str, user: str, key: str) -> tuple[int, str]:
    """Return (uid, display_name) or raise ValueError with a user-facing message."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Odoo URL must start with http:// or https://")
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    try:
        common = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(db, user, key, {})
    except Exception as exc:  # DNS, TLS, non-Odoo host, wrong db name…
        raise ValueError(f"Could not reach Odoo at {base}: {exc}") from exc
    if not uid:
        raise ValueError("Odoo rejected the username / API key for that database.")
    name = ""
    try:
        models = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/object", allow_none=True)
        rows = models.execute_kw(db, uid, key, "res.users", "read", [[uid]], {"fields": ["name"]})
        name = (rows or [{}])[0].get("name") or ""
    except Exception:
        pass
    return int(uid), name


# --------------------------------------------------------------------------- #
# The provider
# --------------------------------------------------------------------------- #

class OdooLoginProvider(OAuthProvider):
    def __init__(self, *, base_url: str, storage: AsyncKeyValue,
                 server_name: str = "Odoo MCP"):
        super().__init__(
            base_url=base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
        )
        self.server_name = server_name
        self._clients = PydanticAdapter[OAuthClientInformationFull](
            storage, OAuthClientInformationFull, default_collection="oauth-clients")
        self._txns = PydanticAdapter[Transaction](storage, Transaction, default_collection="oauth-txns")
        self._codes = PydanticAdapter[StoredCode](storage, StoredCode, default_collection="oauth-codes")
        self._access = PydanticAdapter[StoredAccess](storage, StoredAccess, default_collection="oauth-access")
        self._refresh = PydanticAdapter[StoredRefresh](storage, StoredRefresh, default_collection="oauth-refresh")
        self._users = PydanticAdapter[OdooCredentials](storage, OdooCredentials, default_collection="odoo-users")
        self._ratelimit = PydanticAdapter[Counter](storage, Counter, default_collection="login-ratelimit")

    # ---- client registration (RFC 7591) ---------------------------------- #

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self._clients.get(key=client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("client_id is required")
        await self._clients.put(key=client_info.client_id, value=client_info)

    # ---- /authorize → park the request and send the browser to /login ---- #

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        if not client.client_id:
            raise AuthorizeError(error="invalid_client", error_description="Missing client_id")
        txn_id = secrets.token_urlsafe(32)
        await self._txns.put(key=txn_id, ttl=TXN_TTL, value=Transaction(
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            state=params.state,
            code_challenge=params.code_challenge,
            scopes=params.scopes or [SCOPE],
            resource=params.resource,
            created_at=time.time(),
        ))
        return f"{str(self.base_url).rstrip('/')}/login?txn={txn_id}"

    # ---- routes ------------------------------------------------------------ #

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        routes.append(Route("/login", self._login, methods=["GET", "POST"]))
        return routes

    async def _login(self, request: Request) -> Response:
        if request.method == "GET":
            txn_id = request.query_params.get("txn", "")
            txn = await self._txns.get(key=txn_id) if txn_id else None
            if txn is None:
                return self._page_error("This login link has expired. Go back to Claude and connect again.")
            client = await self._clients.get(key=txn.client_id)
            return HTMLResponse(self._form_html(txn_id, client.client_name if client else None))

        form = await request.form()
        txn_id = str(form.get("txn", ""))
        txn = await self._txns.get(key=txn_id) if txn_id else None
        if txn is None:
            return self._page_error("This login link has expired. Go back to Claude and connect again.")
        client = await self._clients.get(key=txn.client_id)
        client_name = client.client_name if client else None

        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else "unknown"))
        hits = await self._ratelimit.get(key=ip)
        if hits and hits.n >= LOGIN_ATTEMPTS:
            return self._page_error("Too many failed attempts. Try again in 15 minutes.", status=429)

        url = str(form.get("url", "")).strip()
        db = str(form.get("database", "")).strip()
        user = str(form.get("username", "")).strip()
        key = str(form.get("api_key", "")).strip()
        readonly = str(form.get("readonly", "")) in ("on", "1", "true")
        values = {"url": url, "database": db, "username": user, "readonly": readonly}

        if not (url and db and user and key):
            return HTMLResponse(self._form_html(txn_id, client_name, values, "All fields are required."), status_code=400)

        try:
            uid, name = await anyio.to_thread.run_sync(_check_odoo, url, db, user, key)
        except ValueError as exc:
            await self._ratelimit.put(key=ip, ttl=LOGIN_WINDOW, value=Counter(n=(hits.n if hits else 0) + 1))
            return HTMLResponse(self._form_html(txn_id, client_name, values, str(exc)), status_code=400)

        parsed = urlparse(url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        subject = hashlib.sha256(f"{clean_url}|{db}|{uid}".encode()).hexdigest()[:32]
        await self._users.put(key=subject, value=OdooCredentials(
            url=clean_url, database=db, username=user, api_key=key, uid=uid,
            readonly=readonly, display_name=name, created_at=time.time()))

        code = secrets.token_urlsafe(32)
        await self._codes.put(key=_h(code), ttl=CODE_TTL, value=StoredCode(
            client_id=txn.client_id, redirect_uri=txn.redirect_uri,
            redirect_uri_provided_explicitly=txn.redirect_uri_provided_explicitly,
            code_challenge=txn.code_challenge, scopes=txn.scopes,
            expires_at=time.time() + CODE_TTL, subject=subject, resource=txn.resource))
        await self._txns.delete(key=txn_id)
        return RedirectResponse(
            construct_redirect_uri(txn.redirect_uri, code=code, state=txn.state), status_code=302)

    # ---- authorization code → tokens --------------------------------------- #

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        stored = await self._codes.get(key=_h(authorization_code))
        if stored is None or stored.client_id != client.client_id or stored.expires_at < time.time():
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=stored.scopes, expires_at=stored.expires_at,
            client_id=stored.client_id, code_challenge=stored.code_challenge,
            redirect_uri=AnyHttpUrl(stored.redirect_uri),
            redirect_uri_provided_explicitly=stored.redirect_uri_provided_explicitly,
            resource=stored.resource, subject=stored.subject)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        stored = await self._codes.get(key=_h(authorization_code.code))
        if stored is None:
            raise TokenError("invalid_grant", "Authorization code not found or already used.")
        await self._codes.delete(key=_h(authorization_code.code))
        return await self._issue(stored.client_id, stored.scopes, stored.subject)

    async def _issue(self, client_id: str, scopes: list[str], subject: str) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        await self._access.put(key=_h(access), ttl=ACCESS_TTL, value=StoredAccess(
            client_id=client_id, scopes=scopes, expires_at=now + ACCESS_TTL,
            subject=subject, refresh_hash=_h(refresh)))
        await self._refresh.put(key=_h(refresh), ttl=REFRESH_TTL, value=StoredRefresh(
            client_id=client_id, scopes=scopes, expires_at=now + REFRESH_TTL,
            subject=subject, access_hash=_h(access)))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes))

    # ---- refresh tokens (rotated on every use) ----------------------------- #

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        stored = await self._refresh.get(key=_h(refresh_token))
        if stored is None or stored.client_id != client.client_id or stored.expires_at < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=stored.client_id,
                            scopes=stored.scopes, expires_at=stored.expires_at, subject=stored.subject)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        stored = await self._refresh.get(key=_h(refresh_token.token))
        if stored is None:
            raise TokenError("invalid_grant", "Refresh token not found or already used.")
        if not set(scopes).issubset(stored.scopes):
            raise TokenError("invalid_scope", "Requested scopes exceed those granted.")
        await self._refresh.delete(key=_h(refresh_token.token))
        if stored.access_hash:
            await self._access.delete(key=stored.access_hash)
        return await self._issue(stored.client_id, scopes or stored.scopes, stored.subject)

    # ---- bearer verification: attach the user's Odoo creds to the request -- #

    async def load_access_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        stored = await self._access.get(key=_h(token))
        if stored is None or stored.expires_at < time.time():
            return None
        creds = await self._users.get(key=stored.subject)
        if creds is None:
            return None  # user record gone → force re-login
        return AccessToken(
            token=token, client_id=stored.client_id, scopes=stored.scopes,
            expires_at=stored.expires_at, subject=stored.subject,
            claims={"sub": stored.subject, "name": creds.display_name,
                    "odoo": creds.model_dump()})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            stored = await self._refresh.get(key=_h(token.token))
            await self._refresh.delete(key=_h(token.token))
            if stored and stored.access_hash:
                await self._access.delete(key=stored.access_hash)
        else:
            stored = await self._access.get(key=_h(token.token))
            await self._access.delete(key=_h(token.token))
            if stored and stored.refresh_hash:
                await self._refresh.delete(key=stored.refresh_hash)

    # ---- HTML ---------------------------------------------------------------- #

    def _page_error(self, message: str, status: int = 400) -> HTMLResponse:
        body = f'<p class="err">{html.escape(message)}</p>'
        return HTMLResponse(_PAGE.format(title="Login problem", server=html.escape(self.server_name), body=body),
                            status_code=status)

    def _form_html(self, txn_id: str, client_name: str | None,
                   values: dict[str, Any] | None = None, error: str | None = None) -> str:
        v = values or {}
        e = html.escape
        who = e(client_name or "An application")
        err = f'<p class="err">{e(error)}</p>' if error else ""
        body = f"""
<p><strong>{who}</strong> wants to work with your Odoo through <strong>{e(self.server_name)}</strong>.
Sign in with the Odoo account it should use.</p>
{err}
<form method="post" action="/login" autocomplete="off">
  <input type="hidden" name="txn" value="{e(txn_id)}">
  <label>Odoo URL <input name="url" type="url" required placeholder="https://mycompany.odoo.com" value="{e(str(v.get('url','')))}"></label>
  <label>Database <input name="database" required placeholder="mycompany" value="{e(str(v.get('database','')))}"></label>
  <label>Username / email <input name="username" required value="{e(str(v.get('username','')))}"></label>
  <label>API key <input name="api_key" type="password" required></label>
  <label class="chk"><input name="readonly" type="checkbox" {'checked' if v.get('readonly') else ''}> Read-only (block create, write, delete, import)</label>
  <button type="submit">Connect</button>
</form>
<p class="note">Create an API key in Odoo under <em>Preferences → Account Security → New API Key</em>.
Your key is encrypted and used only to run the requests you make through Claude.
Delete the key in Odoo at any time to revoke access.</p>
"""
        return _PAGE.format(title="Connect your Odoo", server=e(self.server_name), body=body)


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {server}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:#f4f4f5; color:#18181b; display:flex; justify-content:center; padding:48px 16px; }}
  main {{ background:#fff; max-width:440px; width:100%; padding:32px; border-radius:12px;
         box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  h1 {{ font-size:20px; margin:0 0 16px; }}
  label {{ display:block; margin:14px 0 0; font-weight:600; font-size:13px; }}
  label input:not([type=checkbox]) {{ display:block; width:100%; box-sizing:border-box; margin-top:4px;
         padding:10px; font-size:15px; border:1px solid #d4d4d8; border-radius:8px; }}
  .chk {{ font-weight:400; }} .chk input {{ margin-right:6px; }}
  button {{ margin-top:22px; width:100%; padding:12px; font-size:15px; font-weight:600;
         background:#714b67; color:#fff; border:0; border-radius:8px; cursor:pointer; }}
  .err {{ background:#fef2f2; color:#991b1b; padding:10px 12px; border-radius:8px; }}
  .note {{ font-size:12.5px; color:#52525b; margin-top:20px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background:#18181b; color:#fafafa; }} main {{ background:#27272a; }}
    label input:not([type=checkbox]) {{ background:#18181b; color:#fafafa; border-color:#3f3f46; }}
    .note {{ color:#a1a1aa; }} .err {{ background:#450a0a; color:#fecaca; }}
  }}
</style></head><body><main><h1>{title}</h1>{body}</main></body></html>"""
