# WIM-Z Data Architecture and Pipeline Specification

**Shared data contract for the Edge (robot), App, and Relay components.**

Version: 0.3 (draft)
Owner: Morgan Hill (morgan@wimzai.com)
Status: Authoritative source of truth for the data schema and data flow. All three component instances build against this file. Do not diverge from it without bumping the version and updating the changelog at the bottom.

---

## 0. Audience and how to use this

Three engineering contexts consume this document:

- **Edge / robot** (Raspberry Pi 5 + Hailo-8, Python firmware): the primary producer and system of record. Owns the local SQLite database and the media filesystem.
- **App** (Flutter, iOS and Android): consumer of data and producer of human-verified labels (dog registration, QR assignment, corrections, trick confirmations).
- **Relay** (AWS Lightsail, api.wimzai.com): handles opt-in sync, cloud aggregation, the training corpus, and model distribution.

If any component needs a field, table, or event type that is not here, add it here first, then build it. The cost of three components inventing three different schemas is that the data never aggregates and the moat never forms.

---

## 1. Data philosophy: what actually makes this data valuable

The defensible asset is not video and not generic detection. Anyone with a camera and an off-the-shelf model gets dog-sit-bark-pose detection cheaply. Four properties make WIM-Z data a moat that a late, better-funded generalist cannot replicate:

1. **Closed-loop linkage.** The value is in the chain: a cue was issued, the dog responded (or did not), a reward was or was not delivered, and the behavior changed over time. Detection alone is a snapshot. The reinforcement loop is the asset. This must be stored as a single queryable unit, not reconstructed later from scattered logs.

2. **Stable longitudinal identity.** The same dog, tracked across sessions, weeks, and firmware updates, produces a response curve. "This individual animal learned this behavior over these sessions" is data that only exists because the device ran the loop on that dog over time. A competitor arriving later starts from zero.

3. **Label provenance.** Every machine-generated label carries the model name, version, and confidence that produced it. Every human correction is marked as such. This lets you retrain on clean, verified data and audit what produced what. Human-verified labels are the highest-value rows in the system.

4. **Capture, do not discard.** Non-selected poses, low-confidence detections, near-misses, and false starts are training gold (hard negatives and edge cases). The current pipeline throws these away. They get logged from now on.

Operating posture: **local-first, sync-optional, consent-gated.** Data lives on the device by default. Aggregation happens only with explicit user consent, in exchange for value (upgrades, features), and only on data structured so it can be cleanly anonymized.

---

## 2. System topology and write responsibilities

```
   [ APP (Flutter) ]                         [ RELAY (Lightsail) ]
   - dog registration                        - opt-in sync endpoint
   - QR assignment                           - cloud landing + corpus
   - human label corrections    <----------> - aggregation / anonymize
   - read dashboards / video                 - model distribution (HEF, app)
        |   ^                                      ^   |
        |   | (verified labels, reads)             |   | (incremental sync)
        v   |                                      |   v
   ===================  EDGE / ROBOT (RPi5)  =======================
   - PRIMARY PRODUCER and SYSTEM OF RECORD
   - local SQLite (WAL)  +  media filesystem
   - all autonomous events, detections, attempts, dispenses
   ================================================================
```

**Source-of-truth rules:**

- The **edge** owns all autonomously generated data (sessions, events, attempts, dispenses, media). It is authoritative for everything the robot observes or does.
- The **app** is authoritative for human-supplied data: dog profile fields, QR-to-dog mapping, and human label corrections. These flow down to the edge and are marked with `label_source = 'human'`.
- The **relay** never originates behavioral data. It moves, anonymizes, and aggregates copies. The cloud corpus is derived, never the master.
- Conflict resolution on sync: **edge-authoritative for machine data, app-authoritative for human-entered fields.** Last-write-wins within each of those domains, tracked by `updated_at`.

---

## 3. Storage architecture (edge)

**Structured data: SQLite, one file, WAL mode.** SQLite is the correct embedded store for the Pi: serverless, transactional, single-file, robust. Configure:

```sql
PRAGMA journal_mode = WAL;        -- concurrent reads while writing
PRAGMA synchronous = NORMAL;      -- good durability/wear balance under WAL
PRAGMA busy_timeout = 5000;       -- ms, avoid lock errors under contention
PRAGMA foreign_keys = ON;
```

