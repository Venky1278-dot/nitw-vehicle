"""
Central configuration.

Every number in this file that stands in for a real sensor reading or a real
piece of infrastructure is marked with a tag so it's easy to grep for later:

  # ASSUMED   -> a physical constant you'll need to calibrate from the real
                 vehicle (battery curve, top speed, etc.)
  # DEMO-ONLY -> exists only because there's no real hardware / SMS provider
                 yet. Delete or replace when you wire up the real thing.

Everything here can be overridden with an environment variable of the same
name, so none of this needs to change in code when you move to real hardware
-- e.g. `SIM_SPEED_KMPH=15 uvicorn main:app`.
"""
import os
from pathlib import Path

def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))

def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))

def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    return default if val is None else val.lower() in ("1", "true", "yes")

BASE_DIR = Path(__file__).resolve().parent
NETWORK_JSON_PATH = BASE_DIR / "data" / "nitw_campus_path_network.json"
FRONTEND_DIR = BASE_DIR.parent / "frontend"

# --- Demo / hardware switch --------------------------------------------
# Flip this (or set VEHICLE_LINK=serial) once the real vehicle + LoRa base
# station exist. See vehicle_link.py for the two implementations.
VEHICLE_LINK_MODE = os.environ.get("VEHICLE_LINK_MODE", "simulated")  # "simulated" | "serial"
DEMO_MODE = _env_bool("DEMO_MODE", True)  # DEMO-ONLY: enables OTP-in-API, demo login hint, etc.
# DEMO-ONLY: pre-fills the login form. Turn this OFF whenever the site is reachable
# from the internet (tunnel / hosting), otherwise anyone can read the password.
DEMO_SHOW_CREDENTIALS = _env_bool("DEMO_SHOW_CREDENTIALS", DEMO_MODE)

# --- Vehicle kinematics ---------------------------------------------------
SIM_SPEED_KMPH = _env_float("SIM_SPEED_KMPH", 20.0)          # ASSUMED cruising speed
JOYSTICK_MAX_SPEED_KMPH = _env_float("JOYSTICK_MAX_SPEED_KMPH", 6.0)  # ASSUMED, kept low for safety
JOYSTICK_MAX_TURN_DEG_S = _env_float("JOYSTICK_MAX_TURN_DEG_S", 70.0) # ASSUMED
SIM_TICK_HZ = _env_float("SIM_TICK_HZ", 2.0)                 # how often the vehicle state updates
ARRIVAL_RADIUS_M = _env_float("ARRIVAL_RADIUS_M", 5.0)       # ASSUMED, ~ GPS module accuracy

# --- Battery model (ASSUMED - replace with real coulomb-counter / ADC reading) ---
BATTERY_DRAIN_IDLE_PCT_PER_S = _env_float("BATTERY_DRAIN_IDLE_PCT_PER_S", 0.0015)
BATTERY_DRAIN_MOVING_PCT_PER_S = _env_float("BATTERY_DRAIN_MOVING_PCT_PER_S", 0.012)
BATTERY_START_PCT = _env_float("BATTERY_START_PCT", 96.0)

# --- Starting position (DEMO-ONLY - real vehicle reports its own GPS fix) ---
VEHICLE_START_TERMINAL = os.environ.get("VEHICLE_START_TERMINAL", "Nitw 2nd Gate")

# --- "LoRa" link simulation (see vehicle_link.py) -------------------------
# Real LoRa has real airtime, not instant delivery. We fake a little of that
# latency so the system behaves the way it will with real hardware.
LORA_SIM_LATENCY_MS_MIN = _env_int("LORA_SIM_LATENCY_MS_MIN", 40)   # DEMO-ONLY
LORA_SIM_LATENCY_MS_MAX = _env_int("LORA_SIM_LATENCY_MS_MAX", 180)  # DEMO-ONLY
# Shared secret used to HMAC-sign command packets so a captured/replayed
# packet can't be re-sent later. Generate a real random one for production
# and load it from an environment variable / secrets manager - never commit it.
VEHICLE_LINK_SECRET = os.environ.get("VEHICLE_LINK_SECRET", "demo-insecure-secret-change-me")  # DEMO-ONLY

# Real base-station serial settings, used only by vehicle_link.SerialLoRaLink
SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
SERIAL_BAUD = _env_int("SERIAL_BAUD", 115200)

# --- OTP -------------------------------------------------------------------
OTP_LENGTH = _env_int("OTP_LENGTH", 6)
OTP_TTL_SECONDS = _env_int("OTP_TTL_SECONDS", 300)
OTP_MAX_ATTEMPTS = _env_int("OTP_MAX_ATTEMPTS", 5)
OTP_CHANNEL = os.environ.get("OTP_CHANNEL", "console")  # "console" (DEMO-ONLY) | "twilio" | "email"

# --- Auth (DEMO-ONLY - replace with real accounts / campus SSO) -----------
DEMO_USERNAME = os.environ.get("DEMO_USERNAME", "operator")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "nitw2026")
DEMO_USER_PHONE = os.environ.get("DEMO_USER_PHONE", "+91-90000-00000")  # where OTP "arrives"
SESSION_TTL_SECONDS = _env_int("SESSION_TTL_SECONDS", 3600)      # hard cap on any session
SESSION_IDLE_SECONDS = _env_int("SESSION_IDLE_SECONDS", 300)     # signed out after this long with no input
LOGIN_MAX_FAILURES = _env_int("LOGIN_MAX_FAILURES", 5)           # per IP, then locked out for a while
LOGIN_LOCKOUT_SECONDS = _env_int("LOGIN_LOCKOUT_SECONDS", 300)

# --- Idle behaviour -----------------------------------------------------------
# Failsafe: if the joystick goes quiet (phone locked, signal lost) the vehicle stops.
# On real hardware this belongs in the vehicle firmware as a link-loss timeout.
JOYSTICK_TIMEOUT_S = _env_float("JOYSTICK_TIMEOUT_S", 1.0)       # ASSUMED
# With nobody connected for this long, the virtual vehicle is reset to its start state.
SIM_IDLE_RESET_SECONDS = _env_int("SIM_IDLE_RESET_SECONDS", 300)  # DEMO-ONLY

# Same-origin by default (the backend serves the website), so no cross-origin access is allowed.
# Set e.g. CORS_ALLOW_ORIGINS=https://my-site.example only if the frontend is hosted elsewhere.
CORS_ALLOW_ORIGINS = [o for o in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",") if o]
