/*
 * WashroomSweep - SoftAP demo mode
 *
 * The ESP32 radio is 802.11b/g/n only. A modern phone hotspot negotiates
 * 802.11ax, whose data frames this hardware cannot demodulate (measured:
 * ~0.03% of bytes recovered). By hosting the AP ourselves we force every
 * client onto a PHY we can actually see.
 *
 * The board plays three roles at once:
 *   1. SoftAP on a fixed channel -- the "room's WiFi"
 *   2. HTTP client that continuously drains the camera phone's MJPEG
 *      stream and discards it, so the phone keeps uploading without
 *      needing a separate viewer device
 *   3. Promiscuous sniffer emitting the same CSV the production firmware
 *      does, so host/sweep.py consumes it unchanged
 *
 * Serial commands:
 *   PULL <ip> <port> <path>   start draining a stream, e.g.
 *                             PULL 192.168.4.2 8080 /video
 *   STOP                      stop draining
 *   MARK                      stimulus mark for the host
 */
#include <WiFi.h>
#include "esp_wifi.h"

#define AP_SSID "WashroomSweep-Test"
#define AP_PASS "sweep12345"
#define AP_CH   6
#define REPORT_INTERVAL_MS 200
#define MAX_DEVICES 32

struct D { uint8_t mac[6]; bool used; uint32_t up,down; uint16_t upn,downn;
           int32_t rssiSum; uint16_t rssiN; uint32_t last; };
static D devs[MAX_DEVICES];
static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
static uint32_t lastReport = 0;

static WiFiClient pull;
static String pullIp = ""; static int pullPort = 0; static String pullPath = "";
static bool pulling = false;
static String pullAuth = "YWRtaW46MTIzMQ==";  // base64 "admin:1231" -- the demo app's login
// Autodiscovery: with no laptop in the loop, walk the DHCP pool looking for a
// camera app answering on the usual port. The dashboard holds the serial port
// during a demo, so nobody can type a PULL command while it runs.
static bool autoFind = true;
static int  probeHost = 2;          // 192.168.4.<probeHost>
static uint32_t lastProbe = 0;
static uint32_t pulledBytes = 0;

static int slotFor(const uint8_t* m, uint32_t now){
  int free=-1, old=0; uint32_t oldest=0xFFFFFFFF;
  for(int i=0;i<MAX_DEVICES;i++){
    if(devs[i].used && !memcmp(devs[i].mac,m,6)) return i;
    if(!devs[i].used && free<0) free=i;
    if(devs[i].last<oldest){oldest=devs[i].last;old=i;}
  }
  int s = free>=0?free:old;
  memcpy(devs[s].mac,m,6); devs[s].used=true;
  devs[s].up=devs[s].down=0; devs[s].upn=devs[s].downn=0;
  devs[s].rssiSum=0; devs[s].rssiN=0; devs[s].last=now;
  return s;
}

static void cb(void* buf, wifi_promiscuous_pkt_type_t type){
  if(type!=WIFI_PKT_DATA) return;
  wifi_promiscuous_pkt_t* p=(wifi_promiscuous_pkt_t*)buf;
  const uint8_t* pl=p->payload; int len=p->rx_ctrl.sig_len;
  if(len<24) return;
  bool toDS=pl[1]&0x01, fromDS=pl[1]&0x02;
  const uint8_t* client; bool isUp;
  if(toDS&&!fromDS){ client=pl+10; isUp=true; }
  else if(!toDS&&fromDS){ client=pl+4; isUp=false; }
  else return;
  if(client[0]&0x01) return;
  uint32_t now=millis();
  portENTER_CRITICAL(&mux);
  int s=slotFor(client,now);
  if(isUp){devs[s].up+=len; devs[s].upn++;} else {devs[s].down+=len; devs[s].downn++;}
  devs[s].rssiSum+=p->rx_ctrl.rssi; devs[s].rssiN++; devs[s].last=now;
  portEXIT_CRITICAL(&mux);
}

static void startPull(){
  if(pullIp.length()==0) return;
  pull.stop();          // fully close any prior socket
  delay(150);           // let the app's single-slot server free it
  pull.setTimeout(3);
  if(pull.connect(pullIp.c_str(), pullPort)){
    pull.printf("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: keep-alive\r\n",
                pullPath.c_str(), pullIp.c_str());
    if(pullAuth.length()) pull.printf("Authorization: Basic %s\r\n", pullAuth.c_str());
    pull.print("\r\n");
    pulling=true;
    // log the first response bytes once, so a 401/404/redirect is visible
    uint32_t t0=millis(); String hdr;
    while(millis()-t0<1500 && hdr.length()<200){
      while(pull.available() && hdr.length()<200) hdr += (char)pull.read();
      if(hdr.indexOf("\r\n\r\n")>=0) break;
      delay(10);
    }
    hdr.replace("\r","|"); hdr.replace("\n","|");
    Serial.printf("#PULL_CONNECTED,%s:%d%s RESP[%s]\n", pullIp.c_str(), pullPort,
                  pullPath.c_str(), hdr.c_str());
  } else {
    Serial.printf("#PULL_FAILED,%s:%d\n", pullIp.c_str(), pullPort);
  }
}

