#!/usr/bin/env python3
"""
spool-scanner (event-driven)
============================
A central Raspberry Pi that keeps Spoolman, multiple Moonraker/Klipper
printers, and a barcode scanner consistent -- treating the scanner and the
Fluidd/Mainsail GUI as EQUAL ways to assign a spool to a printer.

How it works
------------
* The Pi holds a persistent websocket to each printer's Moonraker and listens
  for "notify_active_spool_set" -- which fires whenever ANYTHING changes a
  printer's active spool (the scanner, the GUI, a macro, anything).
* A single event handler reacts to every such change:
    - DEDUP: clear any OTHER printer that my state map shows holding the same
      spool, so a spool is never live on two machines.
    - LOCATION: write the spool's Spoolman "location" field to the printer it
      just landed on. When a spool LEAVES a printer, blank its location -- but
      only if Spoolman still shows it on that printer (race-safe).
* The scanner does NO bookkeeping itself. Scanning printer+spool just POSTs
  "set active spool" to that printer (exactly like a GUI click); the resulting
  notification drives the handler above. Scanner == GUI.

Design choices (per requirements)
---------------------------------
* NO reconcile pass. On cold start the Pi only RECORDS each printer's current
  active spool; it changes nothing. A spool set in the GUI while the Pi was off
  is left exactly as set.
* Offline-during-move self-heal is TARGETED, not a reconcile: if a dedup clear
  fails because a printer is offline, the Pi remembers it owes that printer a
  clear of spool N, and retries when the printer's websocket reconnects -- only
  if the printer still holds N. This cannot revert a legitimate manual set.
  Residual gap: if the Pi restarts during that window, the pending clear is
  forgotten (rare; manual cleanup).

Scanner workflow
----------------
  1. Scan a PRINTER QR (payload = a key in PRINTERS). Sticky selection.
  2. Scan a spool QR ("web+spoolman:s-<id>") -> set active on that printer.
  3. Scan "web+spoolman:clear" -> clear the selected printer's active spool.
Matching is CASE-INSENSITIVE (Spoolman QR labels are uppercase-only).
"""

import asyncio
import json
import functools

import requests
import websockets
from evdev import InputDevice, categorize, ecodes, list_devices

# ====================== EDIT THIS SECTION ================================

# Printer QR payload  ->  that printer's Moonraker base URL.
# The key is the exact text encoded in that printer's QR label and must match
# ALL_PRINTERS in printer_fallback.py. Replace these with your own.
PRINTERS = {
    "voron":   "http://192.168.1.21:7125",
    "trident": "http://192.168.1.22:7125",
    "prusa":   "http://192.168.1.23:7125",
}

# Spoolman base URL (no trailing slash). Used to read/write the location field.
SPOOLMAN_URL = "http://192.168.1.10:7912"

# Substring used to find the scanner among input devices (case-insensitive).
SCANNER_NAME_HINT = "Barcode"

# Idle timeout (seconds) for the scanner's printer selection. 0 disables.
SELECTION_TIMEOUT = 180

# HTTP timeout for Moonraker/Spoolman REST calls, in seconds.
HTTP_TIMEOUT = 5

# ========================================================================

_PRINTERS_LC = {name.lower(): (name, url) for name, url in PRINTERS.items()}
_SPOOL_PREFIX = "web+spoolman:s-"
_CLEAR_CODE = "web+spoolman:clear"


def _ws_url(http_url):
    return http_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/websocket"


# ============================ SHARED STATE ==============================
class State:
    """In-memory view of the farm. No persistence by design."""
    def __init__(self):
        # printer name -> active spool id (int) or None. Absent = unknown/offline.
        self.active = {}
        # printer name -> spool id we failed to clear (retry on reconnect).
        self.pending_clear = {}
        # serializes handler runs so dedup/location writes don't interleave.
        self.lock = asyncio.Lock()

STATE = State()


# ====================== blocking REST in threads ========================
async def _to_thread(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args))

def _moonraker_set_spool(printer_url, spool_id):
    # Setting an integer spool: send {"spool_id": N}.
    # Clearing (spool_id is None): send an EMPTY body so the field is ABSENT.
    # Moonraker then unsets via its documented default (null). Sending an
    # explicit {"spool_id": null} is rejected by some Moonraker builds with
    # HTTP 400 "Unknown", which is why we omit the field instead.
    body = {} if spool_id is None else {"spool_id": spool_id}
    r = requests.post(f"{printer_url}/server/spoolman/spool_id",
                     json=body, timeout=HTTP_TIMEOUT)
    r.raise_for_status()

