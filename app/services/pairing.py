"""Owners, robots and dogs (2026-11-07, v2). Pairing must never block a robot.

Three independent things, joined only through the owner:
- a robot belongs to an owner (robots.owner_user_id, set by pair-device);
- a dog belongs to an owner (user_dogs role=owner);
- robots and dogs are NOT linked to each other. A robot serves all of its owner's dogs, and a
  dog is served by all of its owner's robots. Robot state (online, battery, mode) belongs to
  the robot alone and never depends on which dog is asked about.

If a robot's owner has no dogs yet, a placeholder dog is created for the owner (named after
the robot, e.g. "Robot 05's dog") so events, roll-ups, settings, utterances and commands all
work. It is an ordinary dog: the owner can rename it (same dog_id, history kept) or fold its
history into a real dog with POST /api/dogs/merge.

The v1 `device_dogs` link table (2026-11-07 morning) is no longer read; it stays in the
database, untouched, for rollback.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.database import (
    create_placeholder_dog,
    get_device_owner,
    get_dog_by_id,
    get_dog_owner,
    get_owned_dog_ids,
    get_user_paired_devices,
)

logger = logging.getLogger(__name__)


def placeholder_name(device_id: str) -> str:
    """wimz_robot_05 -> "Robot 05's dog"; anything else -> "<device_id>'s dog"."""
    m = re.search(r"(?:^|_)robot_?(\w+)$", device_id, re.IGNORECASE)
    label = f"Robot {m.group(1)}" if m else device_id
    return f"{label}'s dog"


def placeholder_dog_id(device_id: str, owner: str) -> str:
    # deterministic per (owner, robot): re-ensuring finds the same placeholder
    return f"ph_{owner}_{device_id}"


def _ensure_owner_has_dog(device_id: str, owner: str) -> list[str]:
    owned = get_owned_dog_ids(owner)
    if owned:
        return owned
    dog_id = placeholder_dog_id(device_id, owner)
    existing = get_dog_by_id(dog_id)
    if existing is None:
        create_placeholder_dog(dog_id, placeholder_name(device_id), owner)
        logger.info(f"[PAIRING] owner {owner} has no dogs: created placeholder {dog_id} "
                    f"({placeholder_name(device_id)!r}) for {device_id}")
    elif get_dog_owner(dog_id) != owner:  # pragma: no cover - id embeds the owner
        logger.error(f"[PAIRING] placeholder {dog_id} exists under another owner; not using it")
        return []
    return get_owned_dog_ids(owner)


def device_dog_ids(device_id: Optional[str]) -> list[str]:
    """The dogs a robot serves = its owner's dogs, oldest first (a placeholder is created if
    the owner has none). [] only when the robot has no owner. Never raises."""
    if not device_id:
        return []
    owner = get_device_owner(device_id)
    if not owner:
        return []
    try:
        return _ensure_owner_has_dog(device_id, owner)
    except Exception as e:  # never block a robot on dog bookkeeping
        logger.error(f"[PAIRING] ensure failed for {device_id}: {e}")
        return get_owned_dog_ids(owner)


def primary_dog_id(device_id: Optional[str]) -> Optional[str]:
    """Fallback dog when a robot omits dog_id where a guess is harmless (speech): the owner's
    oldest dog. Data-recording paths (POST /events) refuse to guess instead."""
    dogs = device_dog_ids(device_id)
    return dogs[0] if dogs else None


def dog_devices(dog_id: str, owner: Optional[str] = None) -> list[str]:
    """Robots that serve a dog = all of its owner's robots."""
    owner = owner or get_dog_owner(dog_id)
    return get_user_paired_devices(owner) if owner else []
