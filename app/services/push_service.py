"""FCM HTTP v1 push sender (push contract 2026-07-30).

The relay maps activity-event rows to app-side notification types and delivers
them through Firebase Cloud Messaging so alerts reach a locked phone (the app's
relay WS dies seconds after iOS backgrounds it). Auth is a service-account
JWT-bearer exchange signed with python-jose — no google-auth/firebase-admin
dependency, so a code-only deploy can't crash on a missing package.

Credentials: FCM_CREDENTIALS_PATH env var, defaulting to
data/firebase-service-account.json (gitignored). Missing file = push disabled,
everything else keeps working.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

import httpx
from jose import jwt

from app.config import get_settings
from app.database import delete_push_token, get_dog_by_id, list_push_tokens_for_user

logger = logging.getLogger(__name__)

_OAUTH_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_TOKEN_REFRESH_MARGIN = 300  # refresh 5 min before expiry

_credentials: Optional[dict] = None
_credentials_loaded = False
_access_token: Optional[str] = None
_access_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()

# Guardian rows whose payload.action is a session lifecycle marker, not an
# intervention — never notify on these. The robot emits the PAST-TENSE forms
# ("started"/"stopped"); the bare stems were a guess that never matched, so
# every session start and stop pushed a "Guardian Alert". Both spellings are
# listed so neither the robot nor a future rename can resurrect that.
_GUARDIAN_LIFECYCLE_ACTIONS = {
    "start", "started",
    "stop", "stopped",
    "reset",
    "pause", "paused",
    "resume", "resumed",
}

# panic_alert severities that do NOT warrant a push. Inverted from an allow-list
# of {"warning", "high"} on purpose: no panic_alert has ever reached this relay,
# so the exact strings the robot stamps are unverified, and an allow-list fails
# CLOSED — an unexpected value (or a missing field) would silently swallow the
# alert. A panic alert is the most safety-critical push we send, so anything not
# explicitly known-quiet notifies.
_PANIC_QUIET_SEVERITIES = {"low", "info", "debug", "none", "ok", "normal"}

# Dedicated app types that older builds have not registered in enabled_types yet.
# A device that has not opted into the new type still gets the alert under the
# fallback type it DOES know, rather than the push being silently dropped at the
# enabled_types filter. Once the app registers the dedicated type, that device
# receives the dedicated type instead and the fallback stops being consulted.
_APP_TYPE_FALLBACKS = {
    "panicAlert": "alert",
    "sgSummary": "alert",
}


def _panic_text(payload: dict) -> str:
    """The robot's own pre-phrased sentence for this event, if it sent one.

    The robot phrases these for a lock screen (e.g. "Mostly barking, holding
    steady"), so the relay never composes its own wording — it just carries
    whichever field the robot used.
    """
    for key in ("headline", "message", "summary", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_level4(payload: dict) -> bool:
    """True if this guardian row sits at escalation level 4 or above."""
    try:
        return int(payload.get("escalation_level")) >= 4
    except (TypeError, ValueError):
        return False


def _resolve_token_type(app_type: str, enabled_types: list) -> Optional[str]:
    """Pick the notification type to send this device, or None to skip it.

    Prefers the dedicated type; falls back only when the device has not
    registered it. The resolved type is what goes in data.type, so a device
    never receives a type string its build cannot dispatch on.
    """
    if app_type in enabled_types:
        return app_type
    fallback = _APP_TYPE_FALLBACKS.get(app_type)
    if fallback and fallback in enabled_types:
        return fallback
    return None

_BEHAVIOR_TO_TYPE = {
    "sit": "sit",
    "sitting": "sit",
    "laydown": "lieDown",
    "lie_down": "lieDown",
    "down": "lieDown",
    "come": "stand",
    "stand": "stand",
    "bark": "bark",
}


def _credentials_path() -> Path:
    configured = get_settings().fcm_credentials_path
    if configured:
        return Path(configured)
    return Path(__file__).parent.parent.parent / "data" / "firebase-service-account.json"


def _load_credentials() -> Optional[dict]:
    """Load the service-account JSON once; None (with one log line) if absent."""
    global _credentials, _credentials_loaded
    if _credentials_loaded:
        return _credentials
    _credentials_loaded = True
    path = _credentials_path()
    try:
        with open(path) as f:
            _credentials = json.load(f)
        logger.info(f"[PUSH] FCM credentials loaded (project={_credentials.get('project_id')})")
    except FileNotFoundError:
        logger.warning(f"[PUSH] No FCM credentials at {path} — push notifications disabled")
    except Exception as e:
        logger.error(f"[PUSH] Failed to load FCM credentials: {e}")
    return _credentials


def is_configured() -> bool:
    return _load_credentials() is not None


async def _get_access_token() -> Optional[str]:
    """OAuth2 access token for FCM, cached until shortly before expiry."""
    global _access_token, _access_token_expires_at
    creds = _load_credentials()
    if not creds:
        return None

    async with _token_lock:
        if _access_token and time.time() < _access_token_expires_at:
            return _access_token

        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": creds["client_email"],
                "sub": creds["client_email"],
                "aud": creds["token_uri"],
                "scope": _OAUTH_SCOPE,
                "iat": now,
                "exp": now + 3600,
            },
            creds["private_key"],
            algorithm="RS256",
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                creds["token_uri"],
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        if resp.status_code != 200:
            logger.error(f"[PUSH] OAuth token exchange failed: {resp.status_code} {resp.text[:200]}")
            return None
        body = resp.json()
        _access_token = body["access_token"]
        _access_token_expires_at = time.time() + body.get("expires_in", 3600) - _TOKEN_REFRESH_MARGIN
        return _access_token


def _build_notification(row_type: str, payload: dict) -> Optional[tuple[str, str, str]]:
    """Map an activity row to (app_type, title, body) — None means don't notify.

    Mirrors the app's _activityEventToNotification (contract table 2026-07-30).
    """
    if row_type == "bark":
        emotion = payload.get("emotion") or ""
        title = f"Barking Detected ({emotion})" if emotion else "Barking Detected"
        return ("bark", title, "")

    if row_type == "treat_dispensed":
        return ("treatDispensed", "Treat Dispensed", "")

    if row_type == "coach_reward":
        trick = payload.get("trick") or payload.get("behavior") or ""
        title = f"{trick.capitalize()} rewarded" if trick else "Trick rewarded"
        return ("coachReward", title, "")

    if row_type == "guardian_alert":
        reason = payload.get("reason") or payload.get("alert_type") or ""
        title = f"Guardian: {reason}" if reason else "Guardian Alert"
        return ("alert", title, "")

    if row_type == "guardian":
        # sg_summary and panic_alert are folded into the guardian activity type
        # (contract 2026-08-31); payload["event"] carries which one this row is.
        source = str(payload.get("event") or "").lower()
        action = str(payload.get("action") or "").lower()

        if source == "panic_alert":
            # Push unless the severity is explicitly known-quiet — see
            # _PANIC_QUIET_SEVERITIES for why this fails OPEN.
            severity = str(payload.get("severity") or "").lower()
            if severity in _PANIC_QUIET_SEVERITIES:
                return None
            return ("panicAlert", "Panic alert", _panic_text(payload))

        if source == "sg_summary":
            # A level-4 escalation is the only summary worth waking the phone for;
            # status_pull never reaches here (it is not persisted at all).
            if action != "level4_escalation" and not _is_level4(payload):
                return None
            return ("sgSummary", "Guardian: escalation", _panic_text(payload))

        if action in _GUARDIAN_LIFECYCLE_ACTIONS:
            return None

        # Plain guardian rows: the robot reports escalations as
        # action="intervention" + escalation_level, NOT as a separate
        # sg_summary/level4_escalation event (no such row has ever been
        # written). Route a level-4 intervention to the escalation push so the
        # ceiling case is not delivered as a generic "Guardian Alert".
        if _is_level4(payload):
            return ("sgSummary", "Guardian: escalation", _panic_text(payload))
        return ("alert", "Guardian Alert", _panic_text(payload))

    if row_type == "mission_started":
        return ("missionStarted", "Mission Started", "")

    if row_type == "mission_completed":
        if payload.get("success") is False:
            return ("missionFailed", "Mission Failed", "")
        return ("missionCompleted", "Mission Completed", "")

    if row_type == "behavior_flag":
        behavior = str(payload.get("behavior") or "").lower()
        app_type = _BEHAVIOR_TO_TYPE.get(behavior, "alert")
        pretty = behavior.replace("_", " ").title() if behavior else "Behavior"
        return (app_type, f"{pretty} Detected", "")

    if row_type == "low_battery":
        return ("lowBattery", "Low Battery", "")

    return None


def _resolve_dog_name(dog_id: Optional[str], payload: dict) -> Optional[str]:
    name = payload.get("dog_name")
    if name:
        return str(name)
    if not dog_id:
        return None
    try:
        dog = get_dog_by_id(dog_id)
        return dog.get("name") if dog else None
    except Exception:
        return None


async def _send_fcm(token: str, project_id: str, access_token: str, message: dict) -> None:
    """Send one FCM v1 message; prune the token row on 404/410/UNREGISTERED."""
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                json={"message": {**message, "token": token}},
            )
    except Exception as e:
        logger.warning(f"[PUSH] FCM send failed (network): {e}")
        return

    if resp.status_code == 200:
        return
    if resp.status_code in (404, 410) or "UNREGISTERED" in resp.text:
        delete_push_token(token)
        logger.info(f"[PUSH] Pruned stale FCM token ...{token[-8:]} ({resp.status_code})")
    else:
        logger.warning(f"[PUSH] FCM send failed: {resp.status_code} {resp.text[:200]}")


async def send_push(
    user_id: str,
    app_type: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
) -> str:
    """Generic FCM send to every registered device of a user that enabled `app_type`.

    Returns an outcome string: sent | no_tokens | push_disabled | error. Never raises.
    `image_url` (absolute) attaches a still: Android renders it natively; iOS needs the
    app's Notification Service Extension (mutable-content) to display it.
    """
    try:
        if not is_configured():
            return "push_disabled"
        # Resolve the type per device: a build that has not registered the dedicated
        # type still gets the alert under a fallback type it understands.
        by_type: dict[str, list[dict]] = {}
        for t in list_push_tokens_for_user(user_id):
            resolved = _resolve_token_type(app_type, t["enabled_types"])
            if resolved:
                by_type.setdefault(resolved, []).append(t)
        if not by_type:
            return "no_tokens"
        access_token = await _get_access_token()
        if not access_token:
            return "error"
        project_id = _load_credentials()["project_id"]

        base_data = {k: str(v) for k, v in (data or {}).items()}
        notification = {"title": title, "body": body}
        aps = {"sound": "default"}
        apns: dict = {"payload": {"aps": aps}}
        android: dict = {"priority": "high"}
        if image_url:
            notification["image"] = image_url
            android["notification"] = {"image": image_url}
            aps["mutable-content"] = 1
            apns["fcm_options"] = {"image": image_url}

        sends = []
        for resolved_type, tokens in by_type.items():
            # data.type is the type this device registered for, so the app can always dispatch on it
            message = {"notification": notification, "android": android, "apns": apns,
                       "data": {**base_data, "type": resolved_type}}
            if resolved_type != app_type:
                logger.info(f"[PUSH] {app_type} delivered as '{resolved_type}' for {len(tokens)} device(s) of user {user_id}")
            sends.extend(_send_fcm(t["device_token"], project_id, access_token, message) for t in tokens)

        results = await asyncio.gather(*sends, return_exceptions=True)
        errors = sum(1 for r in results if isinstance(r, Exception))
        logger.info(f"[PUSH] {app_type} sent to {len(results) - errors}/{len(results)} device(s) for user {user_id}")
        return "sent" if errors < len(results) else "error"
    except Exception as e:
        logger.error(f"[PUSH] send_push failed: {e}")
        return "error"


async def notify_activity_event(
    user_id: str,
    row_type: str,
    dog_id: Optional[str],
    payload: Optional[dict],
    event_id: str,
) -> None:
    """Push a freshly ingested activity event to the owner's registered devices.

    Fire-and-forget (asyncio.create_task at the ingest sites) — must never raise
    into WS forwarding. Only devices whose enabled_types contains the mapped
    app type get a message. Thin wrapper over send_push since 2026-11-06.
    """
    try:
        if not is_configured():
            return
        payload = payload if isinstance(payload, dict) else {}
        mapped = _build_notification(row_type, payload)
        if not mapped:
            return
        app_type, title, body = mapped

        dog_name = _resolve_dog_name(dog_id, payload)
        if dog_name:
            body = f"{dog_name}: {body}" if body else dog_name

        await send_push(
            user_id=user_id, app_type=app_type, title=title, body=body,
            data={"type": app_type, "dog_id": dog_id or "", "event_id": str(event_id)},
        )
    except Exception as e:
        logger.error(f"[PUSH] notify_activity_event failed: {e}")
