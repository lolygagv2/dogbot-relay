"""mode_change events -> totals.sg_s / coach_s, and get_trend's 90 days + compare_dog_id (2026-11-07)."""
import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.services import event_store, settings_service

D = "2026-11-03"        # a past local day for dog_t1
D_PREV = "2026-11-02"


def run(c):
    return asyncio.run(c)


def at(day: str, hm: str) -> str:
    """Local HH:MM on `day` in dog_t1's timezone, as a UTC ISO timestamp."""
    tz = settings_service.get_timezone("dog_t1")
    local = datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    return local.astimezone(timezone.utc).isoformat()


def mode(eid, ts, m, dog_id=None):
    return {"event_id": eid, "type": "mode_change", "dog_id": dog_id, "ts": ts, "data": {"mode": m}}


def beat(eid, ts, m):
    return {"event_id": eid, "type": "status", "dog_id": None, "ts": ts, "data": {"battery": 80, "mode": m}}


def ingest(events, device="robot_t"):
    res = run(event_store.ingest_events(events, device_id=device, user_id="user_t1", source="robot",
                                        notify=False, forward=False))
    assert not res["rejected"], res["rejected"]


@pytest.fixture()
def home(temp_db):
    import app.database as db
    db.create_device_pairing("user_t1", "robot_t")
    db.create_dog(dog_id="dog_t3", name="Bex", user_id="user_t1")
    db.add_user_dog("user_t1", "dog_t3", "owner")
    return db


def totals(dog, day):
    return event_store.get_summary(dog, day, refresh=True)["totals"]


# ---- validation + route ----

def test_mode_change_is_device_scoped_and_needs_mode():
    ev, reason = event_store.validate_event(mode("m1", "2026-11-03T10:00:00Z", " Silent_Guardian "))
    assert reason is None and ev["dog_id"] is None and ev["data"]["mode"] == "silent_guardian"
    ev, reason = event_store.validate_event({"event_id": "m2", "type": "mode_change", "ts": "2026-11-03T10:00:00Z", "data": {}})
    assert ev is None and "data.mode" in reason


def test_post_events_accepts_mode_change_without_dog_for_two_dog_owner(home):
    from app.auth import Actor
    from app.models import EventBatchIn
    from app.routers.event_store import post_events
    robot = Actor(kind="robot", id="robot_t", user_id="user_t1", device_id="robot_t")
    batch = EventBatchIn.model_validate({"events": [mode("m1", at(D, "10:00"), "coach")]})
    res = run(post_events(batch, robot))
    assert res["accepted"] == ["m1"] and not res["rejected"]


# ---- roll-up ----

def test_sg_and_coach_seconds_carry_across_midnight(home):
    ingest([
        mode("a", at(D_PREV, "22:00"), "silent_guardian"),   # 2 h on D_PREV, carries into D
        mode("b", at(D, "02:00"), "idle"),
        mode("c", at(D, "10:00"), "coach", "dog_t1"),        # Coach for dog_t1 only
        mode("d", at(D, "10:30"), "idle"),
        mode("e", at(D, "20:00"), "silent_guardian"),        # runs to the end of D
        mode("f", at("2026-11-04", "06:00"), "idle"),
    ])
    t = totals("dog_t1", D)
    assert t["sg_s"] == 6 * 3600 and t["coach_s"] == 1800
    assert totals("dog_t1", D_PREV)["sg_s"] == 2 * 3600
    other = totals("dog_t3", D)                              # same home: SG counts, dog_t1's Coach does not
    assert other["sg_s"] == 6 * 3600 and other["coach_s"] == 0


def test_mode_change_is_not_a_count(home):
    ingest([mode("a", at(D, "10:00"), "coach", "dog_t1"), mode("b", at(D, "10:05"), "idle", "dog_t1")])
    s = event_store.get_summary("dog_t1", D, refresh=True)
    assert "mode_change" not in s["counts"] and s["event_count"] == 0 and s["totals"]["coach_s"] == 300


def test_heartbeat_with_other_mode_ends_the_period(home):
    ingest([mode("a", at(D, "12:00"), "silent_guardian"), beat("s1", at(D, "13:00"), "idle")])
    assert totals("dog_t1", D)["sg_s"] == 3600


def test_robot_going_quiet_ends_the_period(home):
    ingest([mode("a", at(D, "12:00"), "silent_guardian"), beat("s1", at(D, "12:10"), "silent_guardian")])
    assert totals("dog_t1", D)["sg_s"] == 600 + event_store.HEARTBEAT_GRACE_S


def test_offline_all_day_does_not_count(home):
    ingest([mode("a", at(D_PREV, "12:00"), "silent_guardian"), beat("s1", at(D_PREV, "12:10"), "silent_guardian")])
    assert totals("dog_t1", D)["sg_s"] == 0


