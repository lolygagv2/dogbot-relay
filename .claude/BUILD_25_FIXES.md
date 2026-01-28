# Build 25 Fix Specification

> Addresses stability issues, functionality bugs, and data model problems discovered in Build 24 testing.

**Root Cause Summary:** Memory exhaustion (98.3%) caused robot freeze. Triggered by:
1. Bark classifier loading when not needed (and loading TWICE)
2. Audio queue filling without drain
3. WebRTC death spiral (reconnect loop with no backoff)
4. Camera reconfiguration while streaming

---

## Wave 1: Stability Fixes (P0)

### FIX 1.1: WebRTC Reconnect Debounce (APP)

**Problem:** When WebRTC fails, app immediately retries in tight loop causing relay/robot thrashing.

**Solution:** Add exponential backoff with max retries.

**File:** `lib/services/webrtc_service.dart`

```dart
// Add to WebRTCService class
int _reconnectAttempts = 0;
static const int _maxReconnectAttempts = 3;
static const Duration _baseReconnectDelay = Duration(seconds: 5);
Timer? _reconnectTimer;

Future<void> _handleConnectionFailure() async {
  if (_reconnectAttempts >= _maxReconnectAttempts) {
    _logger.warning('WebRTC: Max reconnect attempts reached');
    _notifyConnectionFailed();  // Show UI error
    return;
  }
  
  _reconnectAttempts++;
  final delay = _baseReconnectDelay * _reconnectAttempts;
  _logger.info('WebRTC: Reconnecting in ${delay.inSeconds}s (attempt $_reconnectAttempts)');
  
  _reconnectTimer?.cancel();
  _reconnectTimer = Timer(delay, () {
    connect();
  });
}

void _resetReconnectState() {
  _reconnectAttempts = 0;
  _reconnectTimer?.cancel();
}

// Call _resetReconnectState() on successful connection
// Call _handleConnectionFailure() on connection lost/failed
```

**UI Change:** Show "Connection failed - tap to retry" after max attempts instead of infinite spinner.

---

### FIX 1.2: Conditional Bark Detection (ROBOT)

**Problem:** Bark detector starts for ALL missions, even those that don't use it. Wastes memory and CPU.

**Solution:** Missions declare `requires_bark_detection: true` in their JSON. Only start bark detector if needed.

**File:** `missions/*.json` - Add field:
```json
{
  "name": "Sit Training",
  "requires_bark_detection": false,
  ...
}
```

**File:** `main_treatbot.py` - Check before starting:

```python
def _on_mode_change(self, old_mode: SystemMode, new_mode: SystemMode, reason: str):
    # ... existing code ...
    
    if new_mode == SystemMode.MISSION:
        mission = self.mission_engine.current_mission
        if mission and mission.get('requires_bark_detection', False):
            self._start_bark_detection()
        else:
            self.logger.info("🎤 Bark detection NOT needed for this mission")
    elif new_mode == SystemMode.SILENT_GUARDIAN:
        self._start_bark_detection()
    else:
        self._stop_bark_detection()
```

**Default missions that need bark detection:**
- `quiet_time.json` - Yes
- `sit.json` - No
- `come_and_sit.json` - No

---

### FIX 1.3: Bark Classifier Singleton (ROBOT)

**Problem:** Bark classifier model loaded TWICE (lines 504-510 in logs).

**Solution:** Ensure singleton pattern in bark_classifier.py

**File:** `ai/bark_classifier.py`

```python
_instance = None

class BarkClassifier:
    def __new__(cls, *args, **kwargs):
        global _instance
        if _instance is None:
            _instance = super().__new__(cls)
            _instance._initialized = False
        return _instance
    
    def __init__(self, model_path: str = None):
        if self._initialized:
            return
        # ... existing init code ...
        self._initialized = True
        self.logger.info("Bark classifier initialized (singleton)")
```

---

### FIX 1.4: Audio Queue Drain on Mode Exit (ROBOT)

**Problem:** Audio queue fills up, drops chunks, never drains when exiting mode.

**Solution:** Properly stop and drain audio on mode transitions.

**File:** `audio/bark_buffer_arecord.py`

```python
def stop(self):
    """Stop recording and drain queue."""
    self._running = False
    
    # Drain the queue
    drained = 0
    while not self._queue.empty():
        try:
            self._queue.get_nowait()
            drained += 1
        except:
            break
    
    self.logger.info(f"Audio queue drained: {drained} chunks")
    
    # Kill arecord process
    if self._process:
        self._process.terminate()
        self._process.wait(timeout=2)
        self._process = None
```

