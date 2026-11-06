"""Speech-to-text via the OpenAI transcription endpoint (owner's choice, 2026-11-05).

The uploaded utterance is held in memory only: it is sent for transcription and
discarded. Nothing is written to disk and nothing serves human audio back out.
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class STTError(Exception):
    pass


async def transcribe(wav_bytes: bytes, filename: str = "utterance.wav", content_type: str = "audio/wav") -> str:
    s = get_settings()
    if not s.openai_api_key:
        raise STTError("OPENAI_API_KEY not configured")
    files = {"file": (filename, wav_bytes, content_type)}
    data = {"model": s.openai_stt_model, "language": "en", "response_format": "json"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {s.openai_api_key}"},
                files=files, data=data,
            )
    except httpx.HTTPError as e:
        raise STTError(f"transcription request failed: {e}") from e
    if resp.status_code != 200:
        raise STTError(f"transcription HTTP {resp.status_code}: {resp.text[:200]}")
    text = (resp.json().get("text") or "").strip()
    logger.info(f"[STT] {len(wav_bytes)} bytes -> {len(text)} chars")
    return text
