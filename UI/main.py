"""
lift_serial.py  —  FPGA → Laptop UART bridge  (4-byte frame version)
             + Predictive Maintenance System (software-only)
=====================================================================
Reads 4-byte frames from the Basys3 at 9600 baud,
parses the full elevator state including hall DOWN buttons,
serves it over a local WebSocket for the browser UI,
and continuously runs a predictive maintenance engine.

Install once:
    pip install pyserial websockets

Run:
    python lift_serial.py

Then open index.html in your browser.

CHANGE SERIAL_PORT BELOW to match your laptop:
  Windows : "COM3"            (check Device Manager -> Ports)
  Linux   : "/dev/ttyUSB0"
  Mac     : "/dev/cu.usbserial-XXXXXX"
"""

import serial
import asyncio
import websockets
import json
import threading
import sys
import time
import collections

# ── CONFIG ─────────────────────────────────────────────────────────
SERIAL_PORT = "COM7"       # ← CHANGE THIS
BAUD_RATE   = 9600
WS_PORT     = 8765

# ── Predictive Maintenance Tuning ──────────────────────────────────
UART_WATCHDOG_TIMEOUT   = 5.0    # seconds before declaring UART lost
STUCK_ELEVATOR_TIMEOUT  = 8.0    # seconds moving with no floor change
HEALTH_RECOVERY_RATE    = 0.02   # points recovered per stable packet
REVERSAL_BURST_WINDOW   = 30.0   # seconds window for reversal spike check
REVERSAL_BURST_THRESHOLD= 6      # reversals within window = suspicious
# ───────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════
#  ELEVATOR STATE
# ══════════════════════════════════════════════════════════════════
state = {
    "floor":      0,
    "up":         True,
    "down":       False,
    "door":       False,
    "idle":       True,
    "emergency":  False,
    "cabin_req":  [False] * 5,   # floors 0-4   (sw[4:0])
    "hall_up":    [False] * 4,   # floors 0-3   (sw[8:5])
    "hall_down":  [False] * 4,   # floors 1-4   (sw[12:9])
}

# ══════════════════════════════════════════════════════════════════
#  PREDICTIVE MAINTENANCE STATE
# ══════════════════════════════════════════════════════════════════

# ── Operational counters ──────────────────────────────────────────
pm_counters = {
    "door_cycles":        0,
    "movement_cycles":    0,
    "direction_reversals":0,
    "emergency_count":    0,
    "uart_failures":      0,
    "invalid_state_count":0,
    "stuck_events":       0,
    "packets_received":   0,
    "uptime_seconds":     0,
}

# ── Edge-detection previous state ────────────────────────────────
pm_prev = {
    "door":        False,
    "moving":      False,   # True when up XOR down (exclusive motion)
    "direction":   None,    # "up" | "down" | None
    "floor":       -1,
    "emergency":   False,
    "first_frame": True,
}

# ── Health score ──────────────────────────────────────────────────
health_score  = 100.0    # 0–100 float; display as integer
health_status = "EXCELLENT"

# ── Maintenance log (ring buffer, last 200 events) ────────────────
maintenance_log = collections.deque(maxlen=200)

# ── Active alerts (cleared when condition resolves) ───────────────
active_alerts = []   # list of strings

# ── UART watchdog ─────────────────────────────────────────────────
last_packet_time   = time.time()
uart_disconnected  = False   # spam-prevention flag

# ── Stuck elevator detection ──────────────────────────────────────
moving_since_time  = None    # time when last motion began
moving_since_floor = None    # floor at the start of that motion
stuck_alarm_fired  = False   # prevent repeated stuck alerts per event

# ── Reversal burst tracking ───────────────────────────────────────
reversal_timestamps = collections.deque()   # timestamps of each reversal

# ── Session start ─────────────────────────────────────────────────
session_start = time.time()

# ── Thread lock for shared state ─────────────────────────────────
state_lock = threading.Lock()

# ══════════════════════════════════════════════════════════════════
#  MAINTENANCE LOG
# ══════════════════════════════════════════════════════════════════

def log_maintenance_event(level: str, message: str):
    """
    Append one structured entry to the maintenance ring-buffer.
    level: "INFO" | "WARNING" | "CRITICAL"
    """
    entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "level":     level,
        "message":   message,
    }
    maintenance_log.append(entry)
    tag = {"INFO": "[INFO]", "WARNING": "[WARN]", "CRITICAL": "[CRIT]"}.get(level, "[LOG]")
    print(f"  {tag} {entry['timestamp']}  {message}")

