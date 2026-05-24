#!/usr/bin/env python3
"""
Live scanner debug.
Run from the SAME folder as scanner.py:  python3 debug_scan.py

Reads the scanner the way scanner.py does (directly from the event device,
so it works over SSH). Prints each decoded character as it arrives, and
prints "<CR>" when the scanner sends an Enter terminator.

Interpreting the output after one scan:
  - You see the text AND a trailing <CR>      -> perfect, scanner.py will work.
  - You see the text but NO <CR>              -> scan the "add Enter/CR suffix"
                                                 setup code from the paper manual.
  - Characters are wrong / show as [NN]       -> wrong keyboard layout; scan the
                                                 "US / English keyboard" setup code.
  - Nothing at all                            -> the service is already running and
                                                 has grabbed the device; stop it first
                                                 (sudo systemctl stop spool-scanner).

Ctrl-C to quit. Do this BEFORE installing/starting the systemd service,
because only one program can grab the device at a time.
"""
import sys
from evdev import categorize, ecodes
from scanner import BASE, SHIFT, _SHIFT_KEYS, _ENTER_KEYS, find_scanner

dev = find_scanner()
if dev is None:
    print("Scanner not found (check SCANNER_NAME_HINT in scanner.py).")
    sys.exit(1)

print(f"Reading: {dev.name}")
print("Scan a label now. Ctrl-C to quit.\n")
dev.grab()
shift = False
try:
    for e in dev.read_loop():
        if e.type != ecodes.EV_KEY:
            continue
        ke = categorize(e)
        if ke.keystate == ke.key_down:
            if e.code in _SHIFT_KEYS:
                shift = True
            elif e.code in _ENTER_KEYS:
                sys.stdout.write("  <CR>\n")
                sys.stdout.flush()
            else:
                ch = (SHIFT if shift else BASE).get(e.code)
                sys.stdout.write(ch if ch else f"[{e.code}]")
                sys.stdout.flush()
        elif ke.keystate == ke.key_up and e.code in _SHIFT_KEYS:
            shift = False
except KeyboardInterrupt:
    print("\nbye")
finally:
    try:
        dev.ungrab()
    except Exception:
        pass
