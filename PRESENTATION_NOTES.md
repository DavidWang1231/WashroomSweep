# WashroomSweep — presentation notes

## The one-sentence pitch

A low-cost sweeper you carry into a closed space that tells you, before you
go in, whether a networked camera is streaming that space right now — not
how many devices are on the network.

## What to demo live (all verified working)

**Demo 1 — passive sensing, no network access.** Plug the board in, run the
host script. It lists real devices in the room with per-device uplink and
downlink byte counts, updating every 200 ms. Nothing is joined, nothing is
decrypted — 802.11 header fields only. A 15 s run picked up 18 distinct
clients.

**Demo 2 — the detection result.** Show the captured run: the streaming
camera at **17.9 MB uplink / 0 downlink, 99.9% of all air traffic**, three
orders of magnitude above every other client (all under 12 KB). Uplink/
downlink asymmetry alone separates it from everything else in the room.

**Demo 3 — refusing to guess.** Kill the feed mid-sweep. The tool prints
`UNKNOWN - INCOMPLETE SWEEP`, not "nothing found". Worth doing live: it is
the clearest demonstration of the design rule that we never output SAFE.

Fallback if hardware misbehaves: the simulated end-to-end run (correlation
1.00 on a camera-like device; an idle phone and a large-file downloader both
correctly cleared — heavy traffic is not a camera).

## The narrative

1. **Problem.** Existing advice for hidden cameras is unreliable (lens
   glints, RF "detectors" that beep at everything). We want a specific
   question answered: is something recording *this* space *now*.

2. **Two signals.** Cameras transmit far more than they receive — that is
   the coarse filter. And they use variable-bitrate encoding, so changing
   what the camera sees changes how much it sends — toggle the room light in
   a known pattern and cross-correlate. The second signal is what tells you
   the camera can see *this room*, not just that a camera exists somewhere.

3. **What we built.** ESP32 in promiscuous mode, 200 ms windows, per-device
   uplink/downlink split via the 802.11 ToDS/FromDS bits (works for any
   network without knowing the AP), CSV to a host script that does the
   correlation. Roughly 100 lines of Python.

4. **What we got.** Live over-the-air detection via the asymmetry signal.
   The correlation stage is verified in simulation and by the network-side
   pilot, but we could not close it over the air — see below, and we think
   the reason is worth more than the result would have been.

5. **Prior art.** We do not claim the mechanism is novel — DeWiCam, CSI:DeSpy,
   SnoopDog, Lumos, LocCams all do traffic-analysis camera detection. Our
   contribution is a low-cost ESP32 front end with laptop-assisted analysis,
   the entry-sweep scenario, using the room's own light switch as the
   stimulus, and quantifying where it fails.

## State these before anyone asks

- **Local-storage cameras** (SD card, not streaming) emit nothing. Invisible.
- **Wired cameras.** Invisible.
- **5 GHz and 6 GHz.** The ESP32 radio is 2.4 GHz only — it cannot even
  receive those bands. A 5 GHz camera is invisible to this hardware.
- **802.11ax within 2.4 GHz.** Measured: a 6.6 MB/s download on channel 6
  came back as ~2 KB/s, about 0.03%. The receive path was verified healthy
  at the same time (driver reported channel 6; beacons arriving at the
  correct 100 ms cadence at −50 to −61 dBm).
- **RSSI is not distance.** We report signal strength; we do not localize.
- **The demo rig is not the deployment mode.** We ran the ESP32 as its own
  SoftAP to force the camera onto 802.11n, which this radio can demodulate.
  A real hidden camera is on the room's network and would never join ours —
  real deployment means passively sniffing the target network, where the
  2.4 GHz limit and the ax ceiling both bite. Say this before a judge finds
  it.

## Likely questions

**"Why didn't the light correlation work?"**
Our stand-in was a phone running an IP-camera app, and a phone is a poor
camera stand-in in three specific ways. Its default encoding is constant
bitrate — ~550 KB/s regardless of scene. Its auto-gain defeats the premise:
darkening a scene doesn't simplify it, the sensor amplifies until it returns
a noisy, high-detail image. And the app composites a mandatory front+rear
camera pair, so the front sensor kept imaging the room and held the bitrate
floor at ~430 KB/s. We did confirm the mechanism — in MJPEG mode, fully
occluding the lens dropped the stream from ~700 KB/s to ~1 KB, a 300x swing.
We just couldn't drive a clean square wave. A valid stand-in needs fixed
exposure, a single sensor, and VBR encoding — which is what a real IP camera
is, and what our network-side pilot measured a 2.4x response from.

**"How do you know real cameras are 802.11n?"**
We're inferring from typical specs of inexpensive camera modules — we have
not tested a real IP camera. That's the next thing we'd buy.

**"Isn't a video call a fair camera stand-in?"**
Only partly. A call is bidirectional, so its uplink/downlink ratio is near
0.8, where a camera is one-way. That's why our asymmetry threshold would
reject a video call — correctly, it isn't a camera.

**"How would you fix the band and PHY limits?"**
New silicon, not new software. The ESP32-C5 is dual-band 2.4/5 GHz with
WiFi 6 support and would close both gaps. We didn't use it because our
hardware was what we had; a capture-grade NIC or SDR would also work but
breaks the low-cost framing.

**"How long does a sweep take?"**
30 seconds of stimulus in our current setup, plus channel search in a real
deployment — we lock the channel in the controlled test, but a real sweep
has to scan, which extends it and reduces per-channel sampling density.

**"What stops a false positive from someone uploading a video?"**
Nothing, on asymmetry alone — that's exactly why the correlation stage
exists. An upload doesn't track the room's light; a camera watching the room
does. In simulation the correlation stage correctly cleared a large-file
downloader that asymmetry alone might flag.

## Bugs worth mentioning if engineering rigour comes up

Both were found by testing against hardware and both silently corrupted
results before they were caught:

- **Missing promiscuous filter.** Without an explicit filter the driver also
  delivers FCS-failed frames. Near a busy modern AP those are constant, and
  their header bytes are effectively random — measured as a near-uniform
  spread across all eight ToDS/FromDS combinations where real traffic sits
  in two. Those junk frames minted random MAC addresses that flooded the
  fixed-size device table and evicted real stations before they could be
  reported.
- **Float binning.** Bin indices computed as `int(seconds / 0.2)` misfiled
  ~12% of samples — 18 of 151 bins in a clean noiseless test — dragging a
  true 1.00 correlation down to 0.71, enough to push a detectable camera
  under threshold. Now integer milliseconds throughout.

## Tone

The honest failure is the strongest part of this project. "We quantified
where it fails" is in our contribution statement, and we now have numbers
for it. Don't bury the ax measurement or the stand-in problem — lead with
the detection that worked, then present the limits as findings.
