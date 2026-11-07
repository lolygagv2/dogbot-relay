"""WIM-Z voice agent service (Dec 9 demo build, 2026-11-06). Separate process, 127.0.0.1:8002.

POST /agent/utterance            robot (HMAC) or phone (JWT): one WAV in -> reply text + TTS url
POST /agent/text                 internal: text in (no STT) -> reply text (+ TTS url if configured)
POST /agent/speak                internal: say arbitrary text on the robot
POST /agent/hooks/robot-event    internal + loopback: robot_connected | sg_rearm | low_hopper
GET  /agent/audio/{clip}.mp3     TTS clips only (unguessable id, short TTL)
GET  /agent/health

Human audio is held in memory for transcription only and never stored or served.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.auth import decode_token, verify_device_signature_with_timestamp
from app.config import get_settings
from app.database import get_device_owner, get_device_secret, get_dog_by_id, get_dog_owner, get_user_dog_role
from app.mcp_server import oauth_db
from app.voice_agent import events, stt, tts
from app.voice_agent.brain import get_brain
from app.voice_agent.tools import ToolContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("wimz.agent")

app = FastAPI(title="WIM-Z voice agent", docs_url=None, redoc_url=None)
MAX_UTTERANCE_BYTES = 5 * 1024 * 1024
_background: set[asyncio.Task] = set()


def _bg(coro) -> None:
    """Fire-and-forget with a strong reference (tasks without one can be garbage-collected)."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# ---------------------------------------------------------------------------
# auth dependencies
# ---------------------------------------------------------------------------

class Caller:
    def __init__(self, user_id: str, source: str, device_id: Optional[str] = None):
        self.user_id, self.source, self.device_id = user_id, source, device_id


async def jwt_or_device(
    authorization: Optional[str] = Header(default=None),
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-ID"),
    x_timestamp: Optional[str] = Header(default=None, alias="X-Timestamp"),
    x_signature: Optional[str] = Header(default=None, alias="X-Signature"),
) -> Caller:
    s = get_settings()
    scheme, _, cred = (authorization or "").partition(" ")
    if scheme.lower().startswith("hmac") or (x_device_id and x_signature):
        sig = cred.strip() if scheme.lower().startswith("hmac") else (x_signature or "")
        if not x_device_id or not sig:
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        secret = get_device_secret(x_device_id) or s.device_secret
        if not verify_device_signature_with_timestamp(x_device_id, x_timestamp, sig, secret):
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        owner = get_device_owner(x_device_id)
        if not owner:
            raise HTTPException(status_code=403, detail={"code": "device_not_paired"})
        return Caller(owner, "robot", x_device_id)
    if scheme.lower() == "bearer" and cred:
        payload = decode_token(cred.strip(), s)
        if payload and payload.get("sub"):
            return Caller(payload["sub"], "phone")
    raise HTTPException(status_code=401, detail={"code": "unauthorized"})


async def internal_only(request: Request, authorization: Optional[str] = Header(default=None)) -> None:
    s = get_settings()
    scheme, _, cred = (authorization or "").partition(" ")
    if not s.internal_api_token or scheme.lower() != "bearer" or not secrets.compare_digest(cred.strip(), s.internal_api_token):
        raise HTTPException(status_code=401, detail={"code": "unauthorized"})


