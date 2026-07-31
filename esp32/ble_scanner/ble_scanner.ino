// ============================================================
//  BLE Device Detector  -  Step 2: detect + classify
//  Flash this to BOARD 2 (the "Bluetooth" board).
//
//  Scans for Bluetooth Low Energy devices and, for each one,
//  shows: how close it is, its name (if broadcast), its maker
//  (from the advertised manufacturer ID), and whether its
//  address is randomized/private.
//
//  Columns mirror the WiFi scanner so the mental model matches:
//    RSSI / dist  -> where it is
//    name / maker -> what it is
//    priv         -> real address (lookup-able) vs randomized
//    seen         -> how many times we've spotted it (persistence)
// ============================================================

#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <stdarg.h>

// ===== NETWORK: the SoftAP created by softap_demo.ino =====
// Set WIFI_SSID to "" to run USB/serial only, with no networking at all.
// NOTE: Arduino IDE -> Tools -> Partition Scheme -> "Huge APP (3MB No OTA)"
// BLE + WiFi together is a big build and won't fit the default partition.
#define WIFI_SSID  "WashroomSweep-Test"   // the SoftAP board
#define WIFI_PASS  "sweep12345"
#define UNIT_ID    "ble-B"       // name this board (unique per board)
#define UDP_PORT   4210          // must match monitor.py --udp-port
#define RETRY_INTERVAL 15000     // ms between hotspot reconnect attempts
#define STATUS_LED 2             // onboard LED: fast blink = no hotspot,
                                 // brief blip = connected and reporting

#define SCAN_SECONDS 5       // length of each scan pass

// How much of the time the BLE radio is actually listening, out of 100.
// This is THE knob for detection quality:
//   99 = listen almost continuously. Best detection. BLE and WiFi share one
//        radio, so this leaves WiFi very little airtime.
//   50 = half and half. Noticeably fewer devices, slower to spot them.
// Start high; only lower it if the board starts rebooting with WiFi on.
#define SCAN_WINDOW  90
// Each entry is ~72 bytes of static RAM, which indirectly limits heap. Sized
// to the room: captures here showed 120+ advertisers, so a small table sits
// permanently full and evicts real devices to make room for ghosts.
#define MAX_DEVICES  100     // max devices remembered at once

// Phones rotate their BLE address every ~15 min, so the table fills with
// devices that no longer exist. Expiring quickly keeps the list to what is
// actually present now, which matters far more than remembering the past.
#define FORGET_AFTER 20000   // ms: drop a device if unseen this long
#define MIN_FREE_HEAP 35000  // below this we skip reporting rather than crash

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

WiFiUDP udp;
bool reporting = false;        // true if we're set up to send over WiFi
uint32_t lastConnectTry = 0;   // last hotspot reconnect attempt

// How many times a new device displaced an old one because the table was full.
// If this climbs fast, MAX_DEVICES is too small for this room and real devices
// are being pushed out -- that looks exactly like "detection got worse".
uint32_t evictions = 0;

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