**Media: filesystem, not the database.** Never store 4K video or image blobs in SQLite. The database stores metadata plus a relative path plus a checksum. Media lives on disk in a deterministic, portable layout:

```
/data/
  wimz.db                         # the SQLite database
  media/
    {dog_id}/
      {YYYY-MM-DD}/
        {session_id}/
          {event_id}.mp4          # clip for an attempt or flagged event
          {event_id}_frame.jpg    # representative frame, optional
    _unassigned/                  # media where dog_id was not resolved
      {YYYY-MM-DD}/{session_id}/...
```

Paths stored in the DB are **relative to `/data`** so the whole tree is portable to another disk or to the cloud without rewriting rows.

**microSD constraints (200 GB):**

- Video dominates size; the entire behavioral database is tiny by comparison (megabytes to low gigabytes over a long time).
- **Never cull behavioral data to save space. Tier the video instead.** Behavioral rows are the moat and cost almost nothing to keep.
- Retention classes on media (see `media_asset.retention_class`):
  - `permanent`: clips tied to a human-verified attempt or a flagged training milestone. Keep until synced and beyond.
  - `standard`: routine attempt clips. Keep N days, then compress or cull oldest-first.
  - `ephemeral`: ambient monitoring footage. Short window, cull aggressively.
- SD wear: batch writes, avoid per-event `fsync`. Write events to the DB in small transactions (for example every few seconds or per attempt), not one transaction per row.

---

## 4. Core schema

IDs: use **UUIDv7** (or ULID) for all primary keys. They are time-orderable (so they sort by creation and index well) and collision-free across devices, which matters when edge rows merge into the cloud corpus. Store as TEXT.

Timestamps: store UTC as `INTEGER` epoch milliseconds. Where ordering within a session matters at sub-millisecond scale, also store a monotonic `seq` integer.

