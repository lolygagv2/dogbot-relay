# WIM-Z — Embodied AI / Navigation Build Sheet

**Status:** Reference architecture. NOT scheduled for build yet — recorded so it can be pulled and executed in one focused pass later.
**Scope:** Covers the LLM/AI layer, navigation (demo + production options), the layered safety/hedge design, a demo spec with concrete prompts, and a cheap-sensor BOM.
**Last updated:** [date]

---

## 0. Core design principles (read first)

These five rules govern every decision below. If a later choice violates one of these, the rule wins.

1. **Dumb reflexes override smart AI.** Safety (don't fall, don't crush, don't hit) must NEVER depend on the AI, the cloud, or anything that can lag, hang, or hallucinate. The AI *proposes*; a local firmware reflex layer can always *veto*.
2. **Local for fast/frequent/private; cloud for rare/heavy reasoning.** Continuous perception and motion run on-device (Pi 5 + Hailo). The cloud model is called only for the 1–2 moments per interaction that genuinely need language or higher reasoning.
3. **Design the failure, not the perfection.** Goal is not "never fails." Goal is "every failure is slow, soft, visible, and recoverable." A flaky navigation stack is acceptable if the architecture refuses to let flakiness cause harm.
4. **Navigation is plumbing to buy cheap, not the moat.** Baseline indoor nav is now off-the-shelf (cheap LIDAR + ROS2/Nav2) — which also means it's off-the-shelf for competitors. Buy it cheap; keep defensibility in the LLM/data/product layer.
5. **The model is a bounded judgment layer, not the robot's body.** The model only ever returns text/JSON. Your Python decides what to *do* with it. The model never directly drives motors or holds kernel/shell access.

---

## 1. Layered architecture overview

| Layer | Job | Runs where | Reaction time | Failure consequence |
|---|---|---|---|---|
| **L0 — Reflexes** | Don't fall, don't hit, emergency stop | Firmware / microcontroller (e.g. ESP32), independent of Pi | milliseconds | Catastrophic — must never fail |
| **L1 — Motion + local perception** | Detect/track dog, steer, small moves | Pi 5 + Hailo (local YOLO) | ~real-time | Minor — robot drifts/pauses |
| **L2 — Semantic navigation** | "Go toward the dog," approach loop | Pi (local) + occasional cloud reasoning | seconds | Recoverable — robot re-checks or returns |
| **L3 — Reasoning / language** | Parse command, judge scene, converse | Cloud model (or local LLM) | seconds, rare | Annoying — task stalls, asks human |

**Key boundary:** L0 is a hardware/firmware interrupt that can cut motors regardless of what L1–L3 want. Wire it so the Pi crashing or the network dropping cannot disable the reflex layer.

---

## 2. Compute & "what runs where"

| Task | Runs where | Why |
|---|---|---|
| Cliff / bump / proximity safety stop | **Firmware (ESP32/MCU)** | Must work even if Pi or network is dead |
| Wake-word detection | **Local (Pi)** — Porcupine / openWakeWord | Always-on, low-power, private |
| Dog detection + tracking + steering cues | **Local (Hailo)** — your existing YOLO | High-frequency, zero token cost, video stays home |
| Speech-to-text | Local (whisper.cpp / Vosk) **or** cloud | Local = offline + private; cloud = simpler |
| Intent parsing ("which dog, what task") | Cloud model **or** small local LLM | Small text job; local removes a round trip |
| "Is the dog actually sleeping?" judgment | **Cloud VLM** (1 small image) | Worth a frontier model; happens rarely |
| Conversation / "talk to the person" | Cloud model (or BYO-key, §4) | Heavy reasoning, rare |
| Record / notify / upload-on-reconnect | **Local (Pi)** | Plumbing, no model needed |

---

## 3. Navigation — two options

### Option A — Quick / demo ("good enough," days of work)
- **No SLAM map.** Use the VLM snapshot-loop for semantic "go toward the dog," + local YOLO for tracking, + L0 reflex sensors so it can't fall or crash.
- Looks just as impressive in a 30-sec clip; far less setup.
- Best for: internal proof, investor demo.

### Option B — Robust / production (weeks of integration, not years)
- **Off-the-shelf:** ROS2 + Nav2 + slam_toolbox + a ~$70 2D LIDAR (YDLIDAR X4 / LDROBOT D500 / RPLIDAR A1) + wheel encoders.
- Real mapped home navigation, point-to-point, static-obstacle avoidance.
- **Still requires** L0 cliff sensors (a 2D LIDAR CANNOT see a stair drop-off below its scan plane) and per-chassis tuning (odometry, robot model, calibration).
- Best for: shipping product / pilots.

### Closed-loop "approach the dog" pseudocode (Option A)
```
on wake_word("Claude ..."):
    text = transcribe(audio)
    intent = cloud_or_local_LLM(parse_prompt, text)   # -> {target, task}
    last_action = "none"
    loop (max N steps, with timeout):
        frame = camera.snap_lowres()
        # LOCAL first: is target dog in frame? (Hailo/YOLO)
        det = local_detect(frame, intent.target)
        if det.found:
            cue = steer_toward(det.bbox)   # local, fast
        else:
            cue = vlm_search_step(frame, last_action)  # cloud, coarse direction
        # L0 reflexes can VETO any move:
        if reflex.cliff or reflex.bump or reflex.too_close: stop()
        else: execute_small_move(cue); last_action = cue
        if det.found and det.distance == "near": break
    # final judgment (cloud, 1 small image):
    status = vlm_judge(snap_lowres(), intent.task)   # -> {state, confidence, reason}
    narrate(status); log(status); optionally return_to_base()
```

**Navigation caveats (these are the flaky bits):**
- **Don't ask the VLM for precise distances/angles.** Ask for *coarse corrections* ("turn slightly left," "a bit closer"). Many small dumb moves beat one big smart move.
- **Feed one line of memory each step** (`last_action`, previous position) or it will oscillate / drunk-walk.
- **Don't make the language model do fine-grained dog re-ID** ("Bezik vs. the other Pom") — that's the hard, flaky case. Use local YOLO markers for ID; let the VLM handle navigation + status only. For demo: use one dog.

---

## 4. The LLM / AI layer

### The whole "AI" is 2 cloud calls + 1 local perception loop
1. **Intent parse** (text → structured command).
2. **Image judgment** (1 small image → bounded verdict).
3. **Local perception loop** (Hailo, no tokens) for tracking/steering.

### Model setup recommendation (agnostic)
- **Hybrid, split by job.** Local model/inference for fast+private+frequent; cloud frontier model for rare heavy reasoning. A cloud-only robot is laggy, expensive, and creepy on privacy. Local-only can't do impressive language reasoning. Hybrid gets both.

### BYO-key tier ("plug in your own ChatGPT / Claude")
- **What it is:** user supplies their own API key or existing subscription; an "unlock" turns WIM-Z into a personal AI assistant that rides alongside them.
- **Why it's strong:**
  - Moves token/inference cost **off your books onto the user** — directly fixes the per-unit cost/margin problem.
  - Keeps you **model-agnostic**; you don't pick or bankroll a model and you ride frontier improvements for free.
  - It **is** the "really for the human" platform reveal. (Note: an advisor arrived at this independently — strong validation, and a warning it's not as secret/novel as assumed.)
- **How to spec it:** base product works out-of-box on a bundled **local** model (the $699 dog owner will NOT configure an API key). BYO-key is the **optional upsell tier** for power users / the platform reveal. Build the hooks in now; keep them quiet in messaging until the reveal.

### Token-cost minimization rules
- **Navigation frames never go to the cloud.** Run the "where's the dog / which way" loop locally on Hailo = zero token cost. This is the biggest lever (40 billed images → 1).
- **Send only 1 image to the cloud** (the final judgment frame).
- **Downscale + compress** any image you do send (small low-res JPEG; coarse judgments don't need 1080p).
- **Tiny bounded outputs:** ask for short JSON (`state, confidence, one-line reason`), not prose.
- **One intent call per command**, no polling/streaming for navigation.
- **Minimal per-call context;** don't resend a giant system prompt every call.
- Net cost of a full "check on Bezik": ~1 cheap text call + 1 small image call = pennies.

---

## 5. Safety layers (the hedge — assume nav is unsolved & dangerous)

### Layer 0 — Dumb physical reflexes (the real safety net)
Run in firmware/MCU, independent of Pi & network. Can override any AI/nav command.
- **Cliff sensors** (downward ToF) at front edges → motors cut if floor drops away. **Stops stair falls. Non-negotiable.**
- **Bump / contact sensors** (microswitches / bumper) → immediate stop-and-reverse on physical contact.
- **Forward ToF / ultrasonic** → "something close, stop," independent of camera/cloud.

### Layer 1 — Make the robot inherently low-stakes
Engineer failures to be harmless rather than engineering perfection.
- **Go slow** (crawl). Speed is the enemy of "good enough"; slow gives sensors time and makes collisions harmless.
- **Light + low center of gravity** → can't tip; a bump is a nudge, not an injury.
- **Soft bumpers / rounded edges.**
- **Short autonomous bursts, not continuous roaming** → move a little, stop, re-check. Pauses read as "thinking," not failure.

### Layer 2 — Bounded behavior (can't wander into trouble)
- **Geofence cheaply:** single-room operation, charging home base, floor markers / ArUco tags / boundary strips it won't cross (cheap version of vacuum no-go zones).
- **Timeout-and-return:** if it can't complete in N seconds, stop or return to base — never search forever.
- **"Lost" behavior:** on confusion or network drop mid-task → stop and ask the human ("couldn't find Bezik — keep looking?"), don't improvise.
- **Safe-idle default:** every failure path ends in a safe stop, not blind retry.

### Layer 3 — Demo-day survival (assume network + motors will betray you)
- **Pre-recorded backup** of the exact flow, ready to play if the live run fails ("here's it running yesterday").
- **Controlled, pre-tested space** (run it 20× beforehand), clean lighting, **one dog, no other pets.**
- **Human-in-the-loop kill switch** in hand.
- Remember: live robot demos die from **network and motors**, almost never the AI. Plan for both to fail and you look composed when one does.

**Reframe:** this layered "dumb reflexes veto smart AI" design is how real robots are built anyway — so it's not throwaway backup work, it's the correct production architecture either way.

---

## 6. Demo spec (the micro-example)

**Command:** *"Claude, make sure Bezik is sleeping."*

Flow: wake-word (local) → transcribe → **intent parse** → approach loop (local tracking + coarse cloud/vlm steering, L0 reflexes active) → **sleep-status judgment** (1 cloud image) → narrate result in spoken English → log summary → optionally retrace path to user.

**Connectivity/latency note:** 2 round-trips means a few seconds of "thinking" pauses. Fine for an edited 30-sec clip; frame pauses as deliberate. Needs connectivity — keep the Layer 3 pre-recorded backup for bad-wifi rooms.

---

## 7. Prompts & schemas (concrete)

**A) Intent parse (text in → JSON out)**
```
System: You convert a spoken request about a pet into a command. 
Reply ONLY with JSON: {"target": <dog name or "unknown">, "task": <short task id>, "confidence": 0-1}. No prose.
User: "make sure Bezik is sleeping"
-> {"target":"Bezik","task":"check_sleeping","confidence":0.95}
```

**B) Navigation search-step (image + memory → coarse direction)**
```
System: You help a slow indoor robot find a dog. Given the image and last action, 
reply ONLY JSON: {"visible": bool, "position":"left|center|right|none", 
"distance":"near|far|unknown", "action":"turn_left|turn_right|forward|search", "arrived": bool}. 
Use COARSE directions only. No distances in units.
User: [low-res image] last_action="turn_left", prev="dog far-right"
```

**C) Sleep-status judgment (1 image → verdict)**
```
System: Judge whether the dog in this image is sleeping. 
Reply ONLY JSON: {"state":"sleeping|resting|awake|unsure","confidence":0-1,"reason":"<= 10 words"}.
User: [low-res image]
```

