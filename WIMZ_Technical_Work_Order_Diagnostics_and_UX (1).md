# WIM-Z Technical Work Order: Diagnostics, Optimization, and Idiot-Proofing

Owner: Morgan Hill
Date: June 21, 2026
Status: Hand directly to Claude Code on the robot. Execute in order.
Context: User feedback from deployed testers revealed that setup friction (not AI quality) is the biggest barrier to adoption. A FANG software engineer could not figure out WiFi-to-AP mode switching. Boot takes 90 seconds. These are product-killing failures that outweigh every AI feature behind them.

---

## 1. FPS AND PIPELINE DIAGNOSTIC

Goal: Determine whether the current 30 FPS at 640x640 (YOLOv8n on Hailo-8) is the hardware ceiling or an artificial bottleneck from pipeline inefficiency. The Hailo-8 should run YOLOv8n at 120-150 FPS at 640. We are getting 30. Something is consuming 70-80% of available throughput. Find it.

### 1A. Baseline isolation tests

Run each test for 60 seconds, log average FPS, peak FPS, and per-core CPU utilization. Kill all non-essential services before each test. The point is to isolate each stage of the pipeline and measure its cost independently.

```bash
# PREP: Kill everything that competes for CPU
sudo systemctl stop treatbot  # or whatever the main service is called
# Identify and stop any streaming, audio, web server processes

# TEST 1: Pure Hailo inference, no camera, no stream, no display
# Feed a static 640x640 image in a loop to the Hailo
# This measures raw Hailo throughput with zero CPU pipeline overhead
# Expected: 120-150 FPS for YOLOv8n
# Write a minimal Python script:
python3 << 'EOF'
import time
import numpy as np
from hailo_platform import HEF, VDevice, ConfigureParams, InferVStreams, InputVStreamParams, OutputVStreamParams

hef = HEF("/home/morgan/dogbot/ai/models/dogdetector_14.hef")
target = VDevice()
configure_params = ConfigureParams.create_from_hef(hef, interface=target)
network_group = target.configure(hef, configure_params)[0]
network_group_params = network_group.create_params()

input_vstream_info = hef.get_input_vstream_infos()
output_vstream_info = hef.get_output_vstream_infos()
input_shape = input_vstream_info[0].shape
print(f"Model input shape: {input_shape}")

# Create a dummy frame matching the model input
dummy_frame = np.random.randint(0, 255, size=input_shape, dtype=np.uint8)

input_params = InputVStreamParams.make(network_group, quantized=False)
output_params = OutputVStreamParams.make(network_group, quantized=False)

frames = 0
start = time.time()
with InferVStreams(network_group, input_params, output_params) as pipeline:
    while time.time() - start < 60:
        result = pipeline.infer({input_vstream_info[0].name: np.expand_dims(dummy_frame, axis=0)})
        frames += 1
elapsed = time.time() - start
print(f"Pure Hailo inference: {frames/elapsed:.1f} FPS over {elapsed:.1f}s")
EOF

# TEST 2: Camera capture only, no inference, no stream
# This measures raw camera + ISP cost
# Expected: 30-60 FPS at 640x640 depending on ISP config
python3 << 'EOF'
import time
from picamera2 import Picamera2

cam = Picamera2()
config = cam.create_video_configuration(main={"size": (640, 480), "format": "RGB888"})
cam.configure(config)
cam.start()
time.sleep(2)  # let auto-exposure settle

frames = 0
start = time.time()
while time.time() - start < 60:
    frame = cam.capture_array()
    frames += 1
elapsed = time.time() - start
print(f"Camera capture only (640x480): {frames/elapsed:.1f} FPS")
cam.stop()
EOF

# TEST 3: Camera capture at 1080p, no inference, no stream
python3 << 'EOF'
import time
from picamera2 import Picamera2

cam = Picamera2()
config = cam.create_video_configuration(main={"size": (1920, 1080), "format": "RGB888"})
cam.configure(config)
cam.start()
time.sleep(2)

frames = 0
start = time.time()
while time.time() - start < 60:
    frame = cam.capture_array()
    frames += 1
elapsed = time.time() - start
print(f"Camera capture only (1080p): {frames/elapsed:.1f} FPS")
cam.stop()
EOF

# TEST 4: Camera 640 + Hailo inference, NO streaming, NO audio, NO motors
# This measures the actual vision pipeline without the stream encode tax
# Compare this to TEST 1 and TEST 2 individually
# The gap between (TEST1 + TEST2 combined theoretical) and TEST4 actual = pipeline overhead

# TEST 5: Camera 640 + Hailo inference + H.264 streaming encode
# This is what currently runs. Compare to TEST 4.
# The gap between TEST 4 and TEST 5 = encode tax

# TEST 6: Camera 1080 + Hailo inference (resize to 640 for model), NO streaming
# This tests whether higher-res capture + downscale kills FPS even without encode
```