**File:** `core/audio/bark_detector.py`

```python
def stop(self):
    """Stop bark detection and cleanup."""
    self.logger.info("BarkDetector stopping...")
    self._running = False
    
    if self._buffer:
        self._buffer.stop()
        self._buffer = None
    
    # Don't destroy classifier singleton, just release reference
    self._classifier = None
    
    self.logger.info("BarkDetector stopped")
```

---

### FIX 1.5: Camera Reconfig with WebRTC Pause (ROBOT)

**Problem:** Camera reconfigures to 640x640 while WebRTC is streaming 1080p, causing conflicts.

**Solution:** Pause WebRTC stream, reconfigure camera, resume.

**File:** `services/webrtc_service.py`

```python
async def pause_for_camera_reconfig(self):
    """Pause video track during camera reconfiguration."""
    if self._video_track:
        self._video_track.pause()
        self.logger.info("WebRTC video paused for camera reconfig")

async def resume_after_camera_reconfig(self):
    """Resume video track after camera reconfiguration."""
    if self._video_track:
        self._video_track.resume()
        self.logger.info("WebRTC video resumed after camera reconfig")
```

**File:** `services/detector_service.py` - When switching resolution:

```python
async def _switch_to_detection_resolution(self):
    """Switch camera to 640x640 for AI detection."""
    # Notify WebRTC to pause
    await self.event_bus.publish('webrtc.pause_requested', {})
    
    # Wait for pause confirmation or timeout
    await asyncio.sleep(0.5)
    
    # Reconfigure camera
    self._reconfigure_camera(640, 640)
    
    # Resume WebRTC
    await self.event_bus.publish('webrtc.resume_requested', {})
```

---

### FIX 1.6: Memory Protection (ROBOT)

**Problem:** System hits 98.3% memory and becomes unresponsive.

**Solution:** Reject new commands at 95%, force cleanup at 90%.

**File:** `core/safety.py`

```python
def check_memory_before_command(self, command: str) -> bool:
    """Return False to reject command if memory critical."""
    mem = psutil.virtual_memory()
    
    if mem.percent >= 95:
        self.logger.error(f"❌ Rejecting command '{command}' - memory at {mem.percent}%")
        return False
    
    if mem.percent >= 90:
        self.logger.warning(f"⚠️ Memory at {mem.percent}% - triggering cleanup")
        self._trigger_memory_cleanup()
    
    return True

def _trigger_memory_cleanup(self):
    """Force garbage collection and clear caches."""
    import gc
    gc.collect()
    
    # Clear any frame caches
    self.event_bus.publish_sync('memory.cleanup_requested', {})
```

**File:** `main_treatbot.py` - Check before processing commands:

```python
async def _process_command(self, command: str, params: dict):
    if not self.safety_monitor.check_memory_before_command(command):
        self.logger.warning(f"Command {command} rejected due to memory pressure")
        return {'status': 'error', 'message': 'System memory critical'}
    
    # ... existing command processing ...
```

---

## Wave 2: Functionality Fixes (P1)

### FIX 2.1: Don't Override Mission Mode in Drive Screen (APP)

**Problem:** Entering Drive screen forces Manual mode even when Mission is active.

**Solution:** Only switch to Manual if NOT in Mission mode.

**File:** `lib/screens/drive_screen.dart`

```dart
@override
void initState() {
  super.initState();
  
  // Only switch to manual if not in mission
  final currentMode = context.read<RobotProvider>().currentMode;
  if (currentMode != 'mission') {
    _sendManualControlActive();
  } else {
    _logger.info('Drive screen: Keeping mission mode active');
    _showMissionActiveIndicator = true;
  }
}

@override  
void dispose() {
  final currentMode = context.read<RobotProvider>().currentMode;
  if (currentMode != 'mission') {
    _sendManualControlInactive();
  }
  super.dispose();
}
```

**UI Addition:** Show mission indicator overlay when `_showMissionActiveIndicator` is true:
```dart
if (_showMissionActiveIndicator)
  Positioned(
    top: 16,
    left: 16,
    child: Container(
      padding: EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: Colors.orange.withOpacity(0.9),
        borderRadius: BorderRadius.circular(16),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.flag, size: 16, color: Colors.white),
          SizedBox(width: 4),
          Text('Mission Active', style: TextStyle(color: Colors.white)),
        ],
      ),
    ),
  ),
```

