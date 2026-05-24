#!/usr/bin/env python3
"""
printer_fallback  --  per-printer Spoolman/Location fallback
============================================================
Runs on EACH printer's host (one copy per printer; only MY_NAME differs).
It is a BACKSTOP to the central Pi scanner service, which remains the
authoritative fast path. This script only acts when the Pi did NOT --
i.e. the Pi is offline, or you assigned via the Fluidd/Mainsail GUI while
the Pi happened to be down.

Two rules (the only safe, convergent pair):
  * CLAIM  -- if THIS printer was just assigned a spool (its own active spool
    changed) and after a delay the spool's Spoolman "location" still isn't
    this printer, write this printer's name. Done once per assignment.
    This is the only case allowed to overwrite another printer's name,
    because a fresh assignment is unambiguous proof of ownership.
  * CLEAR  -- if this printer is a steady-state holder (no recent assignment)
    and the spool's location either names a DIFFERENT real printer OR stays
    blank past the delay (an orphan: a spool we hold that nobody recorded),
    clear this printer's active spool. Never writes; just yields. The delay
    ensures a genuinely fresh assignment (handled by CLAIM above) is never
    mistaken for an orphan -- its location gets written before this can fire.

Deliberately does NOTHING when:
  * the location is a non-printer string (a custom/manual location)
  * on startup, beyond clearing itself if the location names someone else or
    stays blank past the delay (startup never CLAIMS, so a reboot can't stamp
    over a correct location, but it will shed a spool nobody has recorded)

The DELAY must exceed the Pi's reaction time (~1s) so the Pi always goes
first when it is alive. Only stdlib is used -- no pip installs on printers.
"""

import json
import time
import urllib.request
import urllib.error

# ====================== EDIT PER PRINTER ================================

# This printer's name -- MUST equal the key used in the Pi's PRINTERS map
# AND the printer's QR payload. Change this on each machine.
MY_NAME = "voron"

# This printer's own Moonraker (local).
MY_MOONRAKER = "http://127.0.0.1:7125"

# Shared Spoolman instance.
SPOOLMAN_URL = "http://192.168.1.10:7912"

# All printer names in the farm (used to recognise "another printer" in a
# location field). Same on every machine.
ALL_PRINTERS = {"voron", "trident", "prusa"}

# Seconds a condition must persist before the fallback acts. Must be > the
# Pi's ~1s reaction. Larger = the Pi more reliably wins; smaller = faster
# fallback. A full move with the Pi offline converges in up to ~2*DELAY.
DELAY = 10

# How often to poll (seconds).
POLL_INTERVAL = 5

# HTTP timeout (seconds).
HTTP_TIMEOUT = 5

# ========================================================================

_ALL_LC = {n.lower() for n in ALL_PRINTERS}
_MY_LC = MY_NAME.lower()


# ----------------------------- HTTP (stdlib) ----------------------------
def _req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw.strip() else {}

def get_my_active():
    resp = _req("GET", f"{MY_MOONRAKER}/server/spoolman/spool_id")
    inner = resp.get("result", resp)
    return inner.get("spool_id")

def clear_my_spool():
    # Empty body unsets via Moonraker's default (some builds reject {"spool_id": null}).
    _req("POST", f"{MY_MOONRAKER}/server/spoolman/spool_id", body={})

def get_location(spool_id):
    resp = _req("GET", f"{SPOOLMAN_URL}/api/v1/spool/{spool_id}")
    return (resp.get("location") or "").strip()

def set_location(spool_id, location):
    _req("PATCH", f"{SPOOLMAN_URL}/api/v1/spool/{spool_id}", body={"location": location})


# --------------------------- decision logic ----------------------------
class Reconciler:
    """Pure-ish decision core; returns ('claim'|'clear', spool_id) or None.
    Kept separate from I/O so it can be unit-tested deterministically."""
    def __init__(self, my_name):
        self.me = my_name.lower()
        self.active = None       # last observed active spool id
        self.claimed = True      # have we reconciled location for this assignment
        self.assigned_at = 0.0   # time of last observed assignment edge
        self.clear_since = None  # when "location names another printer" was first seen
        self.first = True        # first observation == startup (never claim)

    def step(self, now, cur_active, loc):
        # ---- edge: did our own active spool change? ----
        if cur_active != self.active:
            if cur_active is not None:
                # Startup -> treat as steady-state (claimed). Live change -> fresh (claim).
                self.claimed = self.first
                if not self.first:
                    self.assigned_at = now
            else:
                self.claimed = True
            self.active = cur_active
            self.clear_since = None
        self.first = False

        if self.active is None:
            return None

        loc_l = (loc or "").strip().lower()
        names_other = loc_l in _ALL_LC and loc_l != self.me

        if loc_l == self.me:
            self.claimed = True
            self.clear_since = None
            return None

        if not self.claimed:
            # Fresh assignee: claim once, after the Pi's window. Allowed to
            # overwrite blank OR another printer's name (assignment is proof).
            if now - self.assigned_at >= DELAY:
                return ("claim", self.active)
            return None

        # Steady-state holder, location != me.
        # Either it names a different real printer (someone took it), or it is
        # blank (an orphan: we hold a spool nobody has recorded). Both mean we
        # should yield -- but only after the delay, so the Pi (or, for a fresh
        # assignment, our own claim path) gets first crack. A genuinely fresh
        # assignment never reaches here: it is handled by the not-claimed
        # branch above, which writes our name before this can fire.
        if names_other or loc_l == "":
            if self.clear_since is None:
                self.clear_since = now
                return None
            if now - self.clear_since >= DELAY:
                return ("clear", self.active)
            return None

        # A non-printer custom-string location: leave alone (manual location).
        self.clear_since = None
        return None


# -------------------------------- loop ----------------------------------
def main():
    rec = Reconciler(MY_NAME)
    print(f"[{MY_NAME}] fallback running. Pi is authoritative; this only acts "
          f"when the Pi doesn't (DELAY={DELAY}s).")
    while True:
        now = time.time()
        try:
            cur = get_my_active()
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[{MY_NAME}] Moonraker read failed ({e}); skipping cycle")
            time.sleep(POLL_INTERVAL)
            continue

        loc = None
        if cur is not None:
            try:
                loc = get_location(cur)
            except (urllib.error.URLError, OSError, ValueError) as e:
                print(f"[{MY_NAME}] Spoolman read failed ({e}); skipping cycle")
                time.sleep(POLL_INTERVAL)
                continue

        action = rec.step(now, cur, loc)
        if action is not None:
            kind, sid = action
            try:
                if kind == "claim":
                    set_location(sid, MY_NAME)
                    rec.claimed = True
                    print(f"[{MY_NAME}] fallback CLAIM: location of spool {sid} -> {MY_NAME}")
                elif kind == "clear":
                    clear_my_spool()
                    print(f"[{MY_NAME}] fallback CLEAR: dropped spool {sid} (location names another printer)")
            except (urllib.error.URLError, OSError, ValueError) as e:
                # Leave state so it retries next cycle.
                if kind == "claim":
                    rec.claimed = False
                print(f"[{MY_NAME}] fallback {kind} failed ({e}); will retry")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
