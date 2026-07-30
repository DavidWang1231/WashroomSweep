#!/usr/bin/env python3
"""
WashroomSweep live dashboard.

Reads the sniffer board over serial (and optionally a second board running
the BLE presence panel), keeps a rolling picture of every device on the
air, and serves it as a local web page that updates ~4x a second.

    python3 dashboard.py --wifi /dev/cu.usbserial-0001 [--ble /dev/cu.usbserial-0002]
    then open http://localhost:8000

Camera flagging here uses the asymmetry signal only -- sustained one-way
uplink well above everything else on the air. That is the signal we
closed over RF. The light-stimulus correlation is a separate refinement
(see sweep.py); this panel deliberately says CANDIDATE, never "camera
found", and never says a space is safe.

The BLE panel is presence-only and never contributes to the verdict: BLE
throughput is orders of magnitude below live video, so a streaming
camera's payload never appears there.
"""

import re
import csv
import sys
import json
import time
import argparse
import threading
import http.server
import socketserver
from collections import deque

import serial  # pyserial

# --- camera heuristics (asymmetry only; see module docstring) -------------
CAM_MIN_RATE_BPS = 60_000      # ~60 KB/s sustained uplink; video-scale
CAM_MIN_RATIO = 4.0            # uplink/downlink; a camera is one-way
RATE_WINDOW_S = 4.0            # rolling window the live rate is measured over
IDLE_DROP_S = 20.0             # forget a device unseen this long

STATE = {
    "wifi": {},        # mac -> device dict
    "ble": {},         # addr -> ble dict
    "windows": deque(maxlen=200),
    "channel": None,
    "started": time.time(),
    "board_ok": False,
    "ble_ok": False,
}
LOCK = threading.Lock()


def _now():
    return time.time()


def wifi_reader(port, baud, log_writer):
    """Consume the sniffer CSV: WINDOW heartbeats plus per-device rows."""
    try:
        ser = serial.Serial(port, baud, timeout=1)
    except Exception as e:
        print(f"[wifi] cannot open {port}: {e}")
        return
    time.sleep(2.0)
    ser.reset_input_buffer()
    while True:
        try:
            line = ser.readline().replace(b"\x00", b"").decode("utf-8", "ignore").strip()
        except Exception:
            continue
        if not line:
            continue

        if line.startswith("#"):
            m = re.search(r"#CHANNEL_LOCKED,(\d+)", line)
            if m:
                with LOCK:
                    STATE["channel"] = int(m.group(1))
            continue

        if line.startswith("WINDOW,"):
            with LOCK:
                STATE["windows"].append(_now())
                STATE["board_ok"] = True
            continue

        if line.startswith("ts_ms,"):
            continue

        parts = line.split(",")
        if len(parts) != 7:
            continue
        try:
            mac = parts[1]
            up, down = int(parts[2]), int(parts[3])
            rssi = float(parts[6])
        except ValueError:
            continue

        t = _now()
        with LOCK:
            d = STATE["wifi"].setdefault(mac, {
                "mac": mac, "up": 0, "down": 0, "rssi": rssi,
                "first": t, "last": t, "hist": deque(),
            })
            d["up"] += up
            d["down"] += down
            d["rssi"] = rssi
            d["last"] = t
            d["hist"].append((t, up))
            while d["hist"] and t - d["hist"][0][0] > RATE_WINDOW_S:
                d["hist"].popleft()
        if log_writer:
            log_writer.writerow(parts)


BLE_ROW = re.compile(
    r"^([0-9A-Fa-f:]{17})\s+(-?\d+)\s+(\S+)\s+(\S+)\s+(\d+)\s*(\S*)\s*(.*)$")


def ble_reader(port, baud):
    """Parse the BLE presence panel's text table (no reflash needed)."""
    try:
        ser = serial.Serial(port, baud, timeout=1)
    except Exception as e:
        print(f"[ble] cannot open {port}: {e}")
        return
    time.sleep(2.0)
    ser.reset_input_buffer()
    while True:
        try:
            line = ser.readline().replace(b"\x00", b"").decode("utf-8", "ignore").strip()
        except Exception:
            continue
        if not line:
            continue
        m = BLE_ROW.match(line)
        if not m:
            continue
        addr, rssi, strength, priv, seen, maker, name = m.groups()
        with LOCK:
            STATE["ble_ok"] = True
            STATE["ble"][addr] = {
                "addr": addr, "rssi": int(rssi), "strength": strength,
                "priv": priv == "rnd", "seen": int(seen),
                "maker": maker if maker != "-" else "",
                "name": name.strip(), "last": _now(),
            }


