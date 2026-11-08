"""set_coach, coach_session / trick_attempt events, coaching roll-up and trick history (2026-11-08)."""
import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.mcp_server import authz
from app.services import event_store, robot_control, settings_service

D = "2026-10-27"            # a past Tuesday


def run(c):
    return asyncio.run(c)


def at(day, hm):
    tz = settings_service.get_timezone("dog_t1")
    return datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=tz).astimezone(timezone.utc).isoformat()


def cue(eid, ts, trick, result, **data):
    return {"event_id": eid, "type": "trick_attempt", "dog_id": "dog_t1", "ts": ts,
            "data": {"trick": trick, "result": result, **data}}


def session(eid, ts, dur):
    return {"event_id": eid, "type": "coach_session", "dog_id": "dog_t1", "ts": ts, "duration_s": dur,
            "data": {"cues": 4, "successes": 3}}


def ingest(events):
    res = run(event_store.ingest_events(events, device_id="robot_t", user_id="user_t1", source="robot",
                                        notify=False, forward=False))
    assert not res["rejected"], res["rejected"]


@pytest.fixture()
def home(temp_db):
    import app.database as db
    db.create_device_pairing("user_t1", "robot_t")
    return db


def a_day_of_coaching(day=D, prefix=""):
    ingest([session(f"{prefix}s1", at(day, "10:00"), 600),
            cue(f"{prefix}c1", at(day, "10:01"), "Sit", "success", latency_ms=1800),
            cue(f"{prefix}c2", at(day, "10:02"), "sit", "fail"),
            cue(f"{prefix}c3", at(day, "10:03"), "sit", None, success=True, latency_ms=1200),
            cue(f"{prefix}c4", at(day, "10:04"), "down", "no_response")])


# ---- schema ----

@pytest.mark.parametrize("ev,reason", [
    ({"type": "coach_session", "data": {}}, "coach_session requires duration_s"),
    ({"type": "coach_session", "duration_s": 60, "data": {"cues": "many"}}, "coach_session data.cues must be an integer"),
    ({"type": "trick_attempt", "data": {"result": "success"}}, "trick_attempt requires data.trick"),
    ({"type": "trick_attempt", "data": {"trick": "sit"}}, "trick_attempt requires data.result"),
    ({"type": "trick_attempt", "data": {"trick": "sit", "result": "great"}}, "trick_attempt requires data.result"),
])
def test_bad_events_rejected(ev, reason):
    out, why = event_store.validate_event({"event_id": "x", "dog_id": "dog_t1", "ts": "2026-10-27T10:00:00Z", **ev})
    assert out is None and why.startswith(reason)


def test_trick_attempt_normalised():
    ev, why = event_store.validate_event(cue("x", "2026-10-27T10:00:00Z", " Sit ", None, success=False, latency_ms="900"))
    assert why is None and ev["data"] == {"trick": "sit", "result": "fail", "success": False, "latency_ms": 900}


# ---- daily / weekly roll-up ----

def test_day_summary_coaching_totals(home):
    a_day_of_coaching()
    s = event_store.get_summary("dog_t1", D, refresh=True)
    t = s["totals"]
    assert s["counts"]["coach_session"] == 1 and s["counts"]["trick_attempt"] == 4
    assert t["coach_s"] == 600 and t["cues"] == 4 and t["cue_successes"] == 2
    assert t["success_rate"] == 0.5 and t["success_pct"] == 50
    assert t["cues_by_trick"] == {"sit": {"cues": 3, "successes": 2}, "down": {"cues": 1, "successes": 0}}


def test_no_cues_means_no_rate_and_mode_time_kept(home):
    ingest([{"event_id": "m1", "type": "mode_change", "dog_id": "dog_t1", "ts": at(D, "09:00"), "data": {"mode": "coach"}},
            {"event_id": "m2", "type": "mode_change", "dog_id": "dog_t1", "ts": at(D, "09:30"), "data": {"mode": "idle"}}])
    t = event_store.get_summary("dog_t1", D, refresh=True)["totals"]
    assert t["cues"] == 0 and t["success_rate"] is None and t["success_pct"] is None
    assert t["coach_s"] == 1800                         # no coach_session rows: time in Coach mode


def test_week_recomputes_rates_instead_of_summing(home):
    a_day_of_coaching(D, "a")                             # 2/4
    ingest([cue("b1", at("2026-10-29", "10:00"), "sit", "success"),
            cue("b2", at("2026-10-29", "10:01"), "sit", "success")])   # 2/2
    w = event_store.get_weekly_summary("dog_t1", D)
    t = w["totals"]
    assert t["cues"] == 6 and t["cue_successes"] == 4 and t["success_rate"] == 0.67 and t["success_pct"] == 67
    assert t["cues_by_trick"]["sit"] == {"cues": 5, "successes": 4}


# ---- trick history ----

