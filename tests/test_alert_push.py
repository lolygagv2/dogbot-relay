"""Bare robot `alert` events: stored, not pushed (2026-11-19)."""
from app.routers import websocket as ws
from app.services.push_service import _build_notification


def test_bare_alert_not_pushed():
    assert _build_notification("guardian_alert", {"event": "alert"}) is None
    assert _build_notification("guardian_alert", {"event": "alert", "device_id": "r"}) is None


def test_alert_with_reason_pushed_with_title():
    assert _build_notification("guardian_alert", {"event": "alert", "reason": "low battery"}) == \
        ("alert", "Guardian: low battery", "")
    assert _build_notification("guardian_alert", {"event": "alert", "message": "Stuck"})[0] == "alert"


def test_unknown_dog_alert_still_pushed():
    assert _build_notification("guardian_alert", {}) == ("alert", "Guardian Alert", "")


def test_alert_top_level_fields_kept():
    assert "alert" in ws._FLATTENED_EVENTS