const char* proximity(int rssi) {
  if (rssi > -50) return "IN-ROOM";
  if (rssi > -70) return "near";
  if (rssi > -82) return "wall";
  return "far";
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
      strncpy(devices[i].addr, addr, sizeof(devices[i].addr) - 1);
      devices[i].addr[sizeof(devices[i].addr) - 1] = '\0';
      return i;
    }

  // Table full. Phones rotate their addresses constantly, so the table fills
  // with ghosts; evict the one we haven't heard from in the longest time
  // rather than going blind to every new device.
  evictions++;
  int oldest = 0;
  for (int i = 1; i < MAX_DEVICES; i++)
    if (devices[i].lastSeen < devices[oldest].lastSeen) oldest = i;
  devices[oldest] = {};
  devices[oldest].used = true;
  strncpy(devices[oldest].addr, addr, sizeof(devices[oldest].addr) - 1);
  devices[oldest].addr[sizeof(devices[oldest].addr) - 1] = '\0';
  return oldest;
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

    uint8_t t = d.getAddressType();
    devices[i].randomAddr = (t == BLE_ADDR_TYPE_RANDOM || t == BLE_ADDR_TYPE_RPA_RANDOM);

    // Always leave room for the terminator -- strncpy won't add one if the
    // source is longer than the buffer.
    if (d.haveName()) {
      strncpy(devices[i].name, d.getName().c_str(), sizeof(devices[i].name) - 1);
      devices[i].name[sizeof(devices[i].name) - 1] = '\0';
    }

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

  // Sort strongest (closest) first.
  for (int a = 0; a < n; a++)
    for (int b = a + 1; b < n; b++)
      if (devices[idx[b]].rssi > devices[idx[a]].rssi) {
        int t = idx[a]; idx[a] = idx[b]; idx[b] = t;
      }

  Serial.println("\n============ BLE devices (closest first) ============");
  Serial.println("ADDRESS            RSSI  dist     priv  seen  maker      name");
  for (int k = 0; k < n; k++) {
    BleDev& d = devices[idx[k]];
    Serial.printf("%-17s  %4d  %-7s  %-4s  %4lu  %-9s  %s\n",
                  d.addr, d.rssi, proximity(d.rssi),
                  d.randomAddr ? "rnd" : "-",
                  d.seen,
                  d.maker[0] ? d.maker : "-",
                  d.name[0] ? d.name : "");
  }
  // Free heap is the number to watch: BLE and WiFi stacks together are tight,
  // and abort()/reboot crashes are usually memory running out. If this keeps
  // falling pass after pass, something is leaking.
  Serial.printf("---- %d/%d slots used | %lu evictions | free heap %lu ----\n",
                n, MAX_DEVICES, (unsigned long)evictions,
                (unsigned long)ESP.getFreeHeap());
  if (n >= MAX_DEVICES)
    Serial.println("!! table FULL -- real devices are being pushed out. "
                   "Raise MAX_DEVICES or lower FORGET_AFTER.");
}

// Device names come from the air, so quotes/backslashes must be escaped
// before they go into JSON.
void jsonEscape(const char* in, char* out, size_t cap) {
  size_t o = 0;
  for (size_t i = 0; in[i] && o + 2 < cap; i++) {
    unsigned char c = in[i];
    if (c == '"' || c == '\\') { out[o++] = '\\'; out[o++] = c; }
    else if (c >= 32 && c < 127) out[o++] = c;   // drop control/non-ASCII bytes
  }
  out[o] = 0;
}

// Append to a buffer SAFELY.
//
// snprintf() returns the length it WOULD have written, not what it did. Adding
// that straight onto len lets len run past the end of the buffer -- and then
// "cap - len" underflows (it's unsigned) into a huge value, so the next write
// scribbles all over memory. That was a real crash: Guru Meditation /
// LoadProhibited. This helper never advances len past the buffer, and reports
// false if the text didn't fit so the caller can flush and start a new packet.
static bool appendf(char* buf, size_t cap, int& len, const char* fmt, ...) {
  if (len < 0 || (size_t)len >= cap) return false;
  va_list ap;
  va_start(ap, fmt);
  int r = vsnprintf(buf + len, cap - len, fmt, ap);
  va_end(ap);
  if (r < 0 || (size_t)r >= cap - len) {
    buf[len] = '\0';   // roll back the partial write
    return false;
  }
  len += r;
  return true;
}

