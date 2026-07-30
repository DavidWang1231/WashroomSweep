#!/usr/bin/env python3
"""
WashroomSweep host-side correlator.

Reads the CSV stream from the ESP32 sniffer over serial, builds a
per-MAC byte-rate time series, and cross-correlates each device against
a square-wave reference built from the operator-marked light stimulus
(3s off / 3s on, x5). High correlation with allowed lag means that
device's traffic tracks the room's light level, i.e. it can see this
room.

We deliberately never print "SAFE" or "no camera" -- RF silence is not
proof of absence (local-storage recording and wired cameras are
invisible to this method). Output is always one of:
    NO NETWORKED CAMERA DETECTED
    CANDIDATE DETECTED
    UNKNOWN - HIGH AMBIENT TRAFFIC
    UNKNOWN - INCOMPLETE SWEEP

Usage:
    python3 sweep.py [serial_port] [--baud 115200] [--channel 6] [--ap AA:BB:CC:DD:EE:FF]

Opening the serial port asserts DTR and resets the ESP32, wiping any
configuration typed earlier into a serial monitor -- so this program
waits for the board to reboot and then pushes the channel (and optional
BSSID filter) itself. Always pass --channel in a controlled test; pass
--ap to restrict counting to your own hotspot's BSSID (suppresses
neighbors), or omit it for a real sweep where the camera's AP is unknown.

Then:
    - press Enter at the moment you toggle the light off for the first
      time -- this forwards a MARK command to the board and starts the
      reference square wave from that instant
    - toggle the light 3s off / 3s on, five cycles; the sweep ends
      automatically

The board emits a WINDOW heartbeat line every 200ms even with zero
traffic. If too few heartbeats arrive during the sweep (dead link,
wrong channel, unplugged board), the verdict is UNKNOWN - INCOMPLETE
SWEEP rather than a false "nothing detected".
"""

import os
import sys
import csv
import time
import glob
import argparse
import threading
from collections import defaultdict

import serial  # pyserial

STIMULUS_PERIOD_S = 3.0       # 3s off / 3s on
STIMULUS_CYCLES = 5
MAX_LAG_S = 2.0               # cameras buffer; allow up to ~2s offset
BIN_S = 0.2                   # must match the ESP32 REPORT_INTERVAL_MS
CORR_THRESHOLD = 0.6          # empirically set from pilot data; tune after runs
ASYMMETRY_MIN_RATIO = 1.5     # up_bytes / down_bytes, coarse camera-like filter
HIGH_TRAFFIC_BYTES_PER_S = 2_000_000  # ambient noise floor before we distrust correlation
MIN_WINDOW_FRACTION = 0.8     # sweep is INCOMPLETE below this heartbeat coverage
MIN_UP_BYTES = 100_000        # evidence floor: ~3.3 KB/s over 30s, far below any video
MIN_ACTIVE_BINS = 25          # a candidate needs traffic in at least this many bins


def find_default_port():
    candidates = (glob.glob("/dev/cu.usbserial*")
                  + glob.glob("/dev/cu.usbmodem*")       # C3/S3 native USB
                  + glob.glob("/dev/cu.SLAB_USBtoUART*")
                  + glob.glob("/dev/cu.wchusbserial*"))
    return candidates[0] if candidates else None


def _consume_line(session, line, log_writer):
    """Parse one line of board output into a session. Shared by both
    transports so serial and UDP behave identically."""
    if not line:
        return

    if line.startswith("#"):
        if "AP_LOCKED" in line or "CHANNEL_LOCKED" in line or "AP_FILTER" in line:
            print(f"[board] {line}")
        return

    if line.startswith("WINDOW,"):
        try:
            ts_ms = int(line.split(",")[1])
        except (ValueError, IndexError):
            return
        with session.lock:
            session.windows.append(ts_ms)
        return

    if line.startswith("MARK,"):
        try:
            ts_ms = int(line.split(",")[1])
        except (ValueError, IndexError):
            return
        with session.lock:
            if session.mark_ts_ms is None:
                session.mark_ts_ms = ts_ms
                print(f"[mark] stimulus t=0 at board ts_ms={ts_ms}")
        return

    if line.startswith("ts_ms,mac"):
        return

    parts = line.split(",")
    if len(parts) != 7:
        return
    try:
        row = (int(parts[0]), parts[1], int(parts[2]), int(parts[3]),
               int(parts[4]), int(parts[5]), float(parts[6]))
    except ValueError:
        return
    with session.lock:
        session.rows.append(row)
    log_writer.writerow(list(row))


