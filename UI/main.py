"""
lift_serial.py  —  FPGA → Laptop UART bridge  (4-byte frame version)
=====================================================================
Reads 4-byte frames from the Basys3 at 9600 baud,
parses the full elevator state including hall DOWN buttons,
and serves it over a local WebSocket for the browser UI.

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

# ── CONFIG ─────────────────────────────────────────────────────────
SERIAL_PORT = "COM5"       # ← CHANGE THIS
BAUD_RATE   = 9600
WS_PORT     = 8765
# ───────────────────────────────────────────────────────────────────

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
    global state

    emergency = bool((b0 >> 6) & 1)
    door      = bool((b0 >> 5) & 1)
    up        = bool((b0 >> 4) & 1)
    down      = bool((b0 >> 3) & 1)
    floor     = int(b0 & 0x07)

    cabin_req  = [(b1 >> i) & 1 == 1 for i in range(5)]   # bits [4:0]

    hall_up    = [(b2 >> (i + 1)) & 1 == 1 for i in range(4)]  # bits [4:1]
    idle       = bool(b2 & 1)

    # hall_down: sw[12:9] packed into bits [4:1]
    # sw[9]=floor1, sw[10]=floor2, sw[11]=floor3, sw[12]=floor4
    hall_down  = [(b3 >> (i + 1)) & 1 == 1 for i in range(4)]  # bits [4:1]

    state = {
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


def serial_reader():
    print(f"[UART] Opening {SERIAL_PORT} at {BAUD_RATE} baud...")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    except serial.SerialException as e:
        print(f"[UART] ERROR: {e}")
        print("[UART] Tip: Check SERIAL_PORT setting at top of this file.")
        sys.exit(1)

    print("[UART] Connected. Waiting for frames...")
    buf = []

    while True:
        raw = ser.read(1)
        if not raw:
            continue
        val = raw[0]

        if val & 0x80:
            # Start of new frame
            buf = [val]
        else:
            if buf:
                buf.append(val)

        # Full 4-byte frame received
        if len(buf) == 4:
            b0, b1, b2, b3 = buf
            # Sanity: b0 bit7=1, b1/b2/b3 bit7=0
            if (b0 & 0x80) and not (b1 & 0x80) and not (b2 & 0x80) and not (b3 & 0x80):
                parse_frame(b0, b1, b2, b3)
                # Uncomment to debug in terminal:
                # print(f"Floor={state['floor']} Up={state['up']} Down={state['down']} "
                #       f"Door={state['door']} Emerg={state['emergency']} "
                #       f"Cabin={state['cabin_req']} HallUp={state['hall_up']} HallDn={state['hall_down']}")
            buf = []


async def ws_handler(websocket):
    print(f"[WS] Client connected")
    try:
        while True:
            await websocket.send(json.dumps(state))
            await asyncio.sleep(0.1)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        print("[WS] Client disconnected")


async def main():
    print(f"[WS] WebSocket server starting on ws://localhost:{WS_PORT}")
    async with websockets.serve(ws_handler, "localhost", WS_PORT):
        print("[WS] Ready — open index.html in your browser")
        await asyncio.Future()


if __name__ == "__main__":
    t = threading.Thread(target=serial_reader, daemon=True)
    t.start()
    asyncio.run(main())