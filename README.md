# WashroomSweep

A low-cost, standalone sweeper that tells you, **before you enter a closed
space** (washroom, changing room, short-term rental), whether a networked
camera is **currently streaming that space**.

It answers *"is something recording HERE right now"* — not *"how many devices
are on this network"*.

Built by a team of two University of Waterloo undergraduates for the
**Wireless for Humanity Student Design Competition**.

## How it works

An ESP32 in WiFi promiscuous mode passively counts bytes per source MAC.
No decryption, no network association — 802.11 header fields only
(transmitter address, frame length, RSSI).

Two detection signals, used together:

1. **Uplink/downlink asymmetry.** A streaming camera transmits far more than
   it receives; phones and laptops are the opposite. This is a coarse filter.
2. **Bitrate response to a stimulus.** Cameras use variable-bitrate encoding,
   so changing what the camera *sees* changes how much data it *sends*. We
   toggle the room light in a known square wave (3 s off / 3 s on, repeated
   5×) and cross-correlate each device's byte-rate series against that
   reference, with a small lag search (cameras buffer, so up to ~2 s of
   offset is allowed). High correlation means that device can see this room.

The verdict is always one of:

```
NO NETWORKED CAMERA DETECTED
CANDIDATE DETECTED
UNKNOWN - HIGH AMBIENT TRAFFIC
UNKNOWN - INCOMPLETE SWEEP
```

We deliberately never output "SAFE" or "no camera": RF silence is not proof
of absence.

## What we do and don't claim

We do **not** claim novelty of the mechanism — traffic-analysis camera
detection is established prior art (DeWiCam, CSI:DeSpy, SnoopDog, Lumos,
LocCams). Our contribution is:

- a **low-cost ESP32 sensing front end with laptop-assisted analysis** (no
  laptop-grade capture hardware in the RF path; the correlation itself runs
  in ~100 lines of Python on the host),
- the **entry-sweep** scenario: a quick check before entering a space,
- using the room's **existing light switch** as the stimulus, and
- quantifying **when the method fails**.

### Known blind spots (stated honestly)

- **Local-storage cameras** (recording to an SD card, not streaming) produce
  no RF traffic and are **undetectable** by this method.
- **Wired cameras** are likewise invisible to RF monitoring.
- **2.4 GHz only.** The ESP32 radio (classic, C3, and S3 alike) cannot see
  5 GHz or 6 GHz WiFi; a camera streaming on those bands is invisible to
  this hardware.
- **RSSI is not distance.** We report signal strength but do not localize,
  and never claim a device's position.
- A busy RF environment can drown the signal; the tool then reports
  `UNKNOWN - HIGH AMBIENT TRAFFIC` rather than guessing.
- If the serial feed itself dies mid-sweep (unplugged board, wrong
  channel), the verdict is `UNKNOWN - INCOMPLETE SWEEP` — a heartbeat from
  the board every 200 ms is required evidence that we were actually
  listening.

## Repository layout

```
esp32/wifi_sniffer/   Main detector sketch (Arduino IDE, any ESP32 variant).
                      Promiscuous capture; uplink/downlink split via the
                      802.11 ToDS/FromDS bits (works for any BSS, no AP
                      knowledge needed) with an optional BSSID filter for
                      controlled tests; per-client byte counts in 200 ms
                      windows; CSV + WINDOW heartbeat over serial; MARK
                      command for stimulus alignment; channel lock/hop.
esp32/ble_presence/   Supplementary BLE presence panel (adapted from
                      Justin Fang's scanner). Lists nearby BLE devices for
                      situational awareness only — BLE cannot carry live
                      video, so this board never feeds the verdict.
host/sweep.py         Host-side correlator (Python, pyserial). Reads the
                      serial CSV, builds per-MAC byte-rate series, generates
                      the square-wave reference at the operator's mark, and
                      cross-correlates with lag search. Logs every run to
                      logs/ as CSV for later plotting.
```

## Quick start

**ESP32 (Arduino IDE or arduino-cli):** flash
`esp32/wifi_sniffer/wifi_sniffer.ino` to the detector board. Serial is
115200 baud. Commands over serial: `CH <n>` (lock channel), `HOP` (toggle
hopping), `AP <mac>` / `AP OFF` (restrict counting to one BSSID / clear),
`MARK`.

**Host:**

```sh
cd host
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python sweep.py /dev/cu.usbserial-XXXX --channel 6 --ap AA:BB:CC:DD:EE:FF
```

Opening the port resets the ESP32 (DTR), so `sweep.py` waits for the reboot
and pushes the channel/BSSID configuration itself — don't pre-configure via
a serial monitor. `--ap` suppresses neighboring networks in a controlled
test; omit it for a real sweep, where the camera's AP is unknown to you.

Press Enter at the instant you first switch the light **off**, then toggle
the light 3 s off / 3 s on for five cycles. The script records, correlates,
and prints the verdict plus a per-device table with evidence counts.

## Validation

What has been verified so far — and, just as importantly, what hasn't:

- Pilot (network-side): an iPhone streaming video measured ~5 MB/s with
  lights on vs ~2 MB/s with lights off (~2.4×) — the VBR assumption holds.
  This was measured *inside* the network on a laptop, not over the air.
- Host pipeline (simulation only): verified end-to-end against a simulated
  ESP32 on a pseudo-terminal speaking the real serial protocol. A
  camera-like device with 0.4 s encoder lag is detected (correlation 1.00);
  an idle phone and a large-file downloader are correctly not flagged
  (heavy traffic alone is not a camera); killing the feed mid-sweep yields
  `UNKNOWN - INCOMPLETE SWEEP`, not a false all-clear.
- Both sketches compile clean on classic ESP32, ESP32-C3, and ESP32-S3
  (arduino-esp32 core 3.3.11).
- **Not yet validated: real over-the-air RF capture on ESP32 hardware.**
  The simulation exercises the full host pipeline but proves nothing about
  the promiscuous-mode capture path itself.

## Team

- **David (Jiacheng) Wang** — detection pipeline (ESP32 sniffer + host
  correlator)
- **Justin Fang** — device scanning groundwork
  ([washroom_security](https://github.com/justinfang37/washroom_security)),
  BLE presence panel