class UdpSession:
    """Same interface as SweepSession, fed by UDP instead of a serial cable.

    Lets the board run untethered: it joins (or hosts) a WiFi network and
    sends the identical CSV lines to this host's UDP port. Note the board
    must still be sniffing the channel it is transmitting on, so its own
    uplink shows up in the capture -- filter its MAC out when reading
    results.
    """

    def __init__(self, listen_port, board_addr=None):
        import socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", listen_port))
        self.sock.settimeout(1.0)
        self.board_addr = board_addr   # (ip, port) to send commands back to
        self.rows = []
        self.windows = []
        self.mark_ts_ms = None
        self.lock = threading.Lock()
        self.running = True
        self._buf = ""

    def _send(self, text):
        if self.board_addr:
            self.sock.sendto(text.encode(), self.board_addr)

    def send_mark(self):
        self._send("MARK\n")

    def configure_board(self, channel, ap):
        # No DTR reset over UDP, so no reboot wait is needed.
        if channel is not None:
            self._send(f"CH {channel}\n")
            time.sleep(0.2)
        if ap is not None:
            self._send(f"AP {ap}\n")
            time.sleep(0.2)

    def reader_thread(self, log_writer):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
            except Exception:
                continue
            if self.board_addr is None:
                self.board_addr = addr      # learn the board from its first packet
            self._buf += data.decode("utf-8", errors="ignore")
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                _consume_line(self, line.strip(), log_writer)


class SweepSession:
    def __init__(self, port, baud):
        self.ser = serial.Serial(port, baud, timeout=1)
        self.rows = []             # raw (ts_ms, mac, up, down, up_pkts, down_pkts, rssi)
        self.windows = []          # ts_ms of every WINDOW heartbeat received
        self.mark_ts_ms = None      # ts_ms at which the stimulus reference starts
        self.lock = threading.Lock()
        self.running = True

    def reader_thread(self, log_writer):
        while self.running:
            try:
                line = self.ser.readline().decode("utf-8", errors="ignore").strip()
            except Exception:
                continue
            _consume_line(self, line, log_writer)

    def send_mark(self):
        self.ser.write(b"MARK\n")

    def configure_board(self, channel, ap):
        """Push channel lock / BSSID filter after the DTR-triggered reboot.

        Opening the serial port resets the ESP32, so anything configured
        beforehand (e.g. via a serial monitor) is gone by the time we're
        connected. Wait out the reboot, then send our own settings.
        """
        print("Waiting for board reboot...")
        time.sleep(2.5)
        if channel is not None:
            self.ser.write(f"CH {channel}\n".encode())
            time.sleep(0.2)
        if ap is not None:
            self.ser.write(f"AP {ap}\n".encode())
            time.sleep(0.2)


# All binning is done in integer milliseconds. BIN_S (0.2) has no exact
# binary floating-point representation, so computing bin indices as
# int(seconds / BIN_S) truncates unpredictably right at bin boundaries --
# verified this was silently misfiling ~12% of bins (18/151 in a clean
# noiseless test) and dragging real correlations well below threshold.
BIN_MS = round(BIN_S * 1000)
STIMULUS_PERIOD_MS = round(STIMULUS_PERIOD_S * 1000)


def build_reference(duration_s):
    """Square wave: off for first 3s (0), on for next 3s (1), x5 cycles."""
    n_bins = int(duration_s * 1000) // BIN_MS  # 30s / 200ms = exactly 150
    ref = []
    for i in range(n_bins):
        t_ms = i * BIN_MS
        cycle_pos_ms = t_ms % (2 * STIMULUS_PERIOD_MS)
        ref.append(0.0 if cycle_pos_ms < STIMULUS_PERIOD_MS else 1.0)
    return ref


