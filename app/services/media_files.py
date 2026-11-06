"""Media file storage shared by the upload router, the event store and the photo routes.

Files live in `media_storage_dir` (default <repo>/data/media, persistent across reboots).
Retention: video 1 h, stills 24 h, and stills pinned by `pin_media` (treat snapshots) 7 days
(PHOTO ACCESS amendment 2026-11-06). Pins are sidecar files `<name>.ttl` holding the TTL in
seconds, so they survive restarts. Seed stills under <repo>/seeds/media are read-only.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

VIDEO_TTL_S = 3600
IMAGE_TTL_S = 24 * 3600
TREAT_STILL_TTL_S = 7 * 24 * 3600
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".mp4": "video/mp4", ".mov": "video/quicktime"}
DOWNLOAD_PREFIX = "/api/media/download/"
SEEDS_MEDIA_DIR = Path(__file__).parent.parent.parent / "seeds" / "media"


def media_dir() -> Path:
    s = get_settings()
    d = Path(s.media_storage_dir) if s.media_storage_dir else Path(__file__).parent.parent.parent / "data" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def media_type_for(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def filename_from_url(url: Optional[str]) -> Optional[str]:
    if not url or not isinstance(url, str) or not url.startswith(DOWNLOAD_PREFIX):
        return None
    name = os.path.basename(url[len(DOWNLOAD_PREFIX):])
    return name or None


def resolve(filename: str) -> Optional[Path]:
    """Stored file for a download name, or a seed still; None if missing/expired."""
    safe = os.path.basename(filename or "")
    if not safe:
        return None
    p = media_dir() / safe
    if p.is_file():
        return p
    if safe.startswith("seed_"):
        sp = SEEDS_MEDIA_DIR / safe
        if sp.is_file():
            return sp
    return None


def pin_media(url_or_name: str, ttl_seconds: int = TREAT_STILL_TTL_S) -> bool:
    """Extend a stored file's lifetime (sidecar TTL). Returns True if the file exists."""
    name = filename_from_url(url_or_name) or os.path.basename(url_or_name or "")
    if not name or name.startswith("seed_"):
        return bool(name)
    p = media_dir() / name
    if not p.is_file():
        return False
    try:
        (media_dir() / f"{name}.ttl").write_text(str(int(ttl_seconds)))
    except OSError as e:  # pragma: no cover
        logger.warning(f"[MEDIA] pin failed for {name}: {e}")
        return False
    return True


def _ttl_for(path: Path) -> int:
    side = path.with_name(path.name + ".ttl")
    if side.is_file():
        try:
            return int(side.read_text().strip())
        except (ValueError, OSError):
            pass
    return IMAGE_TTL_S if path.suffix.lower() in IMAGE_EXTS else VIDEO_TTL_S


def cleanup_old_files() -> int:
    now = time.time()
    count = 0
    try:
        for path in media_dir().iterdir():
            if not path.is_file() or path.suffix == ".ttl":
                continue
            if now - path.stat().st_mtime > _ttl_for(path):
                path.unlink(missing_ok=True)
                path.with_name(path.name + ".ttl").unlink(missing_ok=True)
                count += 1
        # orphan sidecars
        for side in media_dir().glob("*.ttl"):
            if not side.with_name(side.name[:-4]).exists():
                side.unlink(missing_ok=True)
    except Exception as e:  # pragma: no cover
        logger.error(f"[MEDIA-CLEANUP] Error: {e}")
    if count:
        logger.info(f"[MEDIA-CLEANUP] Removed {count} expired file(s)")
    return count
