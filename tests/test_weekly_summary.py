"""weekly_summary roll-up, get_trend weeks beyond 90 days, and no event-store pruning (2026-11-08)."""
import asyncio
import inspect
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services import event_store, settings_service

WEEK = "2026-10-26"           # a past Monday
DAYS = [(date(2026, 10, 26) + timedelta(days=i)).isoformat() for i in range(7)]


def run(c):
    return asyncio.run(c)


def at(day: str, hm: str) -> str:
    tz = settings_service.get_timezone("dog_t1")
    return datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=tz).astimezone(timezone.utc).isoformat()


def ingest(events):
    res = run(event_store.ingest_events(events, device_id="robot_t", user_id="user_t1", source="robot",
                                        notify=False, forward=False))
    assert not res["rejected"], res["rejected"]


def bark(eid, ts, dur):
    return {"event_id": eid, "type": "bark_episode", "dog_id": "dog_t1", "ts": ts, "duration_s": dur,
            "data": {"bark_count": 5, "severity": "med"}}


def treat(eid, ts):
    return {"event_id": eid, "type": "treat", "dog_id": "dog_t1", "ts": ts, "data": {"count": 1, "source": "auto"}}


@pytest.fixture()
def home(temp_db):
    import app.database as db
    db.create_device_pairing("user_t1", "robot_t")
    return db


def test_week_start_is_monday():
    assert event_store.week_start_of("2026-10-26") == WEEK
    assert event_store.week_start_of("2026-11-01") == WEEK          # Sunday
    assert event_store.week_start_of("2026-11-02") == "2026-11-02"


def test_weekly_sums_dailies_and_is_cached(home):
    ingest([bark("b1", at(DAYS[0], "10:00"), 30), bark("b2", at(DAYS[3], "11:00"), 90),
            treat("t1", at(DAYS[0], "12:00")), treat("t2", at(DAYS[6], "12:00"))])
    w = event_store.get_weekly_summary("dog_t1", DAYS[4])
    assert w["week_start"] == WEEK and w["date"] == WEEK and w["week_end"] == DAYS[6] and w["days"] == DAYS
    assert w["counts"]["bark_episode"] == 2 and w["counts"]["treat"] == 2
    assert w["totals"]["bark_s"] == 120 and w["totals"]["treats"] == 2
    assert w["event_count"] == 4 and w["top_episodes"][0]["event_id"] == "b2"
    daily = [event_store.get_summary("dog_t1", d) for d in DAYS]
    assert w["totals"]["bark_s"] == sum(d["totals"]["bark_s"] for d in daily)
    row = home.get_weekly_summary_row("dog_t1", WEEK)
    assert row is not None and row["summary"]["totals"]["bark_s"] == 120


def test_weekly_recomputes_when_a_day_changes(home):
    ingest([bark("b1", at(DAYS[1], "10:00"), 30)])
    assert event_store.get_weekly_summary("dog_t1", WEEK)["totals"]["bark_s"] == 30
    ingest([bark("b2", at(DAYS[2], "10:00"), 45)])                  # marks that day dirty
    assert event_store.get_weekly_summary("dog_t1", WEEK)["totals"]["bark_s"] == 75


def test_current_week_is_partial_and_not_cached(home):
    today = settings_service.local_now("dog_t1").date()
    w = event_store.get_weekly_summary("dog_t1", today.isoformat())
    assert w["days"][-1] == today.isoformat() and len(w["days"]) == today.weekday() + 1
    assert home.get_weekly_summary_row("dog_t1", w["week_start"]) is None


def test_combine_settle_stats():
    dailies = [
        {"date": "a", "counts": {}, "totals": {"treat_cap": 5},
         "time_to_settle": {"n": 3, "unresolved": 1, "median_s": 10, "mean_s": 12, "min_s": 4, "max_s": 20}},
        {"date": "b", "counts": {}, "totals": {"treat_cap": 6},
         "time_to_settle": {"n": 1, "unresolved": 0, "median_s": 100, "mean_s": 100, "min_s": 100, "max_s": 100}},
        {"date": "c", "counts": {}, "totals": {"treat_cap": None},
         "time_to_settle": {"n": 0, "unresolved": 2, "median_s": None, "mean_s": None, "min_s": None, "max_s": None}},
    ]
    t = event_store.combine_daily_summaries("dog", WEEK, dailies, "UTC")
    assert t["time_to_settle"] == {"n": 4, "unresolved": 3, "median_s": 10, "mean_s": 34, "min_s": 4, "max_s": 100}
    assert t["totals"]["treat_cap"] == 6


def test_week_route(home):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import Actor, resolve_actor
    app.dependency_overrides[resolve_actor] = lambda: Actor(kind="admin", id="user_t1", user_id="user_t1")
    try:
        r = TestClient(app).get(f"/summary/dog_t1/week/{DAYS[2]}")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200 and r.json()["week_start"] == WEEK


# ---- retention ----

def test_retention_never_deletes_store_events(home, monkeypatch):
    import app.main as main
    old = (datetime.now(timezone.utc) - timedelta(days=800)).isoformat()
    ingest([treat("ancient", old)])
    main._run_retention_cleanup()
    assert home.get_event("ancient") is not None
    assert not hasattr(home, "delete_old_store_events")
    assert "delete_old_store_events" not in inspect.getsource(main)


# ---- get_trend weeks ----

def test_trend_over_90_days_is_weekly(monkeypatch):
    from tests.test_mode_time import _trend
    per_dog = {"dog_a": {"treats": 7, "bark_episodes": 2}, "dog_b": {"treats": 3}}
    r, c = _trend(monkeypatch, per_dog, dog_id="dog_a", days=180, compare_dog_id="dog_b")
    assert r["granularity"] == "week" and r["weeks"] == 26 and len(r["series"]) == 26
    dates = [x["date"] for x in r["series"]]
    assert dates == sorted(dates) and all(date.fromisoformat(d).weekday() == 0 for d in dates)
    assert r["to"] >= dates[-1] and (date.fromisoformat(r["to"]) - date.fromisoformat(dates[-1])).days < 7
    assert r["totals"]["treats"] == 26 * 7 and r["comparison"]["treats"]["compare"] == 26 * 3
    assert all(x[1] == "week" for x in c.calls)
    assert len([x for x in c.calls if x[0] == "dog_a"]) == 52 and len([x for x in c.calls if x[0] == "dog_b"]) == 26
    assert [x["date"] for x in r["compare"]["series"]] == dates


@pytest.mark.parametrize("asked,weeks", [(91, 13), (365, 53), (5000, 105)])
def test_trend_weeks_rounding_and_cap(monkeypatch, asked, weeks):
    from tests.test_mode_time import _trend
    r, _ = _trend(monkeypatch, {"dog_a": {"treats": 1}}, dog_id="dog_a", days=asked)
    assert r["granularity"] == "week" and r["weeks"] == weeks and r["days"] == min(asked, 730)
