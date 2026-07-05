# WIM-Z Implementation Proposal: Supervision Event Layer, Post-Hoc VLM, and Session Highlight Reel

Owner: Morgan Hill (morgan@wimzai.com)
Status: Proposal / build order. Hand to the App, Robot (Edge), and Relay Claude Code instances.
Contract: This document **defers to `WIMZ_Data_Architecture_Spec.md`** for all schema. It sequences the build and adds tables additively. It does not redesign anything in the spec. Where this proposal needs a new table or event type, the spec is bumped first (current v0.2 to v0.3), then built.

---

## 0. The spine: one timeline, two renderings

Everything below rests on a single principle. The device produces a **scored event timeline**: every observation gets a stable identity (track), a place (zone, in Vigilant mode), a confidence, and a producing model. That timeline is the source of truth.

From that one timeline, two products are rendered:

- **The session report** makes the claims in plain language ("Bezik barked 14 times, mostly 2 to 4pm near the door, calm after the third intervention").
- **The highlight reel** is the proof: the actual footage of each claimed event, cut and captioned from the same rows.

The VLM never free-watches an hour of video and invents a story. It renders from the structured rows, which carry confidence and provenance. This keeps it cheap, truthful, and defensible to a scientific reviewer. It is the same label-provenance discipline the data spec already enforces, extended to the narrative layer.

Three jobs, three layers, no overlap:

| Layer | Where | Real-time? | Job |
|-------|-------|-----------|-----|
| Detection + tracking + zones | Edge (Hailo + supervision) | Yes | Produce the scored timeline; fire treats/interventions |
| Report + failure analysis + auto-labeling | Relay (VLM, per-call) | No | Render language, audit failures, bootstrap labels |
| Highlight reel | Edge cut + Relay stitch | No | Render the proof video from the timeline |

The VLM stays entirely off the treat/intervention hot path. That path remains YOLO on Hailo, exactly as today.

---

## 1. Scope

In scope:

- A. Edge: integrate supervision (ByteTrack + zones) between Hailo output and the event writer. Collapse the per-frame firehose into tracks, zone transitions, and aggregates. Add a salience score at write time. Maintain a rolling proxy-video buffer.
- B. Relay: feed the cleaner timeline into the existing session-report VLM call. Add a selective vision path for failure analysis and a batch path for auto-labeling captured hard negatives.
- C. Highlight reel: select salient segments from the timeline, pre-cut on the edge, stitch and caption on the relay, play back in the app.
- D. App: surface the report, the reel, and the human-review queue.

Out of scope (unchanged from prior proposals):

- Always-on or conversational on-device LLM.
- Real-time changes to the detection/intervention path.
- Cross-session re-identification (handled by your QR/trained-identity scheme; ByteTrack is within-session only).
- Room-coordinate zone anchoring while driving (requires SLAM; deferred with full autonomous nav).

---

## 2. Schema changes required first (bump spec to v0.3, additive only)

Add to `WIMZ_Data_Architecture_Spec.md`, bump to v0.3, then build. No changes to existing tables.

