# Relay Brief for the Robot — Dec 9 AI Summit build (2026-11-06)

**From:** relay-side Claude. **Re:** event store, shared settings, robot control, voice agent.
Relay commits `c1133f3` (event store/settings/replayer), `3083fe5` (MCP), plus the
amendments of 2026-11-06 (command_result, settings fetch on connect, songs as sounds,
status fields). **Awaiting Morgan's manual Lightsail deploy.** Base URL `https://api.wimzai.com`.

Everything below is additive. Existing WS behaviour (`/ws/device`, `status_update`,
`activity_event`, schedules, WebRTC) is unchanged. Where the robot used to report barks,
treats and missions as `activity_event` frames, move those to `POST /events` so they are
not double-counted in the app timeline.

---

## 1. Authentication for the new HTTP endpoints

Same HMAC identity as `POST /api/device/register`:

```
Authorization: HMAC-SHA256 <hex sig>
X-Device-ID: <device_id>
X-Timestamp: <unix seconds>            # optional; sig over device_id+timestamp or device_id
```

`sig = HMAC_SHA256(device_secret, device_id + timestamp)` (the relay also accepts
`device_id:timestamp` and plain `device_id`). The device must be paired to an owner, and
every `dog_id` you reference must belong to that owner, or the event/request is rejected
(`status` events may omit `dog_id`, see §2).

---

## 2. `POST /events` — typed event ingest (replaces activity_event for these types)

Batch of 1..200 events, idempotent on `event_id` (use a UUID; resend freely on retry).

```json
POST /events
{"events": [
  {"event_id": "uuid", "type": "bark_episode", "dog_id": "<dog>", "ts": "2026-12-09T14:03:11-05:00",
   "duration_s": 94, "data": {"bark_count": 18, "severity": "med", "peak_db": 78, "trigger": "door",
                               "image_url": "/api/media/download/<file>"}}
]}
→ 200 {"accepted": ["uuid"], "duplicate": [], "rejected": [{"event_id": "...", "reason": "..."}]}
```

### Verified `EventIn` shape (contract fix 2026-11-06)

This is the exact per-event shape the relay validates (`app/models.py: EventIn`), matching
what the robot agent found in the robot's sender:

| field | type | required | notes |
|---|---|---|---|
| `event_id` | string, 1-128 chars | yes | UUID; idempotency key |
| `type` | enum (table below) | yes | |
| `dog_id` | string, 1-128 chars | **yes, except `type: "status"`** | must belong to the device owner |
| `ts` | string | yes | ISO-8601 with offset (naive = UTC); epoch s/ms also accepted |
| `duration_s` | number 0-86400 | per type | |
| `data` | object | no (default `{}`) | per-type fields below |
| `device_id` | string | no | **ignored for robot callers**; the relay uses the HMAC `X-Device-ID` |

**Status is device-scoped.** Send `status` without a `dog_id` (it describes the robot, not a
dog). The relay stores it once, with no dog, keyed by your device_id; there are no per-dog
copies, and the response lists your `event_id` as usual. Every dog paired with your robot
sees it: the dog status the app, connector and agent read (`GET /api/dogs/{dog}/status`) is
the latest status from the dog's paired robot, looked up by device_id. Sending a `dog_id` on
status still works and is still checked against the owner, but isn't needed.

`ts` is ISO-8601 with offset (naive = UTC); `device_id` is taken from the HMAC identity.
`data.image_url` / `data.clip_url` must be relay media paths (`/api/media/download/...`,
upload a still or clip first via the existing `POST /api/media/upload`); anything else is
stripped.