def snapshot():
    """Build the JSON the page polls: live rates, ranking, candidates."""
    t = _now()
    with LOCK:
        for mac in [m for m, d in STATE["wifi"].items() if t - d["last"] > IDLE_DROP_S]:
            del STATE["wifi"][mac]
        for a in [a for a, d in STATE["ble"].items() if t - d["last"] > 60]:
            del STATE["ble"][a]

        devices = []
        for d in STATE["wifi"].values():
            span = max(RATE_WINDOW_S, 0.001)
            rate = sum(b for _, b in d["hist"]) / span
            ratio = (d["up"] / d["down"]) if d["down"] else float("inf")
            candidate = rate >= CAM_MIN_RATE_BPS and ratio >= CAM_MIN_RATIO
            devices.append({
                "mac": d["mac"],
                "up": d["up"], "down": d["down"],
                "rate": rate,
                "ratio": None if ratio == float("inf") else round(ratio, 1),
                "rssi": round(d["rssi"]),
                "age": round(t - d["last"], 1),
                "candidate": candidate,
            })
        devices.sort(key=lambda x: -x["rate"])

        recent = [w for w in STATE["windows"] if t - w < 3.0]
        live = len(recent) >= 5           # ~15 expected in 3s at 200ms

        ble = sorted(STATE["ble"].values(), key=lambda x: -x["rssi"])
        return {
            "devices": devices[:40],
            "ble": ble[:20],
            "ble_active": STATE["ble_ok"],
            "channel": STATE["channel"],
            "live": live,
            "n_devices": len(devices),
            "candidates": [d for d in devices if d["candidate"]],
            "uptime": round(t - STATE["started"]),
        }


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>WashroomSweep</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0b0f14;color:#dbe4ee;
  font:15px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
header{padding:14px 20px;border-bottom:1px solid #1e2936;display:flex;
  align-items:center;gap:18px;flex-wrap:wrap}
