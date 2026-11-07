"""Deferred robot results (2026-11-07): interim command_result + photo_ready."""
import asyncio

from app.services import command_results as cr


def test_interim_does_not_wake_waiter_and_photo_ready_does():
    async def main():
        cr.register("cmd_p1", dog_id="dog_t1", device_id="robot_t", command="take_photo")
        waiter = asyncio.create_task(cr.wait("cmd_p1", 2.0))
        await asyncio.sleep(0.05)
        cr.resolve("cmd_p1", {"command_id": "cmd_p1", "ok": True, "result": {"deferred": True}})
        await asyncio.sleep(0.05)
        assert not waiter.done()                                     # still waiting
        assert (await cr.wait("cmd_p1", 0.05)) is None               # a late waiter isn't fooled either
        final = cr.from_photo_ready({"type": "photo_ready", "command_id": "cmd_p1", "photo_id": "ph_9",
                                     "image_url": "/api/media/download/snap_9.jpg"})
        meta = cr.resolve("cmd_p1", final)
        res = await waiter
        assert meta["command"] == "take_photo"
        assert res["ok"] is True and res["interim"] is False
        assert res["result"] == {"image_url": "/api/media/download/snap_9.jpg", "photo_id": "ph_9", "deferred": False}
        assert cr.get("cmd_p1")["result"]["photo_id"] == "ph_9"     # kept for polling
    asyncio.run(main())


def test_photo_ready_after_waiter_gave_up_is_still_stored():
    async def main():
        cr.register("cmd_p2", command="take_photo")
        assert (await cr.wait("cmd_p2", 0.05)) is None               # caller timed out
        cr.resolve("cmd_p2", cr.from_photo_ready({"command_id": "cmd_p2", "photo_id": "ph_2", "image_url": "/x.jpg"}))
        assert cr.get("cmd_p2")["result"]["image_url"] == "/x.jpg"
    asyncio.run(main())


def test_photo_ready_without_image_is_a_failure():
    out = cr.from_photo_ready({"command_id": "c", "photo_id": "p"})
    assert out["ok"] is False and out["error"] == "no_image_url"


def test_plain_command_result_unchanged():
    async def main():
        cr.register("cmd_t", command="dispense_treat")
        waiter = asyncio.create_task(cr.wait("cmd_t", 1.0))
        await asyncio.sleep(0.01)
        cr.resolve("cmd_t", {"command_id": "cmd_t", "ok": True, "result": {"dispensed": 1}})
        assert (await waiter)["result"] == {"dispensed": 1}
    asyncio.run(main())
