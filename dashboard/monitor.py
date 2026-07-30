#!/usr/bin/env python3
"""
Live dashboard for the washroom detector.

Two-board setup:

  BOARD 1  softap_demo.ino  (access point + sniffer)
           Hosts the WiFi network AND sniffs it. Talks CSV over USB serial:
               WINDOW,<ts_ms>,<interval_ms>,<active>
               <ts_ms>,<mac>,<up_bytes>,<down_bytes>,<up_pkts>,<down_pkts>,<rssi>
           His rows are per-window deltas (~200ms), so this script adds them up
           and derives a live bytes/sec rate.

  BOARD 2  ble_scanner.ino  (Bluetooth scanner)
           Joins his network and reports BLE devices over UDP (wireless).

Both feeds land in one dashboard.

USAGE
    python3 monitor.py               # normal: USB boards + wireless, both
    python3 monitor.py --no-serial   # skip USB, so you can reflash a board
    python3 monitor.py --port /dev/cu.usbserial-0001    # one specific board

    Then open http://localhost:8080  (opens automatically)

REQUIREMENTS
    pip3 install pyserial      (only needed for USB; wireless works without it)

NOTE
    Only one program can hold a serial port at a time -- so this and
    host/sweep.py cannot both read the sniffer board at once. Run one or the other. Same
    goes for uploading a sketch: stop this first, or start it --no-serial.
"""

import argparse
import json
import re
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# pyserial is only needed for USB mode, so don't hard-fail without it.
try:
    import serial
    from serial.tools import list_ports
    HAVE_SERIAL = True
except ImportError:
    HAVE_SERIAL = False

BAUD = 115200
STALE_AFTER = 60.0      # seconds without an update before we drop a device
HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- parsing

# WiFi row:  MAC  RSSI  dist  ch  kind  priv  frames  data
WIFI_ROW = re.compile(
    r"^([0-9A-Fa-f:]{17})\s+(-?\d+)\s+(\S+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s*$"
)
# BLE row:   ADDRESS  RSSI  dist  priv  seen  maker  [name]
BLE_ROW = re.compile(
    r"^([0-9A-Fa-f:]{17})\s+(-?\d+)\s+(\S+)\s+(\S+)\s+(\d+)\s+(\S+)\s*(.*)$"
)
# WiFi footer:  ---- 14 device(s) | 147 raw frames heard (now on ch 6) ----
WIFI_FOOTER = re.compile(r"(\d+)\s+device\(s\).*?(\d+)\s+raw frames")

# --- The sniffer/AP board speaks CSV over serial ---
#   heartbeat : WINDOW,<ts_ms>,<interval_ms>,<active_devices>
#   per device: <ts_ms>,<mac>,<up_bytes>,<down_bytes>,<up_pkts>,<down_pkts>,<rssi>
# Its rows are PER-WINDOW deltas (~200 ms), not running totals, so we add them
# up ourselves and also derive a live bytes/sec rate.
SNIFFER_ROW = re.compile(
    r"^(\d+),([0-9A-Fa-f:]{17}),(\d+),(\d+),(\d+),(\d+),(-?[\d.]+)\s*$"
)
SNIFFER_WINDOW = re.compile(r"^WINDOW,(\d+)")
SNIFFER_MARK = re.compile(r"^MARK,(\d+)")

RATE_WINDOW = 3.0   # seconds of history used for the bytes/sec figure

devices = {}        # key -> record shown in the UI
accum = {}          # key -> running totals + recent samples, for the sniffer's deltas
meta = {"wifi_frames": 0, "ports": {}, "windows": 0, "last_mark": None}
lock = threading.Lock()

# --- sweep state -----------------------------------------------------------
# Raw material for the correlation test. We keep a rolling buffer so a sweep
# can be started at any moment without missing the first rows.
sweep_rows = deque()      # (ts_ms, mac, up, down, up_pkts, down_pkts, rssi)
sweep_windows = deque()   # ts_ms of WINDOW heartbeats
SWEEP_BUFFER_S = 120

sweep = {"phase": "idle",      # idle | recording | analyzing | done | error
         "elapsed": 0.0, "duration": 30.0,
         "result": None, "error": None, "available": False}

writers = {}   # port -> open serial object, so we can send MARK to the board


def load_sweep_module():
    """Load the correlation math from host/sweep.py."""
    import importlib.util
    path = HERE.parent / "host" / "sweep.py"
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("sweep_analysis", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)   # safe: it's guarded by __main__
        return mod
    except Exception as e:
        print(f"NOTE: couldn't load {path} ({e}); sweep disabled.")
        return None


