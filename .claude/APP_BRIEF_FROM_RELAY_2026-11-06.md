# Relay Brief for the App — Dec 9 AI Summit build (2026-11-06)

**From:** relay-side Claude. **Re:** event store, settings, AI connectors, voice agent.
Relay commits `c1133f3`, `3083fe5` plus the 2026-11-06 amendments (connectors UI,
mission drafts, agent transcript, songs as sounds). **Awaiting Morgan's manual Lightsail
deploy.** Base URL `https://api.wimzai.com`, all REST below uses the app JWT unless noted.
Dog access: owner/caretaker may write, viewer may read.

Error bodies are uniform: `{"detail": {"code": "...", ...}}`. Codes you will see:
`guard_denied` (403, with `guard` and `reason`), `treat_cap_reached` (429, `used`, `cap`,
`resets_at`), `treat_rate` (429), `robot_offline` (409), `command_not_allowed` (403),
`version_mismatch` (409, `current_version`), `invalid_value` (422).

---

## 1. Daily roll-up and events

```
GET /summary/{dog_id}/{date}         date = YYYY-MM-DD | today | yesterday (dog timezone); ?refresh=1
→ {"dog_id","date","tz","counts":{bark_episode,intervention,settle,pacing,departure,arrival,treat,music,mission_run},
   "totals":{bark_s,pacing_s,music_s,treats,treat_cap,alone_s,interventions_by_kind{},missions_success,missions_fail},
   "time_to_settle":{n,unresolved,median_s,mean_s,min_s,max_s},
   "top_episodes":[{event_id,ts,duration_s,bark_count,severity,settle_s,interventions[],image_url}],
   "first_event_ts","last_event_ts","event_count"}

GET /api/dogs/{dog_id}/events?since=&until=&type=bark_episode,treat&limit=100&order=desc
→ {"dog_id","events":[{event_id,type,ts,duration_s,data{},source,device_id}],"count"}     (status excluded unless type=status)

GET /api/dogs/{dog_id}/status
→ {"dog_id","device_id","robot_online","mode","battery","last_status":{battery,mode,treats_remaining,docked,light_level},
   "last_status_ts","treats_today","treat_cap","quiet_hours":{enabled,start,end,active},"timezone","replay_active"}
```

`image_url` values are relay paths (`/api/media/download/...`); stills live 24 h.