| type | required | optional / notes |
|---|---|---|
| `bark_episode` | `duration_s`, `data.bark_count` (int), `data.severity` `low\|med\|high` | `peak_db`, `trigger`, `image_url`, `clip_url`. Send when the episode ends. Keep the `event_id`: interventions and the settle reference it. |
| `intervention` | `data.kind` `sound\|voice\|treat\|music\|approach` | `episode_id` (the bark_episode event_id), `sound_id`, `result` `settled\|no_change\|escalated` |
| `settle` | none | `episode_id` (strongly preferred; the relay computes time-to-settle from it) |
| `pacing` | `duration_s` | `zone`, `distance_m` |
| `departure` / `arrival` | none | `method` `door\|schedule\|manual\|detected`. Departure arms departure-triggered missions and music; arrival cancels them. |
| `treat` | `data.source` `auto\|app\|connector\|agent\|schedule` | `command_id` (echo the relay's command_id when the dispense was relay-issued, see §4), `count` (default 1), `multiplier` |
| `music` | `data.action` `start\|stop`, `data.track` (a song sound_id) | `duration_s` on stop, `trigger` |
| `mission_run` | `data.mission_id` or `data.mission_type`, `data.result` `success\|fail\|aborted` | `duration_s`, `trigger` `schedule\|departure\|manual\|agent` |
| `status` | `data.battery` (int), `data.mode`; **no `dog_id` needed** | `treats_remaining`, `wifi_rssi`, **`docked`** (bool), **`light_level`** (0-255). Send every few minutes and on mode change. Never rolled up or pushed. `treats_remaining <= 3` triggers the agent's low-hopper speech; a transition into `silent_guardian` triggers the "re-armed" speech. |

The relay mirrors these into the app timeline, evaluates the owner's notify rules, and
builds the daily roll-up (`GET /summary/{dog}/{date}`).

---

## 3. Settings: fetch on connect, apply `settings_updated`

On connect (after you receive the existing `profiles` message with the dog ids), fetch
per dog:

```
GET /api/dogs/{dog_id}/settings        (HMAC)
→ {"dog_id": "...", "settings": {"<key>": {"value": {...}, "version": n, "updated_at": "...", "updated_by": "..."}}}
```

Keys and shapes (all have defaults):

- `timezone`: `"America/New_York"`
- `quiet_hours`: `{"enabled": true, "start": "22:00", "end": "07:00"}`
- `treat_schedule`: `{"daily_cap": 10, "default_multiplier": 1.0, "max_per_hour": 3, "windows": [{"start": "07:00", "end": "09:00", "multiplier": 1.5}]}` — the robot enforces the cap on its own auto-dispenses; the relay enforces it on relay-issued ones.
- `sg_sensitivity`: `{"level": "low|medium|high", "min_episode_s": 5, "escalate_after_s": 60}`
- `music_plan`: `{"enabled": true, "volume": 40, "on_departure": {"track": "song_calm_01", "delay_s": 60, "duration_min": 30}, "schedule": [{"time": "13:00", "track": "song_calm_02", "duration_min": 20}]}` — `track` is a **song sound_id** (see `sounds`). On departure the relay also sends `play_music` (§4), so either side may start it; de-duplicate by track.
- `notify_rules`: relay-only (push rules). Ignore.
- `missions`: `{"missions": [{"id": "m_recall", "type": "recall", "name": "...", "enabled": true, "trigger": {"kind": "schedule|departure|manual", "time": "10:00", "delay_s": 300, "cooldown_s": 7200, "days": ["mon",...]}, "duration_s": 120, "reward_treats": 1, "goal": "..."}]}` — `schedule` missions run on the robot's clock; `departure` missions are started by the relay (`start_mission` command) after `delay_s`, or 15 min after creation if no departure arrives; `manual` only on command.
- `sounds`: `{"sounds": [{"id": "chime", "kind": "sound", "label": "Calm chime", "builtin": true, "file": "chime.wav", "url": "/api/sounds/file/<dog>/chime"}, {"id": "song_calm_01", "kind": "song", ...}]}` — builtin ids `chime`, `whistle`, `shush`, `good_dog` (sounds) and `song_calm_01`, `song_calm_02` (songs) are files the robot already has; `builtin: false` entries were uploaded by the owner: download them from `url` (HMAC) and cache by `id`.

Afterwards the relay pushes every change over the WS:

```json
{"type": "settings_updated", "dog_id": "...", "key": "treat_schedule", "value": {...}, "version": 7, "timestamp": "..."}
{"type": "sound_updated",    "dog_id": "...", "sound_id": "calm_voice", "url": "/api/sounds/file/<dog>/calm_voice", "file": "calm_voice.wav"}
```

Apply per key; `version` is monotonic per key, ignore anything older than what you hold.

---

## 4. Commands from the relay (app, assistants, voice agent, triggers)

All arrive as the existing envelope; **every** one carries a `command_id` and `source`:

```json
{"type": "command", "command": "<name>", "data": {...}, "command_id": "uuid", "source": "agent|connector:<id>|app:<user>|relay:trigger", "timestamp": "..."}
```

| command | data | expected behaviour |
|---|---|---|
| `dispense_treat` | `{source, command_id, multiplier, dog_id}` | dispense; then `POST /events` a `treat` with `data.command_id` = this `command_id` (that links it to the relay's cap ledger) |
| `set_mode` | `{mode: idle\|silent_guardian\|coach\|mission, source, timestamp}` | as today (`manual` is never sent over this path) |
| `audio` | `{file, sound_id, url, source}` | play a sound from the `sounds` list (download `url` if not cached) |
| `play_music` | `{track, duration_min, volume, trigger, source}` | start the song `track` (a song sound_id) for `duration_min` |
| `play_sound` | `{url, clip_id, kind: "tts", source}` | **voice agent speech**: download the mp3 at `url` (absolute, short-lived) and play it immediately at speech volume |
| `start_mission` | `{mission_id, mission_type, dog_id, trigger, duration_s, reward_treats}` | run the mission; report with a `mission_run` event |
| `take_photo` | `{with_hud, source, command_id}` | take a still, upload via `/api/media/upload`, and return the path in `command_result.result.image_url` (below); the relay/app keep it, the agent only gets a confirmation |
| `go_to` | `{target: dock\|bed\|kitchen\|door\|crate\|living_room\|owner, source}` | navigate; `command_result` when arrived or failed |
| `run_trick` | `{trick: sit\|down\|stay\|spin\|place\|paw\|speak, source}` | cue the trick; `command_result.result = {"trick": "...", "success": true/false}` |

### `command_result` — reply to **every** command

```json
{"type": "command_result", "command_id": "<the command's id>", "ok": true, "result": {...}, "error": null, "timestamp": "..."}
```

Send it when the command completes (treat dispensed, sound started, mission started,
photo uploaded, trick finished, navigation arrived). The relay resolves waiting callers
(the voice agent waits up to 10 s for `take_photo` and 60 s for `run_trick`), forwards
the frame to the owner's app, and keeps the last result for 10 minutes. On failure send
`ok: false` with a short `error` string. Unknown command → `ok: false, error: "unsupported"`.

---

## 5. Voice agent (robot microphone path)

Push-to-talk / wake-word clip: record one utterance (16 kHz mono WAV, ≤ 30 s, ≤ 5 MB)
and POST it with the HMAC headers:

```
POST /agent/utterance   multipart: file=<wav>, dog_id=<dog>
→ 200 {"text_in": "...", "text_out": "...", "audio_url": "https://api.wimzai.com/agent/audio/<clip>.mp3",
       "clip_id": "...", "expires_at": <unix>, "tools_used": [...], "refused": [...]}
```

Download `audio_url` and play it (same handling as `play_sound`). The clip expires after
15 minutes. The relay never stores your audio.

Agent-initiated speech (boot greeting about 20 s after connect, Silent Guardian re-arm,
low hopper) arrives as the `play_sound` command above, so no extra work beyond §4.

---

## 6. Stage robot and the demo dog

The demo replayer can be loaded with `device_id = <stage robot id>`. When that robot is
online and paired to the demo account, relay-issued commands (treats, sounds, agent speech)
go to it for real; the replayer's seed events still fill the day. Keep the demo dog's id
(`demo_dog`) in the stage robot's profiles if you want it to accept commands for it.

## Open questions for the robot side

- Which of the builtin sound/song files exist on the Pi today? I assumed `chime.wav`,
  `whistle.wav`, `shush.wav`, `good_dog.wav`, `song_calm_01`, `song_calm_02`.
- Does the robot already emit a departure/arrival signal (door, schedule)? If not, the
  app can send `departure` via the relay (it will count the same).

---

## 7. PHOTO ACCESS (added 2026-11-06, later the same day)

- Attach a still to **every `treat` event** (taken about 2 s after the dispense) and to bark
  alerts (`bark_episode`, optionally `intervention`): upload via `POST /api/media/upload`
  first, then put the returned `download_url` in `data.image_url`. The relay keeps treat
  stills 7 days and other stills 24 h (video 1 h). Uploads now land in a persistent
  directory on the relay, so links survive a relay reboot.
- `take_photo` from the relay now also comes from assistants (connector `camera` scope):
  always `with_hud: false`, at most one per 30 s per assistant, never during quiet hours
  (the relay enforces all three). Reply with `command_result` whose `result` includes the
  uploaded still, e.g. `{"ok": true, "result": {"image_url": "/api/media/download/<device>_<hex>.jpg"}}`
  within 10 s; the relay hands the image to the assistant or the voice agent. Live video
  is never reachable this way.