```sql
-- Per-device zone definitions. Zones are only meaningful in stationary/Vigilant mode.
CREATE TABLE device_zone (
  zone_id        TEXT PRIMARY KEY,        -- UUIDv7
  device_id      TEXT NOT NULL REFERENCES device(device_id),
  name           TEXT NOT NULL,           -- 'door' | 'couch' | 'bowl' | ...
  polygon_json   TEXT NOT NULL,           -- JSON [[x,y],...] in frame coords for the Vigilant view
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL
);

-- A continuous track of one detected subject within a session (ByteTrack output).
-- This is the aggregate row that replaces logging every frame.
CREATE TABLE track_summary (
  track_summary_id TEXT PRIMARY KEY,      -- UUIDv7
  session_id     TEXT NOT NULL REFERENCES session(session_id),
  device_id      TEXT NOT NULL REFERENCES device(device_id),
  dog_id         TEXT REFERENCES dog(dog_id),   -- NULL if unidentified
  tracker_id     INTEGER NOT NULL,        -- ByteTrack id, unique within session
  started_at     INTEGER NOT NULL,
  ended_at       INTEGER,
  frames         INTEGER,                 -- frames the track was alive
  zones_json     TEXT,                    -- {"door":{"enter_ts":...,"dwell_ms":...}, ...}
  salience       REAL,                    -- 0..1, computed at close (see A6)
  model_id       TEXT REFERENCES model_registry(model_id),
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL
);
CREATE INDEX idx_track_session ON track_summary(session_id, started_at);

-- The finished proof video for a session or window.
CREATE TABLE highlight_reel (
  reel_id        TEXT PRIMARY KEY,        -- UUIDv7
  session_id     TEXT NOT NULL REFERENCES session(session_id),
  dog_id         TEXT,                    -- denormalized
  window_start   INTEGER NOT NULL,
  window_end     INTEGER NOT NULL,
  media_id       TEXT REFERENCES media_asset(media_id),  -- the stitched mp4
  segments_json  TEXT NOT NULL,           -- ordered [{event_id, t0, t1, caption, confidence}]
  duration_ms    INTEGER,
  model_id       TEXT,                    -- VLM that wrote captions, if used
  generated_at   INTEGER NOT NULL,
  created_at     INTEGER NOT NULL
);
```

New `event_type` values (append to the spec section 5 taxonomy):

| event_type     | meaning                                | payload example |
|----------------|----------------------------------------|-----------------|
| `track_start`  | a new tracker_id appeared              | `{"tracker_id":7}` |
| `track_end`    | tracker_id lost / closed               | `{"tracker_id":7,"frames":312}` |
| `zone_enter`   | tracked subject entered a zone         | `{"tracker_id":7,"zone":"door"}` |
| `zone_exit`    | tracked subject left a zone            | `{"tracker_id":7,"zone":"door","dwell_ms":42000}` |

Add a `salience REAL` column to the existing `event` table (additive) so individual discrete events (bark, treat, pose) carry their own selection weight for the reel.

---

## 3. Workstream A: Edge (highest priority)

Goal: supervision sits between Hailo and the event writer, producing tracks, zone transitions, and a salience score, while collapsing the firehose. This is what makes the timeline clean enough to render from.

### A1. Hailo to supervision adapter

There is no free converter for raw HEF output, so build the small adapter once.

```python
import supervision as sv
import numpy as np

def hailo_to_detections(hailo_out, conf_thresh=0.25):
    # hailo_out: parsed arrays from the HEF output (already produced by your
    # existing Hailo post-processing / hailo-apps-infra parsing).
    boxes_xyxy = hailo_out["boxes"]        # (N,4)
    scores     = hailo_out["scores"]       # (N,)
    class_ids  = hailo_out["classes"].astype(int)  # (N,)
    keep = scores >= conf_thresh
    return sv.Detections(
        xyxy=boxes_xyxy[keep],
        confidence=scores[keep],
        class_id=class_ids[keep],
    )
```

Keypoints from `dogpose_14` stay in your own structure; supervision tracks the boxes, pose stays attached by index.

### A2. ByteTrack (both modes)

```python
tracker = sv.ByteTrack()   # one instance per session

# per frame:
detections = hailo_to_detections(hailo_out)
detections = tracker.update_with_detections(detections)
# detections.tracker_id is now populated
```

`tracker_id` is the within-session identity. It is **not** Elsa-vs-Bezik; that resolution still comes from QR/trained identity and gets written to `dog_id` separately. When identity resolves, stamp the `dog_id` onto the open `track_summary`.

### A3. Zones (Vigilant / stationary mode only)

```python
zones = {
    z["name"]: sv.PolygonZone(polygon=np.array(json.loads(z["polygon_json"])))
    for z in load_device_zones(device_id)
}

# per frame, Vigilant mode only:
for name, zone in zones.items():
    mask = zone.trigger(detections)   # bool per detection
    # diff mask against last frame's per-tracker state -> zone_enter / zone_exit
```

Skip this block entirely when driving. Zones are anchored to the parked frame.

