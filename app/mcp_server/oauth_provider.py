"""WIM-Z as OAuth 2.1 identity provider for MCP clients (Dec 9 demo build, 2026-11-06).

Implements the MCP SDK's `OAuthAuthorizationServerProvider` protocol on top of
`oauth_db`. PKCE (S256) verification and client authentication are done by the SDK's
handlers; this class owns registration storage, the login/consent hand-off, single-use
codes, opaque token minting with refresh rotation and family revocation.

Per-assistant isolation: every connector registers its own client (DCR), gets its own
consent, its own token family and its own audit rows.
"""
from __future__ import annotations

import logging
import secrets
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from app.config import get_settings
from app.mcp_server import oauth_db
from app.mcp_server.authz import ALL_SCOPES, normalize_scopes

logger = logging.getLogger(__name__)

ACCESS_PREFIX = "wmz_at_"
REFRESH_PREFIX = "wmz_rt_"


def issuer_url() -> str:
    s = get_settings()
    return (s.mcp_public_base_url or s.public_base_url).rstrip("/")


def resource_url() -> str:
    return issuer_url() + "/mcp"


def _canon(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return url.rstrip("/").lower()


def _redirect_ok(uri: str, debug: bool) -> bool:
    try:
        p = urlparse(uri)
    except ValueError:
        return False
    if p.scheme == "https" and p.netloc:
        return True
    if p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1"):
        return True  # RFC 8252 loopback redirects for native clients / local testing
    if debug and p.scheme == "http":
        return True
    return False


class WimzOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    # ---- clients ----

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = oauth_db.load_client(client_id)
        if not row:
            return None
        row = dict(row)
        row.pop("_client_name", None)
        # Public clients are verified by PKCE only; never make the SDK demand a secret from
        # them even though we issued one (some clients insist on receiving a secret).
        if (row.get("token_endpoint_auth_method") or "none") == "none":
            row["client_secret"] = None
        try:
            return OAuthClientInformationFull.model_validate(row)
        except Exception as e:  # pragma: no cover
            logger.error(f"[OAUTH] stored client {client_id} invalid: {e}")
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        s = get_settings()
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris:
            raise RegistrationError(error="invalid_redirect_uri", error_description="redirect_uris required")
        for u in uris:
            if not _redirect_ok(u, s.debug):
                raise RegistrationError(error="invalid_redirect_uri",
                                        error_description=f"redirect_uri must be https (or loopback http): {u}")
        if not client_info.client_name:
            client_info.client_name = urlparse(uris[0]).hostname or "MCP client"
        info = client_info.model_dump(mode="json", exclude_none=True)
        oauth_db.save_client(info)
        logger.info(f"[OAUTH] registered client {client_info.client_id} name={client_info.client_name!r} "
                    f"auth={client_info.token_endpoint_auth_method} redirects={uris}")

    # ---- authorize ----

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        requested = normalize_scopes(params.scopes or [])
        unknown = [sc for sc in requested if sc not in ALL_SCOPES]
        if unknown:
            raise AuthorizeError(error="invalid_scope", error_description=f"unknown scope(s): {', '.join(unknown)}")
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(16)
        oauth_db.create_login_session(sid, client.client_id, {
            "state": params.state,
            "scopes": requested,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
            "client_name": client.client_name,
        }, csrf)
        return f"{issuer_url()}/authorize/login?sid={sid}"

    # ---- codes ----

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        row = oauth_db.load_code(authorization_code)
        if not row or row["client_id"] != client.client_id:
            return None
        if row["used_at"] is not None:
            # Replayed code: burn everything that was minted from it (RFC 6749 §4.1.2).
            if row["family_id"]:
                oauth_db.revoke_family(row["family_id"])
            logger.warning(f"[OAUTH] authorization code replay for client {client.client_id}; family revoked")
            return None
        if row["expires_at"] < time.time():
            return None
        return AuthorizationCode(
            code=row["code"], scopes=row["scopes"], expires_at=row["expires_at"], client_id=row["client_id"],
            code_challenge=row["code_challenge"], redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row["resource"], subject=row["user_id"],
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        family_id = str(uuid.uuid4())
        if not oauth_db.consume_code(authorization_code.code, family_id):
            raise TokenError(error="invalid_grant", error_description="authorization code already used")
        user_id = authorization_code.subject or ""
        if not user_id:
            raise TokenError(error="invalid_grant", error_description="code has no subject")
        oauth_db.touch_client(client.client_id)
        return self._mint(client.client_id, user_id, authorization_code.scopes, authorization_code.resource, family_id)

    def _mint(self, client_id: str, user_id: str, scopes: list[str], resource: Optional[str], family_id: str) -> OAuthToken:
        s = get_settings()
        scopes = normalize_scopes(scopes)
        now = time.time()
        access = ACCESS_PREFIX + secrets.token_urlsafe(32)
        refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
        oauth_db.save_token(access, "access", client_id, user_id, scopes, resource, family_id, now + s.mcp_access_token_ttl)
        oauth_db.save_token(refresh, "refresh", client_id, user_id, scopes, resource, family_id, now + s.mcp_refresh_token_ttl)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=s.mcp_access_token_ttl,
                          scope=" ".join(scopes), refresh_token=refresh)

    # ---- refresh ----

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = oauth_db.load_token(refresh_token)
        if not row or row["token_type"] != "refresh" or row["client_id"] != client.client_id:
            return None
        if row["revoked_at"] is not None:
            # Reuse of a rotated refresh token = theft signal: kill the whole family.
            oauth_db.revoke_family(row["family_id"])
            logger.warning(f"[OAUTH] refresh token reuse for client {client.client_id}; family revoked")
            return None
        if row["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"],
                            expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["user_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        row = oauth_db.load_token(refresh_token.token)
        if not row or row["revoked_at"] is not None:
            raise TokenError(error="invalid_grant", error_description="refresh token revoked")
        granted = normalize_scopes(scopes or refresh_token.scopes)
        if not set(granted) <= set(refresh_token.scopes):
            raise TokenError(error="invalid_scope", error_description="requested scopes exceed the original grant")
        oauth_db.revoke_token_hash(row["token_hash"])
        oauth_db.revoke_access_tokens_in_family(row["family_id"])
        oauth_db.touch_client(client.client_id)
        return self._mint(client.client_id, row["user_id"], granted, row["resource"], row["family_id"])

    # ---- access ----

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = oauth_db.load_token(token)
        if not row or row["token_type"] != "access" or row["revoked_at"] is not None:
            return None
        if row["expires_at"] < time.time():
            return None
        if row["resource"] and _canon(row["resource"]) != _canon(resource_url()):
            logger.warning(f"[OAUTH] token resource mismatch: {row['resource']} != {resource_url()}")
            return None
        client = oauth_db.load_client(row["client_id"]) or {}
        return AccessToken(
            token=token, client_id=row["client_id"], scopes=row["scopes"], expires_at=int(row["expires_at"]),
            resource=row["resource"], subject=row["user_id"],
            claims={"client_name": client.get("_client_name") or client.get("client_name")},
        )

    # ---- revoke ----

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        row = oauth_db.load_token(token.token)
        if not row:
            return
        if row["token_type"] == "refresh":
            oauth_db.revoke_family(row["family_id"])
        else:
            oauth_db.revoke_token_hash(row["token_hash"])