```sql
-- A physical robot.
CREATE TABLE device (
  device_id        TEXT PRIMARY KEY,        -- UUIDv7, stable per unit
  hardware_rev     TEXT,                    -- e.g. 'rpi5-hailo8-v3'
  firmware_version TEXT,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);

-- The human owner / household. PII lives here and is treated specially on sync.
CREATE TABLE user (
  user_id          TEXT PRIMARY KEY,        -- UUIDv7
  display_name     TEXT,                    -- PII
  contact          TEXT,                    -- PII (email/phone)
  consent_version  TEXT,                    -- which consent they accepted
  consent_scope    TEXT,                    -- JSON: {behavioral:true, media:false,...}
  consent_at       INTEGER,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);

-- An individual dog. This identity must be STABLE over time. It is the
-- backbone of the longitudinal moat.
CREATE TABLE dog (
  dog_id           TEXT PRIMARY KEY,        -- UUIDv7, never recycled
  user_id          TEXT REFERENCES user(user_id),
  name             TEXT,                    -- PII-adjacent; not used as a key
  qr_code_id       TEXT,                    -- app-generated marker id; the fleet's physical markers are ArUco, called "QR" throughout
  id_method        TEXT,                    -- 'qr' | 'direct_trained' | 'manual'  ('qr' covers ArUco markers)
  breed            TEXT,
  birthdate        INTEGER,                 -- epoch ms, nullable
  weight_g         INTEGER,                 -- nullable
  color            TEXT,                    -- v0.3: app-authoritative, human-entered; robot-consumed
  treats_per_reward INTEGER,                -- v0.3: app-authoritative reward config; robot-consumed (null -> robot defaults to 1)
  signature        TEXT,                    -- optional visual embedding ref/hash
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
CREATE INDEX idx_dog_qr ON dog(qr_code_id);

-- Every model that ever produces a label is registered here, so every
-- machine label is attributable. This is label provenance.
CREATE TABLE model_registry (
  model_id         TEXT PRIMARY KEY,        -- UUIDv7
  name             TEXT NOT NULL,           -- e.g. 'dogdetector_14'
  kind             TEXT NOT NULL,           -- 'detection' | 'pose' | 'audio' | ...
  version          TEXT NOT NULL,           -- e.g. '14' or semver
  artifact_hash    TEXT,                    -- HEF / weights hash
  deployed_at      INTEGER NOT NULL
);

-- A continuous run on the device: a training block, a monitoring window,
-- a piloted session, a daycare shift.
CREATE TABLE session (
  session_id       TEXT PRIMARY KEY,        -- UUIDv7
  device_id        TEXT NOT NULL REFERENCES device(device_id),
  mode             TEXT NOT NULL,           -- 'training' | 'monitor' | 'play' | 'daycare' | 'pilot'
  initiated_by     TEXT NOT NULL,           -- 'autonomous' | 'user_pilot' | 'scheduled'
  app_version      TEXT,
  model_versions   TEXT,                    -- JSON snapshot of active model_ids
  started_at       INTEGER NOT NULL,
  ended_at         INTEGER,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
CREATE INDEX idx_session_device_time ON session(device_id, started_at);

-- The atomic log row. EVERYTHING observed or done becomes an event,
-- including non-selected poses and low-confidence detections.
CREATE TABLE event (
  event_id         TEXT PRIMARY KEY,        -- UUIDv7
  session_id       TEXT NOT NULL REFERENCES session(session_id),
  device_id        TEXT NOT NULL REFERENCES device(device_id),
  dog_id           TEXT REFERENCES dog(dog_id),  -- NULL if unidentified
  ts               INTEGER NOT NULL,        -- epoch ms
  seq              INTEGER,                 -- monotonic within session
  event_type       TEXT NOT NULL,           -- see Section 5 taxonomy
  payload          TEXT,                    -- JSON, typed per event_type
  confidence       REAL,                    -- 0..1 for machine-produced events
  model_id         TEXT REFERENCES model_registry(model_id),  -- producer, if machine
  label_source     TEXT NOT NULL DEFAULT 'machine', -- 'machine' | 'human' | 'auto_rule'
  media_id         TEXT REFERENCES media_asset(media_id),     -- NULL if none
  synced           INTEGER NOT NULL DEFAULT 0,
  created_at       INTEGER NOT NULL
);
CREATE INDEX idx_event_session ON event(session_id, ts);
CREATE INDEX idx_event_dog_type ON event(dog_id, event_type, ts);
CREATE INDEX idx_event_unsynced ON event(synced) WHERE synced = 0;

-- THE MOAT TABLE. One row per training attempt, linking cue -> response ->
-- reward -> outcome as a single queryable unit. Reference the underlying
-- events by id so the raw signal is preserved, but denormalize the key
-- fields here for fast longitudinal queries.
CREATE TABLE training_attempt (
  attempt_id       TEXT PRIMARY KEY,        -- UUIDv7
  session_id       TEXT NOT NULL REFERENCES session(session_id),
  dog_id           TEXT REFERENCES dog(dog_id),
  trick_label      TEXT NOT NULL,           -- 'sit' | 'down' | 'quiet' | ...
  cue_event_id     TEXT REFERENCES event(event_id),
  cue_type         TEXT,                    -- 'voice' | 'visual' | 'llm_audio'
  cue_ts           INTEGER NOT NULL,
  response_event_id TEXT REFERENCES event(event_id),
  detected_response TEXT,                   -- detected pose/behavior label
  response_ts      INTEGER,
  latency_ms       INTEGER,                 -- response_ts - cue_ts
  success          INTEGER,                 -- 0/1, or graded 0..100
  confidence       REAL,
  reward_dispensed INTEGER NOT NULL DEFAULT 0,
  dispense_id      TEXT REFERENCES dispense_log(dispense_id),
  model_versions   TEXT,                    -- JSON snapshot
  label_source     TEXT NOT NULL DEFAULT 'machine',
  media_id         TEXT REFERENCES media_asset(media_id),
  synced           INTEGER NOT NULL DEFAULT 0,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
CREATE INDEX idx_attempt_dog_trick_time ON training_attempt(dog_id, trick_label, cue_ts);

-- Physical treat dispenses (44-slot carousel).
CREATE TABLE dispense_log (
  dispense_id      TEXT PRIMARY KEY,        -- UUIDv7
  session_id       TEXT NOT NULL REFERENCES session(session_id),
  dog_id           TEXT REFERENCES dog(dog_id),
  ts               INTEGER NOT NULL,
  slot             INTEGER,                 -- 0..43
  trigger          TEXT,                    -- 'attempt' | 'manual_pilot' | 'schedule'
  attempt_id       TEXT,                    -- soft ref to training_attempt
  dispensed_confirmed INTEGER NOT NULL DEFAULT 0, -- v0.2: IR through-beam broke = treat physically ejected
  confirm_latency_ms  INTEGER,                    -- v0.2: fire -> beam-break in ms; also a jam-trend signal
  synced           INTEGER NOT NULL DEFAULT 0,
  created_at       INTEGER NOT NULL
);

-- Index of media files. The bytes live on disk; this row is the pointer.
CREATE TABLE media_asset (
  media_id         TEXT PRIMARY KEY,        -- UUIDv7
  session_id       TEXT REFERENCES session(session_id),
  dog_id           TEXT REFERENCES dog(dog_id),
  kind             TEXT NOT NULL,           -- 'video' | 'image'
  rel_path         TEXT NOT NULL,           -- relative to /data
  codec            TEXT,                    -- 'h264' | 'h265' | 'jpeg'
  width            INTEGER,
  height           INTEGER,
  duration_ms      INTEGER,
  size_bytes       INTEGER,
  sha256           TEXT,                    -- integrity + dedupe on sync
  start_ts         INTEGER,
  end_ts           INTEGER,
  retention_class  TEXT NOT NULL DEFAULT 'standard', -- 'permanent'|'standard'|'ephemeral'
  synced           INTEGER NOT NULL DEFAULT 0,
  created_at       INTEGER NOT NULL
);
CREATE INDEX idx_media_retention ON media_asset(retention_class, created_at);

-- Periodic per-dog, per-trick rollups: the longitudinal response curve,
-- precomputed so progress queries are cheap and so the curve survives
-- even if old raw rows are pruned on the cloud side.
CREATE TABLE outcome_snapshot (
  snapshot_id      TEXT PRIMARY KEY,        -- UUIDv7
  dog_id           TEXT NOT NULL REFERENCES dog(dog_id),
  trick_label      TEXT NOT NULL,
  window_start     INTEGER NOT NULL,
  window_end       INTEGER NOT NULL,
  attempts         INTEGER NOT NULL,
  successes        INTEGER NOT NULL,
  success_rate     REAL NOT NULL,
  avg_latency_ms   INTEGER,
  mastery_level    REAL,                    -- derived 0..1
  created_at       INTEGER NOT NULL
);
CREATE INDEX idx_snapshot_dog_trick ON outcome_snapshot(dog_id, trick_label, window_start);

-- v0.2: Natural-language summary of one session, generated by the Relay LLM
-- layer from already-structured rows. Relay-generated, app-read. No always-on
-- model; one cheap per-session API call. See the Queryable-Store proposal.
CREATE TABLE session_report (
  report_id        TEXT PRIMARY KEY,        -- UUIDv7, generated on Relay
  session_id       TEXT NOT NULL REFERENCES session(session_id),
  dog_id           TEXT NOT NULL REFERENCES dog(dog_id),  -- denormalized for fast read
  generated_at     INTEGER NOT NULL,        -- epoch ms, UTC
  model_id         TEXT NOT NULL,           -- API model string, e.g. 'claude-haiku-4-5' (literal, NOT a model_registry FK)
  input_hash       TEXT NOT NULL,           -- sha256 of the structured input, for idempotency
  summary_text     TEXT NOT NULL,           -- the report shown in the app
  stats_json       TEXT,                    -- the raw numbers the summary was built from
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
-- Idempotency guard: same session + same input never produces a duplicate report.
CREATE UNIQUE INDEX idx_session_report_idem ON session_report(session_id, input_hash);

-- Tracks what has been pushed to the cloud, per table, for incremental sync.
CREATE TABLE sync_state (
  table_name       TEXT PRIMARY KEY,
  last_synced_at   INTEGER NOT NULL DEFAULT 0,  -- high-water mark (created_at/updated_at)
  last_attempt_at  INTEGER,
  last_error       TEXT
);

-- Schema version for migrations.
CREATE TABLE schema_meta (
  key              TEXT PRIMARY KEY,
  value            TEXT
);
INSERT INTO schema_meta(key, value) VALUES ('schema_version', '0.3');
```