### 1B. CPU profiling during normal operation

While the full system is running normally (all services, streaming, audio):

```bash
# Snapshot of what is eating CPU
ps -eo pid,pcpu,pmem,comm --sort=-pcpu | head -20

# Continuous monitoring (run in separate SSH session during a 5-min session)
top -b -d 1 -n 300 | grep -E "python|ffmpeg|gst|x264|libcamera|hailo" > /tmp/cpu_profile.log

# Check for thermal throttling
vcgencmd measure_temp
vcgencmd measure_clock arm
vcgencmd get_throttled  # 0x0 = no throttling, anything else = problem

# Check if software H.264 encode is running
ps aux | grep -E "x264|ffmpeg|gst.*x264|gst.*h264"

# Memory pressure
free -m
cat /proc/meminfo | grep -E "MemFree|MemAvail|Buffers|Cached"
```

### 1C. What to report

After running all tests, report in this format:

```
TEST 1 (pure Hailo):         ___ FPS   CPU: ___% per core
TEST 2 (camera 640 only):    ___ FPS   CPU: ___% per core
TEST 3 (camera 1080 only):   ___ FPS   CPU: ___% per core
TEST 4 (cam640 + Hailo):     ___ FPS   CPU: ___% per core
TEST 5 (cam640 + Hailo + stream): ___ FPS  CPU: ___% per core
TEST 6 (cam1080 + Hailo 640, no stream): ___ FPS  CPU: ___% per core
Thermal throttled: yes/no
Software H.264 process found: yes/no (process name)
Top 5 CPU consumers during normal operation: (list)
```

The diagnosis falls out of the gaps between tests:
- TEST 1 vs TEST 4 gap = camera + pre-processing overhead
- TEST 4 vs TEST 5 gap = encode tax (the prime suspect)
- TEST 2 vs TEST 3 gap = resolution scaling cost on ISP/CPU
- TEST 4 vs current 30 FPS = overhead from other services (audio, motors, web server, Python GIL)

### 1D. Known suspects to check in the code

Open `core/ai_controller_3stage_fixed.py` and `core/vision/camera_manager.py` and look for:

- Frame format: is the camera outputting YUV420 (efficient) or RGB888 (3x the data)? If RGB888, are there unnecessary color conversions?
- numpy copies: `frame.copy()`, `np.array(frame)`, or any operation that duplicates the frame buffer. Each copy at 640x640x3 is ~1.2MB. At 1080p its ~6MB. These add up fast.
- Synchronous blocking: is the Hailo inference call blocking the main loop? Is the camera capture waiting for the previous inference to finish? These should be decoupled (camera fills a buffer, inference reads from it asynchronously).
- GIL contention: if everything runs in one Python process with threads, the GIL serializes CPU-bound work. Look for whether capture, inference dispatch, post-processing, and encode are all in the same process.
- Resolution mismatch: is the camera capturing at a higher resolution than 640 and then the code is resizing in Python (slow) vs using the ISP (fast)?

---

## 2. ENCODING STREAM FIX

This is the "for sure" fix regardless of diagnosis. The stream and inference pipelines must be decoupled.

### 2A. Current problem

Stream resolution is locked to inference resolution. When you run at 640, both capture at 640, infer at 640, and encode at 640. The stream looks grainy. When you try 1080, both capture at 1080, infer at 1080 (or try to), and encode at 1080. The CPU dies.

### 2B. Target architecture

```
Camera captures at native resolution (1080p on IMX708, or whatever the sensor outputs)
    |
    +---> ISP downscale to 640x640 ---> Hailo (inference) ---> detections
    |                                                              |
    +---> Encode at 720p or 1080p ----> Stream to phone <----------+
           (MJPEG if H.264 is too                    (overlay annotations)
            heavy, test both)
```

Picamera2 supports multiple output streams from a single capture. Use the `main` stream for the higher-res encode and the `lores` stream for inference:

```python
from picamera2 import Picamera2

cam = Picamera2()
config = cam.create_video_configuration(
    main={"size": (1280, 720), "format": "RGB888"},   # owner stream
    lores={"size": (640, 480), "format": "RGB888"},    # inference feed
    encode="main"  # only encode the main stream
)
cam.configure(config)
cam.start()

# Inference reads from lores (cheap, small)
inference_frame = cam.capture_array("lores")

# Stream reads from main (pretty, larger)
stream_frame = cam.capture_array("main")
```

