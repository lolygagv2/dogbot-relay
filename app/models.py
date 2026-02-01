from pydantic import BaseModel, EmailStr, Field
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


class AuthResult(BaseModel):
    type: Literal["auth_result"] = "auth_result"
    success: bool
    message: Optional[str] = None


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