**WS frames** (on `/ws/app`, in addition to today's):

```json
{"type":"event","event":{event_id,type,dog_id,device_id,ts,duration_s,data,source}}
{"type":"activity_event", ...}            // unchanged shape; new events are mirrored here too
{"type":"command_result","command_id","dog_id","device_id","ok","result","error","timestamp"}
{"type":"mission_draft","dog_id","draft_id","draft":{...},"actor":"connector:<client_id>|agent","created_at"}
{"type":"agent_transcript","dog_id","role":"user|agent","text","ts"}
{"type":"replay_jump","dog_id","virtual_time"}   {"type":"replay_reset","dog_id"}     // demo tooling only
```

Push notifications may now carry an image (FCM `notification.image`, APNs
`mutable-content: 1` + `fcm_options.image`). iOS needs a Notification Service Extension
to render it; without one the push is text-only. `data` gains `rule_id`, `image_url`,
`clip_url` (clip is a relay path to an mp4 to open in-app).

---

## 2. Settings (shared with assistants and the voice agent)

```
GET  /api/dogs/{dog_id}/settings                 → {"dog_id","settings":{key:{value,version,updated_at,updated_by}}}
GET  /api/dogs/{dog_id}/settings/{key}
PUT  /api/dogs/{dog_id}/settings/{key}           {"value":{...},"expected_version":n?}   → the row; 409 on version mismatch
POST /api/dogs/{dog_id}/settings/{key}/reset
GET  /api/dogs/{dog_id}/settings/audit?limit=50  → {"entries":[{id,key,actor,action put|reset|denied,old_value,new_value,reason,created_at}]}
```

Keys / value shapes:

- `timezone`: IANA string (default `America/New_York`) — drives day boundaries of the roll-up.
- `quiet_hours`: `{enabled, start:"22:00", end:"07:00"}` — assistants/agent may only widen it; the app is unrestricted.
- `treat_schedule`: `{daily_cap:10, default_multiplier:1.0, max_per_hour:3, windows:[{start,end,multiplier}]}` — assistants may only lower the cap.
- `sg_sensitivity`: `{level: low|medium|high, min_episode_s, escalate_after_s}`
- `music_plan`: `{enabled, volume 0-100, on_departure:{track, delay_s, duration_min}, schedule:[{time,track,duration_min}]}` — `track` is a sound id of kind `song`.
- `notify_rules`: `{rules:[{id, enabled, event_type, when{min_duration_s,max_duration_s,severity_in,result_in,source_in,kind_in,data_eq}, cooldown_s, attach none|still|clip, app_type, title, body, override_quiet}]}` — `app_type` must be one of your existing enabled_types (bark, treatDispensed, coachReward, alert, missionStarted, missionCompleted, missionFailed, lowBattery). Title/body templates accept `{dog_name} {duration_min} {bark_count} {severity} {kind} {result} {mission_type} {source} {settle_min}`.
- `missions`: `{missions:[{id,type,name,enabled,trigger:{kind schedule|departure|manual, time, delay_s, cooldown_s, days[]}, duration_s, reward_treats, goal}]}` — `departure` is the new trigger kind.
- `sounds`: `{sounds:[{id, kind sound|song, label, builtin, file, url}]}`. Songs are sounds with `kind:"song"` (builtin `song_calm_01`, `song_calm_02`).

Sound library:

```
GET  /api/dogs/{dog_id}/sounds                       metadata
POST /api/dogs/{dog_id}/sounds/{sound_id}?label=..&kind=sound|song   multipart file (wav/mp3 ≤ 5 MB) — replace/add
POST /api/dogs/{dog_id}/sounds/reset                 back to builtins
GET  /api/sounds/file/{dog_id}/{sound_id}            the bytes (JWT or robot HMAC); builtin files live on the robot → 404 here
```

---

## 3. Robot control from the app over HTTP (optional; WS commands still work)

```
POST /api/dogs/{dog_id}/command   {"command": "...", "data": {...}, "device_id"?: "...", "wait_s"?: 0-90}
→ 202 {"command_id","device_id","command_sent","delivered","simulated","guard":{treats_used,treat_cap,multiplier}}
  with wait_s: 200 and "result":{ok,result,error} when the robot's command_result arrives, else 202 with "timed_out":true
GET  /api/dogs/{dog_id}/command/{command_id}   → the stored command_result (10 min) or 404
```

Commands: `dispense_treat`, `set_mode` (idle|silent_guardian|coach|mission), `play_sound {sound_id}`,
`start_mission {mission_id|mission_type}`, `go_to {target}`, `take_photo {with_hud}`, `standby`,
`run_trick {trick}`. Treat cap and rate are enforced here (429s above).

---

## 4. AI connectors screen

```
GET  /api/dogs/{dog_id}/ai/connectors
→ {"connector_url": "https://api.wimzai.com/mcp",
   "assistants": [{"client_id","client_name","scopes":["read","settings"],"connected_at","last_used_at","active_tokens"}]}
POST /api/dogs/{dog_id}/ai/connectors/{client_id}/revoke     → {"revoked": n}
POST /api/dogs/{dog_id}/ai/revoke_all                        → {"revoked": n}
GET  /api/dogs/{dog_id}/ai/audit?limit=50
→ {"entries":[{id,ts,actor,client_name,tool,dog_id,outcome ok|refused|denied_scope|error,input,result_summary,duration_ms}]}
```

Assistants are per user (the connector signs in as the owner); `dog_id` in the path is for
routing and access checks. Setup flow for the owner in Claude/ChatGPT/Grok/Perplexity:
add a custom connector with `connector_url`, sign in with WIM-Z email + password on our
page, tick the permissions (settings / missions / physical; read is implicit; the camera
can never be turned on from a connector).

---

## 5. Mission drafts (assistant proposes, owner approves in the app)

Assistants and the voice agent never schedule directly. They create a pending draft; the
app shows it (WS `mission_draft` above, or list) and the owner decides:

```
GET /api/dogs/{dog_id}/missions/drafts?status=pending|approved|declined|all
→ {"drafts":[{draft_id, dog_id, actor, status, draft:{id,type,name,enabled,trigger,duration_s,reward_treats,goal}, warnings[], created_at, decided_at, mission_id}]}
PUT /api/dogs/{dog_id}/missions/drafts/{draft_id}   {"decision": "approve" | "decline"}
→ approve: appends the mission to the `missions` setting (actor app:<user>) and returns {"status":"approved","mission_id"}
   decline: {"status":"declined"}
```

A departure-triggered mission starts within 15 minutes of approval even if no departure
event arrives (relay-side timer), then on each departure after `delay_s`.

---

## 6. Voice agent from the phone

```
POST /agent/utterance    (JWT)  multipart file=<wav ≤ 5 MB / 30 s>, dog_id
→ {"text_in","text_out","audio_url","clip_id","expires_at","tools_used":[...],"refused":[...]}   header x-agent-timing
```

Play `audio_url` (mp3, expires in 15 min). The relay never stores the uploaded audio.
Every utterance and reply is also broadcast as WS `agent_transcript` so a live transcript
can be shown. Physical actions (treat, go_to, trick, photo, standby) are honoured for
owner/caretaker JWTs only.

---

## Demo tooling (admin JWT only)

`/admin/replay` load/start/pause/speed/jump/reset replays `seeds/demo_day.json` for
`demo_dog` through the same pipeline, so the app sees `event`, `activity_event`, pushes,
and roll-ups exactly as from a robot. Expect `replay_jump` / `replay_reset` frames.

---

## 7. PHOTO ACCESS (added 2026-11-06, later the same day)

- Two more connector scopes appear on the consent screen and in `GET …/ai/connectors`
  `scopes`: `photos` (see snapshots WIM-Z already took) and `camera` (ask WIM-Z for one new
  snapshot, no HUD, max one per 30 s per assistant, refused in quiet hours). Both are
  unticked by default. Consent text: "Photos: see snapshots WIM-Z already took. Camera:
  ask WIM-Z to take a new snapshot. Never live video."
- Stills: `GET /api/dogs/{dog_id}/events/{event_id}/photo` (JWT) returns the image attached
  to an event (404 no photo, 410 expired); `GET /api/dogs/{dog_id}/media/{filename}` serves
  a fresh `take_photo` still by its download name. Treat stills live 7 days, other stills
  24 h. Robots now attach a still to every treat event and to bark alerts.
- `mcp_audit` (…/ai/audit) will show `get_event_photo` and `take_photo` calls per assistant.