SWEEP = load_sweep_module()


def proximity(rssi):
    """Same buckets the boards use, for data that arrives as raw RSSI."""
    if rssi > -50:
        return "IN-ROOM"
    if rssi > -70:
        return "near"
    if rssi > -82:
        return "wall"
    return "far"


def upsert(key, fields):
    """Insert or update a device, tracking how its signal is trending."""
    now = time.time()
    with lock:
        rec = devices.get(key)
        if rec is None:
            rec = {"key": key, "first_seen": now, "trend": 0, "prev_rssi": fields["rssi"]}
            devices[key] = rec
        else:
            # Compare against the reading from ~2s ago to get a stable trend.
            if now - rec.get("trend_at", 0) > 2.0:
                delta = fields["rssi"] - rec["prev_rssi"]
                rec["trend"] = 1 if delta >= 4 else (-1 if delta <= -4 else 0)
                rec["prev_rssi"] = fields["rssi"]
                rec["trend_at"] = now
        rec.update(fields)
        rec["last_seen"] = now


def trim_sweep_buffers():
    """Keep only the recent past. Call with the lock held."""
    if not sweep_rows and not sweep_windows:
        return
    newest = max(sweep_rows[-1][0] if sweep_rows else 0,
                 sweep_windows[-1] if sweep_windows else 0)
    cutoff = newest - SWEEP_BUFFER_S * 1000
    while sweep_rows and sweep_rows[0][0] < cutoff:
        sweep_rows.popleft()
    while sweep_windows and sweep_windows[0] < cutoff:
        sweep_windows.popleft()


def handle_sniffer_row(mac, up, down, uppkts, downpkts, rssi):
    """One 200 ms window from the sniffer board: accumulate totals, compute rate."""
    key = "wifi:" + mac.upper()
    now = time.time()

    with lock:
        a = accum.setdefault(key, {"up": 0, "down": 0, "uppkts": 0,
                                   "downpkts": 0, "samples": deque()})
        a["up"] += up
        a["down"] += down
        a["uppkts"] += uppkts
        a["downpkts"] += downpkts

        s = a["samples"]
        s.append((now, up, down))
        while s and now - s[0][0] > RATE_WINDOW:
            s.popleft()
        span = (now - s[0][0]) if len(s) > 1 else RATE_WINDOW
        span = max(span, 0.5)
        up_rate = sum(x[1] for x in s) / span
        down_rate = sum(x[2] for x in s) / span
        totals = dict(a)

    rssi_i = int(round(rssi))
    upsert(key, {
        "source": "wifi",
        "mac": mac.upper(),
        "rssi": rssi_i,
        "dist": proximity(rssi_i),
        "channel": 0,               # his CSV doesn't carry the channel
        "kind": "device",
        # locally-administered bit => randomized/private MAC
        "priv": bool(int(mac.split(":")[0], 16) & 0x02),
        "frames": totals["uppkts"] + totals["downpkts"],
        "up": totals["up"],
        "down": totals["down"],
        "uppkts": totals["uppkts"],
        "downpkts": totals["downpkts"],
        "up_rate": int(up_rate),
        "down_rate": int(down_rate),
    })


