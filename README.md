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

- a low-cost **standalone** implementation (ESP32, no laptop-grade capture
  hardware in the loop),
- the **entry-sweep** scenario: a quick check before entering a space,
- using the room's **existing light switch** as the stimulus, and
- quantifying **when the method fails**.

### Known blind spots (stated honestly)

- **Local-storage cameras** (recording to an SD card, not streaming) produce
  no RF traffic and are **undetectable** by this method.
- **Wired cameras** are likewise invisible to RF monitoring.
- **RSSI is not distance.** We report signal strength but do not localize,
  and never claim a device's position.
- A busy RF environment can drown the signal; the tool then reports
  `UNKNOWN - HIGH AMBIENT TRAFFIC` rather than guessing.

## Repository layout

```
esp32/wifi_sniffer/   Main detector sketch (Arduino IDE, any ESP32 variant).
                      Promiscuous capture, per-MAC uplink/downlink byte
                      counts in 200 ms windows, CSV over serial, MARK
                      command for stimulus alignment, channel lock/hop.
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
115200 baud. Useful commands over the serial monitor: `CH <n>` (lock
channel), `HOP` (toggle hopping), `AP <mac>` (override AP MAC), `MARK`.

**Host:**

```sh
cd host
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python sweep.py /dev/cu.usbserial-XXXX
```

Start the script, press Enter at the instant you first switch the light
**off**, then toggle the light 3 s off / 3 s on for five cycles. The script
records, correlates, and prints the verdict plus a per-device table.

## Validation

- Pilot (network-side): an iPhone streaming video measured ~5 MB/s with
  lights on vs ~2 MB/s with lights off (~2.4×) — the VBR assumption holds.
- Host pipeline: verified end-to-end against a simulated ESP32 on a pseudo
  terminal (camera-like device with 0.4 s encoder lag detected at
  correlation 0.97; an idle phone and a large-file downloader correctly
  not flagged — heavy traffic alone is not a camera).
- Both sketches compile clean on classic ESP32, ESP32-C3, and ESP32-S3
  (arduino-esp32 core 3.3.11).

## Team

- **David (Jiacheng) Wang** — detection pipeline (ESP32 sniffer + host
  correlator)
- **Justin Fang** — device scanning groundwork
  ([washroom_security](https://github.com/justinfang37/washroom_security)),
  BLE presence panel
