/*
 * WashroomSweep - ESP32 promiscuous WiFi sniffer
 *
 * Passive, header-only 802.11 sniffer. No decryption, no association.
 * For each source MAC on the local channel, accumulates uplink vs
 * downlink byte/packet counts and average RSSI over a rolling window,
 * then emits one CSV row per active MAC over serial for the host-side
 * (Python) correlation script.
 *
 * Board: any ESP32 variant (classic / C3 / S3) works unchanged, since
 * this only uses the common esp_wifi promiscuous API. Arduino IDE, no
 * ESP-IDF project needed.
 *
 * Direction logic: for a data frame, if addr2 (transmitter) equals the
 * AP's MAC, this is AP -> station traffic, i.e. downlink for whoever is
 * addr1 (the receiver). Otherwise addr2 is a station transmitting, i.e.
 * uplink for addr2. The AP's MAC is learned automatically from the
 * first beacon frame seen (addr2 of a management/beacon frame is the
 * BSSID), or can be set manually with the "AP <mac>" serial command if
 * multiple networks are in range and the wrong one gets locked.
 *
 * Serial commands (type text + Enter over the Arduino serial monitor):
 *   MARK        - emit a MARK event now; host uses this as the t=0
 *                 instant for the light-toggle stimulus
 *   CH <n>      - stop hopping and lock to channel n (1-13)
 *   HOP         - toggle channel hopping (cycles 1/6/11) vs channel lock
 *   AP <mac>    - manually set/override the AP MAC, e.g. AP aa:bb:cc:dd:ee:ff
 *
 * Serial output, CSV, one header row at boot then one row per active
 * MAC per REPORT_INTERVAL_MS:
 *   ts_ms,mac,up_bytes,down_bytes,up_pkts,down_pkts,rssi_avg
 * Two special row shapes appear in the same stream:
 *   MARK,ts_ms                  - stimulus mark, host aligns on this
 *   #comment text...            - informational only, host should skip
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
static volatile bool apLocked = false;

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
  if (type == WIFI_PKT_MISC) return;

  wifi_promiscuous_pkt_t *pkt = (wifi_promiscuous_pkt_t *)buf;
  const uint8_t *payload = pkt->payload;
  int len = pkt->rx_ctrl.sig_len;
  if (len < 16) return; // shorter than addr1+addr2, not safe to parse

  if (type == WIFI_PKT_MGMT) {
    uint8_t subtype = (payload[0] >> 4) & 0x0F;
    if (subtype == 8 && !apLocked) { // beacon: addr2 is the BSSID
      memcpy(apMac, payload + 10, 6);
      apLocked = true;
    }
    return;
  }

  if (type != WIFI_PKT_DATA) return;

  const uint8_t *addr1 = payload + 4;
  const uint8_t *addr2 = payload + 10;
  int32_t rssi = pkt->rx_ctrl.rssi;
  uint32_t now = millis();

  portENTER_CRITICAL(&tableMux);
  if (apLocked && macEquals(addr2, apMac)) {
    // AP -> station: downlink, attribute to the receiving station (addr1)
    int slot = findOrAllocDevice(addr1, now);
    devices[slot].downBytes += len;
    devices[slot].downPkts += 1;
    devices[slot].rssiSum += rssi;
    devices[slot].rssiCount += 1;
    devices[slot].lastSeenMs = now;
  } else {
    // station -> anything: uplink, attribute to the transmitting station (addr2)
    int slot = findOrAllocDevice(addr2, now);
    devices[slot].upBytes += len;
    devices[slot].upPkts += 1;
    devices[slot].rssiSum += rssi;
    devices[slot].rssiCount += 1;
    devices[slot].lastSeenMs = now;
  }
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
    int v[6];
    if (sscanf(macStr.c_str(), "%x:%x:%x:%x:%x:%x",
               &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) == 6) {
      for (int i = 0; i < 6; i++) apMac[i] = (uint8_t)v[i];
      apLocked = true;
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
    portENTER_CRITICAL(&tableMux);
    for (int i = 0; i < MAX_DEVICES; i++) {
      if (!devices[i].inUse) continue;

      if (devices[i].upBytes == 0 && devices[i].downBytes == 0) {
        // silent for a while: free the slot so a long sweep doesn't fill the table
        if (now - devices[i].lastSeenMs > STALE_TIMEOUT_MS) devices[i].inUse = false;
        continue;
      }

      float rssiAvg = devices[i].rssiCount > 0
        ? (float)devices[i].rssiSum / devices[i].rssiCount
        : 0.0f;

      Serial.print(now);
      Serial.print(',');
      printMacHex(devices[i].mac);
      Serial.print(',');
      Serial.print(devices[i].upBytes);
      Serial.print(',');
      Serial.print(devices[i].downBytes);
      Serial.print(',');
      Serial.print(devices[i].upPkts);
      Serial.print(',');
      Serial.print(devices[i].downPkts);
      Serial.print(',');
      Serial.println(rssiAvg, 1);

      devices[i].upBytes = 0;
      devices[i].downBytes = 0;
      devices[i].upPkts = 0;
      devices[i].downPkts = 0;
      devices[i].rssiSum = 0;
      devices[i].rssiCount = 0;
    }
    portEXIT_CRITICAL(&tableMux);
    lastReportMs = now;
  }
}