### A4. Event debouncing (collapse the firehose)

Do not write a `detection` row per frame. Write transitions and aggregates:

- On a new `tracker_id`: one `track_start` event, open a `track_summary`.
- On zone mask transition: `zone_enter` / `zone_exit`, update the open `track_summary.zones_json`.
- Discrete events (`bark`, `pose`, `cue_issued`, `treat_dispensed`) log as today, now stamped with the active `tracker_id` and `zone`.
- On track loss: `track_end`, close the `track_summary` (set `ended_at`, `frames`, compute `salience`).

Keep the spec rule: low-confidence detections and rejected poses are still logged as hard negatives. They just carry the track and zone context now, which makes them far more useful for retraining.

### A5. Rolling proxy buffer (feeds the reel)

Continuously record a low-bitrate proxy in fixed segments so the "last hour" footage exists without storing the full-res stream.

```bash
# 10s segments, H.264 proxy, ring of ~360 files = 1 hour
ffmpeg -i <camera_src> -c:v libx264 -preset veryfast -b:v 800k \
  -f segment -segment_time 10 -segment_wrap 360 \
  -strftime 1 /var/wimz/proxy/%Y%m%d_%H%M%S.ts
```

Index each segment by start time in `media_asset` (`retention_class='proxy'`). Full-res clips for selected events stay `retention_class='standard'` as today.

### A6. Salience scoring (the selection weight)

Computed at `track_summary` close and on each discrete event write. Config-driven weights so you can tune what owners care about without redeploying.

```
salience =
    w_behavior  * behavior_weight(event_type)   # bark+intervention > idle
  + w_conf      * confidence
  + w_novelty   * is_first_in_zone_or_new_behavior
  + w_outcome   * intervention_succeeded
  + w_owner     * owner_relevant(treats earned, tricks, calm-after-bark)
```

This single score drives both the reel (top segments to fit the duration) and report emphasis. Tune the weights from the human-review queue over time.

Acceptance: a one-hour Vigilant session produces a handful of `track_summary` rows and a scored event timeline, not tens of thousands of `detection` rows. One SQL query returns the top-N salient moments for any window.

---

## 4. Workstream B: Relay (post-hoc VLM)

The existing per-call report layer stays. It now reads a cleaner timeline, so the reports get sharper with no new cost. Two new paths are added, both selective.

### B1. Text-only session report (every session, cheap)

Unchanged path from your prior proposal. Feed the structured rows (track summaries, discrete events with confidence) to the report model. No frames. Cents per session. Writes `session_report` with `input_hash` for idempotency.

Constraint: the prompt instructs the model to summarize only the supplied rows and to hedge or omit anything below a confidence floor. Claims must trace to rows.

### B2. Vision path for failure analysis (selective)

Triggered only on flagged failures: a treat fired on a low-confidence pose, a missed bark a user reported, a misclassification caught in review. Send the short clip plus the structured context to a vision-capable model and ask what actually happened. The output becomes a review-queue aid and, once a human confirms, a corrected `label_source='human'` row. This is expensive per call, so it runs on the exception, not the stream.

### B3. Auto-labeling backlog (batch, off-hours)

Your spec already hoards low-confidence detections and rejected poses as training gold, unlabeled. Run a batch VLM pass to pre-label that backlog ("this clip is chewing," "this is pacing") to bootstrap new behavior classes before any human labeling. Pre-labels are `label_source='auto_rule'`, never treated as ground truth until human-verified, but they make the human queue fast and they seed new classes (the chewing/scratching roadmap) without waiting for hand labels. This directly serves the "improves automatically as devices log data" priority.

Model choice note: text reports can run on a small/cheap model; vision and labeling need a vision-capable model. Hosted API now. Self-hosting (Gemma on-device) is a post-raise privacy/cost optimization, not a build item here.

---

## 5. Workstream C: Highlight reel

Build dumb first, smart later. v1 needs no VLM.

### C1. Segment selection

