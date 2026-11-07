"""Relay-dispatched take_photo results land in dog_photos (gallery catch-up, 2026-11-07)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import app.database as db
from app.routers import dogs
from app.services import media_files, photo_gallery as pg

META = {"command": "take_photo", "dog_id": "dog_t1", "device_id": "wimz_robot_t"}
OWNER = {"user_id": "user_t1"}


@pytest.fixture
def media(tmp_path, monkeypatch):
    monkeypatch.setattr(media_files, "media_dir", lambda: tmp_path)
    (tmp_path / "wimz_robot_t_abc123.jpg").write_bytes(b"\xff\xd8jpeg")
    return tmp_path


def ok_result(url="/api/media/download/wimz_robot_t_abc123.jpg"):
    return {"type": "command_result", "command_id": "cmd1", "ok": True, "result": {"image_url": url}}


def test_records_final_photo_and_pins_it(temp_db, media):
    pid = pg.record_take_photo("cmd1", META, ok_result())
    assert pid == "photo_cmd_cmd1"
    rows = db.get_dog_photos("dog_t1")
    assert [(r["id"], r["photo_url"], r["is_profile_photo"]) for r in rows] == [
        ("photo_cmd_cmd1", "/api/dogs/dog_t1/media/wimz_robot_t_abc123.jpg", False)]
    assert (media / "wimz_robot_t_abc123.jpg.ttl").read_text() == str(pg.GALLERY_TTL_S)


def test_repeat_result_is_one_entry(temp_db, media):
    assert pg.record_take_photo("cmd1", META, ok_result()) == "photo_cmd_cmd1"
    assert pg.record_take_photo("cmd1", META, ok_result()) == "photo_cmd_cmd1"
    assert len(db.get_dog_photos("dog_t1")) == 1


@pytest.mark.parametrize("meta,msg", [
    ({**META, "command": "dispense_treat"}, ok_result()),                          # not a photo
    (None, ok_result()),                                                          # app passthrough: no meta
    (META, {**ok_result(), "ok": False}),                                         # failed capture
    (META, {"ok": True, "result": {"deferred": True}}),                           # interim
    (META, {"ok": True, "result": {}}),                                           # no image url
    (META, ok_result("/api/media/download/wimz_robot_t_missing.jpg")),           # file not stored
    ({**META, "dog_id": None}, ok_result()),                                      # no dog
])
def test_not_recorded(temp_db, media, meta, msg):
    assert pg.record_take_photo("cmd1", meta, msg) is None
    assert db.get_dog_photos("dog_t1") == []


def test_photo_ready_shape_is_recorded(temp_db, media):
    from app.services import command_results as cr
    msg = cr.from_photo_ready({"command_id": "cmd2", "photo_id": "ph",
                               "image_url": "/api/media/download/wimz_robot_t_abc123.jpg"})
    assert pg.record_take_photo("cmd2", META, msg) == "photo_cmd_cmd2"


def test_list_photos_since(temp_db, media):
    pg.record_take_photo("cmd1", META, ok_result())
    before = datetime.now(timezone.utc) - timedelta(minutes=1)
    after = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert [p.id for p in asyncio.run(dogs.list_dog_photos("dog_t1", OWNER))] == ["photo_cmd_cmd1"]
    assert [p.id for p in asyncio.run(dogs.list_dog_photos("dog_t1", OWNER, since=before))] == ["photo_cmd_cmd1"]
    assert asyncio.run(dogs.list_dog_photos("dog_t1", OWNER, since=after)) == []
    naive = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None)  # treated as UTC
    assert len(asyncio.run(dogs.list_dog_photos("dog_t1", OWNER, since=naive))) == 1
