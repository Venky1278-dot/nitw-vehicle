"""
This module is the one piece of the backend that pretends to be radio
hardware. Everything else (main.py, vehicle_simulator.py) talks to a
`VehicleLink` and has no idea whether the vehicle on the other end is real
or simulated.

                 ┌────────────┐   LoRa    ┌──────────────┐
  this backend   │ base-station│◄─────────►│ vehicle ESP32│  REAL deployment
  (FastAPI) ─────│   ESP32     │  packets  │ + GPS/IMU    │
                  └────────────┘            └──────────────┘

Right now there's no hardware, so `SimulatedVehicleLink` plays the role of
*both* boxes: it runs a `VehicleSimulator` as the "vehicle", packs its state
into the exact same binary struct a real packet would use, adds a random
delay to mimic LoRa airtime, unpacks it again, and hands it to the backend -
so the rest of the system already exercises the real wire format.

To go live: implement `SerialLoRaLink` (reads/writes the packed structs
below over a USB-serial connection to a base-station ESP32) and flip
`VEHICLE_LINK_MODE=serial` in config.py / the environment. Nothing else in
the backend needs to change.
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import random
import struct
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

import config
from vehicle_simulator import Mode, LockState, VehicleSimulator

MODE_CODE = {Mode.AUTONOMOUS: 0, Mode.CALIBRATION: 1, Mode.MANUAL: 2}
CODE_MODE = {v: k for k, v in MODE_CODE.items()}
LOCK_CODE = {LockState.LOCKED: 0, LockState.AWAITING_OTP: 1, LockState.UNLOCKED: 2}
CODE_LOCK = {v: k for k, v in LOCK_CODE.items()}

CMD_SET_MODE, CMD_NAVIGATE, CMD_JOYSTICK, CMD_UNLOCK, CMD_ESTOP = range(5)

# ---- Wire formats -----------------------------------------------------
# Telemetry: vehicle -> base station. 19 bytes - comfortably inside a LoRa
# payload (typically 50-250 bytes depending on spreading factor).
_TELEMETRY_FMT = "<iiHHBBBI"   # lat_udeg, lon_udeg, heading_d10, speed_d10kmph, battery, mode, lock, seq
# Command: base station -> vehicle. 17 bytes, HMAC-signed + nonce'd so a
# captured packet can't be replayed later.
_COMMAND_FMT = "<BiiII"       # type, param1, param2, nonce, hmac32


@dataclass
class TelemetryPacket:
    lat: float
    lon: float
    heading_deg: float
    speed_kmph: float
    battery_pct: float
    mode: Mode
    lock_state: LockState
    seq: int


@dataclass
class CommandPacket:
    type: int
    param1: int = 0
    param2: int = 0
    nonce: int = 0
    hmac32: int = 0


def pack_telemetry(p: TelemetryPacket) -> bytes:
    return struct.pack(
        _TELEMETRY_FMT,
        int(round(p.lat * 1e6)),
        int(round(p.lon * 1e6)),
        int(round(p.heading_deg * 10)) % 3600,
        max(0, int(round(p.speed_kmph * 10))),
        max(0, min(255, int(round(p.battery_pct)))),
        MODE_CODE[p.mode],
        LOCK_CODE[p.lock_state],
        p.seq & 0xFFFFFFFF,
    )


def unpack_telemetry(raw: bytes) -> TelemetryPacket:
    lat_i, lon_i, head_i, speed_i, batt, mode_c, lock_c, seq = struct.unpack(_TELEMETRY_FMT, raw)
    return TelemetryPacket(
        lat=lat_i / 1e6,
        lon=lon_i / 1e6,
        heading_deg=head_i / 10.0,
        speed_kmph=speed_i / 10.0,
        battery_pct=float(batt),
        mode=CODE_MODE[mode_c],
        lock_state=CODE_LOCK[lock_c],
        seq=seq,
    )


def _sign(type_: int, param1: int, param2: int, nonce: int) -> int:
    msg = struct.pack("<Biii", type_, param1, param2, nonce)
    digest = hmac.new(config.VEHICLE_LINK_SECRET.encode(), msg, hashlib.sha256).digest()
    return struct.unpack("<I", digest[:4])[0]


def pack_command(type_: int, param1: int = 0, param2: int = 0, nonce: Optional[int] = None) -> bytes:
    nonce = nonce if nonce is not None else random.randint(0, 2**32 - 1)
    sig = _sign(type_, param1, param2, nonce)
    return struct.pack(_COMMAND_FMT, type_, param1, param2, nonce, sig)


def unpack_command(raw: bytes) -> CommandPacket:
    type_, p1, p2, nonce, sig = struct.unpack(_COMMAND_FMT, raw)
    return CommandPacket(type=type_, param1=p1, param2=p2, nonce=nonce, hmac32=sig)


def verify_command(cmd: CommandPacket) -> bool:
    return _sign(cmd.type, cmd.param1, cmd.param2, cmd.nonce) == cmd.hmac32


class VehicleLink(ABC):
    """What the rest of the backend is allowed to know about the vehicle."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_command(self, type_: int, param1: int = 0, param2: int = 0) -> bool:
        """Returns True if the vehicle accepted the command."""

    @abstractmethod
    def on_telemetry(self, callback: Callable[[TelemetryPacket], None]) -> None:
        """Register a callback invoked on every telemetry packet received."""