---

## 8. Cheap-sensor BOM (approximate, USD)

| Item | Purpose | Layer | Approx. unit cost |
|---|---|---|---|
| Downward ToF (e.g. VL53L0X/L1X) ×2–4 | Cliff / stair-drop detection | L0 | ~$3–5 ea |
| Mechanical bump microswitches / bumper | Contact stop-reverse | L0 | ~$1–3 |
| Forward ToF / ultrasonic (HC-SR04 or ToF) | "Too close, stop" | L0 | ~$2–5 |
| 2D LIDAR (YDLIDAR X4 / LDROBOT D500 / RPLIDAR A1) | SLAM nav (Option B only) | L2 | ~$50–99 |
| ESP32 (likely already in stack) | Firmware reflex layer / MCU | L0 | ~$5 |
| **Already in stack:** Pi 5 + Hailo | Local inference | L1 | — |
| **Already in stack:** Cytron 10A driver (3/5 vehicles) | Drive motors | L1 | — |
| **Already in stack:** TMC2209 | Treat dispensing ONLY (not drive) | — | — |
| **Already in stack:** camera, mic, speaker, dispenser servo | Perception / I/O / treats | — | — |

*Net new safety BOM for the cheap/demo path (Option A): roughly $15–30/unit. Option B adds the LIDAR.*

