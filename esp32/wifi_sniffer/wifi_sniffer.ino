/*
 * WashroomSweep - ESP32 promiscuous WiFi sniffer
 *
 * Passive, header-only 802.11 sniffer. No decryption, no association.
 * For each client MAC on the local channel, accumulates uplink vs
 * downlink byte/packet counts and average RSSI over a rolling window,
 * then emits one CSV row per active client over serial for the
 * host-side (Python) correlation script.
 *
 * Board: any ESP32 variant (classic / C3 / S3) works unchanged, since
 * this only uses the common esp_wifi promiscuous API. Arduino IDE, no
 * ESP-IDF project needed.
 *
 * Direction logic uses the ToDS/FromDS bits in the frame control field,
 * which classify direction for ANY infrastructure BSS without needing
 * to know the AP's identity:
 *   ToDS=1, FromDS=0  ->  client -> AP   (uplink,   client = addr2)
 *   ToDS=0, FromDS=1  ->  AP -> client   (downlink, client = addr1)
 * Everything else (IBSS, WDS 4-address) is ignored, as are frames whose
 * client address is broadcast/multicast (group bit set).
 *
 * Optionally, the sweep can be restricted to one BSS with the "AP" serial
 * command: once set, only frames to/from that BSSID are counted. Use this
 * in a controlled test (known hotspot) to suppress ambient traffic from
 * neighboring networks. Leave it unset for a real sweep -- a hidden
 * camera is on an AP you do NOT know, so filtering would blind you.
 * There is deliberately no automatic AP learning: in a dense environment
 * the first beacon heard is almost never the network you care about.
 *
 * Serial commands (type text + Enter over the serial monitor):
 *   MARK        - emit a MARK event now; host uses this as the t=0
 *                 instant for the light-toggle stimulus
 *   CH <n>      - stop hopping and lock to channel n (1-13)
 *   HOP         - toggle channel hopping (cycles 1/6/11) vs channel lock
 *   AP <mac>    - restrict counting to this BSSID, e.g. AP aa:bb:cc:dd:ee:ff
 *   AP OFF      - clear the BSSID restriction (count every BSS)
 *
 * Serial output, one header row at boot, then per REPORT_INTERVAL_MS:
 *   WINDOW,<ts_ms>,<interval_ms>,<active_devices>   heartbeat, ALWAYS
 *                 emitted every window even with zero traffic, so the
 *                 host can tell "quiet room" from "dead link / wrong
 *                 channel" and refuse to report a verdict on a dead feed
 *   ts_ms,mac,up_bytes,down_bytes,up_pkts,down_pkts,rssi_avg
 *                 one row per client that had traffic this window
 * Special rows in the same stream:
 *   MARK,ts_ms                  - stimulus mark, host aligns on this
 *   #comment text...            - informational only, host skips
 *                                  any line starting with '#'
 */

#include <WiFi.h>
#include "esp_wifi.h"

#define REPORT_INTERVAL_MS 200
#define MAX_DEVICES 32
#define STALE_TIMEOUT_MS 30000UL

struct DeviceStats {
  uint8_t mac[6];
  bool inUse;
  uint32_t upBytes, downBytes;
  uint16_t upPkts, downPkts;
  int32_t rssiSum;
  uint16_t rssiCount;
  uint32_t lastSeenMs;
};

static DeviceStats devices[MAX_DEVICES];
static portMUX_TYPE tableMux = portMUX_INITIALIZER_UNLOCKED;

static uint8_t apMac[6] = {0, 0, 0, 0, 0, 0};
static volatile bool apFilterOn = false;

static bool hopping = false;
static uint8_t currentChannel = 6;
static uint32_t lastHopMs = 0;
static const uint8_t hopChannels[3] = {1, 6, 11};
static uint8_t hopIdx = 0;

static uint32_t lastReportMs = 0;

static bool macEquals(const uint8_t *a, const uint8_t *b) {
  return memcmp(a, b, 6) == 0;
}