---

## 5. Event taxonomy

`event.event_type` is a controlled vocabulary. `event.payload` is JSON shaped per type. Log machine observations even when they are not acted on.

| event_type        | meaning                                  | payload (JSON) example |
|-------------------|------------------------------------------|------------------------|
| `detection`       | object/dog detected in frame             | `{"bbox":[x,y,w,h],"class":"dog","track_id":7}` |
| `pose`            | pose/behavior classified                 | `{"pose":"sit","bbox":[...],"keypoints":[...]}` |
| `pose_rejected`   | pose detected but NOT selected/acted on  | `{"pose":"down","reason":"low_conf"}` |
| `bark`            | audio bark/vocalization detected         | `{"db":62,"duration_ms":900,"class":"bark"}` |
| `dog_identified`  | QR or signature resolved to a dog_id     | `{"method":"qr","qr_code_id":"..."}` |
| `cue_issued`      | robot issued a training cue              | `{"trick":"sit","cue_type":"llm_audio","text":"..."}` |
| `treat_dispensed` | carousel fired (pairs with dispense_log) | `{"slot":12}` |
| `pilot_action`    | live operator command                    | `{"action":"drive","vec":[0.4,0.0]}` |
| `error`           | fault / exception                        | `{"code":"motor_stall","detail":"..."}` |

