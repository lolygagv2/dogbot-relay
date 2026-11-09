from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from typing import Optional, Literal
from datetime import datetime
from enum import Enum


# ============== Enums ==============

class RobotMode(str, Enum):
    IDLE = "idle"
    GUARDIAN = "guardian"
    TRAINING = "training"
    MANUAL = "manual"
    DOCKING = "docking"


class LEDPattern(str, Enum):
    BREATHING = "breathing"
    RAINBOW = "rainbow"
    CELEBRATION = "celebration"
    SEARCHING = "searching"
    ALERT = "alert"
    IDLE = "idle"


class ErrorCode(str, Enum):
    LOW_BATTERY = "LOW_BATTERY"
    OVERHEAT = "OVERHEAT"
    MOTOR_FAULT = "MOTOR_FAULT"
    CAMERA_FAULT = "CAMERA_FAULT"
    NETWORK_ERROR = "NETWORK_ERROR"


class ErrorSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class DogColor(str, Enum):
    BLACK = "black"
    YELLOW = "yellow"
    BROWN = "brown"
    WHITE = "white"
    MIXED = "mixed"


class DogRole(str, Enum):
    OWNER = "owner"
    CARETAKER = "caretaker"
    VIEWER = "viewer"


# ============== User & Auth Models ==============

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class User(BaseModel):
    user_id: str
    email: EmailStr
    devices: list[str] = []
    created_at: datetime


class TokenResponse(BaseModel):
    token: str
    expires_in: int
    user_id: Optional[str] = None


class AuthResult(BaseModel):
    type: Literal["auth_result"] = "auth_result"
    success: bool
    message: Optional[str] = None


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=8)


# ============== Dog Models ==============

class DogCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    breed: Optional[str] = None
    color: Optional[DogColor] = None
    aruco_marker_id: Optional[int] = None


class DogUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    breed: Optional[str] = None
    color: Optional[DogColor] = None
    profile_photo_url: Optional[str] = None
    aruco_marker_id: Optional[int] = None


class DogPhotoCreate(BaseModel):
    photo_url: str
    is_profile_photo: bool = False


class Dog(BaseModel):
    id: str
    name: str
    breed: Optional[str] = None
    color: Optional[DogColor] = None
    profile_photo_url: Optional[str] = None
    aruco_marker_id: Optional[int] = None
    role: Optional[DogRole] = None
    created_at: datetime


class DogPhoto(BaseModel):
    id: str
    dog_id: str
    photo_url: str
    is_profile_photo: bool
    captured_at: datetime


# ============== Device Models ==============

class DeviceRegister(BaseModel):
    device_id: str
    firmware_version: str


class DeviceRegisterResponse(BaseModel):
    success: bool
    websocket_url: str


class DevicePair(BaseModel):
    pairing_code: str


class DevicePairResponse(BaseModel):
    success: bool
    device_id: str


class UserPairDeviceRequest(BaseModel):
    device_id: str = Field(..., min_length=1)


class UserPairDeviceResponse(BaseModel):
    success: bool
    device_id: str
    message: str


class Device(BaseModel):
    device_id: str
    name: str = "WIM-Z Robot"
    owner_id: Optional[str] = None
    is_online: bool = False
    last_seen: Optional[datetime] = None
    firmware_version: str = "1.0.0"
    local_ip: Optional[str] = None
    pairing_code: Optional[str] = None


# ============== Telemetry & Status ==============

class Telemetry(BaseModel):
    battery: float = 100.0
    temperature: float = 25.0
    mode: RobotMode = RobotMode.IDLE
    dog_detected: bool = False
    current_behavior: Optional[str] = None
    confidence: Optional[float] = None
    is_charging: bool = False
    treats_remaining: int = 15
    last_treat_time: Optional[datetime] = None
    active_mission_id: Optional[str] = None
    wifi_strength: int = -50
    uptime_seconds: int = 0


