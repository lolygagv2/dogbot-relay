# Relay Brief — from App Claude (2026-07-27)

Context: robot (tb5, `60e526f`+`fbc5d10`) and app (Build 146, `14be154`) just
shipped the local-mode overhaul: local bus-event delivery, coach
cancel-and-replace, robot-authoritative mode sync, network_state hotspot UX.
These are the relay-side items, newest first.

## 1. FEED_WORTHY_EVENTS: coaching_started / coach_progress (answer needed)

Confirm whether `coaching_started` and `coach_progress` are in
FEED_WORTHY_EVENTS (the replay buffer).

- If NOT (expected): reply to App Claude so the app adds them to its
  transient watermark carve-out (`isTransientMsg` in websocket_client.dart —
  same treatment as `controller_*` B127 and `audio_state` B140). A live
  event type that gets a seq but is never replayed WILL eventually be
  silently eaten on robots whose persisted watermark runs ahead — the
  "renders on one robot only" bug class.
- Either way: `coach_progress` fires at ~2Hz during the watching stage. It
  must not be written to durable history; add an exclusion or throttle if
  the relay persists forwarded events by default.

## 2. Persist network_state per robot (new robot event)

Robot sends `network_state` on every relay connect and after every WiFi
rejoin (payload: mode wifi|ap, ssid, ip, signal, and `local_ap`
{ssid WIMZ-<serial>, password wimzsetup, ip, api, ws}).

The app caches it per device for the offline "join the robot's hotspot"
prompt — but an app session that connects AFTER the robot sent it misses it.
Please store the latest per device_id and deliver it to each newly
connecting app session (like a retained message). This was flagged as the
relay item in the robot's APP_BRIEF_LOCAL_MODE_2026-07-26.

## 3. Confirm local_mode command round trip

App's new Settings → "Switch to Local Mode" flow: app sends `local_mode`
command over relay → robot answers with `local_mode_starting` (carries
local_ap). Presumably rides the generic command/event forwarding — please
confirm nothing filters either direction.

## Carried debts (older contracts, still open as far as the app knows)

4. **Row-level dog_id on activity rows** (treat_dispensed / mission /
   guardian / bark / behavior_flag) + dog_name/id_method in payload — per
   EVENT_DOG_ATTRIBUTION_CONTRACT_2026-07-13 (in the app repo's .claude/).
   Robot already stamps events; the relay rows are what the app's history
   hydration reads. This automatically fixes per-dog Activity graph
   attribution (the app currently must include untagged events).
5. **audio_state → FEED_WORTHY_EVENTS** (carried from 2026-07-12) — ⚠️ do
   NOT do this without telling App Claude first: the app's B140 carve-out
   assumes audio_state is never replayed. If it becomes replayable, the app
   needs a buffered-frame guard before the relay flips it, or stale
   playback states will flap the music UI on reconnect.
6. **POST /dogs upsert-by-client-id** (owed since 2026-07-12) — relay
   minting its own ids caused duplicate dog profiles per login and wrong
   voice manifests. App has a name-match merge as a stopgap.
7. **Auth contract drift** (status unknown to app): register endpoint 500
   (signup broken), /validate 404, no user_id claim in token (app falls
   back to JWT sub). If any were fixed since, ignore.

Replies/questions: leave a brief in the app repo
(~/wimzapp/.claude/) or have Morgan relay it.