Rules:
- **Always set `confidence` and `model_id`** for machine-produced events. A label with no provenance is near worthless for retraining.
- **`pose_rejected` and low-confidence `detection` rows are kept, not dropped.** These are the hard negatives the current pipeline loses.
- Add new types by appending to this table and bumping the schema version. Do not overload an existing type with a different meaning.

---

## 6. The closed training loop, worked

A single "sit" attempt that currently produces one timestamped video file becomes a connected set of rows:

1. `session` row opens (`mode='training'`).
2. `cue_issued` **event** ("sit", llm_audio) with `model_id`, `ts`.
3. `pose` **event** ("sit", confidence 0.91) with the producing `model_id`.
4. `media_asset` row for the clip on disk (`rel_path`, `sha256`, `retention_class='standard'`).
5. `dispense_log` row (slot fired), confirmed by the IR through-beam (`dispensed_confirmed=1`), and a `treat_dispensed` event. `reward_dispensed` on the attempt should read from `dispensed_confirmed`, not from the fire command.
6. **`training_attempt`** row ties them together: `trick_label='sit'`, `cue_event_id`, `response_event_id`, `latency_ms`, `success=1`, `reward_dispensed=1`, `dispense_id`, `media_id`.
7. On a schedule, `outcome_snapshot` recomputes this dog's "sit" success rate and mastery over the latest window.

**Migrating today's data:** your current `time + trick label` filenames map directly. Parse each file into a `media_asset` row plus a `training_attempt` row (trick_label from the label, cue_ts from the timestamp, `label_source='machine'`, `success` unknown unless the filename or a sidecar log says otherwise). Your existing bark and pose text logs become `event` rows. Write a one-time backfill script; do not lose the history, it is the start of the longitudinal record.

---

## 7. Input and write paths

- **Edge writes** in small batched transactions (per attempt, or every few seconds for ambient events) to protect the SD card. One transaction per single row is the wrong pattern at volume.
- **IDs are generated at the producing component** (UUIDv7), so edge and app can both create rows offline and merge later without collisions.
- **Idempotency:** sync uses the primary key; re-sending a row is a no-op upsert. Never auto-increment IDs that would collide across devices.
- **App writes** flow as: dog registration and QR mapping create/update `dog` rows; a human correcting a label writes an `event` or updates a `training_attempt` with `label_source='human'` and a fresh `updated_at`. Human edits win over machine values for the same field.

---

## 8. Output and export paths (training and mining)

Two export families, both driven off the same tables:

**Vision training export.** Join `media_asset` to `event`/`training_attempt` to emit a labeled dataset. Prefer human-verified labels:

- YOLO or COCO format from `pose`/`detection` events that have a `media_id` and `label_source='human'` where available, falling back to high-confidence machine labels.
- Always carry `model_id` and `confidence` into the manifest so downstream filtering by provenance is possible.

**Behavioral / time-series export.** Emit `training_attempt` and `event` streams as JSONL or Parquet for mining response curves, latency distributions, reward schedules, and per-breed or per-dog learning patterns.

Example mining queries (run locally):

```sql
-- This dog's "sit" mastery trajectory
SELECT window_start, success_rate, avg_latency_ms
FROM outcome_snapshot
WHERE dog_id = ? AND trick_label = 'sit'
ORDER BY window_start;

-- Highest-value training clips for human review (acted-on, mid-confidence)
SELECT a.attempt_id, a.media_id, a.confidence
FROM training_attempt a
WHERE a.label_source = 'machine'
  AND a.confidence BETWEEN 0.4 AND 0.7
  AND a.media_id IS NOT NULL
ORDER BY a.created_at DESC;

-- Hard negatives: poses the model saw but rejected
SELECT * FROM event
WHERE event_type = 'pose_rejected'
ORDER BY ts DESC;
```

