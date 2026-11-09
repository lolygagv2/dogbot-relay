import asyncio

from app.services.treat_watchdog import TreatWatchdog


def _run(coro):
    return asyncio.run(coro)


def test_times_out_and_notifies_app():
    sent = []

    async def send(user_id, payload):
        sent.append((user_id, payload))
        return 1

    async def go():
        w = TreatWatchdog(timeout_s=0.05)
        w.app_treat_sent("u1", "robot1", {"command": "dispense_treat", "command_id": "c1"}, send)
        await asyncio.sleep(0.1)
        assert w.pending_count("robot1") == 0

    _run(go())
    assert len(sent) == 1
    user, payload = sent[0]
    assert user == "u1"
    assert payload["code"] == "TREAT_NOT_DISPENSED"
    assert payload["command_id"] == "c1"
    assert payload["device_id"] == "robot1"


def test_dispensed_clears_oldest_only():
    sent = []

    async def send(user_id, payload):
        sent.append(payload)
        return 1

    async def go():
        w = TreatWatchdog(timeout_s=0.05)
        w.app_treat_sent("u1", "robot1", {"command_id": "a"}, send)
        w.app_treat_sent("u1", "robot1", {"command_id": "b"}, send)
        w.treat_dispensed("robot1")
        w.treat_dispensed("other_robot")  # no-op
        await asyncio.sleep(0.1)

    _run(go())
    assert [p["command_id"] for p in sent] == ["b"]


def test_default_timeout_is_40s():
    assert TreatWatchdog().timeout_s == 40.0