class Detection(BaseModel):
    detected: bool
    behavior: Optional[str] = None
    confidence: Optional[float] = None
    bbox: Optional[list[float]] = None
    timestamp: datetime


# ============== Mission Models ==============

class Mission(BaseModel):
    id: str
    name: str
    description: str
    target_behavior: str
    required_duration: float = 3.0
    cooldown_seconds: int = 15
    daily_limit: int = 10
    is_active: bool = False
    rewards_given: int = 0
    progress: float = 0.0
    success_count: int = 0
    fail_count: int = 0
    created_at: datetime
    last_run: Optional[datetime] = None


# ============== WebSocket Events (Robot -> App) ==============

class DetectionEvent(BaseModel):
    event: Literal["detection"] = "detection"
    data: dict
    timestamp: datetime


class StatusEvent(BaseModel):
    event: Literal["status"] = "status"
    data: dict
    timestamp: datetime


class TreatEvent(BaseModel):
    event: Literal["treat"] = "treat"
    data: dict
    timestamp: datetime


class MissionEvent(BaseModel):
    event: Literal["mission"] = "mission"
    data: dict
    timestamp: datetime


class ErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    data: dict
    timestamp: datetime


# ============== WebSocket Commands (App -> Robot) ==============

class MotorCommand(BaseModel):
    command: Literal["motor"] = "motor"
    left: float = Field(..., ge=-1.0, le=1.0)
    right: float = Field(..., ge=-1.0, le=1.0)


class ServoCommand(BaseModel):
    command: Literal["servo"] = "servo"
    pan: float = Field(..., ge=-90.0, le=90.0)
    tilt: float = Field(..., ge=-45.0, le=45.0)


class TreatCommand(BaseModel):
    command: Literal["treat"] = "treat"


class LEDCommand(BaseModel):
    command: Literal["led"] = "led"
    pattern: LEDPattern


class AudioCommand(BaseModel):
    command: Literal["audio"] = "audio"
    file: str


class ModeCommand(BaseModel):
    command: Literal["mode"] = "mode"
    mode: RobotMode


# ============== WebSocket Messages ==============

class WSAuth(BaseModel):
    type: Literal["auth"] = "auth"
    token: str
    device_id: Optional[str] = None


class WSPing(BaseModel):
    type: Literal["ping"] = "ping"


class WSPong(BaseModel):
    type: Literal["pong"] = "pong"


# ============== REST API Responses ==============

class SuccessResponse(BaseModel):
    success: bool = True


class ErrorResponse(BaseModel):
    success: bool = False
    error: dict


# ============== Metrics Models ==============

class MetricEventRequest(BaseModel):
    dog_id: str
    metric_type: str = Field(..., description="One of: treat_count, detection_count, session_minutes")
    value: int = 1
    mission_type: Optional[str] = None
    mission_result: Optional[str] = None
    details: Optional[str] = None


class HealthResponse(BaseModel):
    status: str = "ok"


# ============== Event Storage Models (Build 46) ==============

class DeviceEvent(BaseModel):
    id: int
    device_id: str
    event_type: str
    message: dict
    timestamp: str


class DeviceEventListResponse(BaseModel):
    events: list[DeviceEvent]
    total: int
    limit: int
    offset: int


class DailyScore(BaseModel):
    date: str
    score: int


class DeviceEventSummaryResponse(BaseModel):
    daily_scores: list[DailyScore]
    total_treats: int
    total_tricks: int
    total_barks: int
    active_minutes: int
    period_days: int


# ============== Schedule Models (Build 34 - Updated Build 35) ==============