def test_two_robots_overlapping_count_once(home):
    home.create_device_pairing("user_t1", "robot_u")
    ingest([mode("a", at(D, "12:00"), "silent_guardian"), mode("b", at(D, "14:00"), "idle")], device="robot_t")
    ingest([mode("c", at(D, "13:00"), "silent_guardian"), mode("d", at(D, "15:00"), "idle")], device="robot_u")
    assert totals("dog_t1", D)["sg_s"] == 3 * 3600


def test_new_mode_change_invalidates_cached_days(home):
    ingest([mode("a", at(D, "12:00"), "silent_guardian")])
    first = event_store.get_summary("dog_t1", D)["totals"]["sg_s"]
    assert first == 12 * 3600
    later = event_store.get_summary("dog_t1", "2026-11-04")["totals"]["sg_s"]
    assert later == 24 * 3600
    ingest([mode("b", at(D, "13:00"), "idle")])               # cached D and D+1 are now stale
    assert event_store.get_summary("dog_t1", D)["totals"]["sg_s"] == 3600
    assert event_store.get_summary("dog_t1", "2026-11-04")["totals"]["sg_s"] == 0


def test_running_mode_today_is_recomputed(home):
    now = datetime.now(timezone.utc)
    ingest([mode("a", (now - timedelta(minutes=30)).isoformat(), "coach")])
    today = settings_service.local_now("dog_t1").date().isoformat()
    s = event_store.get_summary("dog_t1", today)
    if (now - timedelta(minutes=30)).astimezone(settings_service.get_timezone("dog_t1")).date().isoformat() == today:
        assert s["mode_running"] is True and 1700 <= s["totals"]["coach_s"] <= 1900


# ---- get_trend ----

class FakeClient:
    def __init__(self, per_dog):
        self.per_dog, self.calls = per_dog, []

    async def get_status(self, dog_id):
        return {"timezone": "UTC"}

    async def get_summary(self, dog_id, date):
        self.calls.append((dog_id, date))
        t = dict(self.per_dog[dog_id])
        return {"date": date, "counts": {"bark_episode": t.pop("bark_episodes", 0)}, "totals": t,
                "time_to_settle": {"median_s": t.pop("settle", None)}}


def _trend(monkeypatch, per_dog, **kw):
    from app.mcp_server import tools as mt
    from mcp.server.mcpserver import MCPServer
    client = FakeClient(per_dog)
    monkeypatch.setattr(mt, "_client", lambda tool: (client, "user_t1"))
    monkeypatch.setattr(mt, "_check_dog", lambda user, dog: {"id": dog})
    srv = MCPServer(name="t")
    mt.register_tools(srv)
    fn = srv._tool_manager.get_tool("get_trend").fn
    return run(fn.__wrapped__(**kw) if hasattr(fn, "__wrapped__") else fn(**kw)), client


def test_trend_90_days_with_mode_time_and_comparison(monkeypatch):
    per_dog = {"dog_a": {"treats": 2, "sg_s": 3600, "coach_s": 600, "bark_episodes": 3, "settle": 40},
               "dog_b": {"treats": 1, "sg_s": 3600, "coach_s": 0, "bark_episodes": 1, "settle": 20}}
    r, c = _trend(monkeypatch, per_dog, dog_id="dog_a", days=90, compare_dog_id="dog_b")
    assert r["days"] == 90 and len(r["series"]) == 90 and r["series"][-1]["date"] > r["series"][0]["date"]
    assert r["totals"]["sg_s"] == 90 * 3600 and r["totals"]["coach_s"] == 90 * 600
    assert [x["date"] for x in r["compare"]["series"]] == [x["date"] for x in r["series"]]
    cmp = r["comparison"]
    assert cmp["treats"] == {"dog": 180, "compare": 90, "difference": 90, "pct_difference": 100}
    assert cmp["coach_s"]["compare"] == 0 and cmp["coach_s"]["pct_difference"] is None
    assert cmp["settle_median_s_avg"] == {"dog": 40, "compare": 20, "difference": 20, "pct_difference": 100}
    assert len([x for x in c.calls if x[0] == "dog_b"]) == 90      # comparison: current period only
    assert len([x for x in c.calls if x[0] == "dog_a"]) == 180     # plus the previous period


@pytest.mark.parametrize("asked,got", [(0, 1), (1, 1), (30, 30), (500, 90)])
def test_trend_days_clamped(monkeypatch, asked, got):
    r, _ = _trend(monkeypatch, {"dog_a": {"treats": 1}}, dog_id="dog_a", days=asked)
    assert r["days"] == got and len(r["series"]) == got and "comparison" not in r


def test_trend_compare_same_dog_refused(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError):
        _trend(monkeypatch, {"dog_a": {}}, dog_id="dog_a", compare_dog_id="dog_a")


def test_trend_docstring_mentions_new_fields():
    from app.mcp_server import tools as mt
    src = inspect.getsource(mt)
    assert "compare_dog_id" in src and "sg_s / coach_s" in src and "TREND_MAX_DAYS = 90" in src
