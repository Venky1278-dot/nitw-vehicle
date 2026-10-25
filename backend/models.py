from typing import Optional, List, Tuple
from pydantic import BaseModel, Field

# Single source of truth for these enums lives with the vehicle state machine.
from vehicle_simulator import Mode, LockState  # noqa: F401


# ---- Auth -----------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    expires_in: int
    idle_timeout_s: int


# ---- Network / map ----------------------------------------------------------
class TerminalOut(BaseModel):
    id: int
    name: str
    lat: float
    lon: float
    node: int
    offset_m: float


class NetworkOut(BaseModel):
    nodes: List[Tuple[float, float]]
    edges: List[Tuple[int, int, float]]
    terminals: List[TerminalOut]


# ---- Commands ---------------------------------------------------------------
class ModeRequest(BaseModel):
    mode: Mode


class NavigateRequest(BaseModel):
    destination_id: int


class JoystickRequest(BaseModel):
    x: float = Field(ge=-1.0, le=1.0, description="steering, -1=left .. 1=right")
    y: float = Field(ge=-1.0, le=1.0, description="throttle, -1=reverse .. 1=forward")


class OTPVerifyRequest(BaseModel):
    code: str


class NavigateResponse(BaseModel):
    """The route polyline is sent ONCE here, not on every telemetry tick -
    a real LoRa packet is far too small to carry a full route, so the
    backend (which computed the route) hands it to the browser directly and
    the vehicle link only ever carries live position after this point."""
    destination_name: str
    route: List[Tuple[float, float]]
    distance_m: float
    eta_seconds: float


# ---- Telemetry (what the frontend receives over the WebSocket) -------------
class TelemetryOut(BaseModel):
    seq: int
    ts: float
    lat: float
    lon: float
    heading_deg: float
    speed_kmph: float
    battery_pct: float
    mode: Mode
    lock_state: LockState
    destination_name: Optional[str] = None
    distance_remaining_m: Optional[float] = None
    eta_seconds: Optional[float] = None
    arrived_event: bool = False
    otp_debug: Optional[str] = None  # DEMO-ONLY: remove once a real OTP channel is wired up
    link_latency_ms: Optional[float] = None