```sql
-- top moments in a window, fit to a duration budget
SELECT event_id, ts, event_type, confidence, salience, media_id
FROM event
WHERE session_id = ? AND ts BETWEEN ? AND ? AND salience >= ?
ORDER BY salience DESC
LIMIT 20;   -- then greedily fill ~120s of segment time, re-sort chronologically
```

Each selected event defines a window `[t0, t1]` (a few seconds around `ts`).

### C2. Edge pre-cut (low bandwidth)

For each window, find the covering proxy segments and trim. Only the salient segments leave the device, never the full hour.

```bash
# trim a window from the proxy ring, copy codec (no re-encode)
ffmpeg -ss <t0> -to <t1> -i <covering_segment.ts> -c copy /var/wimz/reel/<event_id>.ts
```

Upload the trimmed segments to the relay, tagged with `event_id`.

### C3. Relay stitch + caption

Order segments chronologically, concatenate, burn one caption per segment. For v1 the caption is templated from the row ("2:14pm  Bezik barked at the door, calmed after treat"). For v2 the report VLM writes the caption lines from the same rows, so report and reel share one voice.

```bash
# concat list in chronological order, then burn captions from an .srt built from rows
ffmpeg -f concat -safe 0 -i list.txt -vf "subtitles=captions.srt" -c:v libx264 reel.mp4
```

Write `highlight_reel` with the `media_id`, the ordered `segments_json` (each with `event_id`, times, caption, confidence), and the `model_id` if a VLM wrote captions.

### C4. App playback

Report at top, "Watch the last hour in 2 minutes" button below, each caption timestamped and tappable to the moment. Confidence rides along so the UI can mark a hedged moment differently from a certain one.

MVP cutline: C1 + C2 + C3 with templated captions and hard cuts is a shippable, demo-able reel with zero VLM dependency. VLM narration (v2) is a layer on top, not a prerequisite.

---

## 6. App surface (Workstream D)

- Session report view (B1 output).
- Highlight reel player (C4), captions tappable to the moment.
- Human-review queue: failures from B2 and auto-labels from B3, one-tap confirm/correct, writing `label_source='human'` rows. These are the highest-value rows in the system.
- Zone editor (Vigilant mode): let the user draw door/couch/bowl polygons once on a still frame, writing `device_zone`.

---

## 7. Build order

1. **A1 to A4**: supervision adapter, ByteTrack, zones, debounced event writing. This alone cleans the timeline and is the foundation for everything else.
2. **A5 + A6**: proxy buffer and salience. Needed before any reel.
3. **B1 fed by the new timeline**: sharper reports at no new cost. Quick win, demo-able.
4. **C1 to C4 (MVP, templated captions)**: the proof video. The feature that changes what the product feels like.
5. **B3 auto-labeling**: bootstrap chewing/scratching classes from the hard-negative backlog.
6. **B2 failure analysis** and **C v2 VLM captions**: polish once the loop is closed.

---

## 8. Why this is the right shape for the raise (strategic tie-in, kept separate from the build)

Stated once, not woven into the tactical sections above.

The clean timeline is the multiplier. It is the same asset on both sides you named: the cleaner the structured data coming off the device, the easier a scientific study is to produce, and the better the in-product implementation feels. supervision is the mechanism that produces that cleanliness, and it costs you adapter work, not a re-platform.

The report-plus-proof-video pair is the demo-able expression of "makes your dog's whole day better." Report as claim, footage as evidence, both traceable to confidence-scored, provenance-stamped rows. That is exactly what an investor wants to watch and exactly what a reviewer like Dr. Andre needs to trust the data. The auto-generated reels also become the deployed-footage highlight asset you have been missing, with no separate production effort.

None of supervision, the VLM, or the reel pipeline belongs in the pitch as technology. They are the means. The pitch is the wellbeing platform and the per-dog longitudinal data they make real.

---

## Changelog
- v0.1 (draft): initial proposal. Defers to `WIMZ_Data_Architecture_Spec.md` v0.2; proposes additive bump to v0.3 (device_zone, track_summary, highlight_reel tables; track/zone event types; salience columns).