static void handleLine(String l){
  l.trim(); if(!l.length()) return;
  if(l.equalsIgnoreCase("MARK")){ Serial.printf("MARK,%lu\n", millis()); }
  else if(l.equalsIgnoreCase("STOP")){ pulling=false; autoFind=false; pull.stop(); Serial.println("#PULL_STOPPED"); }
  else if(l.equalsIgnoreCase("AUTO")){ autoFind=!autoFind; Serial.printf("#AUTOFIND,%s\n", autoFind?"on":"off"); }
  else if(l.startsWith("AUTH ")||l.startsWith("auth ")){
    pullAuth = l.substring(5); pullAuth.trim();
    Serial.printf("#AUTH_SET,%s\n", pullAuth.c_str());
  }
  else if(l.startsWith("PULL ")||l.startsWith("pull ")){
    int a=l.indexOf(' '), b=l.indexOf(' ',a+1), c=l.indexOf(' ',b+1);
    if(a>0&&b>a&&c>b){
      pullIp=l.substring(a+1,b); pullPort=l.substring(b+1,c).toInt(); pullPath=l.substring(c+1);
      pullIp.trim(); pullPath.trim();
      autoFind=false;
      startPull();
    }
  }
}

void setup(){
  Serial.begin(115200); delay(300);
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS, AP_CH);
  delay(500);
  esp_wifi_set_promiscuous(true);
  wifi_promiscuous_filter_t f={};
  f.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA;
  esp_wifi_set_promiscuous_filter(&f);
  esp_wifi_set_promiscuous_rx_cb(&cb);
  for(int i=0;i<MAX_DEVICES;i++) devs[i].used=false;
  Serial.printf("#SoftAP,%s,%s,ch%d,%s\n", AP_SSID, AP_PASS, AP_CH,
                WiFi.softAPIP().toString().c_str());
  Serial.println("ts_ms,mac,up_bytes,down_bytes,up_pkts,down_pkts,rssi_avg");
  lastReport=millis();
}

void loop(){
  static String buf;
  while(Serial.available()){
    char ch=Serial.read();
    if(ch=='\n'||ch=='\r'){ if(buf.length()){handleLine(buf); buf="";} }
    else buf+=ch;
  }
  // With no client configured yet, probe the DHCP pool for a camera app.
  // One host every 1.5s, so a full pass over .2-.10 takes ~13s.
  if(autoFind && !pulling && WiFi.softAPgetStationNum() > 0
     && millis() - lastProbe > 1500){
    lastProbe = millis();
    pullIp   = String("192.168.4.") + probeHost;
    pullPort = 8081;
    pullPath = "/video";
    Serial.printf("#PROBE,%s\n", pullIp.c_str());
    startPull();
    if(!pull.connected()) pulling = false;      // nothing there; keep walking
    probeHost++;
    if(probeHost > 10) probeHost = 2;
  }

  // drain the camera stream so the phone keeps uploading
  if(pulling){
    static uint32_t lastTry=0;
    if(!pull.connected() && millis()-lastTry>2000){ lastTry=millis(); startPull(); }
    else { uint8_t tmp[512]; while(pull.available()){ int n=pull.read(tmp,sizeof(tmp)); if(n<=0)break; pulledBytes+=n; } }
  }
  uint32_t now=millis();
  if(now-lastReport < REPORT_INTERVAL_MS) return;
  lastReport=now;
  static D snap[MAX_DEVICES]; int n=0;
  portENTER_CRITICAL(&mux);
  for(int i=0;i<MAX_DEVICES;i++){
    if(!devs[i].used) continue;
    if(!devs[i].up && !devs[i].down){ if(now-devs[i].last>30000) devs[i].used=false; continue; }
    snap[n++]=devs[i];
    devs[i].up=devs[i].down=0; devs[i].upn=devs[i].downn=0; devs[i].rssiSum=0; devs[i].rssiN=0;
  }
  portEXIT_CRITICAL(&mux);
  Serial.printf("WINDOW,%lu,%d,%d,STA=%d,pulled=%lu\n", now, REPORT_INTERVAL_MS, n,
                WiFi.softAPgetStationNum(), pulledBytes);
  for(int i=0;i<n;i++){
    float r = snap[i].rssiN? (float)snap[i].rssiSum/snap[i].rssiN : 0.0f;
    Serial.printf("%lu,%02X:%02X:%02X:%02X:%02X:%02X,%lu,%lu,%u,%u,%.1f\n",
      now, snap[i].mac[0],snap[i].mac[1],snap[i].mac[2],snap[i].mac[3],snap[i].mac[4],snap[i].mac[5],
      snap[i].up, snap[i].down, snap[i].upn, snap[i].downn, r);
  }
}