// Must be called with tableMux held.
static int findOrAllocDevice(const uint8_t *mac, uint32_t now) {
  int freeSlot = -1;
  int oldestSlot = 0;
  uint32_t oldestTime = 0xFFFFFFFFUL;
  for (int i = 0; i < MAX_DEVICES; i++) {
    if (devices[i].inUse && macEquals(devices[i].mac, mac)) {
      return i;
    }
    if (!devices[i].inUse && freeSlot < 0) {
      freeSlot = i;
    }
    if (devices[i].lastSeenMs < oldestTime) {
      oldestTime = devices[i].lastSeenMs;
      oldestSlot = i;
    }
  }
  int slot = (freeSlot >= 0) ? freeSlot : oldestSlot; // evict LRU if table is full
  memcpy(devices[slot].mac, mac, 6);
  devices[slot].inUse = true;
  devices[slot].upBytes = devices[slot].downBytes = 0;
  devices[slot].upPkts = devices[slot].downPkts = 0;
  devices[slot].rssiSum = 0;
  devices[slot].rssiCount = 0;
  devices[slot].lastSeenMs = now;
  return slot;
}

static void promiscuousCallback(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_DATA) return;

  wifi_promiscuous_pkt_t *pkt = (wifi_promiscuous_pkt_t *)buf;
  const uint8_t *payload = pkt->payload;
  int len = pkt->rx_ctrl.sig_len;
  if (len < 24) return; // shorter than a 3-address data header

  bool toDS   = payload[1] & 0x01;
  bool fromDS = payload[1] & 0x02;
  const uint8_t *addr1 = payload + 4;   // receiver
  const uint8_t *addr2 = payload + 10;  // transmitter

  const uint8_t *client;
  bool isUplink;
  if (toDS && !fromDS) {          // client -> AP; addr1 = BSSID
    if (apFilterOn && !macEquals(addr1, apMac)) return;
    client = addr2;
    isUplink = true;
  } else if (!toDS && fromDS) {   // AP -> client; addr2 = BSSID
    if (apFilterOn && !macEquals(addr2, apMac)) return;
    client = addr1;
    isUplink = false;
  } else {
    return; // IBSS or WDS frame, not a client<->AP exchange
  }
  if (client[0] & 0x01) return;   // broadcast/multicast, not a real client

  int32_t rssi = pkt->rx_ctrl.rssi;
  uint32_t now = millis();

  portENTER_CRITICAL(&tableMux);
  int slot = findOrAllocDevice(client, now);
  if (isUplink) {
    devices[slot].upBytes += len;
    devices[slot].upPkts += 1;
  } else {
    devices[slot].downBytes += len;
    devices[slot].downPkts += 1;
  }
  devices[slot].rssiSum += rssi;
  devices[slot].rssiCount += 1;
  devices[slot].lastSeenMs = now;
  portEXIT_CRITICAL(&tableMux);
}

static void setChannel(uint8_t ch) {
  currentChannel = ch;
  esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
}