class ScheduleType(str, Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"


# Day name to number mapping
DAY_NAME_TO_NUM = {
    "sunday": 0, "sun": 0,
    "monday": 1, "mon": 1,
    "tuesday": 2, "tue": 2,
    "wednesday": 3, "wed": 3,
    "thursday": 4, "thu": 4,
    "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
}


class ScheduleCreate(BaseModel):
    """Schedule creation - accepts both relay format and app format."""
    # App format fields (primary)
    schedule_id: Optional[str] = None
    mission_name: Optional[str] = None
    start_time: Optional[str] = None  # "HH:MM" format
    end_time: Optional[str] = None    # "HH:MM" format
    days_of_week: Optional[list[str]] = None  # ["monday", "tuesday"]
    cooldown_hours: Optional[int] = None

    # Relay format fields (fallback)
    id: Optional[str] = None
    mission_id: Optional[str] = None
    hour: Optional[int] = Field(None, ge=0, le=23)
    minute: Optional[int] = Field(None, ge=0, le=59)
    weekdays: Optional[list[int]] = None  # [0, 1, 2] for Sun, Mon, Tue

    # Common fields
    dog_id: str
    name: Optional[str] = None
    type: ScheduleType = ScheduleType.DAILY
    enabled: bool = True

    def get_schedule_id(self) -> Optional[str]:
        return self.schedule_id or self.id

    def get_mission_id(self) -> str:
        return self.mission_name or self.mission_id or ""

    def get_hour(self) -> int:
        if self.start_time:
            try:
                return int(self.start_time.split(":")[0])
            except (ValueError, IndexError):
                return 9
        return self.hour or 9

    def get_minute(self) -> int:
        if self.start_time:
            try:
                return int(self.start_time.split(":")[1])
            except (ValueError, IndexError):
                return 0
        return self.minute or 0

    def get_weekdays(self) -> Optional[list[int]]:
        if self.days_of_week:
            return [DAY_NAME_TO_NUM.get(d.lower(), 0) for d in self.days_of_week if d.lower() in DAY_NAME_TO_NUM]
        return self.weekdays


class ScheduleUpdate(BaseModel):
    """Schedule update - accepts both formats."""
    # App format
    mission_name: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    days_of_week: Optional[list[str]] = None
    cooldown_hours: Optional[int] = None

    # Relay format
    mission_id: Optional[str] = None
    hour: Optional[int] = Field(None, ge=0, le=23)
    minute: Optional[int] = Field(None, ge=0, le=59)
    weekdays: Optional[list[int]] = None

    # Common
    dog_id: Optional[str] = None
    name: Optional[str] = None
    type: Optional[ScheduleType] = None
    enabled: Optional[bool] = None


class Schedule(BaseModel):
    """Schedule response - returns both formats for compatibility."""
    id: str
    schedule_id: Optional[str] = None  # Alias for app compatibility
    user_id: str
    dog_id: str
    mission_id: str
    mission_name: Optional[str] = None  # Alias for app compatibility
    name: Optional[str] = None
    type: ScheduleType
    hour: int
    minute: int
    start_time: Optional[str] = None  # "HH:MM" for app compatibility
    end_time: Optional[str] = None
    weekdays: Optional[list[int]] = None
    days_of_week: Optional[list[str]] = None  # For app compatibility
    cooldown_hours: Optional[int] = None
    enabled: bool
    next_run: Optional[str] = None
    created_at: str
    updated_at: str


class ScheduleListResponse(BaseModel):
    schedules: list[Schedule]
    scheduling_enabled: bool


# ============== Dec 9 demo build (2026-11-06): event store ==============

EVENT_TYPES = (
    "bark_episode", "intervention", "settle", "pacing", "departure", "arrival",
    "treat", "music", "mission_run", "status", "mode_change",
    "coach_session", "trick_attempt",
)


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    type: Literal[
        "bark_episode", "intervention", "settle", "pacing", "departure", "arrival",
        "treat", "music", "mission_run", "status", "mode_change",
        "coach_session", "trick_attempt",
    ]
    # coach_session / trick_attempt (2026-11-08): see event_store.validate_event for the shapes.
    # mode_change (2026-11-07): device-scoped like status; data.mode is the mode just entered.
    # With dog_id it counts toward that dog only (e.g. a Coach session), without it toward all
    # of the owner's dogs. Rolled up as totals.sg_s / totals.coach_s.
    # status: device-scoped, stored with dog_id NULL when omitted (contract fix 2026-11-06).
    # Other types: required, except a robot whose owner has exactly one dog may omit it and
    # the relay fills in that dog (owner model 2026-11-07).
    dog_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    ts: str = Field(..., description="ISO-8601 timestamp; naive = UTC")
    duration_s: Optional[float] = Field(default=None, ge=0, le=86400)
    data: dict = Field(default_factory=dict)
    device_id: Optional[str] = None   # ignored for robot callers (taken from HMAC identity)



class EventBatchIn(BaseModel):
    events: list[EventIn] = Field(..., min_length=1, max_length=200)


class EventRejected(BaseModel):
    event_id: str
    reason: str


class EventBatchResult(BaseModel):
    accepted: list[str] = Field(default_factory=list)
    duplicate: list[str] = Field(default_factory=list)
    rejected: list[EventRejected] = Field(default_factory=list)


# ============== Settings value schemas (one per key) ==============

class QuietHours(BaseModel):
    """Opt-in (2026-11-07). When enabled, suppresses ONLY: music, proactive agent speech (boot
    greeting, low hopper), assistant speak requests, and non-urgent notifications. Bark alerts
    and the SG re-arm line always get through."""
    enabled: bool = False
    start: str = Field(default="22:00", pattern=r"^\d{2}:\d{2}$")
    end: str = Field(default="07:00", pattern=r"^\d{2}:\d{2}$")


class TreatWindow(BaseModel):
    start: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    end: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    multiplier: float = Field(default=1.0, ge=0, le=3)


class TreatSchedule(BaseModel):
    daily_cap: int = Field(default=10, ge=0, le=100)
    # False = no daily cap at all ("unlimited", the app's toggle off). daily_cap keeps the
    # last number so switching back on restores it. Hourly limit and windows still apply.
    daily_cap_enabled: bool = True
    default_multiplier: float = Field(default=1.0, ge=0, le=3)
    max_per_hour: int = Field(default=0, ge=0, le=60)   # 0 = no hourly limit (no app control for it)
    windows: list[TreatWindow] = Field(default_factory=list)


class SgSensitivity(BaseModel):
    level: Literal["low", "medium", "high"] = "medium"
    min_episode_s: int = Field(default=5, ge=1, le=600)
    escalate_after_s: int = Field(default=60, ge=5, le=3600)


class MusicOnDeparture(BaseModel):
    track: str = "song_calm_01"
    delay_s: int = Field(default=60, ge=0, le=3600)
    duration_min: int = Field(default=30, ge=1, le=480)


class MusicSlot(BaseModel):
    time: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    track: str
    duration_min: int = Field(default=20, ge=1, le=480)


class MusicPlan(BaseModel):
    enabled: bool = True
    volume: int = Field(default=40, ge=0, le=100)
    on_departure: Optional[MusicOnDeparture] = Field(default_factory=MusicOnDeparture)
    schedule: list[MusicSlot] = Field(default_factory=list)


NOTIFY_APP_TYPES = (
    "bark", "treatDispensed", "coachReward", "alert", "missionStarted",
    "missionCompleted", "missionFailed", "lowBattery",
)


class NotifyWhen(BaseModel):
    min_duration_s: Optional[float] = None
    max_duration_s: Optional[float] = None
    severity_in: Optional[list[str]] = None
    result_in: Optional[list[str]] = None
    source_in: Optional[list[str]] = None
    kind_in: Optional[list[str]] = None
    data_eq: Optional[dict] = None


class NotifyRule(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    enabled: bool = True
    event_type: Literal[
        "bark_episode", "intervention", "settle", "pacing", "departure", "arrival",
        "treat", "music", "mission_run",
    ]
    when: NotifyWhen = Field(default_factory=NotifyWhen)
    cooldown_s: int = Field(default=0, ge=0, le=86400)
    attach: Literal["none", "still", "clip"] = "none"
    app_type: Literal[
        "bark", "treatDispensed", "coachReward", "alert", "missionStarted",
        "missionCompleted", "missionFailed", "lowBattery",
    ] = "alert"
    title: str = Field(default="WIM-Z", max_length=120)
    body: str = Field(default="", max_length=240)
    override_quiet: bool = False


class NotifyRules(BaseModel):
    rules: list[NotifyRule] = Field(default_factory=list)


class MissionTrigger(BaseModel):
    kind: Literal["schedule", "departure", "manual"] = "manual"
    time: Optional[str] = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    delay_s: int = Field(default=300, ge=0, le=7200)
    cooldown_s: int = Field(default=7200, ge=0, le=86400)
    days: Optional[list[str]] = None


# Mode missions (2026-11-07): when one fires, the robot enters that mode for duration_s, then idle.
MODE_MISSION_TYPES = ("silent_guardian", "coach")
TRAINING_MAX_DURATION_S = 3600
MODE_MAX_DURATION_S = 12 * 3600


class MissionDef(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    type: str = Field(..., min_length=1, max_length=64)
    name: str = Field(default="", max_length=120)
    enabled: bool = True
    trigger: MissionTrigger = Field(default_factory=MissionTrigger)
    duration_s: int = Field(default=120, ge=10, le=MODE_MAX_DURATION_S)
    reward_treats: int = Field(default=1, ge=0, le=3)
    goal: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def _training_duration_cap(self):
        if self.type not in MODE_MISSION_TYPES and self.duration_s > TRAINING_MAX_DURATION_S:
            raise ValueError(f"duration_s may be at most {TRAINING_MAX_DURATION_S} for a {self.type} mission")
        return self


class Missions(BaseModel):
    missions: list[MissionDef] = Field(default_factory=list)


class SoundDef(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    kind: Literal["sound", "song"] = "sound"   # songs are sounds too (amendment 2026-11-06)
    label: str = Field(default="", max_length=120)
    builtin: bool = True
    file: str = Field(..., max_length=160)
    url: Optional[str] = None


class Sounds(BaseModel):
    sounds: list[SoundDef] = Field(default_factory=list)


class SettingPut(BaseModel):
    value: object
    expected_version: Optional[int] = None


# ============== Robot control ==============

class SpeakRequest(BaseModel):
    """POST /api/dogs/{dog_id}/speak. Text only: no voice, URL or audio fields are accepted."""
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=280)


class CommandRequest(BaseModel):
    command: Literal[
        "dispense_treat", "set_mode", "play_sound", "start_mission", "go_to", "move",
        "take_photo", "standby", "run_trick", "listen", "play_tts",  # play_tts: voice agent only (allowlist)
        "schedule_sync",
    ]
    data: dict = Field(default_factory=dict)
    device_id: Optional[str] = None
    # Wait up to N seconds for the robot's command_result (amendment 2026-11-06).
    wait_s: Optional[float] = Field(default=None, ge=0, le=90)


class MissionDraftCreate(BaseModel):
    draft: MissionDef
    warnings: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=240)


class MissionDraftDecision(BaseModel):
    decision: Literal["approve", "decline"]


class AgentTranscriptIn(BaseModel):
    role: Literal["user", "agent"]
    text: str = Field(..., max_length=2000)
    ts: Optional[str] = None


# ============== Replayer ==============

class ReplayLoad(BaseModel):
    seed: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]+\.json$")
    dog_id: Optional[str] = None
    device_id: Optional[str] = None
    date: Optional[str] = None  # YYYY-MM-DD; default today in the seed timezone


class ReplaySpeed(BaseModel):
    speed: float = Field(default=60, ge=1, le=3600)


class ReplayJump(BaseModel):
    to: Optional[str] = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    offset_s: Optional[float] = Field(default=None, ge=0)
