"""
A stand-in for the firmware that will eventually run on the vehicle's own
ESP32. It owns the vehicle's state and advances it by small time steps
("ticks"), the same way the real vehicle would update its state every loop
iteration using its GPS fix, IMU heading and wheel-encoder speed.

main.py drives this through a VehicleLink (see vehicle_link.py) exactly the
way it will eventually drive the real vehicle: by sending commands and
reading back telemetry, never by touching these internals directly from the
API layer. Only vehicle_link.py reaches into this object.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import List, Optional, Tuple

import config
from path_network import CampusNetwork, haversine_m, bearing_deg, destination_point, polyline_length_m


class Mode(str, Enum):
    AUTONOMOUS = "autonomous"
    CALIBRATION = "calibration"
    MANUAL = "manual"


class LockState(str, Enum):
    LOCKED = "locked"
    AWAITING_OTP = "awaiting_otp"
    UNLOCKED = "unlocked"


class VehicleSimulator:
    """Stands in for the vehicle's own ESP32. Note that it holds a reference
    to the SAME CampusNetwork the website uses - in the real system this is
    the network the vehicle has stored on its own SD card, not something
    the backend sends it. A NAVIGATE command only ever carries a
    destination id; the vehicle (here, this class) computes its own route."""

    def __init__(self, network: CampusNetwork, start_node_id: int):
        self.network = network
        self.lat, self.lon = network.nodes[start_node_id]
        self.heading_deg = 0.0
        self.speed_kmph = 0.0
        self.battery_pct = config.BATTERY_START_PCT
        self.mode = Mode.AUTONOMOUS
        self.lock_state = LockState.LOCKED
        self.seq = 0

        # Autonomous-drive state
        self.destination_name: Optional[str] = None
        self._route: List[Tuple[float, float]] = []   # remaining waypoints, route[0] == current pos
        self._route_total_m: float = 0.0
        self.distance_remaining_m: float = 0.0
        self.arrived_event: bool = False  # true for exactly one tick on arrival

        # Manual / calibration drive state (from joystick)
        self._stick_x = 0.0  # steering, -1..1
        self._stick_y = 0.0  # throttle, -1..1
        self._stick_ts = 0.0  # when the last joystick packet arrived (monotonic seconds)

        self._estopped = False

    # ---- commands (called by VehicleLink after verifying the packet) ----
    def set_mode(self, mode: Mode):
        self.mode = mode
        self._estopped = False
        if mode != Mode.AUTONOMOUS:
            # Requirement: automatic driving is only available in autonomous
            # mode, so leaving it cancels any active route immediately.
            self._route = []
            self.destination_name = None
            self.speed_kmph = 0.0
        else:
            self._stick_x = self._stick_y = 0.0

    def navigate_to(self, destination_id: int):
        """destination_id is a terminal id. This is the ONLY thing that
        would travel over LoRa in the real system - the vehicle looks up
        its own position and the destination on its own copy of the map
        (here, self.network) and computes its own route, the same way it
        will once it's reading nitw_campus_path_network.json off an SD
        card instead of sharing a Python object with the backend."""
        if self.mode != Mode.AUTONOMOUS:
            raise PermissionError("Autonomous driving is only available in autonomous mode.")
        terminal = self.network.terminal_by_id.get(destination_id)
        if terminal is None:
            raise ValueError(f"Unknown destination id {destination_id}.")

        src_node = self.network.nearest_node_to(self.lat, self.lon)
        node_path = self.network.shortest_path_nodes(src_node, terminal.node)
        route_latlon = [self.network.nodes[i] for i in node_path]

        self.lock_state = LockState.LOCKED  # a fresh trip clears any previous arrival/unlock
        self.destination_name = terminal.name
        self._route = [(self.lat, self.lon)] + route_latlon
        self._route_total_m = polyline_length_m(self._route)
        self.distance_remaining_m = self._route_total_m
        self.arrived_event = False
        self._estopped = False

    def apply_joystick(self, x: float, y: float):
        if self.mode not in (Mode.CALIBRATION, Mode.MANUAL):
            raise PermissionError("Manual control is disabled outside calibration/manual mode.")
        self._stick_x = max(-1.0, min(1.0, x))
        self._stick_y = max(-1.0, min(1.0, y))
        self._stick_ts = time.monotonic()

    def unlock(self):
        if self.lock_state == LockState.AWAITING_OTP:
            self.lock_state = LockState.UNLOCKED

    def emergency_stop(self):
        self._estopped = True
        self.speed_kmph = 0.0
        self._route = []
        self._stick_x = self._stick_y = 0.0

    # ---- simulation step --------------------------------------------------
    def tick(self, dt_s: float):
        self.arrived_event = False
        if not self._estopped:
            if self.mode == Mode.AUTONOMOUS:
                self._tick_autonomous(dt_s)
            else:
                self._tick_manual(dt_s)
        self._drain_battery(dt_s)
        self.seq += 1
        return self._as_telemetry_packet()

    def _tick_autonomous(self, dt_s: float):
        if len(self._route) < 2:
            self.speed_kmph = 0.0
            return
        self.speed_kmph = config.SIM_SPEED_KMPH
        step_m = self.speed_kmph * (1000 / 3600) * dt_s
        self._advance_along_route(step_m)
        if len(self._route) < 2 or self.distance_remaining_m <= config.ARRIVAL_RADIUS_M:
            self._arrive()

    def _advance_along_route(self, step_m: float):
        while step_m > 0 and len(self._route) >= 2:
            seg_len = haversine_m(self._route[0], self._route[1])
            if seg_len <= step_m or seg_len < 1e-6:
                step_m -= seg_len
                self.distance_remaining_m = max(0.0, self.distance_remaining_m - seg_len)
                self._route.pop(0)
                self.lat, self.lon = self._route[0]
            else:
                frac = step_m / seg_len
                self.heading_deg = bearing_deg(self._route[0], self._route[1])
                self.lat, self.lon = destination_point(self._route[0], self.heading_deg, step_m)
                self._route[0] = (self.lat, self.lon)
                self.distance_remaining_m = max(0.0, self.distance_remaining_m - step_m)
                step_m = 0
        if len(self._route) >= 2:
            self.heading_deg = bearing_deg(self._route[0], self._route[1])

    def _arrive(self):
        self.speed_kmph = 0.0
        self._route = []
        self.distance_remaining_m = 0.0
        if self.lock_state == LockState.LOCKED:
            self.lock_state = LockState.AWAITING_OTP
            self.arrived_event = True

    def reset(self, start_node_id: int):
        """Back to the factory state (used when nobody has been connected for a while)."""
        self.__init__(self.network, start_node_id)

    def _tick_manual(self, dt_s: float):
        # Failsafe: no joystick packet recently -> treat the stick as released.
        if time.monotonic() - self._stick_ts > config.JOYSTICK_TIMEOUT_S:
            self._stick_x = self._stick_y = 0.0
        self.speed_kmph = max(0.0, self._stick_y) * config.JOYSTICK_MAX_SPEED_KMPH
        turn = self._stick_x * config.JOYSTICK_MAX_TURN_DEG_S
        self.heading_deg = (self.heading_deg + turn * dt_s) % 360
        step_m = self.speed_kmph * (1000 / 3600) * dt_s
        if step_m > 0:
            self.lat, self.lon = destination_point((self.lat, self.lon), self.heading_deg, step_m)

    def _drain_battery(self, dt_s: float):
        drain = config.BATTERY_DRAIN_IDLE_PCT_PER_S * dt_s
        if self.speed_kmph > 0:
            drain += config.BATTERY_DRAIN_MOVING_PCT_PER_S * dt_s
        self.battery_pct = max(0.0, self.battery_pct - drain)

    def eta_seconds(self) -> Optional[float]:
        if self.mode == Mode.AUTONOMOUS and self.speed_kmph > 0 and self._route:
            return self.distance_remaining_m / (self.speed_kmph * 1000 / 3600)
        return None

    def _as_telemetry_packet(self):
        from vehicle_link import TelemetryPacket  # local import avoids a circular import
        return TelemetryPacket(
            lat=self.lat, lon=self.lon, heading_deg=self.heading_deg,
            speed_kmph=self.speed_kmph, battery_pct=self.battery_pct,
            mode=self.mode, lock_state=self.lock_state, seq=self.seq,
        )
