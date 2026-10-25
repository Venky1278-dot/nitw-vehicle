"""
NITW campus vehicle - operator backend.

Run with:  uvicorn main:app --reload
Then open  http://localhost:8000

Swap-to-real-hardware checklist lives in the project README.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import config
import auth
from path_network import CampusNetwork
from vehicle_simulator import VehicleSimulator, Mode
from vehicle_link import (
    SimulatedVehicleLink, TelemetryPacket,
    CMD_SET_MODE, CMD_NAVIGATE, CMD_JOYSTICK, CMD_UNLOCK, CMD_ESTOP, MODE_CODE,
)
from otp_manager import OTPManager, build_channel
from models import (
    LoginRequest, LoginResponse, NetworkOut, ModeRequest, NavigateRequest,
    NavigateResponse, JoystickRequest, OTPVerifyRequest, TelemetryOut,
)
from path_network import polyline_length_m

# ---------------------------------------------------------------------------
# Wiring. This is the one place that decides simulated vs. real hardware.
# ---------------------------------------------------------------------------
network = CampusNetwork(config.NETWORK_JSON_PATH)
start_terminal = network.terminals[config.VEHICLE_START_TERMINAL]
sim = VehicleSimulator(network, start_node_id=start_terminal.node)

if config.VEHICLE_LINK_MODE == "simulated":
    vehicle_link = SimulatedVehicleLink(sim)
else:
    from vehicle_link import SerialLoRaLink
    vehicle_link = SerialLoRaLink()  # raises NotImplementedError until you write it

otp_manager = OTPManager(build_channel())

_latest_packet: TelemetryPacket | None = None
_ws_clients: dict[WebSocket, str] = {}   # websocket -> session token that opened it
_last_client_seen = time.time()
_idle_reset_done = False


async def _on_telemetry(packet: TelemetryPacket):
    global _latest_packet
    _latest_packet = packet
    if sim.arrived_event:
        # Arrival detected -> send the unlock OTP automatically.
        asyncio.create_task(otp_manager.issue("operator", config.DEMO_USER_PHONE))
    out = _build_telemetry_out(packet)
    dead = []
    for ws, token in list(_ws_clients.items()):
        try:
            if not auth._valid(token):          # idle timeout / expiry reached
                await ws.close(code=4401)
                dead.append(ws)
                continue
            await ws.send_json(out.model_dump())
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.pop(ws, None)


def _build_telemetry_out(packet: TelemetryPacket) -> TelemetryOut:
    """Combine what arrived 'over the link' (packet) with trip context the
    backend already knows (it's the one that issued the route) - a real
    base station would do exactly this enrichment before showing anything
    to the operator."""
    otp_debug = None
    if config.DEMO_MODE and sim.lock_state.value == "awaiting_otp":
        rec = otp_manager._records.get("operator")  # DEMO-ONLY introspection
        if rec:
            otp_debug = rec.code

    return TelemetryOut(
        seq=packet.seq, ts=time.time(),
        lat=packet.lat, lon=packet.lon, heading_deg=packet.heading_deg,
        speed_kmph=packet.speed_kmph, battery_pct=packet.battery_pct,
        mode=packet.mode, lock_state=packet.lock_state,
        destination_name=sim.destination_name,
        distance_remaining_m=sim.distance_remaining_m if sim._route else None,
        eta_seconds=sim.eta_seconds(),
        arrived_event=sim.arrived_event,
        otp_debug=otp_debug,
        link_latency_ms=getattr(vehicle_link, "last_latency_ms", None),
    )


def _lan_ip() -> str | None:
    """Best-effort local network address, so we can print a URL phones can open."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is actually sent
            return s.getsockname()[0]
    except OSError:
        return None


async def _idle_watchdog():
    """With nobody connected for SIM_IDLE_RESET_SECONDS, park the vehicle back at its
    start state so the next visitor begins from a clean slate."""
    global _last_client_seen, _idle_reset_done
    while True:
        await asyncio.sleep(min(10, max(1, config.SIM_IDLE_RESET_SECONDS / 3)))
        if _ws_clients:
            _last_client_seen, _idle_reset_done = time.time(), False
        elif not _idle_reset_done and time.time() - _last_client_seen > config.SIM_IDLE_RESET_SECONDS:
            sim.reset(start_terminal.node)
            otp_manager._records.clear()
            _idle_reset_done = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    ip = _lan_ip()
    if ip and not os.environ.get("RENDER"):
        print(f"\n  Phone/tablet on the same Wi-Fi: http://{ip}:8000  (start uvicorn with --host 0.0.0.0)\n")
    vehicle_link.on_telemetry(_on_telemetry)
    await vehicle_link.start()
    watchdog = asyncio.create_task(_idle_watchdog())
    yield
    watchdog.cancel()
    await vehicle_link.stop()


app = FastAPI(title="NITW Campus Vehicle - Operator Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")  # map tile servers want an origin
    if request.url.scheme == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.post("/api/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request):
    ip = request.client.host if request.client else "unknown"   # real client IP behind a proxy needs --proxy-headers
    token = auth.login(body.username, body.password, ip)
    return LoginResponse(token=token, expires_in=config.SESSION_TTL_SECONDS,
                         idle_timeout_s=config.SESSION_IDLE_SECONDS)


@app.post("/api/keepalive")
def keepalive(_: str = Depends(auth.require_auth)):
    """Sent by the page while the person is actually using it; resets the idle clock."""
    return {"ok": True}


@app.post("/api/logout")
def logout(token: str = Depends(auth.require_auth)):
    auth.logout(token)
    return {"ok": True}


@app.get("/api/demo-info")
def demo_info():
    """DEMO-ONLY: lets the login screen show working credentials. Remove
    this endpoint once real accounts exist."""
    if not config.DEMO_SHOW_CREDENTIALS:
        raise HTTPException(status_code=404)
    return {"username": config.DEMO_USERNAME, "password": config.DEMO_PASSWORD}


# ---------------------------------------------------------------------------
# Map / network
# ---------------------------------------------------------------------------
@app.get("/api/network", response_model=NetworkOut)
def get_network(_: str = Depends(auth.require_auth)):
    return network.as_network_dict()


# ---------------------------------------------------------------------------
# Vehicle commands - every one of these is just a tiny packet over
# vehicle_link, exactly as it will be with real LoRa hardware.
# ---------------------------------------------------------------------------
@app.post("/api/mode")
async def set_mode(body: ModeRequest, _: str = Depends(auth.require_auth)):
    await vehicle_link.send_command(CMD_SET_MODE, param1=MODE_CODE[body.mode])
    return {"mode": body.mode}


@app.post("/api/navigate", response_model=NavigateResponse)
async def navigate(body: NavigateRequest, _: str = Depends(auth.require_auth)):
    terminal = network.terminal_by_id.get(body.destination_id)
    if terminal is None:
        raise HTTPException(status_code=404, detail="Unknown destination.")

    try:
        # Only a destination id crosses the "link" - see vehicle_link.py.
        accepted = await vehicle_link.send_command(CMD_NAVIGATE, param1=body.destination_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not accepted:
        raise HTTPException(status_code=502, detail="Vehicle link rejected the command.")

    # Compute the same route independently, purely so the website has
    # something to draw immediately - the vehicle isn't consulted for this.
    src_node = network.nearest_node_to(sim.lat, sim.lon)
    node_path = network.shortest_path_nodes(src_node, terminal.node)
    route = [network.nodes[i] for i in node_path]
    distance_m = polyline_length_m([(sim.lat, sim.lon)] + route)
    eta_s = distance_m / (config.SIM_SPEED_KMPH * 1000 / 3600)
    return NavigateResponse(destination_name=terminal.name, route=route,
                             distance_m=distance_m, eta_seconds=eta_s)


@app.post("/api/joystick")
async def joystick(body: JoystickRequest, _: str = Depends(auth.require_auth)):
    try:
        await vehicle_link.send_command(CMD_JOYSTICK, param1=int(body.x * 1000), param2=int(body.y * 1000))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"ok": True}


@app.post("/api/estop")
async def estop(_: str = Depends(auth.require_auth)):
    await vehicle_link.send_command(CMD_ESTOP)
    return {"ok": True}


# ---------------------------------------------------------------------------
# OTP / unlock
# ---------------------------------------------------------------------------
@app.post("/api/otp/request")
async def otp_request(_: str = Depends(auth.require_auth)):
    if sim.lock_state.value != "awaiting_otp":
        raise HTTPException(status_code=409, detail="Vehicle hasn't arrived yet.")
    code = await otp_manager.issue("operator", config.DEMO_USER_PHONE)
    resp = {"sent": True, "destination": config.DEMO_USER_PHONE}
    if config.DEMO_MODE:
        resp["debug_otp"] = code  # DEMO-ONLY: never return the code itself in production
    return resp


@app.post("/api/otp/verify")
async def otp_verify(body: OTPVerifyRequest, _: str = Depends(auth.require_auth)):
    ok, message = otp_manager.verify("operator", body.code)
    if ok:
        await vehicle_link.send_command(CMD_UNLOCK)
    return {"unlocked": ok, "message": message}


# ---------------------------------------------------------------------------
# Live telemetry
# ---------------------------------------------------------------------------
@app.websocket("/ws/telemetry")
async def ws_telemetry(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    await websocket.accept()
    if not auth._valid(token):
        await websocket.close(code=4401)
        return
    _ws_clients[websocket] = token
    try:
        if _latest_packet:
            await websocket.send_json(_build_telemetry_out(_latest_packet).model_dump())
        while True:
            await websocket.receive_text()  # we don't expect client messages; just detect disconnect
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.pop(websocket, None)


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=str(config.FRONTEND_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(config.FRONTEND_DIR / "index.html"))
