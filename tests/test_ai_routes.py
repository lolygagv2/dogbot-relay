"""App-facing AI connector routes: list, revoke one, revoke all, audit (owner or caretaker only)."""
import asyncio
import time

import pytest
from fastapi import HTTPException

import app.database as db
from app.auth import Actor
from app.mcp_server import oauth_db
from app.routers import ai

OWNER = Actor(kind="app", id="user_t1", user_id="user_t1")
VIEWER = Actor(kind="app", id="user_t2", user_id="user_t2")        # conftest: viewer of dog_t1
CARETAKER = Actor(kind="app", id="user_t3", user_id="user_t3")
CONN = Actor(kind="connector", id="claude", user_id="user_t1")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def seeded(temp_db):
    db.create_user("user_t3", "care@example.com", "x")
    db.add_user_dog("user_t3", "dog_t1", "caretaker")
    for cid, name in (("c_claude", "Claude"), ("c_gpt", "ChatGPT"), ("c_old", "Old")):
        oauth_db.save_client({"client_id": cid, "client_name": name, "redirect_uris": ["https://x/cb"]})
    future, past = time.time() + 3600, time.time() - 10
    oauth_db.save_token("a1", "access", "c_claude", "user_t1", ["read", "physical"], None, "fam1", future)
    oauth_db.save_token("r1", "refresh", "c_claude", "user_t1", ["read", "physical"], None, "fam1", future)
    oauth_db.save_token("a2", "access", "c_claude", "user_t1", ["read"], None, "fam2", future)
    oauth_db.save_token("a3", "access", "c_gpt", "user_t1", ["read"], None, "fam3", future)
    oauth_db.save_token("a4", "access", "c_old", "user_t1", ["read"], None, "fam4", past)       # expired
    oauth_db.save_token("a5", "access", "c_claude", "user_t3", ["read"], None, "fam5", future)  # other user
    oauth_db.insert_audit("connector:c_claude", "get_dogs", "ok", client_id="c_claude", user_id="user_t1", dog_id="dog_t1")
    oauth_db.insert_audit("connector:c_claude", "give_treat", "refused", client_id="c_claude", user_id="user_t1", dog_id="dog_t1")


def test_list_active_connectors_only(seeded):
    out = run(ai.list_connectors("dog_t1", actor=OWNER))
    assert out["connector_url"].endswith("/mcp")
    got = {a["client_id"]: a for a in out["assistants"]}
    assert set(got) == {"c_claude", "c_gpt"}                       # expired c_old is not listed
    assert {"client_id", "client_name", "scopes", "last_used_at"} <= set(got["c_claude"])
    assert got["c_claude"]["client_name"] == "Claude"


def test_revoke_one_client_every_family_for_this_user_only(seeded):
    assert run(ai.revoke_connector("dog_t1", "c_claude", actor=OWNER)) == {"client_id": "c_claude", "revoked": 3}
    assert {a["client_id"] for a in run(ai.list_connectors("dog_t1", actor=OWNER))["assistants"]} == {"c_gpt"}
    assert [a["client_id"] for a in run(ai.list_connectors("dog_t1", actor=CARETAKER))["assistants"]] == ["c_claude"]


def test_revoke_all_is_a_kill_switch_for_this_user(seeded):
    assert run(ai.revoke_all("dog_t1", actor=OWNER))["revoked"] == 5   # 4 live + 1 expired, all marked
    assert run(ai.list_connectors("dog_t1", actor=OWNER))["assistants"] == []
    assert len(run(ai.list_connectors("dog_t1", actor=CARETAKER))["assistants"]) == 1


def test_audit_newest_first(seeded):
    rows = run(ai.ai_audit("dog_t1", limit=50, actor=OWNER))["entries"]
    assert [r["tool"] for r in rows] == ["give_treat", "get_dogs"]


def test_owner_or_caretaker_only(seeded):
    assert run(ai.list_connectors("dog_t1", actor=CARETAKER))["assistants"]
    for call in (lambda a: ai.list_connectors("dog_t1", actor=a), lambda a: ai.ai_audit("dog_t1", limit=5, actor=a),
                 lambda a: ai.revoke_connector("dog_t1", "c_claude", actor=a), lambda a: ai.revoke_all("dog_t1", actor=a)):
        with pytest.raises(HTTPException) as e:
            run(call(VIEWER))
        assert e.value.status_code == 403 and e.value.detail["code"] == "owner_or_caretaker_only"
        with pytest.raises(HTTPException) as e:
            run(call(CONN))
        assert e.value.status_code == 403 and e.value.detail["code"] == "app_only"


def test_connections_alias_matches_per_dog_route(seeded):
    per_dog = run(ai.list_connectors("dog_t1", actor=OWNER))
    assert run(ai.list_connections_alias(None, actor=OWNER)) == per_dog
    assert run(ai.list_connections_alias("dog_t1", actor=OWNER)) == per_dog
    assert run(ai.list_connections_alias(None, actor=CARETAKER))["assistants"]
    for args in ((None, VIEWER), ("dog_t1", VIEWER)):          # user_t2 is only a viewer
        with pytest.raises(HTTPException) as e:
            run(ai.list_connections_alias(args[0], actor=args[1]))
        assert e.value.detail["code"] == "owner_or_caretaker_only"
    with pytest.raises(HTTPException) as e:
        run(ai.list_connections_alias(None, actor=CONN))
    assert e.value.detail["code"] == "app_only"