def handle_line(line, state):
    """Feed one line of serial output through the parser."""
    line = line.rstrip()
    if not line:
        return

    # ---- Sniffer board: comma-separated, so it can't be confused with the
    # space-aligned tables our own sketches print. ----
    if line.startswith("#") or line.startswith("ts_ms,"):
        return                                  # his config echoes / CSV header
    m = SNIFFER_WINDOW.match(line)
    if m:
        with lock:
            meta["windows"] += 1
            meta["ports"][state.get("port", "serial")] = "connected (sniffer board)"
            sweep_windows.append(int(m.group(1)))
            trim_sweep_buffers()
        return
    m = SNIFFER_MARK.match(line)
    if m:
        with lock:
            meta["last_mark"] = int(m.group(1))
        return
    m = SNIFFER_ROW.match(line)
    if m:
        ts, mac, up, down, uppkts, downpkts, rssi = m.groups()
        row = (int(ts), mac, int(up), int(down),
               int(uppkts), int(downpkts), float(rssi))
        with lock:
            sweep_rows.append(row)
            trim_sweep_buffers()
        handle_sniffer_row(mac, int(up), int(down),
                         int(uppkts), int(downpkts), float(rssi))
        return

    # Table headers tell us which board / which format follows.
    if "WiFi devices" in line:
        state["mode"] = "wifi"
        return
    if "BLE devices" in line:
        state["mode"] = "ble"
        return
    if line.startswith(("MAC ", "ADDRESS", "====")):
        return
    if line.startswith("----"):
        m = WIFI_FOOTER.search(line)
        if m:
            with lock:
                meta["wifi_frames"] = int(m.group(2))
        return

    if state["mode"] == "wifi":
        m = WIFI_ROW.match(line)
        if m:
            mac, rssi, dist, ch, kind, priv, up, down = m.groups()
            upsert("wifi:" + mac.upper(), {
                "source": "wifi",
                "mac": mac.upper(),
                "rssi": int(rssi),
                "dist": dist,
                "channel": int(ch),
                "kind": kind,
                "priv": priv == "rnd",
                "frames": 0,          # serial table doesn't carry a frame count
                "up": int(up),
                "down": int(down),
                "uppkts": 0,
                "downpkts": 0,
            })
    elif state["mode"] == "ble":
        m = BLE_ROW.match(line)
        if m:
            addr, rssi, dist, priv, seen, maker, name = m.groups()
            upsert("ble:" + addr.lower(), {
                "source": "ble",
                "mac": addr.lower(),
                "rssi": int(rssi),
                "dist": dist,
                "priv": priv == "rnd",
                "seen": int(seen),
                "maker": "" if maker == "-" else maker,
                "name": name.strip(),
            })


def reader(port):
    """Continuously read one serial port, reconnecting if it drops."""
    state = {"mode": None, "port": port}
    while True:
        try:
            with serial.Serial(port, BAUD, timeout=2) as ser:
                with lock:
                    meta["ports"][port] = "connected"
                writers[port] = ser   # so a sweep can send MARK to this board
                buf = b""
                while True:
                    chunk = ser.read(256)
                    if chunk:
                        buf += chunk
                        while b"\n" in buf:
                            raw, buf = buf.split(b"\n", 1)
                            handle_line(raw.decode("utf-8", "replace"), state)
        except Exception as e:
            writers.pop(port, None)
            with lock:
                meta["ports"][port] = f"disconnected ({type(e).__name__})"
            time.sleep(2)


def send_to_board(text):
    """Write a command to whichever serial board is open. True if it went out."""
    for port, ser in list(writers.items()):
        try:
            ser.write(text.encode())
            ser.flush()
            return True
        except Exception:
            continue
    return False


