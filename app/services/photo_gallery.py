"""Gallery entries for relay-dispatched snapshots (2026-11-07).

When the voice agent, a Claude connector or any other relay caller asks the robot for a
`take_photo`, the final command_result is recorded in `dog_photos` so the app can pull it from
GET /api/dogs/{dog_id}/photos even if it was closed when the photo was taken. The still is
pinned in the media store for 7 days; the app is expected to copy it into its own gallery.

Photos the app takes itself over /ws/app (no relay command_id) are not recorded here.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from app import database
from app.services import media_files

logger = logging.getLogger(__name__)

GALLERY_TTL_S = media_files.TREAT_STILL_TTL_S  # 7 days


def gallery_photo_id(command_id: str) -> str:
    """Deterministic, so a repeated command_result never makes a second entry."""
    return f"photo_cmd_{command_id}"


def _image_name(result) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    url = result.get("image_url") or result.get("url")
    if not url or not isinstance(url, str):
        return None
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    return name or None


def record_take_photo(command_id: str, meta: Optional[dict], message: dict) -> Optional[str]:
    """Record a successful, final take_photo result. Returns the gallery photo id, else None."""
    meta = meta or {}
    if meta.get("command") != "take_photo" or not command_id:
        return None
    if not message.get("ok", True):
        return None
    result = message.get("result")
    if isinstance(result, dict) and result.get("deferred") is True:
        return None  # interim; the final result comes later
    dog_id = meta.get("dog_id")
    name = _image_name(result)
    if not dog_id or not name:
        return None
    if not media_files.resolve(name):
        logger.warning(f"[GALLERY] {command_id}: still {name} not in media store; not recorded")
        return None
    media_files.pin_media(name, GALLERY_TTL_S)
    photo_id = gallery_photo_id(command_id)
    try:
        database.create_dog_photo(photo_id=photo_id, dog_id=dog_id,
                                  photo_url=f"/api/dogs/{dog_id}/media/{name}", is_profile_photo=False)
    except sqlite3.IntegrityError:
        return photo_id  # already recorded
    except Exception as e:  # never break result routing over the gallery
        logger.error(f"[GALLERY] {command_id}: could not record {name}: {e}")
        return None
    logger.info(f"[GALLERY] Recorded {photo_id} ({name}) for dog {dog_id}")
    return photo_id