---

### FIX 2.2: Custom Voice Lookup Path (ROBOT)

**Problem:** Custom voice recordings not playing, falls back to defaults.

**Solution:** Check correct path structure: `/voices/custom/{dog_id}/{command}.mp3`

**File:** `coaching_engine.py`

```python
def play_command(self, command: str, dog_id: str = None) -> bool:
    """Play voice command, preferring custom recordings."""
    
    if dog_id:
        # Check for custom recording
        custom_path = Path(f"/home/morgan/dogbot/VOICEMP3/custom/{dog_id}/{command}.mp3")
        if custom_path.exists():
            self.logger.info(f"🎤 Playing custom voice: {custom_path}")
            return self.audio_service.play_file(str(custom_path))
        else:
            self.logger.debug(f"No custom voice at {custom_path}, using default")
    
    # Fall back to default
    default_path = Path(f"/home/morgan/dogbot/VOICEMP3/talks/{command}.mp3")
    if default_path.exists():
        return self.audio_service.play_file(str(default_path))
    
    self.logger.warning(f"No voice file found for command: {command}")
    return False
```

**File:** `main_treatbot.py` - Ensure dog_id passed to call_dog:

```python
async def _handle_call_dog(self, params: dict):
    dog_id = params.get('dog_id') or params.get('data', {}).get('dog_id')
    dog_name = params.get('dog_name') or params.get('data', {}).get('dog_name')
    
    self.logger.info(f"☁️ Call dog: id={dog_id}, name={dog_name}")
    
    # Try to play custom "name" recording for this dog
    if dog_id:
        played = self.coaching_engine.play_command('name', dog_id)
        if played:
            return {'status': 'ok', 'voice': 'custom'}
    
    # Fall back to generic call
    # ... existing fallback code ...
```

---

### FIX 2.3: Clear Cached State on Logout (APP)

**Problem:** After logout/login, old battery/temp data shows until force-close.

**Solution:** Reset all providers on logout.

**File:** `lib/providers/robot_provider.dart`

```dart
void clearState() {
  _batteryLevel = 0;
  _isCharging = false;
  _temperature = 0.0;
  _currentMode = 'idle';
  _isConnected = false;
  _robotStatus = null;
  notifyListeners();
}
```

**File:** `lib/providers/dog_provider.dart`

```dart
void clearState() {
  _dogs = [];
  _selectedDog = null;
  _selectedDogId = null;
  notifyListeners();
}
```

**File:** `lib/services/auth_service.dart`

```dart
Future<void> logout() async {
  // Close WebSocket
  await _relayService.disconnect();
  
  // Clear all provider state
  _robotProvider.clearState();
  _dogProvider.clearState();
  _analyticsProvider.clearState();
  _missionsProvider.clearState();
  
  // Clear stored token
  await _secureStorage.delete(key: 'auth_token');
  
  // Navigate to login
  _navigationService.navigateToLogin();
}
```

---

### FIX 2.4: Add Sign Out Button to Settings (APP)

**Problem:** No way to sign out from the app.

**Solution:** Add Sign Out option in Settings screen.

**File:** `lib/screens/settings_screen.dart`

```dart
// Add to settings list
ListTile(
  leading: Icon(Icons.logout, color: Colors.red),
  title: Text('Sign Out'),
  onTap: () => _confirmSignOut(context),
),

Future<void> _confirmSignOut(BuildContext context) async {
  final confirmed = await showDialog<bool>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Text('Sign Out'),
      content: Text('Are you sure you want to sign out?'),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(ctx, false),
          child: Text('Cancel'),
        ),
        TextButton(
          onPressed: () => Navigator.pop(ctx, true),
          child: Text('Sign Out', style: TextStyle(color: Colors.red)),
        ),
      ],
    ),
  );
  
  if (confirmed == true) {
    await context.read<AuthService>().logout();
  }
}
```

---

### FIX 2.5: Call Dog Uses Selected Dog (APP)

**Problem:** Call Dog always plays "Bezik" regardless of selected dog.

**Solution:** Pass selected dog_id in call_dog command.

**File:** `lib/services/relay_service.dart`

```dart
Future<void> sendCallDog() async {
  final selectedDog = _dogProvider.selectedDog;
  
  await sendCommand('call_dog', {
    'dog_id': selectedDog?.id,
    'dog_name': selectedDog?.name,
  });
}
```

---

## Wave 3: Data Model Fixes (P2)

