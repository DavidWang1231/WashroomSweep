# ble_scanner — Justin's BLE board

Latest version from
[justinfang37/washroom_security](https://github.com/justinfang37/washroom_security).
Scans BLE advertisements and reports them over WiFi (UDP) to the dashboard.

Handles three things a naive scanner gets wrong:

- **Address rotation.** Phones change their BLE address every ~15 minutes, so a
  long-lived table fills with devices that no longer exist. Entries expire after
  20 s, keeping the list to what is actually present.
- **Eviction blindness.** Once the table is full every new device displaces an
  old one. An eviction counter surfaces that — if it climbs fast, the table is
  too small for the room and real devices are being pushed out.
- **Memory pressure.** BLE and WiFi stacks together are tight. Free heap is
  reported each pass and reporting is skipped below a floor rather than crashing.

## Flashing

**Select Tools > Partition Scheme > Huge APP.** At the default scheme this
sketch is 124% of available flash and will not build; with Huge APP it sits at
51%.

**Classic ESP32 only.** It uses `BLE_ADDR_TYPE_RANDOM` /
`BLE_ADDR_TYPE_RPA_RANDOM`, which don't exist on the C3/S3 BLE stack, so those
targets fail to compile. `../ble_presence/` carries a portable variant that
compares the raw address-type byte instead and builds everywhere.

## It never feeds the camera verdict

BLE throughput is orders of magnitude below live video, so a streaming camera's
payload never appears here. This board is presence-only context.