This gives you pretty 720p (or 1080p if the CPU can handle the encode) for the owner while inference stays at 640. One capture, two forks, no double pipeline.

### 2C. MJPEG vs H.264

The Pi 5 has no hardware H.264 encoder. Software H.264 at 1080p may consume 2-3 CPU cores. MJPEG is per-frame JPEG compression, which is lighter and has no inter-frame dependency. Test both:

```bash
# Check if libcamera supports MJPEG output directly
libcamera-vid --codec mjpeg --width 1280 --height 720 -t 30000 -o test.mjpeg &
# Monitor CPU during this

# Compare to H.264
libcamera-vid --codec h264 --width 1280 --height 720 -t 30000 -o test.h264 &
# Monitor CPU during this
```

If MJPEG is 20-30% less CPU than H.264 at the same resolution, use MJPEG. The visual quality difference at the bitrates you're streaming over WiFi/cellular is minimal, and the CPU savings go directly to your inference and service headroom.

### 2D. Adaptive bitrate indicator

Add a connection quality indicator to the app's live view. When stream quality degrades due to bandwidth:
- Show a signal-strength icon (1-4 bars derived from frame delivery latency or dropped frames)
- Show "Weak connection" text when below threshold
- This reframes grain as "bad signal" not "bad product"

This is an app-side change, not robot-side. Add to the Flutter app's video player widget.

---

## 3. BOOT TIME AND NETWORK OVERHAUL

Hard requirements. These are not suggestions, they are specs to build against:

- Boot to LEDs/sound (sign of life): under 5 seconds from power on
- Boot to camera live on phone: under 30 seconds
- WiFi connection or AP fallback: under 15 seconds total
- Zero user-facing mode selection. The user never chooses WiFi vs AP. Ever.
- Zero state where the device is on but unreachable from the app

### 3A. Boot sequence overhaul

Current problem: ~90 second boot. User sees nothing and thinks device is broken.

```bash
# DIAGNOSE: What is taking so long?
# Run on the robot:
systemd-analyze
systemd-analyze blame | head -20
systemd-analyze critical-chain treatbot.service

# This tells you exactly which services are in the critical path
# and which are blocking boot
```

Fixes, in priority order:

**Immediate sign of life (under 5 seconds):**
- Add a systemd service at `sysinit.target` (very early boot) that turns on the NeoPixel LEDs in a breathing/pulsing pattern and plays a short startup chime through the speaker
- This runs before Python, before the AI pipeline, before networking
- The user sees light and hears sound within seconds of power-on
- This is a simple bash script or C program, not the full Python stack

```ini
# /etc/systemd/system/wimz-alive.service
[Unit]
Description=WIM-Z Sign of Life
DefaultDependencies=no
After=sysinit.target
Before=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/wimz-alive.sh
# This script: set NeoPixel to breathing teal, play chime via aplay

[Install]
WantedBy=sysinit.target
```

**Camera live fast (under 30 seconds):**
- The camera and streaming service must start before the AI pipeline loads
- Currently the AI models load (Hailo HEF compilation/loading takes several seconds) and the camera likely doesn't start until the full pipeline is ready
- Decouple: start camera + stream immediately, start AI inference as a second stage that attaches to the already-running camera
- The user sees their dog on screen before the AI is ready. The AI annotations appear a few seconds later. This is the correct UX: value (seeing the dog) before sophistication (AI overlays)

**Defer non-critical services:**
- Audio detection, treat calibration, mission engine, social features, telemetry upload: all start after camera is live
- Use systemd `After=wimz-camera.service` dependencies to sequence correctly
- Anything the user doesn't see in the first 30 seconds starts after the things they do see

```bash
# Profile current boot and identify what to defer
systemd-analyze blame | head -30
# Look for anything >2 seconds that isn't camera or network
# Those are defer candidates
```

### 3B. Network: Kill the WiFi/AP mode concept entirely

Current problem: 5-minute failover between WiFi and AP mode. User has to understand modes. FANG engineer gave up.

Target: The device is always reachable. The user provides WiFi credentials once. Everything else is invisible.

**The logic, expressed as pseudocode the Claude Code instance should implement:**