def analyze_sweep(rows, windows, mark_ts_ms, duration_s):
    """Verdict logic from sweep.py, returning structured data instead of printing."""
    S = SWEEP
    if mark_ts_ms is None:
        return {"verdict": "UNKNOWN - INCOMPLETE SWEEP",
                "detail": "The board never acknowledged the MARK, so there is no "
                          "reference point to correlate against.",
                "coverage": "0/0", "devices": []}

    duration_ms = int(duration_s * 1000)
    expected = duration_ms // S.BIN_MS
    got = sum(1 for w in windows if 0 <= w - mark_ts_ms < duration_ms)
    coverage_ok = got >= S.MIN_WINDOW_FRACTION * expected

    in_sweep = [r for r in rows if 0 <= r[0] - mark_ts_ms < duration_ms]
    reference = S.build_reference(duration_s)
    total_bytes = sum(r[2] + r[3] for r in in_sweep)
    ambient = total_bytes / duration_s if duration_s > 0 else 0

    results, candidates = [], []
    for mac in sorted(set(r[1] for r in in_sweep)):
        up_total = sum(r[2] for r in in_sweep if r[1] == mac)
        down_total = sum(r[3] for r in in_sweep if r[1] == mac)
        ratio = (up_total / down_total) if down_total > 0 else (
            float("inf") if up_total > 0 else 0.0)

        series = S.bin_series(in_sweep, mac, mark_ts_ms, duration_s, "up")
        corr, lag = S.best_lag_correlation(series, reference)
        active_bins = sum(1 for v in series if v > 0)
        enough = up_total >= S.MIN_UP_BYTES and active_bins >= S.MIN_ACTIVE_BINS
        asymmetric = ratio >= S.ASYMMETRY_MIN_RATIO
        correlated = abs(corr) >= S.CORR_THRESHOLD
        is_candidate = enough and asymmetric and correlated

        results.append({
            "mac": mac.upper(), "up": up_total, "down": down_total,
            "ratio": None if ratio == float("inf") else round(ratio, 2),
            "corr": round(corr, 2), "lag": round(lag, 2),
            "active_bins": active_bins,
            "evidence": "ok" if enough else f"thin ({active_bins} bins)",
            "candidate": is_candidate,
        })
        if is_candidate:
            candidates.append(results[-1])

    results.sort(key=lambda r: (not r["candidate"], -abs(r["corr"])))

    # Verdict precedence: a positive detection stands even on a
    # degraded sweep, but a negative result on a broken feed means nothing.
    if candidates:
        verdict = "CANDIDATE DETECTED"
        detail = "; ".join(
            f"{c['mac']} — upload/download {'∞' if c['ratio'] is None else c['ratio']}×, "
            f"light correlation {c['corr']} at {c['lag']}s lag" for c in candidates)
        if not coverage_ok:
            detail += ". Coverage was degraded: this detection stands, but the " \
                      "absence of other candidates is not meaningful."
    elif not coverage_ok:
        verdict = "UNKNOWN - INCOMPLETE SWEEP"
        detail = (f"Only {got} of {expected} heartbeat windows arrived. The link "
                  "dropped or the board was interrupted, so this proves nothing.")
    elif ambient > S.HIGH_TRAFFIC_BYTES_PER_S:
        verdict = "UNKNOWN - HIGH AMBIENT TRAFFIC"
        detail = (f"{ambient/1e6:.1f} MB/s of ambient traffic drowns out the "
                  "correlation. Try again somewhere quieter.")
    else:
        verdict = "NO NETWORKED CAMERA DETECTED"
        detail = ("No device's upload tracked the light. This is NOT an all-clear: "
                  "a camera recording to local storage, a wired camera, or anything "
                  "on 5 GHz transmits nothing this test can see.")

    return {"verdict": verdict, "detail": detail,
            "coverage": f"{got}/{expected}", "devices": results}


def run_sweep():
    """Send MARK, record for the stimulus duration, then analyze."""
    S = SWEEP
    duration = S.STIMULUS_PERIOD_S * 2 * S.STIMULUS_CYCLES   # 3s x2 x5 = 30s

    with lock:
        sweep_rows.clear()
        sweep_windows.clear()
        meta["last_mark"] = None
        sweep.update(phase="recording", elapsed=0.0, duration=duration,
                     result=None, error=None)

    if not send_to_board("MARK\n"):
        with lock:
            sweep.update(phase="error",
                         error="No board on USB to send MARK to. Is it plugged in, "
                               "and is monitor.py running without --no-serial?")
        return

    t0 = time.time()
    while True:
        el = time.time() - t0
        if el >= duration:
            break
        with lock:
            sweep["elapsed"] = round(el, 1)
        time.sleep(0.1)

    with lock:
        sweep.update(phase="analyzing", elapsed=duration)
        rows = list(sweep_rows)
        wins = list(sweep_windows)
        mark = meta["last_mark"]

    try:
        result = analyze_sweep(rows, wins, mark, duration)
        with lock:
            sweep.update(phase="done", result=result)
    except Exception as e:
        with lock:
            sweep.update(phase="error", error=f"Analysis failed: {e}")