void sendReport() {
  if (!reporting) return;
  if (WiFi.status() != WL_CONNECTED) return;  // hotspotPoll() handles reconnects

  // Reporting is the FIRST thing to give up when memory gets tight. Crashing
  // loses the whole device table; skipping one report loses nothing important.
  if (ESP.getFreeHeap() < MIN_FREE_HEAP) {
    Serial.printf("!! heap low (%lu) -- skipping this report\n",
                  (unsigned long)ESP.getFreeHeap());
    return;
  }

  uint32_t now = millis();
  // static, not on the stack: 1.3 KB of locals in a task that also runs the
  // WiFi/BLE callbacks is asking for trouble.
  static char buf[1024];
  static char nameEsc[48], makerEsc[32];
  int i = 0;
  while (i < MAX_DEVICES) {
    int len = 0;
    buf[0] = '\0';
    appendf(buf, sizeof(buf), len,
            "{\"unit\":\"%s\",\"src\":\"ble\",\"devices\":[", UNIT_ID);

    int inChunk = 0;
    while (i < MAX_DEVICES && inChunk < 6) {
      BleDev& d = devices[i];
      if (!d.used || now - d.lastSeen > FORGET_AFTER) { i++; continue; }

      jsonEscape(d.name, nameEsc, sizeof(nameEsc));
      jsonEscape(d.maker, makerEsc, sizeof(makerEsc));
      if (!appendf(buf, sizeof(buf), len,
            "%s{\"mac\":\"%s\",\"rssi\":%d,\"dist\":\"%s\",\"priv\":%d,"
            "\"seen\":%lu,\"maker\":\"%s\",\"name\":\"%s\"}",
            inChunk ? "," : "", d.addr, d.rssi, proximity(d.rssi),
            d.randomAddr ? 1 : 0, d.seen, makerEsc, nameEsc)) {
        break;   // didn't fit: send what we have, retry this device next packet
      }
      inChunk++;
      i++;
    }

    if (inChunk == 0) break;
    if (!appendf(buf, sizeof(buf), len, "]}")) continue;  // no room to close

    // Broadcast, so you don't have to hardcode the laptop's IP address.
    udp.beginPacket(IPAddress(255, 255, 255, 255), UDP_PORT);
    udp.print(buf);
    udp.endPacket();
    delay(5);
  }
}

// ---------------------------------------------------------------------
//  Hotspot handling -- NON-BLOCKING.
//
//  Never sit in a wait loop here: every second spent waiting on WiFi is a
//  second we are not scanning for BLE devices. We kick off the connection
//  and then just check on it once per pass.
// ---------------------------------------------------------------------

void hotspotBegin() {
  if (strlen(WIFI_SSID) == 0) {
    Serial.println("No hotspot configured -- USB/serial only.");
    return;
  }
  Serial.printf("Joining hotspot \"%s\" in the background...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();   // cancel anything in flight ("cannot set config")
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  lastConnectTry = millis();
}

void hotspotPoll() {
  if (strlen(WIFI_SSID) == 0) return;
  bool up = (WiFi.status() == WL_CONNECTED);

  if (up && !reporting) {
    reporting = true;
    udp.begin(UDP_PORT);
    // Bringing up WiFi costs a big chunk of heap. Print it here: if this
    // number is small, expect trouble, and lower MAX_DEVICES.
    Serial.printf("Network connected. IP %s -- broadcasting to UDP port %d "
                  "(free heap %lu)\n",
                  WiFi.localIP().toString().c_str(), UDP_PORT,
                  (unsigned long)ESP.getFreeHeap());
  } else if (!up && reporting) {
    reporting = false;
    Serial.println("Hotspot lost -- scanning continues, will retry.");
  }

  // Retry on our own schedule, without ever blocking the scan loop.
  if (!up && millis() - lastConnectTry > RETRY_INTERVAL) {
    lastConnectTry = millis();
    Serial.println("Retrying hotspot...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nBLE Device Detector starting...");

  pinMode(STATUS_LED, OUTPUT);

  BLEDevice::init("");
  scanner = BLEDevice::getScan();
  scanner->setAdvertisedDeviceCallbacks(new FoundCallback());
  scanner->setActiveScan(true);   // ask devices for names (uses a bit more power)

  scanner->setInterval(100);
  scanner->setWindow(SCAN_WINDOW);
  Serial.printf("Scan duty cycle: %d/100\n", SCAN_WINDOW);

  // Start WiFi only after BLE has taken the memory it needs.
  hotspotBegin();
}

void loop() {
  scanner->start(SCAN_SECONDS, false);
  scanner->stop();                // make sure the scan is fully finished
  scanner->clearResults();        // free the library's buffer; our table persists
  printTable();
  sendReport();
  hotspotPoll();                  // returns immediately -- never blocks the scan

  // LED: fast blink = no hotspot, brief blip = connected and reporting.
  digitalWrite(STATUS_LED,
    ((reporting && WiFi.status() == WL_CONNECTED)
       ? (millis() % 2000) < 80
       : (millis() % 200) < 100) ? HIGH : LOW);

  delay(200);
}
