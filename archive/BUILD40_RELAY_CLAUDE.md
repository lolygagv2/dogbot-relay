# Build 40 - Relay Claude Instructions

**Date:** February 2, 2026
**Based on:** Build 39 test results cross-referenced across all three Claude instances
**Build 39 test window:** 21:01-21:22 local / 02:01-02:22 UTC

---

## Status

Relay performed well in Build 39. Message routing works correctly. The MP3 413 error was an nginx config issue (resolved manually by Morgan — `client_max_body_size 50M` was in the wrong server block). Your upload endpoint code is correct and working.

You have two small code tasks.

**Important context:** Relay runs as a local codebase on Morgan's computer. You write code, Morgan pushes to GitHub, then pulls on the AWS Lightsail server. Any server/nginx config is done manually by Morgan — you only handle application code.

---

## ✅ RESOLVED: MP3 Upload 413 Error

**What happened:** `client_max_body_size 50M` was in the `default` server block (port 80, `server_name _`), not the `wimz-relay` server block (`server_name api.wimzai.com`, port 443). All API traffic goes through the wimz-relay block via Certbot's HTTPS redirect, so the 50M limit was never applied.

Morgan added the directive to the correct server block and reloaded nginx. Curl now reaches FastAPI (returns 401 without auth, not 413).

The app is now getting 422 because it's missing the `device_id` form field. App Claude is fixing this. Your upload endpoint signature is correct:

```python
async def upload_music(
    file: UploadFile = File(...),
    dog_id: str = Form(...),
    device_id: str = Form(...),   # App wasn't sending this
    user: dict = Depends(get_current_user)
)
```

**No code changes needed from you on this item.**

---

## P1-L1: Add Warning Log for Empty Mission Data

### The Problem

Robot sends `mission_progress` with all null values. Relay routes it faithfully (correct behavior), but we should log a warning so this is visible without grepping.

### What to Do

In your WebSocket handler where robot events are forwarded to the app, add a check when the event type is `mission_progress`:

```python
# When forwarding mission_progress from robot to app
if msg_type == "mission_progress":
    data = message.get("data", {})
    if not data.get("status") and not data.get("action"):
        logger.warning(
            f"[MISSION] Empty progress data from {device_id} — "
            f"fields: status={data.get('status')}, action={data.get('action')}, "
            f"stage={data.get('stage_number')}/{data.get('total_stages')}"
        )
```

This is purely diagnostic — **still forward the message**. Don't block it.

---

## P1-L2: Verify /missions 404 Is Expected

### Context

```
02:01:34 INFO: "GET /missions HTTP/1.1" 404 Not Found
```

App calls `GET /missions` on the relay. The relay correctly returns 404 because this endpoint doesn't exist here. Robot is adding its own `/missions` endpoint.

### Decision

**Do nothing.** The 404 is correct. Don't add this endpoint. If Morgan explicitly requests a relay-side proxy for missions later, it's a simple addition — but for now, leave it.

---

## DO NOT Do These Things

1. **Do NOT add mission data validation that blocks forwarding.** Warn in logs, but always forward.
2. **Do NOT add schedule endpoints.** Schedules live on the robot.
3. **Do NOT change WebSocket message routing logic.** It works.
4. **Do NOT change the upload endpoint.** It's correct. The 422 is app-side.

---

## Testing Checklist

After Build 40 deploys:

1. Start mission from app → Check relay logs for the new warning if robot sends null data
2. Upload MP3 from app → Should succeed end-to-end (nginx fixed, app adding `device_id`)
3. Verify `schedule_created` events route from robot to app (already works, just confirm)

---

*Build 40 — Add one diagnostic log line. That's it. Your relay is solid.*