def test_trick_history(home):
    now = datetime.now(timezone.utc)
    ingest([cue("h1", (now - timedelta(days=2)).isoformat(), "sit", "success", latency_ms=1000),
            cue("h2", (now - timedelta(days=1)).isoformat(), "sit", "fail", latency_ms=3000),
            cue("h3", (now - timedelta(hours=1)).isoformat(), "spin", "success"),
            cue("old", (now - timedelta(days=60)).isoformat(), "sit", "success")])
    h = event_store.trick_history("dog_t1", days=30, recent=2)
    sit = next(t for t in h["tricks"] if t["trick"] == "sit")
    assert sit["attempts"] == 2 and sit["successes"] == 1 and sit["success_pct"] == 50
    assert sit["avg_latency_ms"] == 2000 and sit["last_result"] == "fail"
    assert [r["event_id"] for r in h["recent"]] == ["h3", "h2"]
    assert h["totals"] == {"attempts": 3, "successes": 2, "success_rate": 0.67, "success_pct": 67}
    assert event_store.trick_history("dog_t1", days=90)["totals"]["attempts"] == 4


def test_trick_history_route(home):
    from fastapi.testclient import TestClient
    from app.auth import Actor, resolve_actor
    from app.main import app
    app.dependency_overrides[resolve_actor] = lambda: Actor(kind="admin", id="user_t1", user_id="user_t1")
    try:
        r = TestClient(app).get("/api/dogs/dog_t1/tricks/history?days=7&recent=3")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200 and r.json()["days"] == 7 and r.json()["tricks"] == []


# ---- get_trend ----

def test_trend_coaching_fields(monkeypatch):
    from tests.test_mode_time import _trend
    per_dog = {"dog_a": {"cues": 4, "cue_successes": 3, "success_rate": 0.75, "coach_s": 600,
                         "cues_by_trick": {"sit": {"cues": 4, "successes": 3}}},
               "dog_b": {"cues": 2, "cue_successes": 1, "success_rate": 0.5}}
    r, _ = _trend(monkeypatch, per_dog, dog_id="dog_a", days=7, compare_dog_id="dog_b")
    assert r["series"][0]["cues"] == 4 and r["series"][0]["success_rate"] == 0.75 and "_by_trick" not in r["series"][0]
    t = r["totals"]
    assert t["cues"] == 28 and t["cue_successes"] == 21 and t["success_rate"] == 0.75 and t["success_pct"] == 75
    assert t["cues_by_trick"] == {"sit": {"cues": 28, "successes": 21, "success_rate": 0.75, "success_pct": 75}}
    assert r["delta"]["success_rate"] == 0.0 and r["delta"]["cues"] == 0
    assert r["comparison"]["success_rate"] == {"dog": 0.75, "compare": 0.5, "difference": 0.25, "pct_difference": 50}
    assert "_by_trick" not in r["compare"]["series"][0]


def test_trend_weekly_has_coaching_fields(monkeypatch):
    from tests.test_mode_time import _trend
    r, _ = _trend(monkeypatch, {"dog_a": {"cues": 10, "cue_successes": 5}}, dog_id="dog_a", days=120)
    assert r["granularity"] == "week" and r["totals"]["success_pct"] == 50


# ---- set_coach ----

def test_coach_mode_command_and_result():
    assert robot_control.coach_mode_command(True) == {"mode": "coach"}
    assert robot_control.coach_mode_command(False) == {"mode": "idle"}
    r = robot_control.coach_mode_tool_result(False, {"delivered": True, "command_id": "c1"})
    assert r["coach"] == "off" and r["mode"] == "idle" and r["delivered"] and "Coach mode off" in r["message"]
    assert "coach" in robot_control.SAFE_MODES


def test_registration_scopes_and_not_strict():
    assert authz.TOOL_SCOPES["set_coach"] == "settings" and authz.TOOL_SCOPES["get_trick_history"] == "read"
    from app.mcp_server import tools as mt
    src = inspect.getsource(mt)
    assert '@mcp.tool(name="set_coach"' in src and '@mcp.tool(name="get_trick_history"' in src
    from app.voice_agent import tools as vt
    t = {x["name"]: x for x in vt.TOOLS}
    assert not t["set_coach"].get("strict") and not t["get_trick_history"].get("strict")
    assert sum(1 for x in vt.TOOLS if x.get("strict")) <= vt.MAX_STRICT_TOOLS


def test_agent_set_coach_dispatch(temp_db):
    from app.voice_agent import tools as vt
    sent = []

    class C:
        async def command(self, dog_id, command, data, wait_s=None):
            sent.append((command, data))
            return {"delivered": True, "command_id": "c9"}

    ctx = vt.ToolContext(user_id="user_t1", source="phone")
    ctx.client = C()
    r = run(vt.dispatch(ctx, "set_coach", {"dog_id": "dog_t1", "on": True}))
    assert r["ok"] and r["coach"] == "on" and sent == [("set_mode", {"mode": "coach"})]


def test_agent_readback():
    from app.voice_agent.brain import Brain
    on = {"tool": "set_coach", "result": {"ok": True, "coach": "on"}}
    off = {"tool": "set_coach", "result": {"ok": True, "coach": "off"}}
    assert Brain._readback_sentence(on) == "I've told the robot to start Coach mode."
    assert any(t in Brain._readback_sentence(on) for t in Brain._readback_tokens(on))
    assert any(t in Brain._readback_sentence(off) for t in Brain._readback_tokens(off))