def bin_series(rows, mac, mark_ts_ms, duration_s, field_idx):
    """Bucket one device's byte counts into BIN_S-wide bins starting at mark_ts_ms."""
    n_bins = int(duration_s * 1000) // BIN_MS
    series = [0.0] * n_bins
    duration_ms = int(duration_s * 1000)
    for ts_ms, m, up, down, up_pkts, down_pkts, rssi in rows:
        if m != mac:
            continue
        rel_ms = ts_ms - mark_ts_ms
        if rel_ms < 0 or rel_ms >= duration_ms:
            continue
        b = rel_ms // BIN_MS
        if 0 <= b < n_bins:
            series[b] += up if field_idx == "up" else down
    return series


def normalized_xcorr(a, b):
    """Pearson correlation of two equal-length sequences; 0 if degenerate."""
    n = len(a)
    if n == 0 or len(b) != n:
        return 0.0
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    den_a = sum((x - mean_a) ** 2 for x in a) ** 0.5
    den_b = sum((x - mean_b) ** 2 for x in b) ** 0.5
    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


def best_lag_correlation(series, reference):
    """Slide `reference` against `series` over +/-MAX_LAG_S, return best |corr|."""
    max_lag_bins = int(MAX_LAG_S / BIN_S)
    best = 0.0
    best_lag = 0
    n = len(series)
    for lag in range(-max_lag_bins, max_lag_bins + 1):
        a, b = [], []
        for i in range(n):
            j = i + lag
            if 0 <= j < len(reference):
                a.append(series[i])
                b.append(reference[j])
        if len(a) < n // 2:  # not enough overlap at this lag
            continue
        c = normalized_xcorr(a, b)
        if abs(c) > abs(best):
            best = c
            best_lag = lag
    return best, best_lag * BIN_S


