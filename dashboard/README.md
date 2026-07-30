# Dashboard

Justin's live dashboard (`monitor.py` + `dashboard.html`), running against this
repo's firmware and analysis code.

## Running it

```sh
cd dashboard
../host/.venv/bin/python monitor.py --port /dev/cu.usbserial-XXXX
```

Then open http://localhost:8080 (it opens automatically).

Only one program may hold a serial port. Stop this before reflashing a board,
or start it with `--no-serial`.

## What it needs from the rest of the repo

`monitor.py` loads the correlation maths from `../host/sweep.py` at startup. If
that file is missing it sets `SWEEP = None` and the sweep button returns
"host/sweep.py not found" — the camera-detection feature is inert. Keep the two
directories side by side.

It parses the CSV emitted by `esp32/wifi_sniffer` and `esp32/softap_demo`
(`WINDOW,...` heartbeats plus per-device rows). Note it does **not** parse the
space-aligned table printed by `esp32/wifi_scanner`-style sketches — that regex
expects eight columns where those sketches print five.

## Flashing the BLE board

`ble_scanner` overflows the default partition table (measured at 124% of flash).
In the Arduino IDE select **Tools > Partition Scheme > Huge APP**, which brings
it to 51%. It also only builds for the classic ESP32: the
`BLE_ADDR_TYPE_RANDOM` / `BLE_ADDR_TYPE_RPA_RANDOM` constants don't exist on the
C3/S3 BLE stack. `esp32/ble_presence/` carries a portable version that compares
the raw address-type byte instead.