def udp_listener(udp_port):
    """Receive JSON reports broadcast by boards over the hotspot."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", udp_port))
    except OSError as e:
        with lock:
            meta["ports"][f"udp:{udp_port}"] = f"bind failed ({e.strerror})"
        return
    with lock:
        meta["ports"][f"udp:{udp_port}"] = "listening"

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            msg = json.loads(data.decode("utf-8", "replace"))
            src = msg.get("src")
            unit = msg.get("unit", addr[0])

            with lock:
                meta["ports"][f"udp:{udp_port}"] = f"listening ({unit})"
                if src == "wifi" and "frames" in msg:
                    meta["wifi_frames"] = msg["frames"]

            for d in msg.get("devices", []):
                if src == "wifi":
                    upsert("wifi:" + d["mac"].upper(), {
                        "source": "wifi",
                        "mac": d["mac"].upper(),
                        "rssi": d["rssi"],
                        "dist": d["dist"],
                        "channel": d["ch"],
                        "kind": d["kind"],
                        "priv": bool(d["priv"]),
                        "frames": d["frames"],
                        # up/down bytes: the upload figure is the camera signal
                        "up": d.get("up", 0),
                        "down": d.get("down", 0),
                        "uppkts": d.get("uppkts", 0),
                        "downpkts": d.get("downpkts", 0),
                    })
                elif src == "ble":
                    upsert("ble:" + d["mac"].lower(), {
                        "source": "ble",
                        "mac": d["mac"].lower(),
                        "rssi": d["rssi"],
                        "dist": d["dist"],
                        "priv": bool(d["priv"]),
                        "seen": d["seen"],
                        "maker": d.get("maker", ""),
                        "name": d.get("name", ""),
                    })
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
            continue  # ignore malformed/truncated datagrams
        except Exception:
            time.sleep(0.5)


def autodetect():
    """Guess which serial ports are ESP32 boards."""
    if not HAVE_SERIAL:
        return []
    hits = []
    for p in list_ports.comports():
        d = p.device
        if any(t in d for t in ("usbserial", "usbmodem", "wchusb", "SLAB", "ttyUSB", "ttyACM")):
            hits.append(d)
    return hits


# ---------------------------------------------------------------- web server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # keep the console quiet

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/api/sweep/start"):
            if SWEEP is None:
                self._json({"ok": False,
                            "error": "host/sweep.py not found, so the sweep "
                                     "analysis is unavailable."}, 400)
                return
            with lock:
                busy = sweep["phase"] in ("recording", "analyzing")
            if busy:
                self._json({"ok": False, "error": "A sweep is already running."}, 409)
                return
            threading.Thread(target=run_sweep, daemon=True).start()
            self._json({"ok": True})
            return
        self._json({"ok": False, "error": "unknown endpoint"}, 404)

    def do_GET(self):
        if self.path.startswith("/api/sweep"):
            with lock:
                state = dict(sweep)
                state["available"] = SWEEP is not None
                if SWEEP is not None:
                    state["period"] = SWEEP.STIMULUS_PERIOD_S
                    state["cycles"] = SWEEP.STIMULUS_CYCLES
            self._json(state)
            return

        if self.path.startswith("/api/devices"):
            now = time.time()
            with lock:
                for k in [k for k, r in devices.items() if now - r["last_seen"] > STALE_AFTER]:
                    del devices[k]
                    accum.pop(k, None)   # drop its running totals too
                payload = {
                    # Sort strongest first, but in 3 dB buckets with the MAC as
                    # a tiebreaker. RSSI wobbles a couple of dB constantly, and
                    # sorting on the raw value made rows swap places every
                    # second -- permanently mid-animation and unreadable.
                    "devices": sorted(
                        [dict(r, age=round(now - r["last_seen"], 1),
                                 uptime=round(now - r["first_seen"]))
                         for r in devices.values()],
                        key=lambda r: (-round(r["rssi"] / 3), r["mac"]),
                    ),
                    "meta": dict(meta),
                }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        page = (HERE / "dashboard.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", action="append", default=[],
                    help="serial port (repeatable); auto-detects if omitted")
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--udp-port", type=int, default=4210,
                    help="listen for wireless board reports on this port")
    ap.add_argument("--no-serial", action="store_true",
                    help="don't open any serial port -- use this while "
                         "reflashing a board, so the upload isn't blocked")
    args = ap.parse_args()

    # Serial is ON by default: the sniffer board reports over USB, so the common
    # case should just work. Pass --no-serial to free the port for an upload.
    ports = [] if args.no_serial else (args.port or autodetect())

    if ports and not HAVE_SERIAL:
        sys.exit("Reading boards over USB needs pyserial. Run:\n"
                 "    pip3 install pyserial\n"
                 "(or start with --no-serial to run wireless-only)")
    if not args.no_serial and not HAVE_SERIAL:
        print("NOTE: pyserial isn't installed, so USB boards can't be read.")
        print("      pip3 install pyserial      (wireless still works)")

    print("Reading from:")
    for p in ports:
        print("   ", p, "(usb)")
        threading.Thread(target=reader, args=(p,), daemon=True).start()

    print(f"    UDP :{args.udp_port} (wireless)")
    threading.Thread(target=udp_listener, args=(args.udp_port,), daemon=True).start()

    if not ports:
        print("    (wireless only -- no serial ports opened; use --serial for USB)")

    url = f"http://localhost:{args.http_port}"
    print(f"\nDashboard: {url}   (Ctrl+C to stop)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        HTTPServer(("127.0.0.1", args.http_port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