# ══════════════════════════════════════════════════════════════════
#  HEALTH SCORE ENGINE
# ══════════════════════════════════════════════════════════════════

def _clamp(val: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, val))

def _health_status_label(score: float) -> str:
    if score >= 85:  return "EXCELLENT"
    if score >= 65:  return "GOOD"
    if score >= 40:  return "WARNING"
    return "CRITICAL"

def compute_health_score() -> float:
    """
    Recalculate health score from operational counters.
    Starting at 100, apply weighted deductions.
    Recovery: +HEALTH_RECOVERY_RATE per stable packet (applied in caller).
    Score is clamped [0, 100].
    """
    global health_score, health_status

    score = health_score  # mutate incrementally; base is current score

    # Deductions are applied each call as a floor ceiling, not accumulated
    # so we recompute from a penalty-weighted perspective.
    # Strategy: penalty_score = 100 - weighted_penalty_sum; blend toward it.

    c = pm_counters

    penalty = 0.0

    # Emergency stops: -8 each (major event)
    penalty += c["emergency_count"] * 8.0

    # Stuck events: -12 each (very bad)
    penalty += c["stuck_events"] * 12.0

    # UART failures: -5 each
    penalty += c["uart_failures"] * 5.0

    # Invalid FSM states: -6 each
    penalty += c["invalid_state_count"] * 6.0

    # Excessive direction reversals: -0.5 each above threshold
    reversal_excess = max(0, c["direction_reversals"] - 10)
    penalty += reversal_excess * 0.5

    # Door cycles: -0.01 each (wear factor, meaningful at high cycles)
    penalty += c["door_cycles"] * 0.01

    # Movement cycles: -0.005 each
    penalty += c["movement_cycles"] * 0.005

    # Reversal burst: active penalty if recent burst detected
    now = time.time()
    recent_reversals = sum(1 for t in reversal_timestamps if now - t < REVERSAL_BURST_WINDOW)
    if recent_reversals >= REVERSAL_BURST_THRESHOLD:
        penalty += 10.0

    target = _clamp(100.0 - penalty)

    # Blend health score toward target (don't snap)
    # If target is worse (lower) → apply immediately
    # If target is better (higher) → recover slowly
    if target < score:
        health_score = _clamp(target)
    else:
        health_score = _clamp(score + HEALTH_RECOVERY_RATE)

    prev_status = health_status
    health_status = _health_status_label(health_score)

    # Log health status transitions
    if prev_status != health_status:
        if health_score < 40:
            log_maintenance_event("CRITICAL", f"Health degraded to CRITICAL ({health_score:.0f}/100)")
        elif health_score < 65:
            log_maintenance_event("WARNING",  f"Health degraded to WARNING ({health_score:.0f}/100)")
        elif health_score >= 65 and prev_status in ("CRITICAL", "WARNING"):
            log_maintenance_event("INFO",     f"Health recovering — now {health_status} ({health_score:.0f}/100)")

    return health_score

# ══════════════════════════════════════════════════════════════════
#  INVALID FSM STATE DETECTION
# ══════════════════════════════════════════════════════════════════

def detect_invalid_states(s: dict) -> list:
    """
    Check for logically impossible elevator states.
    Returns list of fault strings (empty = clean).
    """
    faults = []

    # Both up and down simultaneously
    if s["up"] and s["down"]:
        faults.append("FAULT: Moving UP and DOWN simultaneously")

    # Moving while door open
    if s["door"] and (s["up"] or s["down"]) and not s["idle"]:
        faults.append("FAULT: Door open while elevator moving")

    # Floor out of valid range 0-4
    if not (0 <= s["floor"] <= 4):
        faults.append(f"FAULT: Invalid floor value {s['floor']}")

    # Emergency active but continuing non-idle motion
    # (FSM should be homing to nearest floor in emergency, which is OK,
    #  but if both up+down are set simultaneously during emergency, that's bad)
    if s["emergency"] and s["up"] and s["down"]:
        faults.append("FAULT: Emergency active with conflicting direction signals")

    return faults

# ══════════════════════════════════════════════════════════════════
#  STUCK ELEVATOR DETECTION
# ══════════════════════════════════════════════════════════════════