The export layer is where machine-vs-human provenance pays off: you can train on verified labels, audit on confidence, and surface the most useful unlabeled clips for human review, which feeds the verified set back. That review-and-verify cycle is the flywheel.

---

## 9. Sync and aggregation: the data-for-value exchange

**Default: nothing leaves the device.** Sync is opt-in and tied to value (model upgrades, premium features). This is both the privacy posture and the business mechanism.

**What syncs, gated by `user.consent_scope`:**

- `behavioral` consent: events, attempts, dispenses, outcome snapshots, dog rows **with PII stripped** (no `name`, no `contact`; keyed only by `dog_id`/`device_id` surrogates).
- `media` consent (separate, stricter): selected clips, by `retention_class` and value, never bulk ambient footage.
- PII (`user.display_name`, `user.contact`, `dog.name`) **stays on device or syncs only under explicit separate consent**, and never enters the aggregated training corpus.

**Mechanism:**

- Incremental, watermark-based. Each table's `sync_state.last_synced_at` advances as rows upload. Only rows newer than the high-water mark go up.
- Upsert by primary key on the relay; resends are safe.
- Media dedupe by `sha256`.
- Edge-authoritative for machine data, app-authoritative for human fields, on conflict.

**Cloud corpus layout (relay):**

```
landing/      raw per-device uploads, append-only, immutable
normalized/   cleaned, schema-aligned, PII removed, surrogate-keyed
corpus/       ML-ready datasets: vision (COCO/YOLO) + behavioral (Parquet)
```

**Why this is the moat, restated for the engineers building it:** an affordable device installs broadly, broad installation generates broad outcome-linked behavioral data, opt-in sync aggregates it under consent, and the corpus trains models no one with an expensive, narrowly-installed platform can match. The cost structure is the data-acquisition strategy. The pipeline only delivers that if the data is (a) outcome-linked, (b) provenance-tagged, and (c) consented and anonymizable. If those three hold, the corpus is an ownable asset.

**Consent and ownership note (not legal advice):** the consent text the user accepts should grant the company a license to use anonymized, aggregated behavioral data for model training and product improvement, version the consent (`user.consent_version`), and record `consent_at`. Confirm the actual license language with counsel before launch. The schema is built so the engineering supports whatever the lawyers land on: PII is separable, consent is recorded per user, and scope is enforced at sync.

---

## 10. Schema versioning and migrations

- `schema_meta.schema_version` is the truth. Every component checks it on startup.
- **Additive-first:** prefer new nullable columns and new tables over altering or repurposing existing ones. SQLite's `ALTER TABLE` is limited; additive changes avoid table rebuilds.
- Each migration: bump the version, write a forward migration, note it in the changelog below. Never silently change a column's meaning, which corrupts provenance retroactively.

---

## 11. Rules of the road (every instance honors these)

1. Structured data in SQLite; media on the filesystem; the DB stores pointers, not bytes.
2. Every machine label carries `model_id` and `confidence`. No anonymous labels.
3. `dog_id` is stable forever and never recycled. It is the longitudinal backbone.
4. Keep behavioral rows indefinitely; tier and cull video by `retention_class`, oldest-first.
5. Log rejected and low-confidence observations. Do not discard hard negatives.
6. The `training_attempt` row is mandatory for every training interaction. The loop is the product.
7. IDs are UUIDv7, generated at the producer. Sync is idempotent upsert by primary key.
8. Nothing syncs without matching `consent_scope`. PII never enters the corpus.
9. Change the schema here first, bump the version, then build.

---

## Changelog

- **0.3** Additive-only. Added `color` and `treats_per_reward` (both app-authoritative, robot-consumed) to `dog`. Clarified that ArUco markers are represented via the existing `qr_code_id` / `id_method='qr'` — the fleet's physical markers are ArUco but are called "QR" everywhere; no separate identity fields added. No existing tables or columns changed.
- **0.2** Additive-only. Added `session_report` table (Relay-generated per-session natural-language summary, with `input_hash` idempotency guard) and two columns on `dispense_log` (`dispensed_confirmed`, `confirm_latency_ms`) for IR-through-beam dispense confirmation. No existing tables or columns changed.
- **0.1** Initial draft. Core schema, event taxonomy, closed-loop model, local-first sync and consent design.
