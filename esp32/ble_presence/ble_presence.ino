/*
 * WashroomSweep - BLE presence panel (SUPPLEMENTARY, not part of the verdict)
 *
 * Adapted from Justin's ble_scanner.ino. Lists nearby Bluetooth Low Energy
 * advertisements: address, signal strength, name (if broadcast), maker
 * (from the advertised manufacturer ID), and whether the address is
 * randomized.
 *
 * Why this does NOT feed into the camera verdict:
 * BLE's practical throughput (tens to low hundreds of kbps) is far below
 * what live video needs, so a streaming camera's actual video payload
 * never appears here -- this board is blind to the one thing we're
 * trying to detect. Seeing a BLE-capable camera nearby only means it has
 * a BLE radio, typically used for one-time WiFi setup, not for the
 * behavior our WiFi board measures (uplink/downlink asymmetry + bitrate
 * response to the light stimulus). So this panel is presence-only
 * situational awareness (phones, wearables, etc. in range), run
 * independently on its own serial monitor, and it never gates
 * NO NETWORKED CAMERA DETECTED / CANDIDATE DETECTED / UNKNOWN.
 *
 * RSSI here is signal strength, not distance: it is affected by antenna
 * orientation, walls, and body blocking as much as by range, so the table
 * below reports a strength category, not a proximity/localization claim
 * (matches the WiFi side's stance -- RSSI is not distance, we do not
 * localize).
 */

#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>

#define SCAN_SECONDS 5       // length of each scan pass
#define MAX_DEVICES  120     // max devices remembered at once
#define FORGET_AFTER 60000   // ms: drop a device if unseen this long

struct BleDev {
  char     addr[18];
  int      rssi;
  char     name[24];
  char     maker[16];
  bool     randomAddr;   // true = private/randomized address
  uint32_t seen;         // how many advertisements we've caught
  uint32_t lastSeen;
  bool     used;
};

BleDev devices[MAX_DEVICES];
BLEScan* scanner;

// A few common Bluetooth SIG company IDs. Extend from the official
// "Company Identifiers" list if you want to name more makers.
const char* makerName(uint16_t id) {
  switch (id) {
    case 0x004C: return "Apple";
    case 0x0006: return "Microsoft";
    case 0x0075: return "Samsung";
    case 0x00E0: return "Google";
    case 0x0087: return "Garmin";
    case 0x0059: return "Nordic";
    case 0x009E: return "Bose";
    case 0x00D7: return "Qualcomm";
    default:     return "";
  }
}

// Qualitative signal-strength bucket only -- deliberately not labeled as
// distance (walls/orientation/body blocking swing RSSI as much as range
// does, so we don't claim this tells you where a device is).
const char* signalStrength(int rssi) {
  if (rssi > -50) return "strong";
  if (rssi > -70) return "medium";
  if (rssi > -82) return "weak";
  return "faint";
}

int findDevice(const char* addr) {
  for (int i = 0; i < MAX_DEVICES; i++)
    if (devices[i].used && strcmp(devices[i].addr, addr) == 0) return i;
  return -1;
}
int addDevice(const char* addr) {
  for (int i = 0; i < MAX_DEVICES; i++)
    if (!devices[i].used) {
      devices[i] = {};
      devices[i].used = true;
      strncpy(devices[i].addr, addr, 17);
      return i;
    }
  return -1;
}

// Called once per advertisement heard during a scan.
class FoundCallback : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice d) override {
    String a = d.getAddress().toString();
    int i = findDevice(a.c_str());
    if (i < 0) i = addDevice(a.c_str());
    if (i < 0) return;

    devices[i].rssi = d.getRSSI();
    devices[i].lastSeen = millis();
    devices[i].seen++;

    // Named address-type constants (BLE_ADDR_TYPE_PUBLIC / BLE_ADDR_PUBLIC,
    // and the various "random" subtypes) differ between the classic-ESP32
    // and C3/S3 BLE stacks and aren't even consistently preprocessor
    // macros vs. enum values, so naming-based detection is a dead end.
    // The address-type byte itself is defined by the Bluetooth Core Spec,
    // not by either vendor's stack: 0 = public, non-zero = some flavor of
    // random. Compare the raw value instead of chasing per-platform names.
    uint8_t t = d.getAddressType();
    devices[i].randomAddr = (t != 0);

    if (d.haveName())
      strncpy(devices[i].name, d.getName().c_str(), 23);

    if (d.haveManufacturerData()) {
      String md = d.getManufacturerData();
      if (md.length() >= 2) {
        uint16_t id = (uint8_t)md[0] | ((uint8_t)md[1] << 8);  // little-endian
        const char* v = makerName(id);
        if (v[0]) strncpy(devices[i].maker, v, 15);
        else      snprintf(devices[i].maker, 15, "0x%04X", id);
      }
    }
  }
};

void printTable() {
  uint32_t now = millis();

  int idx[MAX_DEVICES];
  int n = 0;
  for (int i = 0; i < MAX_DEVICES; i++) {
    if (!devices[i].used) continue;
    if (now - devices[i].lastSeen > FORGET_AFTER) { devices[i].used = false; continue; }
    idx[n++] = i;
  }

  // Sort strongest signal first.
  for (int a = 0; a < n; a++)
    for (int b = a + 1; b < n; b++)
      if (devices[idx[b]].rssi > devices[idx[a]].rssi) {
        int t = idx[a]; idx[a] = idx[b]; idx[b] = t;
      }

  Serial.println("\n===== BLE devices nearby (informational only, not part of the verdict) =====");
  Serial.println("ADDRESS            RSSI  signal   priv  seen  maker      name");
  for (int k = 0; k < n; k++) {
    BleDev& d = devices[idx[k]];
    Serial.printf("%-17s  %4d  %-7s  %-4s  %4lu  %-9s  %s\n",
                  d.addr, d.rssi, signalStrength(d.rssi),
                  d.randomAddr ? "rnd" : "-",
                  d.seen,
                  d.maker[0] ? d.maker : "-",
                  d.name[0] ? d.name : "");
  }
  Serial.printf("---- %d BLE device(s) in range ----\n", n);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nBLE presence panel starting (supplementary -- see file header)...");

  BLEDevice::init("");
  scanner = BLEDevice::getScan();
  scanner->setAdvertisedDeviceCallbacks(new FoundCallback());
  scanner->setActiveScan(true);   // ask devices for names (uses a bit more power)
  scanner->setInterval(100);
  scanner->setWindow(99);
}

void loop() {
  scanner->start(SCAN_SECONDS, false);
  scanner->clearResults();        // free the library's buffer; our table persists
  printTable();
  delay(200);
}