def check_stuck_elevator(s: dict):
    """
    Detect elevator claiming to be in motion but floor not changing.
    Modifies global moving_since_time / moving_since_floor / stuck_alarm_fired.
    """
    global moving_since_time, moving_since_floor, stuck_alarm_fired
    global active_alerts

    is_moving = (s["up"] or s["down"]) and not s["door"] and not s["idle"]
    current_floor = s["floor"]

    if not is_moving:
        # Reset tracking when elevator is not in motion
        moving_since_time  = None
        moving_since_floor = None
        stuck_alarm_fired  = False
        # Clear any existing stuck alert
        active_alerts = [a for a in active_alerts if "STUCK" not in a]
        return

    now = time.time()

    if moving_since_time is None:
        # Elevator just started moving
        moving_since_time  = now
        moving_since_floor = current_floor
        stuck_alarm_fired  = False
        return

    if current_floor != moving_since_floor:
        # Floor changed — elevator is progressing normally, reset
        moving_since_time  = now
        moving_since_floor = current_floor
        stuck_alarm_fired  = False
        active_alerts = [a for a in active_alerts if "STUCK" not in a]
        return

    # Floor has not changed while moving
    elapsed = now - moving_since_time
    if elapsed >= STUCK_ELEVATOR_TIMEOUT and not stuck_alarm_fired:
        stuck_alarm_fired = True
        pm_counters["stuck_events"] += 1
        log_maintenance_event("CRITICAL",
            f"Stuck elevator detected — floor F{current_floor} unchanged for {elapsed:.1f}s while moving")
        if "STUCK ELEVATOR" not in active_alerts:
            active_alerts.append("STUCK ELEVATOR")

# ══════════════════════════════════════════════════════════════════
#  UART WATCHDOG
# ══════════════════════════════════════════════════════════════════

def check_uart_watchdog():
    """
    Called periodically from the watchdog thread.
    Detects UART timeout and reconnect.
    """
    global uart_disconnected, active_alerts
    global last_packet_time

    now = time.time()
    gap = now - last_packet_time

    if gap >= UART_WATCHDOG_TIMEOUT and not uart_disconnected:
        uart_disconnected = True
        pm_counters["uart_failures"] += 1
        log_maintenance_event("CRITICAL",
            f"UART communication lost — no packets for {gap:.1f}s")
        if "UART DISCONNECTED" not in active_alerts:
            active_alerts.append("UART DISCONNECTED")

    elif gap < UART_WATCHDOG_TIMEOUT and uart_disconnected:
        # Reconnected
        uart_disconnected = False
        log_maintenance_event("INFO", "UART communication restored")
        active_alerts = [a for a in active_alerts if "UART DISCONNECTED" not in a]

# ══════════════════════════════════════════════════════════════════
#  OPERATIONAL ANALYTICS — EDGE DETECTION
# ══════════════════════════════════════════════════════════════════

def update_operational_counters(s: dict):
    """
    Update all pm_counters using proper rising-edge detection.
    Must be called once per parsed valid frame.
    """
    global pm_prev, reversal_timestamps
    global active_alerts

    pm_counters["packets_received"] += 1
    pm_counters["uptime_seconds"] = int(time.time() - session_start)

    # ── Skip edge detection on very first frame ───────────────────
    if pm_prev["first_frame"]:
        pm_prev["door"]      = s["door"]
        pm_prev["moving"]    = (s["up"] or s["down"]) and not s["idle"]
        pm_prev["direction"] = "up" if s["up"] and not s["down"] else \
                               "down" if s["down"] and not s["up"] else None
        pm_prev["floor"]     = s["floor"]
        pm_prev["emergency"] = s["emergency"]
        pm_prev["first_frame"] = False
        return

    now = time.time()
    is_moving = (s["up"] or s["down"]) and not s["door"] and not s["idle"]

    # ── Door cycles: False → True ─────────────────────────────────
    if s["door"] and not pm_prev["door"]:
        pm_counters["door_cycles"] += 1

    # ── Movement cycles: idle/door → moving ──────────────────────
    if is_moving and not pm_prev["moving"]:
        pm_counters["movement_cycles"] += 1

    # ── Direction reversals: up→down or down→up ───────────────────
    cur_dir = "up"   if s["up"]   and not s["down"] else \
              "down" if s["down"] and not s["up"]   else None
    prev_dir = pm_prev["direction"]

    if cur_dir and prev_dir and cur_dir != prev_dir:
        pm_counters["direction_reversals"] += 1
        reversal_timestamps.append(now)

    # ── Emergency count: False → True ────────────────────────────
    if s["emergency"] and not pm_prev["emergency"]:
        pm_counters["emergency_count"] += 1
        log_maintenance_event("CRITICAL",
            f"Emergency stop triggered at floor F{s['floor']}")
        if "EMERGENCY STOP" not in active_alerts:
            active_alerts.append("EMERGENCY STOP")
    elif not s["emergency"] and pm_prev["emergency"]:
        # Emergency cleared
        log_maintenance_event("INFO", "Emergency condition cleared")
        active_alerts = [a for a in active_alerts if "EMERGENCY STOP" not in a]

    # ── Reversal burst detection ──────────────────────────────────
    # Prune old timestamps outside window
    cutoff = now - REVERSAL_BURST_WINDOW
    while reversal_timestamps and reversal_timestamps[0] < cutoff:
        reversal_timestamps.popleft()

    recent = len(reversal_timestamps)
    if recent >= REVERSAL_BURST_THRESHOLD:
        burst_alert = f"REVERSAL BURST ({recent})"
        if not any("REVERSAL BURST" in a for a in active_alerts):
            log_maintenance_event("WARNING",
                f"Excessive direction reversals: {recent} in {REVERSAL_BURST_WINDOW:.0f}s window")
            active_alerts.append(burst_alert)
    else:
        active_alerts = [a for a in active_alerts if "REVERSAL BURST" not in a]

    # ── Update previous state ─────────────────────────────────────
    pm_prev["door"]      = s["door"]
    pm_prev["moving"]    = is_moving
    pm_prev["direction"] = cur_dir
    pm_prev["floor"]     = s["floor"]
    pm_prev["emergency"] = s["emergency"]

