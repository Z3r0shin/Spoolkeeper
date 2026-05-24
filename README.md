# IMPORTANT DISCLAIMER
I vibe-coded this whole thing in a day. The debugging and troubleshooting was also 
done in that day. It can break some stuff on your side even if you follow all the 
instructions. Please use your brains and, if you're like me, whatever that could 
help you implement it intelligently.

I may or may not answer messages/issues about this. This is the first time I ever
post something like this to github. Please be gentle.
At least I've posted it...

Anyway, here's the robot-generated description/instructions below :


# Spoolman Barcode Scanner for a Klipper Print Farm

Assign [Spoolman](https://github.com/Donkie/Spoolman) spools to multiple
Moonraker/Klipper printers by scanning QR codes with a cheap Bluetooth/USB
barcode scanner — while keeping Spoolman's **Location** field as the single
source of truth for where every spool currently lives.

The scanner and the Fluidd/Mainsail GUI are treated as **equal** ways to assign
a spool. You never *have* to use the scanner; assigning in the GUI does the same
bookkeeping. A spool is never left loaded on two printers at once, and the
system self-heals from common failure cases (a printer offline during a move, a
spool assigned while the controller was down).

> Built and tested with an Eyoyo EY-034 scanner and a Raspberry Pi 3 A+ running
> DietPi, against Moonraker's `[spoolman]` integration. It works with any scanner
> that presents as a **USB/Bluetooth HID keyboard** on any **Linux** SBC — see
> [Compatibility](#compatibility) for the exact requirements. This is a tool for
> **Moonraker + Spoolman** specifically, not a generic barcode framework.

---

## Contents

- [How it works](#how-it-works)
- [Compatibility](#compatibility)
- [What you need](#what-you-need)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Part 1 — Central scanner (the Pi)](#part-1--central-scanner-the-pi)
- [Part 2 — Per-printer fallback](#part-2--per-printer-fallback)
- [Generating printer QR codes](#generating-printer-qr-codes)
- [Daily use](#daily-use)
- [Configuration reference](#configuration-reference)
- [How the bookkeeping works](#how-the-bookkeeping-works)
- [Design notes and known limits](#design-notes-and-known-limits)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## How it works

There are two pieces, and they are belt-and-suspenders: the central Pi is the
authoritative fast path, and a tiny per-printer script is the fallback.

**Central scanner (`pi/scanner.py`).** A small always-on **Linux** host (a Pi, an
Orange Pi Zero, anything running Python 3 with `evdev`) does two things at
once. It reads the barcode scanner, and it holds a persistent websocket to every
printer's Moonraker, listening for `notify_active_spool_set` — the notification
Moonraker emits whenever *anything* changes a printer's active spool, whether
that was the scanner, the GUI, or a macro. A single event handler reacts to
every such change:

- **Dedup** — clear the active spool on any *other* printer that was holding the
  same spool, so a spool is never live on two machines.
- **Location** — write that spool's Spoolman `location` field to the printer it
  just landed on; blank it when the spool leaves (only if Spoolman still shows
  it on that printer, so a race can't wipe a location another printer just took).

Because the scanner just *sets the active spool* on a printer (exactly like a
GUI click) and lets the event handler do the rest, **the scanner and the GUI are
identical**. Assigning in Fluidd triggers the same bookkeeping.

**Per-printer fallback (`printer/printer_fallback.py`).** One copy runs on each
printer's host. It only acts when the central Pi *didn't* — i.e. the Pi is
offline, or you assigned in the GUI while the Pi happened to be down. It uses a
short delay so that whenever the Pi is alive, the Pi always goes first and the
fallback finds everything already consistent and does nothing. The fallback
follows exactly two rules:

- **Claim** — if *this* printer was just assigned a spool and, after the delay,
  the spool's Location still isn't this printer, write this printer's name.
- **Clear** — if this printer is a steady-state holder (no recent assignment)
  and the spool's Location names a *different* printer, or stays *blank* past the
  delay (an orphan nobody recorded), drop this printer's active spool.

The fallback never writes another printer's name on top of a contested
Location; it only claims a blank, or yields. That asymmetry is what keeps two
printers from fighting over the same field.

---

## Compatibility

This is a Moonraker + Spoolman tool with a specific (if broad) hardware envelope.
Before assuming it fits your setup, check each of these:

- **Backend:** requires **Moonraker** (with its `[spoolman]` integration) and a
  **Spoolman** instance. It relies on Moonraker's `notify_active_spool_set`
  websocket notification, its `POST/GET /server/spoolman/spool_id` endpoint, and
  Spoolman's `/api/v1/spool` location field. It is *not* a generic
  barcode-to-anything framework and will not drive non-Klipper systems.

- **Scanner:** must operate as a **USB or Bluetooth HID keyboard** (the default
  for most scanners). Scanners running in USB-serial/VCP mode, or raw-serial
  modules wired to GPIO/UART (e.g. a GM65 on an MCU), are *not* read by the
  central script as written — that's a different input path. The scanner must
  also read **2D/QR** codes (not 1D-only), be set to a **US/English keyboard
  layout** (the decoder assumes US), and append an **Enter/CR suffix** to each
  scan (the script acts only on a terminated read). Both layout and suffix are
  one-time setup codes in the scanner's manual.

- **Central host (`pi/`):** any **Linux** SBC with Python 3 and `python3-evdev`,
  because it reads the scanner via Linux `/dev/input/event*` devices. A Raspberry
  Pi, Orange Pi Zero, etc. all qualify. A bare microcontroller (ESP32, Arduino)
  does **not** — there's no Linux/evdev there. A free USB port (for a dongle) or
  working onboard Bluetooth is needed for the scanner.

- **Per-printer fallback (`printer/`):** the portable half — Python 3 standard
  library only, plain HTTP. It runs on anything that runs Python 3 and can reach
  Moonraker and Spoolman over the network.

---

## What you need

- A **Spoolman** instance reachable on your LAN.
- One or more printers running **Klipper + Moonraker**, each with Moonraker's
  `[spoolman]` integration configured and pointed at that Spoolman.
- A **central host** for the scanner: any Debian-based SBC with Python 3 and a
  free USB port (or onboard Bluetooth). A Raspberry Pi 3 A+ is plenty.
- A **2D barcode scanner** that reads QR codes and acts as an HID keyboard
  (nearly all of them do). USB dongle or Bluetooth both work.
- Spool QR labels printed from Spoolman, and printer QR labels you generate
  (see below).

---

## Repository layout

```
spoolman-scanner/
├── README.md
├── LICENSE
├── .gitignore
├── pi/                      # runs once, on the central scanner host
│   ├── scanner.py           # the authoritative scanner + event service
│   ├── debug_scan.py        # one-off helper to verify the scanner over SSH
│   └── spool-scanner.service
└── printer/                 # runs on EACH printer's host (one copy each)
    ├── printer_fallback.py
    └── spool-fallback.service
```

---

## Prerequisites

On **each printer**, confirm Moonraker's `[spoolman]` section exists in
`moonraker.conf` and points at your Spoolman, e.g.:

```ini
[spoolman]
server: http://192.168.1.10:7912
```

If any printer's Moonraker has a locked-down `[authorization]` block, add the
central scanner host's IP to its trusted clients so the Pi's calls and websocket
are accepted:

```ini
[authorization]
trusted_clients:
    192.168.1.50/32     # the central scanner host
```

Restart that Moonraker after editing.

---

## Part 1 — Central scanner (the Pi)

These steps assume a minimal Debian/DietPi install. Adjust `sudo`/paths to your
distro and user.

**1. Install dependencies.**

```bash
sudo apt update
sudo apt install -y python3-evdev python3-requests python3-websockets qrencode
```

(`qrencode` is only needed once, to make printer labels. If your distro's
`python3-websockets` is too old, use `pip install --break-system-packages -U
websockets` instead.)

**2. Get the code.**

```bash
git clone https://github.com/<you>/spoolman-scanner.git
cd spoolman-scanner
```

**3. Connect the scanner.**

- *USB dongle:* plug it into the host. The scanner must be switched to its 2.4 GHz
  mode (scan the "2.4G" setup code from the scanner's paper manual if needed). No
  pairing required.
- *Bluetooth:* pair it once with `bluetoothctl` (`scan on`, `pair`, `trust`,
  `connect`). It should pair as an HID keyboard.

**4. Find the scanner's device name** and set it in the config. Over the 2.4 GHz
dongle, many scanners enumerate under a generic name (the Eyoyo EY-034 shows up
as `YuRiot Barcode Scanner Keyboard`, **not** "EY-034"):

```bash
python3 -c "from evdev import InputDevice,list_devices; [print(p, InputDevice(p).name) for p in list_devices()]"
```

If this prints nothing as a non-root user, you lack permission on the input
device — add yourself to the `input` group (`sudo usermod -aG input $USER`,
then log out and back in) or run with `sudo`.

**5. Verify the scanner actually scans** (use this, *not* `cat` — see
Troubleshooting):

```bash
python3 pi/debug_scan.py
```

Scan a spool label. You want to see e.g. `WEB+SPOOLMAN:S-42  <CR>`. If there's no
`<CR>`, enable the scanner's "Add Enter/CR suffix" setup code. If characters are
garbled, set the scanner to a US/English keyboard layout. Exit with **Ctrl-C**
(not Ctrl-Z, which leaves the device grabbed).

**6. Edit `pi/scanner.py`** — set `PRINTERS` (name → Moonraker URL),
`SPOOLMAN_URL`, and `SCANNER_NAME_HINT` to a substring of the name from step 4.

**7. Test in the foreground.**

```bash
python3 pi/scanner.py
```

You should see one `connected; active spool = ... (seed, no changes)` line per
printer. Scan a printer label, then a spool label, and watch for
`OK  spool N -> <printer>`. Confirm the active spool changed in that printer's
Fluidd Spoolman panel and that its Location updated in Spoolman.

**8. Install as a service.** Edit the `ExecStart` path and `User` in
`pi/spool-scanner.service`, then:

```bash
sudo cp pi/spool-scanner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spool-scanner
journalctl -u spool-scanner -f
```

---

## Part 2 — Per-printer fallback

Run **one copy on each printer's host**. It uses only the Python standard
library — nothing to install.

**1. Copy `printer/printer_fallback.py`** to each printer (e.g. into
`/home/pi/spoolman-scanner/printer/`).

**2. Edit two lines per machine:** set `MY_NAME` to *that* printer's name
(matching the key in the Pi's `PRINTERS` map and the printer's QR), and confirm
`MY_MOONRAKER` (usually `http://127.0.0.1:7125`). `SPOOLMAN_URL`, `ALL_PRINTERS`,
`DELAY`, and `POLL_INTERVAL` are the same on every machine.

**3. Test** with the central Pi service *stopped* (so you exercise the fallback,
not the Pi):

```bash
# on the Pi, temporarily:
sudo systemctl stop spool-scanner
# on one printer:
python3 printer/printer_fallback.py
```

Assign a spool to that printer in Fluidd; after ~`DELAY` seconds you should see
`fallback CLAIM: location ... -> <printer>` and the Location update in Spoolman.
Bring the Pi back up and confirm the fallback goes quiet.

**4. Install as a service** on each printer:

```bash
sudo cp printer/spool-fallback.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spool-fallback
journalctl -u spool-fallback -f
```

---

## Generating printer QR codes

Spool labels come from Spoolman itself (they encode `web+spoolman:s-<id>`). You
generate one QR per printer, encoding the printer's name exactly as it appears in
`PRINTERS`:

```bash
qrencode -o voron.png   -s 8 "voron"
qrencode -o trident.png -s 8 "trident"
qrencode -o prusa.png   -s 8 "prusa"
```

Print them and stick one on each machine. Optionally make a clear code:

```bash
qrencode -o clear.png -s 8 "web+spoolman:clear"
```

Matching is case-insensitive, so the printer-name capitalization on the label
doesn't have to match the config exactly.

---

## Daily use

Scanner flow: **scan a printer label, then a spool label.** The printer
selection is sticky, so you can scan several spools in a row for the same
printer; scanning a different printer label re-targets. Scan the clear code (after
a printer) to unload that printer.

Or skip the scanner entirely and assign in Fluidd/Mainsail — the bookkeeping is
identical.

> The scanner beeps the same way on every successful read regardless of whether
> the assignment succeeded. Trust the service log (`journalctl`), not the beep,
> when confirming an action.

---

## Configuration reference

**`pi/scanner.py`**

| Setting | Meaning |
| --- | --- |
| `PRINTERS` | Map of printer-QR text → that printer's Moonraker base URL. |
| `SPOOLMAN_URL` | Spoolman base URL (no trailing slash). |
| `SCANNER_NAME_HINT` | Case-insensitive substring matching the scanner's input-device name. |
| `SELECTION_TIMEOUT` | Seconds before a sticky printer selection expires (`0` = never). |
| `HTTP_TIMEOUT` | Timeout for Moonraker/Spoolman REST calls. |

**`printer/printer_fallback.py`**

| Setting | Meaning |
| --- | --- |
| `MY_NAME` | This printer's name (per machine). Must match the Pi's `PRINTERS` key. |
| `MY_MOONRAKER` | This printer's own Moonraker, usually `http://127.0.0.1:7125`. |
| `SPOOLMAN_URL` | Shared Spoolman base URL. |
| `ALL_PRINTERS` | Set of all printer names in the farm (same on every machine). |
| `DELAY` | Seconds a condition must persist before the fallback acts. Must exceed the Pi's ~1 s reaction time. |
| `POLL_INTERVAL` | How often the fallback polls. |

---

## How the bookkeeping works

The Spoolman **Location** field is the source of truth: it names the printer a
spool is currently loaded on, or is blank if the spool is loaded nowhere.

- **Assign** (scanner or GUI): the active spool is set on a printer → Moonraker
  emits `notify_active_spool_set` → the Pi clears any other printer that held
  that spool and writes Location = the new printer.
- **Move**: assigning a spool that lived elsewhere clears the old printer first,
  so it's never double-loaded.
- **Clear**: unsetting a printer's spool blanks that spool's Location, but only
  if Location still named that printer (race guard).
- **Pi offline / GUI-only**: the per-printer fallback maintains the same
  invariants after a short delay — claiming its own fresh assignments and
  clearing spools whose Location moved away or went blank.

The clear call sends an **empty body** to Moonraker's `POST
/server/spoolman/spool_id` rather than `{"spool_id": null}`, because some
Moonraker builds reject an explicit `null` with HTTP 400. An empty body unsets
via Moonraker's documented default.

---

## Design notes and known limits

- **No reconcile pass.** Nothing periodically rewrites state to match a model,
  so a spool you set in the GUI is never silently reverted. State converges
  through events and the fallback's targeted rules instead.
- **The truly ambiguous case still needs you.** If the Pi is offline and you
  assign the *same* spool to two printers within the delay window, the system
  converges to a consistent single-machine state, but the "winner" is whichever
  printer wrote the Location last — which may not be the one you intended. It's
  never corrupted or double-loaded; you'd just notice and re-assign. No
  distributed scheme can recover an intent that was never recorded.
- **Blank Location is self-deleting.** A spool active on a printer with a blank
  Location is treated as an orphan and cleared after the delay. This removes
  "loaded but intentionally unlocated" as a state. With the Pi up it never
  arises (Location is written within a second).
- **Reboot-mid-claim window.** If you assign a spool while the Pi is down and the
  printer reboots in the few seconds before the fallback writes the Location, the
  reboot erases the "just assigned" memory and the spool is cleared as an orphan.
  Narrow, and the failure is safe (it clears rather than mis-assigns). Closing it
  would require persisting per-printer state across reboots.

---

## Troubleshooting

**`list_devices()` / the probe prints nothing.** You don't have permission on
`/dev/input/event*`. Add your user to the `input` group and re-login, or run as
root. (On DietPi, confirm whether you're the `dietpi` user vs root.)

**`cat` shows nothing when I scan, but the device is listed.** Expected over SSH.
A USB/BT HID scanner types into the host's *local* console (tty1), not your SSH
session's stdin. Use `pi/debug_scan.py`, which reads the event device directly
and works over SSH.

**Scanner isn't named "EY-034".** Over the 2.4 GHz dongle it commonly enumerates
generically (e.g. `YuRiot Barcode Scanner Keyboard`). Set `SCANNER_NAME_HINT` to
a substring of whatever the probe prints.

**Scans don't terminate / pile onto one line.** The scanner isn't sending a
suffix. Scan its "Add Enter/CR suffix" setup code. The service only acts on a
complete, Enter-terminated read.

**Punctuation is wrong** (e.g. `web+spoolman:` mangled). The scanner's keyboard
layout isn't US. Scan its "US/English keyboard" setup code; the decoder assumes
US layout.

**Clearing fails with `400 ... Unknown`.** An older symptom from sending
`{"spool_id": null}`. This repo already sends an empty body to clear, which works
across Moonraker builds. If you still see it, the message after `Failed`/`could
not clear` in the log will name the real cause.

**A printer's events aren't registering.** Check the Pi log for
`websocket down ... reconnecting` for that printer — that points at the network
or an `[authorization]` block rejecting the Pi. Add the Pi's IP to that
Moonraker's `trusted_clients`.

**`Device or resource busy` when starting the scanner.** Another process has the
input device grabbed — usually a `debug_scan.py` you suspended with Ctrl-Z
instead of quitting with Ctrl-C, or a second copy of the service. Kill it first.

---

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE). Note that the
AGPL's network-use clause requires anyone who modifies this and offers it to
others over a network to make their modified source available.