### FIX 3.1: Scope Dogs to User ID (RELAY)

**Problem:** All dogs visible to all users.

**Solution:** Add user_id foreign key, filter by authenticated user.

**File:** `app/models/dog.py`

```python
from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship

class Dog(Base):
    __tablename__ = 'dogs'
    
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey('users.id'), nullable=False, index=True)
    name = Column(String, nullable=False)
    breed = Column(String)
    photo_url = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship
    owner = relationship("User", back_populates="dogs")
```

**File:** `app/routers/dogs.py`

```python
@router.get("/dogs")
async def get_dogs(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get dogs for authenticated user only."""
    dogs = db.query(Dog).filter(Dog.user_id == current_user.id).all()
    return {"dogs": [dog.to_dict() for dog in dogs]}

@router.post("/dogs")
async def create_dog(dog_data: DogCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Create dog for authenticated user."""
    dog = Dog(
        id=f"dog_{uuid.uuid4().hex[:8]}",
        user_id=current_user.id,  # Always use authenticated user
        name=dog_data.name,
        breed=dog_data.breed,
    )
    db.add(dog)
    db.commit()
    return dog.to_dict()

@router.delete("/dogs/{dog_id}")
async def delete_dog(dog_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Delete dog - only if owned by user."""
    dog = db.query(Dog).filter(Dog.id == dog_id, Dog.user_id == current_user.id).first()
    if not dog:
        raise HTTPException(404, "Dog not found or not owned by user")
    
    db.delete(dog)
    db.commit()
    return {"status": "deleted"}
```

**Migration:** Add migration to add user_id column to existing dogs table.

---

### FIX 3.2: Fix Delete Dog (APP + RELAY)

**Problem:** Delete dog fails from profile screen.

**Solution:** Ensure correct API call and handle response.

**File:** `lib/providers/dog_provider.dart`

```dart
Future<bool> deleteDog(String dogId) async {
  try {
    final response = await _apiService.delete('/dogs/$dogId');
    
    if (response.statusCode == 200) {
      _dogs.removeWhere((d) => d.id == dogId);
      
      // Clear selection if deleted dog was selected
      if (_selectedDogId == dogId) {
        _selectedDog = null;
        _selectedDogId = null;
      }
      
      notifyListeners();
      return true;
    }
    
    _logger.error('Delete dog failed: ${response.body}');
    return false;
  } catch (e) {
    _logger.error('Delete dog error: $e');
    return false;
  }
}
```

---

### FIX 3.3: Prevent Duplicate Dogs (APP)

**Problem:** Two "Bezik" entries appeared.

**Solution:** Check for existing dog with same name before creating.

**File:** `lib/providers/dog_provider.dart`

```dart
Future<Dog?> createDog(String name, String? breed) async {
  // Check for duplicate
  if (_dogs.any((d) => d.name.toLowerCase() == name.toLowerCase())) {
    _logger.warning('Dog with name "$name" already exists');
    return null;  // Or throw exception
  }
  
  // ... existing create code ...
}
```

**File:** `app/routers/dogs.py` (RELAY)

```python
@router.post("/dogs")
async def create_dog(dog_data: DogCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Check for duplicate name for this user
    existing = db.query(Dog).filter(
        Dog.user_id == current_user.id,
        func.lower(Dog.name) == dog_data.name.lower()
    ).first()
    
    if existing:
        raise HTTPException(400, f"Dog named '{dog_data.name}' already exists")
    
    # ... create dog ...
```

---

## Testing Checklist for Build 25

### Stability Tests
- [ ] WebRTC disconnects gracefully, reconnects with backoff
- [ ] Can enter Mission mode without memory spike
- [ ] Bark detector only runs when mission requires it
- [ ] Robot survives 30+ minutes of continuous use
- [ ] Memory stays below 80% during normal operation

### Functionality Tests
- [ ] Mission mode persists when entering Drive screen
- [ ] Custom voice recordings play for correct dog
- [ ] Call Dog uses selected dog
- [ ] Logout clears all state
- [ ] Sign Out button works

### Data Model Tests
- [ ] User A cannot see User B's dogs
- [ ] Delete dog works
- [ ] Cannot create duplicate dog names

---

## Deployment Order

1. **Robot first** - All Wave 1 stability fixes
2. **Relay second** - Wave 3 data model (dogs table migration)
3. **App last** - All app fixes after server-side is stable

Test robot stability BEFORE deploying app changes.