def analyze(rows, windows, mark_ts_ms, sweep_duration_s, log_path):
    if mark_ts_ms is None:
        print("NO STIMULUS MARK RECEIVED")
        print("UNKNOWN - INCOMPLETE SWEEP")
        return

    duration_ms = int(sweep_duration_s * 1000)

    # Everything below looks ONLY at the stimulus window. Pre-MARK ambient
    # traffic must not pollute the asymmetry ratio: the longer the operator
    # idles before pressing Enter, the more it would otherwise dominate.
    window_rows = [r for r in rows if 0 <= r[0] - mark_ts_ms < duration_ms]

    # Heartbeat coverage: the board emits one WINDOW line per 200ms no
    # matter what. Too few means the link died or we listened on the wrong
    # channel -- and "no data" must never masquerade as "no camera".
    expected_windows = duration_ms // BIN_MS
    got_windows = sum(1 for w in windows if 0 <= w - mark_ts_ms < duration_ms)
    coverage_ok = got_windows >= MIN_WINDOW_FRACTION * expected_windows
    print(f"\nsweep coverage: {got_windows}/{expected_windows} heartbeat windows")

    macs = sorted(set(r[1] for r in window_rows))
    reference = build_reference(sweep_duration_s)

    total_bytes = sum(r[2] + r[3] for r in window_rows)
    ambient_bytes_per_s = total_bytes / sweep_duration_s if sweep_duration_s > 0 else 0

    candidates = []
    print(f"{'MAC':<18}{'up_total':>10}{'down_total':>12}{'ratio':>8}{'corr':>8}{'lag_s':>8}  evidence")
    for mac in macs:
        up_total = sum(r[2] for r in window_rows if r[1] == mac)
        down_total = sum(r[3] for r in window_rows if r[1] == mac)
        ratio = (up_total / down_total) if down_total > 0 else float("inf") if up_total > 0 else 0.0

        up_series = bin_series(window_rows, mac, mark_ts_ms, sweep_duration_s, "up")
        corr, lag = best_lag_correlation(up_series, reference)
        active_bins = sum(1 for v in up_series if v > 0)

        # Evidence floor: a handful of stray packets can land a high
        # correlation by pure luck, so thin traffic never qualifies.
        enough = up_total >= MIN_UP_BYTES and active_bins >= MIN_ACTIVE_BINS
        print(f"{mac:<18}{up_total:>10}{down_total:>12}{ratio:>8.2f}{corr:>8.2f}{lag:>8.2f}  "
              f"{'ok' if enough else 'thin (' + str(active_bins) + ' bins)'}")

        asymmetric = ratio >= ASYMMETRY_MIN_RATIO
        correlated = abs(corr) >= CORR_THRESHOLD
        if enough and asymmetric and correlated:
            candidates.append((mac, ratio, corr, lag))

    # Verdict precedence: a positive detection stands even on a degraded
    # sweep (the dangerous mistake is a false all-clear, not a cautious
    # flag), but a negative result on a broken feed is meaningless.
    print()
    if candidates:
        for mac, ratio, corr, lag in candidates:
            print(f"CANDIDATE DETECTED: {mac} (uplink/downlink ratio={ratio:.2f}, "
                  f"stimulus correlation={corr:.2f} at lag={lag:.2f}s)")
        if not coverage_ok:
            print("(note: sweep coverage was degraded; detection stands, but absence "
                  "of other candidates is not meaningful)")
    elif not coverage_ok:
        print("UNKNOWN - INCOMPLETE SWEEP")
    elif ambient_bytes_per_s > HIGH_TRAFFIC_BYTES_PER_S:
        print("UNKNOWN - HIGH AMBIENT TRAFFIC")
    else:
        print("NO NETWORKED CAMERA DETECTED")
    print(f"\n(raw log written to {log_path})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("port", nargs="?", default=None)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--channel", type=int, default=None,
                        help="lock the sniffer to this WiFi channel (always set this "
                             "in a controlled test)")
    parser.add_argument("--udp", type=int, default=None, metavar="PORT",
                        help="read CSV from this UDP port instead of a serial "
                             "cable (untethered board)")
    parser.add_argument("--ap", default=None, metavar="MAC",
                        help="count only traffic to/from this BSSID (your hotspot); "
                             "omit for a real sweep where the camera's AP is unknown")
    args = parser.parse_args()

    if args.udp:
        print(f"Listening for board CSV on UDP :{args.udp} ...")
        session = UdpSession(args.udp)
    else:
        port = args.port or find_default_port()
        if not port:
            print("No serial port found/specified. Pass it explicitly, e.g.:")
            print("  python3 sweep.py /dev/cu.usbserial-0001")
            print("  python3 sweep.py --udp 5005      (untethered board)")
            sys.exit(1)
        print(f"Opening {port} @ {args.baud}...")
        session = SweepSession(port, args.baud)

    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"sweep_{int(time.time())}.csv")
    with open(log_path, "w", newline="") as log_file:
        log_writer = csv.writer(log_file)
        log_writer.writerow(["ts_ms", "mac", "up_bytes", "down_bytes", "up_pkts", "down_pkts", "rssi_avg"])

        t = threading.Thread(target=session.reader_thread, args=(log_writer,), daemon=True)
        t.start()

        session.configure_board(args.channel, args.ap)

        input("\nWatching ambient traffic. Press Enter the instant you turn the light OFF "
              "to start the stimulus reference...\n")
        session.send_mark()
        time.sleep(0.05)  # let the board's own MARK line land before ours if it double-fires

        sweep_duration_s = STIMULUS_CYCLES * 2 * STIMULUS_PERIOD_S
        print(f"Toggle the light OFF/ON every {STIMULUS_PERIOD_S:.0f}s, "
              f"{STIMULUS_CYCLES} cycles ({sweep_duration_s:.0f}s total).")
        print("Recording... (this will auto-finish; you don't need to press anything else)")
        time.sleep(sweep_duration_s + 1.0)  # +1s pad for serial/clock slack

        session.running = False
        # Join before the `with` block closes the log file: the reader may
        # be mid-readline (up to its 1s timeout) and would otherwise lose
        # the last rows or write into a closed file.
        t.join(timeout=3.0)

    with session.lock:
        rows_copy = list(session.rows)
        windows_copy = list(session.windows)
        mark_ts_ms = session.mark_ts_ms

    analyze(rows_copy, windows_copy, mark_ts_ms, sweep_duration_s, log_path)


if __name__ == "__main__":
    main()
