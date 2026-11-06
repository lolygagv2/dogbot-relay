"""OAuth provider: registration, authorize hand-off, code single-use, refresh rotation, family revocation."""
import asyncio
import time

import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from app.mcp_server import oauth_db
from app.mcp_server.oauth_provider import WimzOAuthProvider


def _client(method="none"):
    return OAuthClientInformationFull.model_validate({
        "client_id": "cid-1", "client_secret": "s3cret" if method != "none" else None, "client_name": "Test Assistant",
        "redirect_uris": ["https://assistant.example/cb"], "token_endpoint_auth_method": method,
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "scope": "read settings missions physical", "client_id_issued_at": int(time.time()),
    })


def test_register_and_get_client(temp_db):
    p = WimzOAuthProvider()
    asyncio.run(p.register_client(_client()))
    c = asyncio.run(p.get_client("cid-1"))
    assert c and c.client_name == "Test Assistant" and c.client_secret is None   # public client: never demand a secret


def test_register_rejects_plain_http_redirect_outside_debug(temp_db, monkeypatch):
    from types import SimpleNamespace
    from app.mcp_server import oauth_provider
    monkeypatch.setattr(oauth_provider, "get_settings", lambda: SimpleNamespace(debug=False))
    p = WimzOAuthProvider()
    bad = _client(); bad.redirect_uris = [AnyUrl("http://evil.example/cb")]
    with pytest.raises(Exception):
        asyncio.run(p.register_client(bad))
    ok = _client(); ok.redirect_uris = [AnyUrl("http://127.0.0.1:8080/cb")]   # loopback is allowed (RFC 8252)
    asyncio.run(p.register_client(ok))


def test_authorize_creates_login_session(temp_db):
    p = WimzOAuthProvider()
    asyncio.run(p.register_client(_client()))
    url = asyncio.run(p.authorize(_client(), AuthorizationParams(
        state="xyz", scopes=["read", "settings"], code_challenge="abc", redirect_uri=AnyUrl("https://assistant.example/cb"),
        redirect_uri_provided_explicitly=True, resource="http://127.0.0.1:8001/mcp")))
    assert url.startswith("http://127.0.0.1:8001/authorize/login?sid=")
    sid = url.split("sid=")[1]
    sess = oauth_db.get_login_session(sid)
    assert sess["client_id"] == "cid-1" and sess["params"]["scopes"] == ["read", "settings"]


def _issue_code(p, scopes):
    oauth_db.save_code("code-1", "cid-1", "user_t1", scopes, "chal", "https://assistant.example/cb", True,
                       "http://127.0.0.1:8001/mcp")
    return asyncio.run(p.load_authorization_code(_client(), "code-1"))


def test_code_exchange_single_use_and_refresh_rotation(temp_db):
    p = WimzOAuthProvider()
    asyncio.run(p.register_client(_client()))
    code = _issue_code(p, ["read", "settings"])
    assert code.subject == "user_t1"
    tok = asyncio.run(p.exchange_authorization_code(_client(), code))
    assert tok.access_token.startswith("wmz_at_") and tok.scope == "read settings"
    at = asyncio.run(p.load_access_token(tok.access_token))
    assert at and at.subject == "user_t1" and at.scopes == ["read", "settings"] and at.claims["client_name"] == "Test Assistant"

    # replaying the code revokes the family
    assert asyncio.run(p.load_authorization_code(_client(), "code-1")) is None
    assert asyncio.run(p.load_access_token(tok.access_token)) is None

    code2 = (oauth_db.save_code("code-2", "cid-1", "user_t1", ["read"], "chal", "https://assistant.example/cb", True, None),
             asyncio.run(p.load_authorization_code(_client(), "code-2")))[1]
    tok2 = asyncio.run(p.exchange_authorization_code(_client(), code2))
    rt = asyncio.run(p.load_refresh_token(_client(), tok2.refresh_token))
    tok3 = asyncio.run(p.exchange_refresh_token(_client(), rt, ["read"]))
    assert asyncio.run(p.load_access_token(tok2.access_token)) is None     # old access revoked
    assert asyncio.run(p.load_access_token(tok3.access_token)) is not None
    # reuse of the rotated refresh token kills the family
    assert asyncio.run(p.load_refresh_token(_client(), tok2.refresh_token)) is None
    assert asyncio.run(p.load_access_token(tok3.access_token)) is None
    # scope escalation on refresh is refused
    code3 = (oauth_db.save_code("code-3", "cid-1", "user_t1", ["read"], "chal", "https://assistant.example/cb", True, None),
             asyncio.run(p.load_authorization_code(_client(), "code-3")))[1]
    tok4 = asyncio.run(p.exchange_authorization_code(_client(), code3))
    rt4 = asyncio.run(p.load_refresh_token(_client(), tok4.refresh_token))
    with pytest.raises(TokenError):
        asyncio.run(p.exchange_refresh_token(_client(), rt4, ["read", "physical"]))


def test_per_user_connector_views(temp_db):
    p = WimzOAuthProvider()
    asyncio.run(p.register_client(_client()))
    code = _issue_code(p, ["read", "physical"])
    asyncio.run(p.exchange_authorization_code(_client(), code))
    lst = oauth_db.list_connectors_for_user("user_t1")
    assert len(lst) == 1 and lst[0]["client_name"] == "Test Assistant" and "physical" in lst[0]["scopes"]
    assert oauth_db.list_connectors_for_user("user_t2") == []
    assert oauth_db.revoke_client_for_user("cid-1", "user_t1") == 2
    assert oauth_db.list_connectors_for_user("user_t1") == []