def _moonraker_get_spool(printer_url):
    r = requests.get(f"{printer_url}/server/spoolman/spool_id", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json().get("result", r.json()).get("spool_id")

def _spoolman_get_location(spool_id):
    r = requests.get(f"{SPOOLMAN_URL}/api/v1/spool/{spool_id}", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return (r.json().get("location") or "").strip()

def _spoolman_set_location(spool_id, location):
    r = requests.patch(f"{SPOOLMAN_URL}/api/v1/spool/{spool_id}",
                      json={"location": location}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()

# async wrappers
async def moonraker_set_spool(url, sid): await _to_thread(_moonraker_set_spool, url, sid)
async def moonraker_get_spool(url):      return await _to_thread(_moonraker_get_spool, url)
async def spoolman_get_location(sid):    return await _to_thread(_spoolman_get_location, sid)
async def spoolman_set_location(sid, loc): await _to_thread(_spoolman_set_location, sid, loc)


# ========================== CORE EVENT HANDLER ==========================
async def on_active_spool_changed(printer_name, new_spool):
    """The single source of truth for bookkeeping. Triggered by every
    notify_active_spool_set, regardless of who caused the change."""
    async with STATE.lock:
        old_spool = STATE.active.get(printer_name)
        STATE.active[printer_name] = new_spool
        if old_spool == new_spool:
            return

        # A spool LEFT this printer: blank its location iff Spoolman still
        # shows it here (so we don't clobber a location another printer just took).
        if old_spool is not None:
            try:
                if (await spoolman_get_location(old_spool)).lower() == printer_name.lower():
                    await spoolman_set_location(old_spool, "")
                    print(f"   location blanked: spool {old_spool} (left {printer_name})")
            except Exception as e:
                print(f"!! location-blank for spool {old_spool} failed: {e}")

        # A spool ARRIVED on this printer: dedup other holders, record location.
        if new_spool is not None:
            for other, sid in list(STATE.active.items()):
                if other != printer_name and sid == new_spool:
                    _, other_url = _PRINTERS_LC[other.lower()]
                    try:
                        await moonraker_set_spool(other_url, None)
                        STATE.active[other] = None
                        STATE.pending_clear.pop(other, None)
                        print(f"   dedup: cleared spool {new_spool} from {other}")
                    except Exception as e:
                        STATE.pending_clear[other] = new_spool
                        print(f"!! could not clear {other} (offline?): {e}; "
                              f"will retry on its reconnect")
            try:
                await spoolman_set_location(new_spool, printer_name)
                print(f"OK  spool {new_spool} -> {printer_name} (location set)")
            except Exception as e:
                print(f"!! location-set for spool {new_spool} failed: {e}")


# ====================== per-printer websocket loop ======================
async def printer_ws_loop(name, http_url):
    ws_url = _ws_url(http_url)
    seeded = False
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                # On (re)connect, learn the printer's current active spool.
                current = None
                try:
                    current = await moonraker_get_spool(http_url)
                except Exception:
                    pass

                if not seeded:
                    # COLD START: observe only, never change anything.
                    STATE.active[name] = current
                    seeded = True
                    print(f"[{name}] connected; active spool = {current} (seed, no changes)")
                else:
                    # RECONNECT while Pi stayed up: safe to complete a clear we
                    # previously owed this printer, iff it still holds that spool.
                    STATE.active[name] = current
                    owed = STATE.pending_clear.get(name)
                    if owed is not None and current == owed:
                        try:
                            await moonraker_set_spool(http_url, None)
                            STATE.active[name] = None
                            STATE.pending_clear.pop(name, None)
                            print(f"[{name}] reconnected; completed owed clear of spool {owed}")
                        except Exception as e:
                            print(f"[{name}] reconnect clear retry failed: {e}")
                    else:
                        STATE.pending_clear.pop(name, None)
                        print(f"[{name}] reconnected; active spool = {current}")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if msg.get("method") != "notify_active_spool_set":
                        continue
                    params = msg.get("params") or [{}]
                    spool_id = params[0].get("spool_id") if isinstance(params[0], dict) else None
                    await on_active_spool_changed(name, spool_id)

        except Exception as e:
            print(f"[{name}] websocket down ({e}); reconnecting in 5s...")
            await asyncio.sleep(5)


# =============================== SCANNER ================================
BASE = {
    ecodes.KEY_1: "1", ecodes.KEY_2: "2", ecodes.KEY_3: "3", ecodes.KEY_4: "4",
    ecodes.KEY_5: "5", ecodes.KEY_6: "6", ecodes.KEY_7: "7", ecodes.KEY_8: "8",
    ecodes.KEY_9: "9", ecodes.KEY_0: "0",
    ecodes.KEY_A: "a", ecodes.KEY_B: "b", ecodes.KEY_C: "c", ecodes.KEY_D: "d",
    ecodes.KEY_E: "e", ecodes.KEY_F: "f", ecodes.KEY_G: "g", ecodes.KEY_H: "h",
    ecodes.KEY_I: "i", ecodes.KEY_J: "j", ecodes.KEY_K: "k", ecodes.KEY_L: "l",
    ecodes.KEY_M: "m", ecodes.KEY_N: "n", ecodes.KEY_O: "o", ecodes.KEY_P: "p",
    ecodes.KEY_Q: "q", ecodes.KEY_R: "r", ecodes.KEY_S: "s", ecodes.KEY_T: "t",
    ecodes.KEY_U: "u", ecodes.KEY_V: "v", ecodes.KEY_W: "w", ecodes.KEY_X: "x",
    ecodes.KEY_Y: "y", ecodes.KEY_Z: "z",
    ecodes.KEY_MINUS: "-", ecodes.KEY_EQUAL: "=", ecodes.KEY_DOT: ".",
    ecodes.KEY_COMMA: ",", ecodes.KEY_SLASH: "/", ecodes.KEY_SEMICOLON: ";",
    ecodes.KEY_APOSTROPHE: "'", ecodes.KEY_SPACE: " ",
    ecodes.KEY_LEFTBRACE: "[", ecodes.KEY_RIGHTBRACE: "]", ecodes.KEY_BACKSLASH: "\\",
    ecodes.KEY_GRAVE: "`",
}
SHIFT = {
    ecodes.KEY_1: "!", ecodes.KEY_2: "@", ecodes.KEY_3: "#", ecodes.KEY_4: "$",
    ecodes.KEY_5: "%", ecodes.KEY_6: "^", ecodes.KEY_7: "&", ecodes.KEY_8: "*",
    ecodes.KEY_9: "(", ecodes.KEY_0: ")",
    ecodes.KEY_MINUS: "_", ecodes.KEY_EQUAL: "+", ecodes.KEY_SEMICOLON: ":",
    ecodes.KEY_SLASH: "?", ecodes.KEY_DOT: ">", ecodes.KEY_COMMA: "<",
    ecodes.KEY_APOSTROPHE: '"', ecodes.KEY_LEFTBRACE: "{", ecodes.KEY_RIGHTBRACE: "}",
    ecodes.KEY_BACKSLASH: "|", ecodes.KEY_GRAVE: "~",
}
for _kc, _ch in list(BASE.items()):
    if _ch.isalpha():
        SHIFT.setdefault(_kc, _ch.upper())

_SHIFT_KEYS = (ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT)
_ENTER_KEYS = (ecodes.KEY_ENTER, ecodes.KEY_KPENTER)


def find_scanner():
    candidates = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if SCANNER_NAME_HINT.lower() in dev.name.lower():
            caps = dev.capabilities().get(ecodes.EV_KEY, [])
            candidates.append((1 if ecodes.KEY_A in caps else 0, dev))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


async def scanner_loop(loop):
    import time
    dev = None
    while dev is None:
        dev = find_scanner()
        if dev is None:
            print("Scanner not found; retrying in 3s...")
            await asyncio.sleep(3)
    print(f"Scanner: {dev.name}. Scan a PRINTER code, then a spool code.")
    dev.grab()

    sel = {"name": None, "at": 0.0}

    def alive(now):
        if sel["name"] is None:
            return False
        return SELECTION_TIMEOUT <= 0 or (now - sel["at"]) <= SELECTION_TIMEOUT

    buf, shift = [], False
    try:
        async for event in dev.async_read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            ke = categorize(event)
            if ke.keystate == ke.key_down:
                c = event.code
                if c in _SHIFT_KEYS:
                    shift = True
                    continue
                if c in _ENTER_KEYS:
                    code = "".join(buf).strip()
                    buf = []
                    await handle_scan(code, sel, alive)
                else:
                    ch = (SHIFT if shift else BASE).get(c)
                    if ch:
                        buf.append(ch)
            elif ke.keystate == ke.key_up and event.code in _SHIFT_KEYS:
                shift = False
    finally:
        try:
            dev.ungrab()
        except Exception:
            pass


async def handle_scan(code, sel, alive):
    import time
    if not code:
        return
    low = code.lower()
    now = time.time()

    if low in _PRINTERS_LC:
        sel["name"], sel["at"] = _PRINTERS_LC[low][0], now
        print(f"-> Printer selected: {sel['name']}")
        return

    if low.startswith(_SPOOL_PREFIX):
        if not alive(now):
            sel["name"] = None
            print("!! No active printer selection. Scan a printer first.")
            return
        try:
            spool_id = int(low.split("s-", 1)[1])
        except (IndexError, ValueError):
            print(f"!! Could not parse spool id from: {code}")
            return
        name, url = _PRINTERS_LC[sel["name"].lower()]
        try:
            # Just set the active spool, exactly like a GUI click.
            # The notify_active_spool_set handler does dedup + location.
            await moonraker_set_spool(url, spool_id)
            sel["at"] = now
            print(f".. requested spool {spool_id} on {name}")
        except Exception as e:
            print(f"!! Failed to set spool on {name}: {e}")
        return

    if low == _CLEAR_CODE:
        if not alive(now):
            sel["name"] = None
            print("!! Scan a printer first, then the clear code.")
            return
        name, url = _PRINTERS_LC[sel["name"].lower()]
        try:
            await moonraker_set_spool(url, None)
            sel["at"] = now
            print(f".. requested clear on {name}")
        except Exception as e:
            print(f"!! Failed to clear {name}: {e}")
        return

    print(f"?? Unrecognized code: {code}")


# ================================ MAIN ==================================
async def main():
    loop = asyncio.get_running_loop()
    tasks = [asyncio.create_task(printer_ws_loop(n, u)) for n, u in PRINTERS.items()]
    tasks.append(asyncio.create_task(scanner_loop(loop)))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
