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


class HealthResponse(BaseModel):
    status: str = "ok"
