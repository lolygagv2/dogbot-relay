# Relay Brief for the App — robot snapshots in the Photos gallery (2026-11-07)

**From:** relay-side Claude. **Re:** photos the robot takes for the voice agent or a Claude
connector (and any other relay `take_photo`) should show up in the app's Photos gallery.
Base URL `https://api.wimzai.com`. REST uses the app JWT. Error bodies are `{"detail": ...}`.

## Why

Today the app's Photos gallery is only what the phone itself saved. When the voice agent or a
connector asks the robot for a photo, the robot captures and uploads it, and the relay sends
the app a `command_result`, but the app ignores it. If the app is closed, that message is lost.
Example: photos at 12:53 and 13:19 UTC on 2026-11-07 never reached the gallery (still 28).

The relay now **records each of these photos** for the dog, so the app can both save them
live and catch up on ones it missed.

## What the relay does now

- Every successful, final `take_photo` sent through the relay's command dispatcher (voice
  agent, Claude connectors, `POST /api/dogs/{dog_id}/command`, admin) becomes a row in the
  dog's photo list. One row per `command_id`. Repeats never create duplicates.
- Photos the app takes itself over `/ws/app` (no relay `command_id`) are **not** recorded.
  Keep saving those the way you do now.
- The relay keeps the image file for **7 days**. Copy it into the phone's gallery. Don't
  rely on the relay as long-term storage.

## 1. Live: save on `command_result` (app is open)

The relay already sends this over `/ws/app`. It now has one new field, `gallery_photo_id`:

```json
{
  "type": "command_result",
  "command_id": "9dbb42ab-…",
  "command": "take_photo",
  "dog_id": "01a183fe-…",
  "device_id": "wimz_robot_05",
  "gallery_photo_id": "photo_cmd_9dbb42ab-…",
  "ok": true,
  "result": { "image_url": "/api/media/download/wimz_robot_05_53be4a56.jpg", "...": "..." },
  "error": null,
  "timestamp": "…"
}
```

When `command == "take_photo"` **and** `gallery_photo_id` is a non-null string:

1. If the gallery already has an item with that id, stop.
2. Download the image: `GET /api/dogs/{dog_id}/media/{filename}` with the JWT, where
   `{filename}` is the last path segment of `result.image_url`. (Same URL as `photo_url` in §2.)
3. Save it to the gallery under that `gallery_photo_id` for `dog_id`, with the capture time.

If `gallery_photo_id` is `null` (failed or interim result, or not a relay photo), do nothing.
An interim result has `result.deferred: true`; the final one follows as another `command_result`.

## 2. Catch-up: pull missed photos (app opens / returns to foreground / opens Photos)

`GET /api/dogs/{dog_id}/photos?since=<ISO 8601>` → newest first:

```json
[
  {
    "id": "photo_cmd_9dbb42ab-…",
    "dog_id": "01a183fe-…",
    "photo_url": "/api/dogs/01a183fe-…/media/wimz_robot_05_53be4a56.jpg",
    "is_profile_photo": false,
    "captured_at": "2026-11-07T13:19:28.48+00:00"
  }
]
```

- `since` is optional and exclusive (strictly after). A time with no timezone is read as UTC.
  Send the newest `captured_at` you have already saved for that dog. On first run, omit it.
- For each item whose `id` is not in the gallery: prefix `photo_url` with the base URL,
  `GET` it with the JWT, save it under `id`. Same dedupe key as §1.
- Do this for every dog the user can see (viewers may read).
- Download errors: `404 still_not_found` = expired on the relay (over 7 days old). Skip it
  and remember the id so you don't retry. `403` = not this user's robot. Skip.
- The same endpoint also lists photos added with `POST /api/dogs/{dog_id}/photos`
  (`is_profile_photo` may be true, `photo_url` may be a full external URL). Saving those
  is optional; profile photos are unchanged.

## 3. Nice to have

- Show a small "taken by WIM-Z assistant" label. Every relay-recorded id starts with
  `photo_cmd_`.
- Refresh the gallery when §1 saves a photo while the Photos screen is open.

## Test it

1. Ask the voice agent (or Claude) "take a photo of the dog" with the app open → it appears in
   Photos within a second or two.
2. Close the app, ask Claude for a photo, reopen the app → it appears after catch-up.
3. Repeat step 1 and reopen → no duplicates.

Relay files: `app/services/photo_gallery.py`, hook in `app/routers/websocket.py`
(`command_result` handler), `since` in `app/routers/dogs.py` (`list_dog_photos`),
tests `tests/test_photo_gallery.py`.