# ══════════════════════════════════════════════════════════════════
#  MAINTENANCE RECOMMENDATION ENGINE
# ══════════════════════════════════════════════════════════════════

def maintenance_recommendation() -> str:
    """
    Generate the single most important maintenance recommendation
    based on current counters, health score, and active alerts.
    """
    c = pm_counters
    score = health_score

    # Priority order: most severe first

    if c["stuck_events"] > 0:
        return (f"⚠ CRITICAL: Stuck elevator detected {c['stuck_events']}x — "
                "Inspect motor drive, brake system, and guide rails immediately.")

    if "UART DISCONNECTED" in active_alerts:
        return ("⚠ CRITICAL: UART link lost — "
                "Check USB-UART cable, COM port assignment, and FPGA power.")

    if c["emergency_count"] > 0:
        return (f"⚠ CRITICAL: {c['emergency_count']} emergency stop(s) recorded — "
                "Emergency system inspection required before continued operation.")

    if c["invalid_state_count"] > 0:
        return (f"⚠ WARNING: {c['invalid_state_count']} invalid FSM state(s) detected — "
                "Review control logic and UART frame integrity.")

    if any("REVERSAL BURST" in a for a in active_alerts):
        return ("⚠ WARNING: Abnormal direction reversal pattern — "
                "Check request logic, limit switches, and passenger demand patterns.")

    if c["uart_failures"] >= 3:
        return (f"⚠ WARNING: {c['uart_failures']} UART failures recorded — "
                "Check UART communication stability and baud rate configuration.")

    if score < 40:
        return ("⚠ CRITICAL: Health score critically low — "
                "Schedule immediate maintenance inspection.")

    if score < 65:
        return ("⚠ WARNING: Health score degraded — "
                "Schedule preventive maintenance. Inspect door mechanism and motor drive.")

    if c["door_cycles"] > 500:
        return (f"ℹ Door cycle count high ({c['door_cycles']}) — "
                "Inspect door mechanism, sensors, and rollers.")

    if c["movement_cycles"] > 1000:
        return (f"ℹ Movement cycle count high ({c['movement_cycles']}) — "
                "Inspect motor drive system and bearings.")

    if c["direction_reversals"] > 50:
        return (f"ℹ High direction reversal count ({c['direction_reversals']}) — "
                "Review traffic pattern and request servicing logic.")

    return "✓ System operating normally. Continue routine monitoring."

# ══════════════════════════════════════════════════════════════════
#  FRAME PARSER  (original logic, unchanged)
# ══════════════════════════════════════════════════════════════════

