"""
uart_test.py  —  Step 1: Raw UART reception test
=================================================
Run this BEFORE lift_serial.py to confirm:
  1. Your COM port is correct
  2. The FPGA is actually sending bytes
  3. The 4-byte frame structure looks right

Usage:
    pip install pyserial        (only once)
    python uart_test.py

What you should see if everything is working:
------------------------------------------------------------
[UART] Opened COM3 at 9600 baud. Listening...
[RAW]  Bytes: A8 05 09 02
[RAW]  Bytes: A8 05 09 02
[RAW]  Bytes: A9 05 09 02     <- floor changed
...

Byte 0 should always have bit7 = 1  (value >= 128 = 0x80)
Bytes 1, 2, 3 should always have bit7 = 0  (value < 128)

PARSED output will also show what each byte means.
------------------------------------------------------------

CHANGE SERIAL_PORT BELOW before running.
"""

import serial
import time

# ── CHANGE THIS ──────────────────────────────────────────
SERIAL_PORT = "COM5"    # Windows: COM3, COM4 etc.
                        # Linux:   /dev/ttyUSB0
                        # Mac:     /dev/cu.usbserial-XXXX
BAUD_RATE   = 9600
# ─────────────────────────────────────────────────────────

print("=" * 60)
print("  UART Reception Test  —  Lift8 FPGA")
print("=" * 60)
print(f"  Port : {SERIAL_PORT}")
print(f"  Baud : {BAUD_RATE}")
print("=" * 60)

try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2)
    print(f"[OK]   Port opened successfully.\n")
except Exception as e:
    print(f"[FAIL] Could not open port: {e}")
    print()
    print("Tips:")
    print("  - Check Device Manager (Windows) for correct COM number")
    print("  - Make sure FPGA is programmed and USB cable is connected")
    print("  - No other program (like Vivado serial monitor) is using the port")
    exit(1)

# ── Wait for first byte (timeout tells us if nothing is coming) ──
print("[WAIT] Waiting for first byte from FPGA (up to 5 seconds)...")
ser.timeout = 5
first = ser.read(1)

if not first:
    print()
    print("[FAIL] No data received in 5 seconds.")
    print()
    print("Tips:")
    print("  - Confirm XDC has:  PACKAGE_PIN A18  for uart_tx")
    print("  - Confirm lift_fpga_top.v is the updated version with UART")
    print("  - Re-program the FPGA and try again")
    exit(1)

print(f"[OK]   First byte received: 0x{first[0]:02X}  (decimal {first[0]})")
print()
print("─" * 60)
print("  Streaming frames. Press Ctrl+C to stop.")
print("─" * 60)

ser.timeout = 1     # back to 1s timeout for normal reading
buf = []
frame_count = 0
error_count = 0

try:
    while True:
        raw = ser.read(1)
        if not raw:
            print("[WARN] No byte received (timeout) — check FPGA is running")
            continue

        val = raw[0]

        # Frame sync: byte with bit7=1 starts a new frame
        if val & 0x80:
            buf = [val]
        else:
            if buf:
                buf.append(val)

        # Full 4-byte frame
        if len(buf) == 4:
            b0, b1, b2, b3 = buf
            buf = []

            # Validate frame structure
            valid = (b0 & 0x80) and not (b1 & 0x80) and not (b2 & 0x80) and not (b3 & 0x80)

            frame_count += 1

            if valid:
                # ── Parse ───────────────────────────────────
                emergency = (b0 >> 6) & 1
                door      = (b0 >> 5) & 1
                up        = (b0 >> 4) & 1
                down      = (b0 >> 3) & 1
                floor     = b0 & 0x07

                cabin_req  = [(b1 >> i) & 1 for i in range(5)]
                hall_up    = [(b2 >> (i+1)) & 1 for i in range(4)]
                idle       = b2 & 1
                hall_down  = [(b3 >> (i+1)) & 1 for i in range(4)]

                # ── Print ───────────────────────────────────
                print(f"[Frame #{frame_count:04d}]  "
                      f"Raw: {b0:02X} {b1:02X} {b2:02X} {b3:02X}  |  "
                      f"Floor={floor}  "
                      f"{'UP' if up and not down else 'DOWN' if down and not up else 'IDLE':4s}  "
                      f"Door={'OPEN' if door else 'CLOS'}  "
                      f"{'EMERG! ' if emergency else '       '}"
                      f"Cabin={cabin_req}  "
                      f"HallUp={hall_up}  "
                      f"HallDn={hall_down}")
               # time.sleep(1)
            else:
                error_count += 1
                print(f"[BAD   #{frame_count:04d}]  "
                      f"Raw: {b0:02X} {b1:02X} {b2:02X} {b3:02X}  "
                      f"<-- frame sync error (total bad: {error_count})")

except KeyboardInterrupt:
    print()
    print("─" * 60)
    print(f"  Stopped.  Good frames: {frame_count - error_count}   Bad frames: {error_count}")
    if frame_count > 0:
        pct = 100 * (frame_count - error_count) / frame_count
        print(f"  Success rate: {pct:.1f}%")
        if pct > 95:
            print("  [OK] UART link is healthy. Proceed to lift_serial.py")
        else:
            print("  [WARN] High error rate — check baud rate and XDC pin")
    print("─" * 60)
    ser.close()