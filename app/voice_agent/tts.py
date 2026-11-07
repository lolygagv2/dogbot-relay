"""Text-to-speech via ElevenLabs (the voice, and so the accent, is chosen in the ElevenLabs dashboard) and the
short-lived audio clip store the robot downloads from.

Clips live under <agent_audio_dir>/out/<clip_id>.mp3 for `agent_audio_ttl_seconds` and are
served by GET /agent/audio/{clip_id}.mp3 (unguessable id, no-store). Only TTS output ever
lands here; human audio is never stored.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class TTSError(Exception):
    pass


def audio_dir() -> Path:
    s = get_settings()
    base = Path(s.agent_audio_dir) if s.agent_audio_dir else Path(__file__).parent.parent.parent / "data" / "agent_audio"
    out = base / "out"
    out.mkdir(parents=True, exist_ok=True)
    return out


def clip_url(clip_id: str) -> str:
    s = get_settings()
    base = (s.agent_base_url if s.agent_base_url.startswith("https://") else s.public_base_url).rstrip("/")
    return f"{base}/agent/audio/{clip_id}.mp3"


def clip_path(clip_id: str) -> Optional[Path]:
    if not clip_id or not all(c.isalnum() or c in "-_" for c in clip_id):
        return None
    p = audio_dir() / f"{clip_id}.mp3"
    return p if p.exists() else None


def new_clip_id() -> str:
    return secrets.token_urlsafe(24)


async def synthesize(text: str, *, cache_key: Optional[str] = None) -> tuple[str, str]:
    """Return (clip_id, url). `cache_key` reuses a clip for canned phrases."""
    s = get_settings()
    if not s.elevenlabs_api_key or not s.elevenlabs_voice_id:
        raise TTSError("ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID not configured")
    text = text.strip()
    if not text:
        raise TTSError("empty text")
    if cache_key:
        cid = "canned_" + hashlib.sha256((cache_key + s.elevenlabs_voice_id).encode()).hexdigest()[:24]
        if clip_path(cid):
            return cid, clip_url(cid)
    else:
        cid = new_clip_id()

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{s.elevenlabs_voice_id}"
    body = {"text": text[:1200], "model_id": s.elevenlabs_model,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": 1.0}}
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(url, params={"output_format": "mp3_22050_32"},
                                     headers={"xi-api-key": s.elevenlabs_api_key, "accept": "audio/mpeg"},
                                     json=body)
    except httpx.HTTPError as e:
        raise TTSError(f"tts request failed: {e}") from e
    if resp.status_code != 200 or not resp.content:
        raise TTSError(f"tts HTTP {resp.status_code}: {resp.text[:200]}")

    final = audio_dir() / f"{cid}.mp3"
    tmp = final.with_suffix(".tmp")
    tmp.write_bytes(resp.content)
    os.replace(tmp, final)
    logger.info(f"[TTS] {len(text)} chars -> {len(resp.content)} bytes ({cid})")
    return cid, clip_url(cid)


def sweep_once() -> int:
    s = get_settings()
    cutoff = time.time() - s.agent_audio_ttl_seconds
    n = 0
    for p in audio_dir().glob("*.mp3"):
        if p.name.startswith("canned_"):
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


async def sweep_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            n = sweep_once()
            if n:
                logger.info(f"[TTS] swept {n} expired clip(s)")
        except Exception as e:  # pragma: no cover
            logger.warning(f"[TTS] sweep failed: {e}")