---

## 9. Open decisions / TODO (resolve before building)

- [ ] Local LLM choice for offline intent parsing (and whether to bother for v1).
- [ ] STT: local (whisper.cpp/Vosk) vs cloud — privacy vs simplicity.
- [ ] BYO-key: confirm hardware/software hooks are present in the first production run even if disabled (keeps the platform-reveal option alive).
- [ ] Which chassis is the demo unit (Cytron 10A is on 3/5 vehicles).
- [ ] Photo "upload-on-reconnect" queue — does not exist yet; needs building.
- [ ] "Talk to the person" two-way audio path — confirm pipeline.
- [ ] Decide Option A vs B for first impressive demo (recommend A).
- [ ] **Hardware upgrade hooks:** ensure the production unit has compute headroom, I/O, mounting/expansion provisions, and OTA-update capability so the later add-on module + software upgrade can land. Do NOT cost-optimize these away. (Secret to the market, explicit in the spec.)

---

## 10. Strategic notes (carry-overs, brief)

- **Navigation is not the moat** — it's commoditized plumbing. Buy cheap, don't build a company around solving home autonomy.
- **BYO-key** simultaneously fixes token-cost economics AND is the "it's really for the human" platform reveal. It's a tier, not the default.
- **Trojan-horse discipline:** quiet in public messaging, explicit in your own spec sheet. The expensive mistake is shipping a unit too cost-optimized to accept the upgrade you're betting on.
- **The hedge architecture is the production architecture** — building the safety layers is not throwaway demo work.