static void printMacHex(const uint8_t *mac) {
  char buf[18];
  snprintf(buf, sizeof(buf), "%02X:%02X:%02X:%02X:%02X:%02X",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  Serial.print(buf);
}

static void handleSerialLine(String line) {
  line.trim();
  if (line.length() == 0) return;

  if (line.equalsIgnoreCase("MARK") || line.equalsIgnoreCase("m")) {
    Serial.print("MARK,");
    Serial.println(millis());
  } else if (line.equalsIgnoreCase("HOP")) {
    hopping = !hopping;
    Serial.print("#HOP_MODE,");
    Serial.println(hopping ? "on" : "off");
  } else if (line.startsWith("CH ") || line.startsWith("ch ")) {
    int ch = line.substring(3).toInt();
    if (ch >= 1 && ch <= 13) {
      hopping = false;
      setChannel(ch);
      Serial.print("#CHANNEL_LOCKED,");
      Serial.println(ch);
    }
  } else if (line.startsWith("AP ") || line.startsWith("ap ")) {
    String macStr = line.substring(3);
    macStr.trim();
    if (macStr.equalsIgnoreCase("OFF")) {
      apFilterOn = false;
      Serial.println("#AP_FILTER,off");
      return;
    }
    int v[6];
    if (sscanf(macStr.c_str(), "%x:%x:%x:%x:%x:%x",
               &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) == 6) {
      for (int i = 0; i < 6; i++) apMac[i] = (uint8_t)v[i];
      apFilterOn = true;
      Serial.print("#AP_LOCKED,");
      printMacHex(apMac);
      Serial.print(",");
      Serial.println(millis());
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);

  WiFi.mode(WIFI_MODE_STA);
  WiFi.disconnect();
  esp_wifi_set_promiscuous(true);
  // Restrict to management + data frames. Critically, this EXCLUDES
  // FCS-failed frames: near a modern high-rate AP the radio produces a
  // steady stream of mis-decoded garbage whose header bytes (and hence
  // "MAC addresses") are effectively random. Those junk MACs flood the
  // device table, evict real stations via LRU, and zero their counters
  // before the next report -- measured at ~99% loss of real traffic.
  wifi_promiscuous_filter_t filter = {};
  filter.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA;
  esp_wifi_set_promiscuous_filter(&filter);
  esp_wifi_set_promiscuous_rx_cb(&promiscuousCallback);
  setChannel(currentChannel);

  for (int i = 0; i < MAX_DEVICES; i++) devices[i].inUse = false;

  Serial.println("#WashroomSweep sniffer boot");
  Serial.print("#channel_lock,");
  Serial.println(currentChannel);
  Serial.println("ts_ms,mac,up_bytes,down_bytes,up_pkts,down_pkts,rssi_avg");

  lastReportMs = millis();
  lastHopMs = millis();
}

void loop() {
  uint32_t now = millis();

  static String lineBuf;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineBuf.length() > 0) {
        handleSerialLine(lineBuf);
        lineBuf = "";
      }
    } else {
      lineBuf += c;
    }
  }

  if (hopping && (now - lastHopMs >= 300)) {
    hopIdx = (hopIdx + 1) % 3;
    setChannel(hopChannels[hopIdx]);
    lastHopMs = now;
  }

  if (now - lastReportMs >= REPORT_INTERVAL_MS) {
    // Snapshot-and-reset under the lock, print outside it. Serial.print
    // at 115200 baud takes milliseconds per row; holding an
    // interrupt-masking spinlock that long would stall the promiscuous
    // callback (dropped frames = self-inflicted rate noise) and can even
    // trip the watchdog if the UART FIFO backs up.
    static DeviceStats snapshot[MAX_DEVICES];
    int nActive = 0;

    portENTER_CRITICAL(&tableMux);
    for (int i = 0; i < MAX_DEVICES; i++) {
      if (!devices[i].inUse) continue;

      if (devices[i].upBytes == 0 && devices[i].downBytes == 0) {
        // silent for a while: free the slot so a long sweep doesn't fill the table
        if (now - devices[i].lastSeenMs > STALE_TIMEOUT_MS) devices[i].inUse = false;
        continue;
      }

      snapshot[nActive++] = devices[i];

      devices[i].upBytes = 0;
      devices[i].downBytes = 0;
      devices[i].upPkts = 0;
      devices[i].downPkts = 0;
      devices[i].rssiSum = 0;
      devices[i].rssiCount = 0;
    }
    portEXIT_CRITICAL(&tableMux);

    // Heartbeat first, every window, even when nActive == 0: this is how
    // the host distinguishes a genuinely quiet room from a dead link.
    Serial.print("WINDOW,");
    Serial.print(now);
    Serial.print(',');
    Serial.print(REPORT_INTERVAL_MS);
    Serial.print(',');
    Serial.println(nActive);

    for (int i = 0; i < nActive; i++) {
      float rssiAvg = snapshot[i].rssiCount > 0
        ? (float)snapshot[i].rssiSum / snapshot[i].rssiCount
        : 0.0f;

      Serial.print(now);
      Serial.print(',');
      printMacHex(snapshot[i].mac);
      Serial.print(',');
      Serial.print(snapshot[i].upBytes);
      Serial.print(',');
      Serial.print(snapshot[i].downBytes);
      Serial.print(',');
      Serial.print(snapshot[i].upPkts);
      Serial.print(',');
      Serial.print(snapshot[i].downPkts);
      Serial.print(',');
      Serial.println(rssiAvg, 1);
    }

    lastReportMs = now;
  }
}