h1{font-size:17px;margin:0;letter-spacing:.5px;font-weight:600}
.pill{font-size:12px;padding:3px 10px;border-radius:99px;border:1px solid #2b3a4d;color:#8fa3b8}
.ok{color:#3ddc97;border-color:#1c5c43}
.bad{color:#ff6b6b;border-color:#6b2020}
#alert{margin:0;padding:0;max-height:0;overflow:hidden;transition:max-height .25s}
#alert.on{max-height:220px}
.banner{background:#3a0f14;border-bottom:2px solid #ff4d4f;padding:16px 20px}
.banner h2{margin:0 0 6px;font-size:22px;color:#ff8080;letter-spacing:1px}
.banner .mac{font-size:26px;color:#fff;font-weight:700}
.banner .why{color:#e2a0a0;font-size:13px;margin-top:6px}
main{display:grid;grid-template-columns:1fr 340px;gap:0;align-items:start}
@media(max-width:900px){main{grid-template-columns:1fr}}
section{padding:16px 20px}
aside{padding:16px 20px;border-left:1px solid #1e2936;min-height:60vh}
h3{font-size:12px;text-transform:uppercase;letter-spacing:1.4px;color:#6f8399;
  margin:0 0 10px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:right;color:#6f8399;font-weight:500;padding:6px 8px;
  border-bottom:1px solid #1e2936;font-size:11px;text-transform:uppercase;letter-spacing:.8px}
th:first-child,td:first-child{text-align:left}
td{padding:7px 8px;border-bottom:1px solid #141c25;text-align:right}
tr.cand td{background:#2a0f14;color:#ffb3b3;font-weight:600}
tr.cand td:first-child{box-shadow:inset 3px 0 #ff4d4f}
.bar{height:4px;background:#1b6ef3;border-radius:2px;min-width:2px}
tr.cand .bar{background:#ff4d4f}
.note{color:#5d7186;font-size:11.5px;margin-top:10px;line-height:1.6}
.big{font-size:30px;font-weight:700;color:#fff}
.stat{display:inline-block;margin-right:26px}
.stat span{display:block;font-size:11px;color:#6f8399;text-transform:uppercase;letter-spacing:1px}
</style></head><body>
<header>
  <h1>WashroomSweep</h1>
  <span class="pill" id="live">connecting</span>
  <span class="pill" id="ch">channel –</span>
  <span class="pill" id="cnt">0 devices</span>
</header>
<div id="alert"><div class="banner">
  <h2>⚠ CANDIDATE DETECTED</h2>
  <div class="mac" id="cmac"></div>
  <div class="why" id="cwhy"></div>
</div></div>
<main>
  <section>
    <h3>Devices on air</h3>
    <table><thead><tr><th>MAC</th><th>uplink</th><th>up/down</th>
      <th>total up</th><th>total down</th><th>rssi</th><th></th></tr></thead>
      <tbody id="rows"></tbody></table>
    <div class="note">Passive 802.11 header capture — nothing is joined or decrypted.
      A device is flagged when it sustains one-way uplink at video scale.
      This panel never reports a space as safe: local-storage and wired cameras
      emit nothing, and 5&nbsp;GHz / 802.11ax links are outside this radio.</div>
  </section>
  <aside>
    <h3>Bluetooth presence</h3>
    <div id="blebox"></div>
    <div class="note">Informational only — never part of the verdict.
      BLE throughput is far below live video, so a streaming camera's payload
      never appears here.</div>
  </aside>
</main>
<script>
function kb(n){ if(n>=1048576) return (n/1048576).toFixed(1)+' MB';
  if(n>=1024) return (n/1024).toFixed(0)+' KB'; return n+' B'; }
function rate(n){ return n>=1024 ? (n/1024).toFixed(0)+' KB/s' : n.toFixed(0)+' B/s'; }
async function tick(){
  let d; try{ d = await (await fetch('/data')).json(); }catch(e){ return; }
  const live=document.getElementById('live');
  live.textContent = d.live ? 'board live' : 'no data';
  live.className = 'pill ' + (d.live?'ok':'bad');
  document.getElementById('ch').textContent = 'channel ' + (d.channel ?? '–');
  document.getElementById('cnt').textContent = d.n_devices + ' devices';

  const al=document.getElementById('alert');
  if(d.candidates.length){
    al.classList.add('on');
    const c=d.candidates[0];
    document.getElementById('cmac').textContent = c.mac;
    document.getElementById('cwhy').textContent =
      'sustained ' + rate(c.rate) + ' uplink, ' +
      (c.ratio===null ? 'no downlink at all' : c.ratio+'x more up than down') +
      ' — behaviour consistent with a camera streaming this space';
  } else al.classList.remove('on');

  const max=Math.max(1,...d.devices.map(x=>x.rate));
  document.getElementById('rows').innerHTML = d.devices.map(x=>
    `<tr class="${x.candidate?'cand':''}"><td>${x.mac}</td>
     <td>${rate(x.rate)}</td><td>${x.ratio===null?'∞':x.ratio}</td>
     <td>${kb(x.up)}</td><td>${kb(x.down)}</td><td>${x.rssi}</td>
     <td style="width:120px"><div class="bar" style="width:${x.rate/max*100}%"></div></td></tr>`
  ).join('') || '<tr><td colspan="7" style="color:#5d7186">listening…</td></tr>';

  document.getElementById('blebox').innerHTML = !d.ble_active
    ? '<div class="note" style="margin:0">No BLE board connected.</div>'
    : (d.ble.map(b=>`<div style="padding:6px 0;border-bottom:1px solid #141c25">
        <div>${b.name||b.maker||'(unnamed)'} ${b.priv?'<span style="color:#5d7186">rnd</span>':''}</div>
        <div style="color:#6f8399;font-size:12px">${b.addr} · ${b.rssi} dBm · ${b.strength}</div>
       </div>`).join('') || '<div class="note" style="margin:0">scanning…</div>');
}
setInterval(tick,250); tick();
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/data"):
            body = json.dumps(snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wifi", required=True, help="serial port of the sniffer board")
    ap.add_argument("--ble", default=None, help="serial port of the BLE board (optional)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--http", type=int, default=8000)
    ap.add_argument("--log", default=None, help="write raw CSV rows here")
    args = ap.parse_args()

    lw = None
    if args.log:
        f = open(args.log, "w", newline="")
        lw = csv.writer(f)
        lw.writerow(["ts_ms", "mac", "up", "down", "up_pkts", "down_pkts", "rssi"])

    threading.Thread(target=wifi_reader, args=(args.wifi, args.baud, lw), daemon=True).start()
    if args.ble:
        threading.Thread(target=ble_reader, args=(args.ble, args.baud), daemon=True).start()

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", args.http), Handler) as httpd:
        print(f"\n  WashroomSweep dashboard  ->  http://localhost:{args.http}\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