def parse_frame(b0, b1, b2, b3):
    """
    Byte 0 [STATUS]   bit7=1
      [6] emergency  [5] door  [4] Up  [3] Down  [2:0] floor

    Byte 1 [CABIN]    bit7=0
      [4:0] sw[4:0]  cabin buttons floors 0-4

    Byte 2 [HALL UP]  bit7=0
      [4:1] sw[8:5]  hall UP floors 0-3
      [0]   idle

    Byte 3 [HALL DOWN] bit7=0
      [4:1] sw[12:9] hall DOWN floors 1-4
    """
    global state, last_packet_time

    emergency = bool((b0 >> 6) & 1)
    door      = bool((b0 >> 5) & 1)
    up        = bool((b0 >> 4) & 1)
    down      = bool((b0 >> 3) & 1)
    floor     = int(b0 & 0x07)

    cabin_req  = [(b1 >> i) & 1 == 1 for i in range(5)]
    hall_up    = [(b2 >> (i + 1)) & 1 == 1 for i in range(4)]
    idle       = bool(b2 & 1)
    hall_down  = [(b3 >> (i + 1)) & 1 == 1 for i in range(4)]

    new_state = {
        "floor":     floor,
        "up":        up,
        "down":      down,
        "door":      door,
        "idle":      idle,
        "emergency": emergency,
        "cabin_req": cabin_req,
        "hall_up":   hall_up,
        "hall_down": hall_down,
    }

    with state_lock:
        last_packet_time = time.time()

        # # ── Invalid FSM state detection ───────────────────────────
        # faults = detect_invalid_states(new_state)
        # for fault in faults:
        #     pm_counters["invalid_state_count"] += 1
        #     log_maintenance_event("CRITICAL", fault)
        #     if fault not in active_alerts:
        #         active_alerts.append(fault)

        # ── Update elevator state ──────────────────────────────────
        state = new_state

        # ── Update operational counters ────────────────────────────
        update_operational_counters(state)

        # ── Stuck elevator check ───────────────────────────────────
        check_stuck_elevator(state)

        # ── Compute health score ───────────────────────────────────
        compute_health_score()

# ══════════════════════════════════════════════════════════════════
#  SERIAL READER THREAD
# ══════════════════════════════════════════════════════════════════

def serial_reader():
    print(f"[UART] Opening {SERIAL_PORT} at {BAUD_RATE} baud...")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    except serial.SerialException as e:
        print(f"[UART] ERROR: {e}")
        print("[UART] Tip: Check SERIAL_PORT setting at top of this file.")
        sys.exit(1)

    print("[UART] Connected. Waiting for frames...")
    log_maintenance_event("INFO", f"UART connected on {SERIAL_PORT} at {BAUD_RATE} baud")
    buf = []

    while True:
        raw = ser.read(1)
        if not raw:
            continue
        val = raw[0]

        if val & 0x80:
            buf = [val]
        else:
            if buf:
                buf.append(val)

        if len(buf) == 4:
            b0, b1, b2, b3 = buf
            if (b0 & 0x80) and not (b1 & 0x80) and not (b2 & 0x80) and not (b3 & 0x80):
                parse_frame(b0, b1, b2, b3)
            buf = []

# ══════════════════════════════════════════════════════════════════
#  UART WATCHDOG THREAD
# ══════════════════════════════════════════════════════════════════

def uart_watchdog_thread():
    """Runs every second, checks for UART silence."""
    while True:
        time.sleep(1.0)
        with state_lock:
            check_uart_watchdog()

# ══════════════════════════════════════════════════════════════════
#  WEBSOCKET HANDLER
# ══════════════════════════════════════════════════════════════════

async def ws_handler(websocket):
    print(f"[WS] Client connected")
    try:
        while True:
            with state_lock:
                # Build extended packet
                packet = {
                    "state": dict(state),
                    "health": {
                        "score":  round(health_score, 1),
                        "status": health_status,
                    },
                    "stats": dict(pm_counters),
                    "maintenance_log": list(maintenance_log)[-20:],
                    "recommendation":  maintenance_recommendation(),
                    "active_alerts":   list(active_alerts),
                }
                # Also flatten top-level state fields for backward compat
                # (analytics.html reads s.floor, s.up, etc. directly)
                packet.update(state)

            await websocket.send(json.dumps(packet))
            await asyncio.sleep(0.1)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        print("[WS] Client disconnected")

# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

async def main():
    print(f"[WS] WebSocket server starting on ws://localhost:{WS_PORT}")
    async with websockets.serve(ws_handler, "localhost", WS_PORT):
        print("[WS] Ready — open index.html in your browser")
        await asyncio.Future()


if __name__ == "__main__":
    # UART serial reader thread
    t_serial = threading.Thread(target=serial_reader, daemon=True)
    t_serial.start()

    # UART watchdog thread
    t_watchdog = threading.Thread(target=uart_watchdog_thread, daemon=True)
    t_watchdog.start()

    log_maintenance_event("INFO", "Predictive Maintenance System initialised")

    asyncio.run(main())