async def loopback_only(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1") or request.headers.get("x-forwarded-for"):
        raise HTTPException(status_code=403, detail={"code": "loopback_only"})


def _dog_access(user_id: str, dog_id: str) -> str:
    if not get_dog_by_id(dog_id):
        raise HTTPException(status_code=404, detail={"code": "dog_not_found"})
    role = get_user_dog_role(user_id, dog_id)
    if not role:
        raise HTTPException(status_code=403, detail={"code": "dog_access_denied"})
    return role


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    oauth_db.init_oauth_tables()
    tts.audio_dir()
    app.state.sweep = asyncio.create_task(tts.sweep_loop())
    s = get_settings()
    logger.info(f"[AGENT] model={s.agent_model} stt={s.openai_stt_model if s.openai_api_key else 'MISSING KEY'} "
                f"tts={'ok' if s.elevenlabs_api_key and s.elevenlabs_voice_id else 'MISSING KEY/VOICE'} "
                f"relay={s.relay_base_url} internal_token={'set' if s.internal_api_token else 'MISSING'}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    task = getattr(app.state, "sweep", None)
    if task:
        task.cancel()


@app.get("/agent/health")
async def health():
    s = get_settings()
    return {"ok": True, "service": "wimz-agent", "model": s.agent_model, "stt": "openai" if s.openai_api_key else None,
            "tts": "elevenlabs" if s.elevenlabs_api_key and s.elevenlabs_voice_id else None}


async def _run_text(caller: Caller, dog_id: str, text: str, want_tts: bool) -> tuple[dict, dict]:
    role = _dog_access(caller.user_id, dog_id)
    allow_physical = caller.source == "robot" or role in ("owner", "caretaker")
    ctx = ToolContext(user_id=caller.user_id, source=caller.source, role=role, allow_physical=allow_physical)
    timing: dict[str, int] = {}
    _bg(ctx.client.transcript(dog_id, "user", text))
    t = time.monotonic()
    out = await get_brain().respond(user_text=text, ctx=ctx, memory_key=f"{caller.user_id}:{dog_id}", dog_hint=dog_id)
    timing["brain_ms"] = int((time.monotonic() - t) * 1000)
    _bg(ctx.client.transcript(dog_id, "agent", out["text_out"]))
    audio = {"audio_url": None, "clip_id": None, "expires_at": None}
    if want_tts:
        t = time.monotonic()
        try:
            clip_id, url = await tts.synthesize(out["text_out"])
            audio = {"audio_url": url, "clip_id": clip_id,
                     "expires_at": time.time() + get_settings().agent_audio_ttl_seconds}
        except tts.TTSError as e:
            logger.warning(f"[AGENT] TTS failed: {e}")
            audio["error"] = str(e)[:120]
        timing["tts_ms"] = int((time.monotonic() - t) * 1000)
    return {**out, **audio}, timing


@app.post("/agent/utterance")
async def utterance(file: UploadFile = File(...), dog_id: Optional[str] = Form(default=None),
                    caller: Caller = Depends(jwt_or_device)):
    t_all = time.monotonic()
    if not dog_id:
        # a robot may omit dog_id: fall back to its owner's oldest dog (or placeholder)
        from app.services import pairing
        dog_id = pairing.primary_dog_id(caller.device_id) if caller.source == "robot" else None
        if not dog_id:
            raise HTTPException(status_code=422, detail={"code": "dog_id_required"})
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail={"code": "empty_audio"})
    if len(data) > MAX_UTTERANCE_BYTES:
        raise HTTPException(status_code=413, detail={"code": "audio_too_large", "max_bytes": MAX_UTTERANCE_BYTES})
    timing: dict[str, int] = {}
    t = time.monotonic()
    try:
        text_in = await stt.transcribe(data, file.filename or "utterance.wav", file.content_type or "audio/wav")
    except stt.STTError as e:
        logger.warning(f"[AGENT] STT failed: {e}")
        text_in = ""
    finally:
        del data  # human audio is never kept
    timing["stt_ms"] = int((time.monotonic() - t) * 1000)

    if not text_in:
        try:
            clip_id, url = await tts.synthesize("Sorry, I didn't catch that.", cache_key="didnt_catch")
        except tts.TTSError:
            clip_id, url = None, None
        body = {"text_in": None, "text_out": "Sorry, I didn't catch that.", "audio_url": url, "clip_id": clip_id,
                "tools_used": [], "refused": []}
        return JSONResponse(body, headers={"x-agent-timing": f"stt={timing['stt_ms']}"})

    body, t2 = await _run_text(caller, dog_id, text_in, want_tts=True)
    timing.update(t2)
    timing["total_ms"] = int((time.monotonic() - t_all) * 1000)
    body["text_in"] = text_in
    return JSONResponse(body, headers={"x-agent-timing": ";".join(f"{k}={v}" for k, v in timing.items())})


@app.post("/agent/text", dependencies=[Depends(internal_only)])
async def text_turn(payload: dict):
    """Internal/test path: same brain, no STT. {dog_id, user_id, text, tts?:bool, source?: robot|phone}."""
    user_id, dog_id, text = str(payload.get("user_id") or ""), str(payload.get("dog_id") or ""), str(payload.get("text") or "")
    if not (user_id and dog_id and text):
        raise HTTPException(status_code=422, detail={"code": "user_id, dog_id and text required"})
    caller = Caller(user_id, payload.get("source") or "phone")
    body, timing = await _run_text(caller, dog_id, text, want_tts=bool(payload.get("tts")))
    body["text_in"] = text
    return JSONResponse(body, headers={"x-agent-timing": ";".join(f"{k}={v}" for k, v in timing.items())})


@app.post("/agent/speak", dependencies=[Depends(internal_only)])
async def speak(payload: dict):
    """{dog_id, text, device_id?}: synthesise and play on the dog's robot."""
    dog_id, text = str(payload.get("dog_id") or ""), str(payload.get("text") or "")
    if not dog_id or not text:
        raise HTTPException(status_code=422, detail={"code": "dog_id and text required"})
    owner = get_dog_owner(dog_id)
    if not owner:
        raise HTTPException(status_code=404, detail={"code": "dog_not_found"})
    try:
        return await events.say(owner, dog_id, text[:600], device_id=payload.get("device_id"))
    except tts.TTSError as e:
        raise HTTPException(status_code=503, detail={"code": "tts_unavailable", "reason": str(e)[:160]})


@app.post("/agent/hooks/robot-event", status_code=202, dependencies=[Depends(internal_only), Depends(loopback_only)])
async def robot_event(payload: dict, background: BackgroundTasks):
    background.add_task(events.handle, payload)
    return {"accepted": True, "event": payload.get("event")}


@app.get("/agent/audio/{clip}.mp3")
async def audio(clip: str):
    p = tts.clip_path(clip)
    if not p:
        raise HTTPException(status_code=404, detail={"code": "clip_not_found_or_expired"})
    return FileResponse(str(p), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