class SimulatedVehicleLink(VehicleLink):
    """Stands in for (base-station ESP32 <-LoRa-> vehicle ESP32) using an
    in-process VehicleSimulator. Every packet is still serialized through
    the real struct format and a random LoRa-like delay is injected, so the
    rest of the app exercises the exact code path it will use with real
    hardware."""

    def __init__(self, simulator: VehicleSimulator):
        self.sim = simulator
        self._callback: Optional[Callable[[TelemetryPacket], None]] = None
        self._task: Optional[asyncio.Task] = None
        self._last_nonce = -1
        self._next_nonce = 1  # monotonic, like a LoRaWAN frame counter - NOT random, so replays are rejectable
        self.last_latency_ms: float = 0.0

    def on_telemetry(self, callback):
        self._callback = callback

    async def start(self):
        self._task = asyncio.create_task(self._tick_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _tick_loop(self):
        period = 1.0 / config.SIM_TICK_HZ
        while True:
            await asyncio.sleep(period)
            state = self.sim.tick(period)
            # Simulate the vehicle packing its state and transmitting it.
            wire = pack_telemetry(state)
            await self._simulate_airtime()
            # Simulate the base station receiving + decoding it.
            packet = unpack_telemetry(wire)
            if self._callback:
                await self._callback(packet)

    async def _simulate_airtime(self):
        delay = random.uniform(config.LORA_SIM_LATENCY_MS_MIN, config.LORA_SIM_LATENCY_MS_MAX)
        self.last_latency_ms = delay
        await asyncio.sleep(delay / 1000.0)

    async def send_command(self, type_: int, param1: int = 0, param2: int = 0) -> bool:
        # Simulate: base station signs + transmits, vehicle receives + verifies.
        nonce = self._next_nonce
        self._next_nonce += 1
        wire = pack_command(type_, param1, param2, nonce=nonce)
        await self._simulate_airtime()
        cmd = unpack_command(wire)
        if not verify_command(cmd):
            return False  # tampered / corrupted packet
        if cmd.nonce <= self._last_nonce:
            return False  # replay of an old packet
        self._last_nonce = cmd.nonce

        if cmd.type == CMD_SET_MODE:
            self.sim.set_mode(CODE_MODE[cmd.param1])
        elif cmd.type == CMD_JOYSTICK:
            self.sim.apply_joystick(cmd.param1 / 1000.0, cmd.param2 / 1000.0)
        elif cmd.type == CMD_UNLOCK:
            self.sim.unlock()
        elif cmd.type == CMD_ESTOP:
            self.sim.emergency_stop()
        elif cmd.type == CMD_NAVIGATE:
            # param1 is just a destination id - small enough for a real LoRa
            # packet. The vehicle (sim) looks up the route on its own copy
            # of the map, it is never transmitted over the link.
            self.sim.navigate_to(cmd.param1)
        return True


class SerialLoRaLink(VehicleLink):
    """Stub for the real deployment: a base-station ESP32 running a LoRa
    transceiver (e.g. RA-02/SX1278) is plugged into this machine over USB.
    This class would write `pack_command(...)` bytes to the serial port and
    parse `_TELEMETRY_FMT`-shaped lines/bytes coming back.

    Not implemented yet - there's no hardware to test it against. Fill in
    the two TODOs below once the vehicle exists; nothing in main.py,
    vehicle_simulator.py or the frontend has to change.
    """

    def __init__(self, port: str = config.SERIAL_PORT, baud: int = config.SERIAL_BAUD):
        self.port = port
        self.baud = baud
        self._callback = None

    def on_telemetry(self, callback):
        self._callback = callback

    async def start(self):
        # TODO: open the serial port (e.g. `serial_asyncio`), start a read
        # loop that calls `unpack_telemetry()` on each received frame and
        # invokes `self._callback(packet)`.
        raise NotImplementedError(
            "SerialLoRaLink is a stub - implement this once the base-station "
            "ESP32 + LoRa module exist. See the TODOs in this class."
        )

    async def stop(self):
        raise NotImplementedError

    async def send_command(self, type_: int, param1: int = 0, param2: int = 0) -> bool:
        # TODO: write pack_command(type_, param1, param2) to the serial port
        # and await an ACK/NACK from the base station.
        raise NotImplementedError