```
on_boot:
    start AP mode immediately (always available as fallback)
    # AP is ALWAYS running as a hidden safety net
    
    if stored_wifi_credentials exist:
        attempt wifi connection (timeout: 10 seconds)
        if wifi connects:
            # AP can stay up or be taken down, but the device is on WiFi
            route traffic through WiFi
            mark status = "wifi_connected"
        else:
            # WiFi failed, AP is already running
            mark status = "ap_mode"
            # Retry WiFi every 60 seconds in background
    else:
        # First boot, no credentials
        mark status = "ap_mode_first_boot"
        # App connects via AP and prompts for WiFi credentials
        
    # THE USER NEVER CHOOSES. The device is always reachable via one path.
```

**Implementation with NetworkManager:**

```bash
# Create a persistent AP connection profile that auto-starts
nmcli connection add type wifi ifname wlan0 con-name WIMZ-AP \
    autoconnect yes wifi.mode ap wifi.ssid "WIMZ-XXXX" \
    ipv4.method shared ipv4.addresses 192.168.4.1/24

# On stored WiFi credentials, add that profile too
nmcli connection add type wifi ifname wlan0 con-name home-wifi \
    autoconnect yes wifi.ssid "HomeNetwork" wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "password" connection.autoconnect-priority 10

# Priority system: WiFi has higher priority than AP
# NetworkManager tries WiFi first, falls back to AP automatically
# No code needed for "switching modes"
```

**Critical: The Pi 5 has ONE WiFi interface (wlan0).** Running AP and client WiFi simultaneously on a single radio is possible (using virtual interfaces) but flaky on some drivers. The cleaner approach for production:

1. Boot with AP always available
2. Try WiFi in background
3. If WiFi connects, hand off. AP goes dormant but can be reactivated instantly
4. If WiFi drops mid-session, AP reactivates within seconds
5. App handles the routing transparently (checks both endpoints, uses whichever responds)

**App-side changes (Flutter):**
- Remove any "Switch to AP mode" or "Switch to WiFi" buttons
- The app tries both endpoints (WiFi IP and AP IP 192.168.4.1) simultaneously
- Whichever responds first is used
- If the connection drops, retry both
- Show "Connected via WiFi" or "Connected directly" as a subtle status indicator, not a choice

### 3C. WiFi credential entry (first boot flow)

The first time the device boots, there are no stored WiFi credentials. The flow:

1. Device boots, AP starts, LEDs pulse
2. User downloads app (QR on box or quick-start card)
3. App auto-discovers the robot via BLE advertisement or mDNS on AP
4. App shows "Found your WIM-Z!" with the camera feed already live
5. App says "Connect to your home WiFi for the best experience" with a WiFi picker
6. User selects network, enters password
7. Robot connects to WiFi, app follows, camera feed continues uninterrupted
8. Done. User is already seeing their dog. Total time: under 2 minutes.

The user never typed an IP address, never selected a mode, never read a manual.

---

## 4. IDIOT-PROOFING REVIEW

Context: A professional software engineer could not figure out how to switch from WiFi to AP mode and could not start the Xbox controller. From this point forward, assume every user interaction will be attempted incorrectly, in the wrong order, with the wrong expectations, and without reading any instructions.

### 4A. Xbox/Gamepad controller

**Known failure:** User did not know which button to press to start WiFi/pairing.

**Fixes:**
- The controller must work the instant it is turned on. No pairing mode button. No special sequence. Bluetooth auto-reconnect if previously paired. If not paired, the app walks through pairing with a picture of the exact button to hold.
- Every button on the controller must do something or do nothing. No button should cause a confusing or destructive state. Map all unmapped buttons to no-op. The worst outcome of pressing any button should be "nothing happened."
- If the controller disconnects (battery, range, Bluetooth hiccup), the robot must stop moving immediately (failsafe) and the app must show "Controller disconnected, reconnecting..." not just stop responding.
- The app should show a visual controller map on first use: a picture of the controller with labels on each button. One screen, not a manual.

**Review checklist for gamepad.py:**
```
[ ] What happens if the user turns on the controller before the robot?
[ ] What happens if the user turns on the controller after the robot is already in a session?
[ ] What happens if Bluetooth drops mid-drive?
[ ] What happens if the user presses every button simultaneously?
[ ] What happens if the user holds a trigger down for 60 seconds?
[ ] What happens if two controllers try to connect?
[ ] Does the deadzone (0.30) prevent drift on all tested controllers or just the one Morgan tested?
[ ] What is the failure mode if the controller battery dies while driving? Does the robot stop or keep going at the last command?
```

### 4B. Comprehensive failure mode audit

Go through every user-facing interaction and answer: "What happens when an idiot does this wrong?"

