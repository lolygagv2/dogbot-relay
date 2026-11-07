"""Robot <-> dog pairing (2026-11-07). Pairing must never block a robot.

`device_dogs` is the explicit link. When a robot with an owner has no paired dog, it is
paired with all of the owner's existing dogs; only if the owner has none is a placeholder
dog created for the owner (named after the robot, e.g. "Robot 05's dog"), paired to it,
and treated like any other dog: events, roll-ups, settings, utterances and
commands all work against it. The owner can rename it (same dog_id, history kept), or
re-pair the robot to other dogs; events are keyed by dog_id, so nothing is lost, and
POST /api/dogs/merge folds a placeholder's history into a real dog.

Every "which dog is this robot?" question goes through `device_dog_ids()`.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.database import (
    create_placeholder_dog,
    get_device_dog_ids,
    get_device_owner,
    get_dog_by_id,
    get_dog_device_ids,
    get_dog_owner,
    get_owned_dog_ids,
    get_user_paired_devices,
    link_device_dog,
    set_device_dogs,
)

logger = logging.getLogger(__name__)


def placeholder_name(device_id: str) -> str:
    """wimz_robot_05 -> "Robot 05's dog"; anything else -> "<device_id>'s dog"."""
    m = re.search(r"(?:^|_)robot_?(\w+)$", device_id, re.IGNORECASE)
    label = f"Robot {m.group(1)}" if m else device_id
    return f"{label}'s dog"


def placeholder_dog_id(device_id: str, owner: str) -> str:
    # deterministic per (owner, robot): re-ensuring re-links the same placeholder
    return f"ph_{owner}_{device_id}"


def ensure_device_dogs(device_id: str) -> list[str]:
    """Paired dog ids for a robot. A robot with none is paired with all of its owner's
    existing dogs; if the owner has no dogs, with a new placeholder dog.
    Returns [] only when the robot has no owner (nothing to attach a dog to)."""
    dogs = get_device_dog_ids(device_id)
    if dogs:
        return dogs
    owner = get_device_owner(device_id)
    if not owner:
        return []
    owned = get_owned_dog_ids(owner)
    if owned:
        set_device_dogs(device_id, owned)
        logger.info(f"[PAIRING] paired {device_id} with owner {owner}'s existing dogs {owned}")
        return get_device_dog_ids(device_id)
    dog_id = placeholder_dog_id(device_id, owner)
    existing = get_dog_by_id(dog_id)
    if existing is None:
        create_placeholder_dog(dog_id, placeholder_name(device_id), owner)
        logger.info(f"[PAIRING] created placeholder dog {dog_id} ({placeholder_name(device_id)!r}) for {device_id}")
    elif get_dog_owner(dog_id) != owner:  # pragma: no cover - id embeds the owner
        logger.error(f"[PAIRING] placeholder {dog_id} exists under another owner; not linking")
        return []
    link_device_dog(device_id, dog_id)
    logger.info(f"[PAIRING] paired {device_id} -> {dog_id}")
    return get_device_dog_ids(device_id)


def device_dog_ids(device_id: Optional[str]) -> list[str]:
    """The robot's paired dogs (ensured). Use this everywhere a device resolves its dog."""
    if not device_id:
        return []
    try:
        return ensure_device_dogs(device_id)
    except Exception as e:  # never block a robot on pairing bookkeeping
        logger.error(f"[PAIRING] ensure failed for {device_id}: {e}")
        return get_device_dog_ids(device_id)


def primary_dog_id(device_id: Optional[str]) -> Optional[str]:
    dogs = device_dog_ids(device_id)
    return dogs[0] if dogs else None


def dog_devices(dog_id: str, owner: Optional[str]) -> list[str]:
    """Robots for a dog: its device_dogs links; if it has none (e.g. a dog the owner added
    in the app but never paired), fall back to all of the owner's robots."""
    linked = get_dog_device_ids(dog_id)
    if linked:
        return linked
    return get_user_paired_devices(owner) if owner else []