**Power:**
```
[ ] User unplugs during boot: does the robot corrupt its filesystem/database?
    Fix: read-only root filesystem or journaling protection
[ ] User leaves it on the charger for a week: does the battery overcharge?
    Fix: BMS handles this, but verify
[ ] Battery dies mid-session: does the robot save state and shut down cleanly?
    Fix: low-voltage interrupt triggers graceful shutdown
[ ] User plugs in the wrong charger: protected by connector type?
```

**App:**
```
[ ] User deletes and reinstalls the app: does it find the robot again without re-setup?
    Fix: robot stores its identity, app re-discovers via BLE/mDNS
[ ] User has two phones: can both connect? Should they? What happens?
[ ] User's phone goes to sleep during a driving session: what happens to the robot?
    Fix: robot stops on connection loss, does not continue last command
[ ] User force-quits the app during a training mission: does the mission hang forever?
[ ] User has no internet but has WiFi (captive portal): does the robot work?
    Fix: the robot is local-first, no internet required for core function
[ ] User's phone is on cellular but robot is on home WiFi: can they connect?
    Fix: relay server (api.wimzai.com) bridges this, but verify it works
```

**Dog interaction (the user is the idiot, the dog is the agent):**
```
[ ] Dog knocks robot over: does it detect orientation change and stop motors?
[ ] Dog chews on the charging cable while plugged in: is there current-limit protection?
[ ] Dog paws the power button: is the power button recessed or requires a long-press?
[ ] Dog sits on top of the robot: does weight/stall-current detection stop the motors?
[ ] Treats run out mid-session: does the robot detect empty carousel and notify the user?
    Fix: track dispense count, warn at low, stop dispensing at empty
[ ] User loads wrong-sized treats: does the carousel jam detection and recovery work?
    Fix: StallGuard on TMC2209 handles this, but verify UX when it triggers
```

**Network edge cases:**
```
[ ] WiFi password changes: robot can't connect, what does the user see?
    Fix: fall back to AP, app prompts for new credentials
[ ] Router reboots: does the robot reconnect automatically?
[ ] User takes robot to a friend's house: new WiFi, no stored credentials
    Fix: fall back to AP, app prompts for new WiFi at new location
[ ] Multiple WIM-Z robots on the same network: do they conflict?
    Fix: unique mDNS names (WIMZ-XXXX based on serial)
[ ] User is on a 5GHz-only network: does the Pi's WiFi support 5GHz?
    Fix: Pi 5 supports 2.4/5GHz, but verify AP mode works on both bands
```

**Audio:**
```
[ ] User's voice is not recorded/loaded: what happens during coaching?
    Fix: fall back to default voice, prompt user to record
[ ] User records their voice in a noisy room: does the audio quality check warn them?
[ ] Dog barks while user is recording voice commands: does it interfere?
[ ] Volume is too loud and scares the dog: is there an easy volume control?
    Fix: volume slider in app, default to low volume on first boot
```

### 4C. The meta-rule for all future development

Before any feature ships, answer this question: "Can an idiot who has never seen this device, has not read any instructions, and will try every wrong thing first, get value from it in under 2 minutes?" If the answer is no, the feature is not done.

Every error message must tell the user what to do, not what went wrong. "Check WiFi settings" is useless. "Tap here to reconnect" is useful. The user does not care why it broke. They care how to fix it. One tap, one action, no jargon.

Every mode, setting, and configuration option that exists only because the engineer needed it during development must be hidden or removed from the user-facing product. AP mode, resolution selection, manual servo calibration, debug overlays: these are developer tools, not user features. Hide them behind a developer menu (tap serial number 7 times, or similar), never in the main UI.

---

## Execution priority

1. Run the diagnostic tests (Section 1). Takes 30 minutes. Produces data that determines whether Section 2 is a fix or a confirmed non-issue.
2. Implement the stream decoupling (Section 2). Independent of diagnostics, this is correct regardless.
3. Boot sequence overhaul (Section 3A). Sign-of-life in 5 seconds, camera in 30.
4. Kill WiFi/AP mode switching (Section 3B). Never expose network topology to the user.
5. Idiot-proof audit (Section 4). Go through every checklist item, file issues for each failure.

Items 1-2 are one Claude Code session on the robot.
Items 3-4 are a second session focused on systemd and NetworkManager.
Item 5 is a review pass that produces a punch list for follow-up sessions.

---

## Changelog
- v0.1 (June 21, 2026): Initial work order. Covers FPS diagnostics, encode decoupling, boot/network overhaul, and idiot-proofing audit.
