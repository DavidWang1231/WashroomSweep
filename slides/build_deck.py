#!/usr/bin/env python3
"""Generate the WashroomSweep deck as an editable .pptx (16:9)."""

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR

# palette — matches the HTML deck
GROUND = RGBColor(0x0A, 0x10, 0x17)
PANEL  = RGBColor(0x13, 0x1E, 0x2A)
RULE   = RGBColor(0x27, 0x38, 0x47)
INK    = RGBColor(0xD5, 0xE0, 0xEB)
DIM    = RGBColor(0x8A, 0x9F, 0xB4)
FAINT  = RGBColor(0x5D, 0x72, 0x87)
ACCENT = RGBColor(0x35, 0xD0, 0xD8)
ALERT  = RGBColor(0xFF, 0x5D, 0x5D)
GOOD   = RGBColor(0x4A, 0xD9, 0x91)

SANS = "Helvetica Neue"
MONO = "Menlo"

W, H = Inches(13.333), Inches(7.5)
M = Inches(0.85)                      # side margin
CONTENT_W = W - 2 * M

prs = Presentation()
prs.slide_width, prs.slide_height = W, H
BLANK = prs.slide_layouts[6]


def slide(notes=""):
    s = prs.slides.add_slide(BLANK)
    bg = s.background.fill
    bg.solid()
    bg.fore_color.rgb = GROUND
    if notes:
        s.notes_slide.notes_text_frame.text = notes
    return s


def box(s, x, y, w, h, fill=None, line=None, line_w=Pt(1)):
    from pptx.enum.shapes import MSO_SHAPE
    shp = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    shp.adjustments[0] = 0.04
    if fill:
        shp.fill.solid(); shp.fill.fore_color.rgb = fill
    else:
        shp.fill.background()
    if line:
        shp.line.color.rgb = line; shp.line.width = line_w
    else:
        shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def text(s, x, y, w, h, runs, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
         spacing=1.0):
    """runs = [(text, size_pt, color, bold, font, space_after_pt), ...]"""
    tb = s.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    first = True
    for item in runs:
        t, sz, col, bold, fnt, after = item
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.alignment = align
        p.line_spacing = spacing
        p.space_after = Pt(after)
        r = p.add_run(); r.text = t
        r.font.size = Pt(sz); r.font.color.rgb = col
        r.font.bold = bold; r.font.name = fnt
    return tb


def eyebrow(s, label, y=Inches(0.7)):
    text(s, M, y, CONTENT_W, Inches(0.3),
         [(label.upper(), 11, ACCENT, True, SANS, 0)])


def title(s, t, y=Inches(1.05), size=34):
    text(s, M, y, CONTENT_W, Inches(1.3),
         [(t, size, INK, True, SANS, 0)], spacing=0.95)


def cards(s, items, y, h=Inches(1.85), cols=None):
    """items = [(heading, body), ...]"""
    cols = cols or len(items)
    gap = Inches(0.22)
    w = int((CONTENT_W - gap * (cols - 1)) / cols)
    for i, (head, body) in enumerate(items):
        x = M + i * (w + gap)
        box(s, x, y, w, h, fill=PANEL, line=RULE)
        pad = Inches(0.22)
        text(s, x + pad, y + pad, w - 2 * pad, h - 2 * pad,
             [(head, 14, INK, True, SANS, 6),
              (body, 11, DIM, False, SANS, 0)], spacing=1.15)


def metrics(s, items, y, h=Inches(1.6)):
    """items = [(number, label, sub, color), ...]"""
    gap = Inches(0.22)
    n = len(items)
    w = int((CONTENT_W - gap * (n - 1)) / n)
    for i, (num, lbl, sub, col) in enumerate(items):
        x = M + i * (w + gap)
        box(s, x, y, w, h, fill=PANEL, line=RULE)
        bar = box(s, x, y, w, Pt(3), fill=col)
        bar.line.fill.background()
        pad = Inches(0.22)
        runs = [(num, 30, col, True, SANS, 4),
                (lbl.upper(), 10, DIM, True, SANS, 4)]
        if sub:
            runs.append((sub, 9.5, FAINT, False, SANS, 0))
        text(s, x + pad, y + pad + Pt(2), w - 2 * pad, h - 2 * pad, runs, spacing=1.1)


def body(s, t, y, size=15, w=None, col=DIM):
    text(s, M, y, w or Inches(9.6), Inches(1.2),
         [(t, size, col, False, SANS, 0)], spacing=1.35)


def slot(s, label, y, h=Inches(1.5)):
    shp = box(s, M, y, CONTENT_W, h, fill=PANEL, line=RULE)
    shp.line.dash_style = 4  # dashed
    text(s, M, y + h / 2 - Inches(0.18), CONTENT_W, Inches(0.4),
         [(label, 12, FAINT, False, SANS, 0)], align=PP_ALIGN.CENTER)


def teammate(s, label, y):
    box(s, M, y, CONTENT_W, Inches(0.55), fill=PANEL, line=None)
    bar = box(s, M, y, Pt(3), Inches(0.55), fill=ACCENT)
    bar.line.fill.background()
    text(s, M + Inches(0.2), y + Inches(0.15), CONTENT_W - Inches(0.4), Inches(0.3),
         [(label, 11.5, DIM, False, SANS, 0)])


def table(s, headers, rows, y, col_w, row_h=Inches(0.42), hit_rows=()):
    x = M
    # header
    for j, hcell in enumerate(headers):
        text(s, x + sum(col_w[:j]), y, col_w[j], Inches(0.3),
             [(hcell.upper(), 9.5, DIM, True, SANS, 0)])
    line = box(s, M, y + Inches(0.3), CONTENT_W, Pt(1), fill=RULE)
    line.line.fill.background()
    yy = y + Inches(0.38)
    for i, row in enumerate(rows):
        if i in hit_rows:
            hl = box(s, M - Inches(0.1), yy - Inches(0.04),
                     CONTENT_W + Inches(0.2), row_h, fill=RGBColor(0x2E, 0x11, 0x16))
            hl.line.fill.background()
        for j, cell in enumerate(row):
            fnt = MONO if j == 0 or any(c.isdigit() for c in cell) else SANS
            col = ALERT if i in hit_rows else (INK if j == 0 else DIM)
            text(s, x + sum(col_w[:j]), yy, col_w[j], row_h,
                 [(cell, 11, col, i in hit_rows, fnt, 0)])
        sep = box(s, M, yy + row_h - Inches(0.04), CONTENT_W, Pt(0.75), fill=RULE)
        sep.line.fill.background()
        yy += row_h


# ══════════════════════════════════════════════════ 1 title
s = slide("开场。先讲场景不讲技术:一个进门前的扫查器 —— 在你走进洗手间、更衣室、"
          "民宿之前,告诉你这里是不是有摄像头正在把画面传出去。\n\n"
          "队友分工:这一页可由 Justin 开场介绍团队。")
eyebrow(s, "Wireless for Humanity · Student Design Competition", Inches(1.6))
title(s, "WashroomSweep", Inches(2.0), size=54)
body(s, "A low-cost sweeper that tells you, before you enter a closed space, "
        "whether a networked camera is streaming that space right now.",
     Inches(3.3), size=18, col=INK)
text(s, M, Inches(4.6), CONTENT_W, Inches(0.4),
     [("University of Waterloo  ·  2-person team", 12, FAINT, False, MONO, 0)])

# ══════════════════════════════════════════════════ 2 why
s = slide("我们的初衷。洗手间是最典型的场景:进去的人无法预先检查、安装者有充足的隐蔽时间、"
          "受害者完全无法察觉。\n\n"
          "Airbnb 2024年4月30日起全面禁止室内摄像头 —— 真实政策可直接引用。\n\n"
          "注意:如果要加具体统计数字,自己查可靠来源,不要凭印象写。")
eyebrow(s, "Where this started")
title(s, "It began with a specific place:\nthe public washroom", size=32)
body(s, "We wanted a device you could point at a washroom before walking in — "
        "because that is the one place where you cannot inspect first, and where "
        "being recorded does the most harm.", Inches(2.35), size=15, col=INK)
cards(s, [
    ("You can't check it yourself",
     "A cubicle offers dozens of mounting points — vents, hooks, dispensers, smoke "
     "detectors. Nobody dismantles fixtures before using a toilet."),
    ("The installer has time; you don't",
     "Whoever plants a camera can lock a cubicle and work unhurried. You get seconds, "
     "and no reason to suspect anything."),
    ("They disappear into fixtures",
     "A streaming camera now fits inside a USB charger, a clothes hook, or a screw "
     "head, and costs less than a meal."),
], Inches(3.3))
body(s, "Not confined to washrooms — Airbnb banned all indoor cameras in April 2024 — "
        "but washrooms set the constraint: the check must happen from outside, in under "
        "a minute, by someone with no training.", Inches(5.4), size=13)
slot(s, "[ 插图位 ]  新闻截图,或伪装成挂钩/充电器的摄像头产品图", Inches(6.15), Inches(0.95))

# ══════════════════════════════════════════════════ 3 market
s = slide("市面上已有什么,以及缺点。\n\n"
          "重点讲 RF 探测器:几十块的产品只会说''这里有无线信号'',但现代建筑里永远有信号 —— "
          "响个不停等于没响,没有任何信息量。\n\n"
          "手机探测 APP 大多用磁力计找金属,和摄像头没有因果关系,基本是安慰剂。")
eyebrow(s, "What's already on the market")
title(s, "Existing tools answer the wrong question,\nor cost more than the problem",
      size=30)
table(s,
      ["What you can buy", "How it works", "Why it falls short"],
      [["Lens finders  ~$15", "Red LED ring; look for lens retroreflection",
        "Needs line of sight at the right angle; misses anything recessed"],
       ["RF detectors  $20–60", "Broadband power meter; beeps as RF energy rises",
        "No discrimination — radio is always present, so it always beeps"],
       ["Phone apps  free", "Magnetometer, or scanning for IR",
        "Finds metal, not cameras. Screws and pipes trigger it"],
       ["Network scanners  free", "List devices on a network you joined",
        "Needs the password; camera may be on a network you never got"],
       ["Pro NLJD / thermal  $1k–10k", "Junction detection or thermal signature",
        "Effective — and priced for security professionals, not for a washroom check"]],
      Inches(2.5),
      [Inches(2.9), Inches(3.9), Inches(4.8)])
body(s, "Every affordable option answers “is there a signal?” or “is there "
        "a lens?”  We wanted the question people actually have: is something "
        "recording here, right now?", Inches(5.55), size=15, col=INK)

# ══════════════════════════════════════════════════ 4 approach
s = slide("两个信号的分工一定要讲清楚,评委很可能问为什么需要第二个。\n\n"
          "答案:光靠不对称,隔壁房间的摄像头、楼下在上传视频的人,你都区分不了。"
          "相关性回答的是''它拍的是不是这个房间''。")
eyebrow(s, "Our approach")
title(s, "Two signals, used together")
cards(s, [
    ("01   Uplink / downlink asymmetry",
     "A streaming camera transmits far more than it receives; phones and laptops are "
     "the opposite. This is the coarse filter that narrows a room full of devices to "
     "one or two suspects."),
    ("02   Bitrate response to a stimulus",
     "Cameras use variable-bitrate encoding, so changing what the camera sees changes "
     "how much it sends. We toggle the room's own light switch and cross-correlate. "
     "This proves a device can see THIS room."),
], Inches(2.5), h=Inches(2.3), cols=2)
body(s, "Everything is passive: no network association, no decryption. We read 802.11 "
        "header fields only — transmitter address, frame length, signal strength.",
     Inches(5.1), size=15, col=INK)

# ══════════════════════════════════════════════════ 5 system
s = slide("板子负责看,笔记本负责判断。板子只数字节,完全不知道什么是摄像头。\n\n"
          "值得强调:方向判断用 802.11 帧里的 ToDS/FromDS 位,不需要知道路由器是谁 —— "
          "真实的偷拍摄像头在你从没见过的网络上。")
eyebrow(s, "System")
title(s, "Sensor and judgement, split")
cards(s, [
    ("ESP32 — sensing",
     "Promiscuous capture. Counts bytes per source MAC in 200 ms windows, splits "
     "uplink from downlink, emits CSV plus a heartbeat."),
    ("Direction without context",
     "Direction comes from the 802.11 ToDS/FromDS bits, which classify traffic for any "
     "network — we never need to know which router is which."),
    ("Host — judgement",
     "~100 lines of Python. Builds a per-device byte-rate series, correlates against "
     "the light stimulus, prints one verdict."),
], Inches(2.3), h=Inches(1.7))
table(s, ["Verdict", "Meaning"],
      [["CANDIDATE DETECTED", "A device behaves like a camera streaming this space"],
       ["NO NETWORKED CAMERA DETECTED", "Nothing matched — not a claim the room is safe"],
       ["UNKNOWN — HIGH AMBIENT TRAFFIC", "Too noisy to trust the correlation"],
       ["UNKNOWN — INCOMPLETE SWEEP", "The capture itself failed; no conclusion possible"]],
      Inches(4.3), [Inches(4.6), Inches(7.0)], row_h=Inches(0.38))
body(s, "We never output “safe.”  RF silence is not proof of absence.",
     Inches(6.3), size=15, col=INK)

# ══════════════════════════════════════════════════ 6 pilot
s = slide("这是你昨天在笔记本上测的:同一部手机推流,开灯约 5 MB/s,关灯约 2 MB/s,差 2.4 倍。\n\n"
          "说明这是''网络侧''测量 —— 在网络内部测的,不是空口被动测的。诚实区分这两者。")
eyebrow(s, "Validation · step one")
title(s, "First we checked the assumption holds")
body(s, "Before building anything, we measured whether scene brightness actually "
        "changes how much a camera transmits.", Inches(2.15), size=16, col=INK)
metrics(s, [("~5 MB/s", "Lights on", "", INK),
            ("~2 MB/s", "Lights off", "", INK),
            ("2.4×", "Bitrate swing", "Variable-bitrate assumption confirmed", GOOD)],
        Inches(3.0))
body(s, "Measured network-side on a laptop, with a phone streaming video as the camera. "
        "This validates the mechanism; closing it over the air was a separate problem.",
     Inches(4.85), size=13)
slot(s, "[ 插图位 ]  你昨天测试的照片或流量截图", Inches(5.5), Inches(1.35))

# ══════════════════════════════════════════════════ 7 hardware
s = slide("整套系统的射频部分只有一块 ESP32 —— 几十块钱的开发板,没有专用抓包网卡,没有 SDR。\n\n"
          "成本和可复现性是评委关心的点,强调''一块板子 + 100 行 Python''。\n\n"
          "数据都是真机实测:26秒扫完13个信道发现49台设备;心跳59个窗口全部精确200ms,零复位。")
eyebrow(s, "The hardware")
title(s, "One ESP32 does all of it")
metrics(s, [("49", "Devices found", "In a 26 s sweep across all 13 channels", ACCENT),
            ("200 ms", "Reporting window", "Exact across 59 windows, zero resets", ACCENT),
            ("3", "Board variants", "Compiles on ESP32, C3 and S3", ACCENT),
            ("0", "Packets decrypted", "Header fields only; never joins a network", ACCENT)],
        Inches(2.35), h=Inches(1.75))
cards(s, [
    ("Capabilities",
     "Per-device byte counting · uplink/downlink split · channel lock or 13-channel "
     "scan · heartbeat so the host can tell a quiet room from a dead link · stimulus "
     "marker · CSV output"),
    ("SoftAP mode",
     "The board can host its own 802.11n network while still sniffing — the mode that "
     "made our live detection possible. Why that was necessary is on the limitations "
     "slide."),
], Inches(4.45), h=Inches(1.7), cols=2)

# ══════════════════════════════════════════════════ 8 live detection
s = slide("这是重头戏。实测抓到:摄像头 478 KB/s 持续上行,下行为零,比第二活跃的设备高 170 倍。\n\n"
          "演示时指两个地方:uplink 那列的巨大差距,以及 up/down 比值是无穷大 —— "
          "一台设备只发不收还发这么多,它在干什么?")
eyebrow(s, "Live detection")
title(s, "The camera separates itself\nby three orders of magnitude", size=30)
table(s, ["Device", "Uplink", "Up / Down", "Verdict"],
      [["42:BB:12:9C:98:E6", "478 KB/s", "∞  (no downlink)", "CANDIDATE"],
       ["B6:5D:1D:A2:4B:41", "2.8 KB/s", "22.7", "—"],
       ["30:76:F5:7D:E3:9C", "0.1 KB/s", "∞", "—"],
       ["CE:55:9A:10:62:08", "0.1 KB/s", "∞", "—"]],
      Inches(2.55), [Inches(3.6), Inches(2.4), Inches(3.0), Inches(2.6)],
      hit_rows=(0,))
metrics(s, [("17.9 MB", "Camera uplink", "Over 30 s · 0 bytes downlink", ALERT),
            ("99.9%", "Of all air traffic", "Every other client combined: under 12 KB", ALERT),
            ("170×", "Above second place", "Live, over real RF", ALERT)],
        Inches(4.6), h=Inches(1.5))
slot(s, "[ 录屏位 ]  实时界面截图 —— 红色报警横幅弹出的那一刻", Inches(6.25), Inches(0.9))

# ══════════════════════════════════════════════════ 9 UI
s = slide("这是队友做的实时控制台。可以点名讲这几个功能:一键发起30秒扫查并显示进度;"
          "板子可走USB也可走WiFi无线上报;串口掉了自动重连;设备按信号强度分档排序。\n\n"
          "最后一条特别值得讲 —— RSSI每秒抖几个dB,直接按原始值排序会让表格一直动、根本没法读。\n\n"
          "这一页请 Justin 自己讲。")
eyebrow(s, "Operator interface")
title(s, "One page to run the sweep from")
body(s, "The boards and the analysis run behind a single live console — the operator "
        "never touches a terminal.", Inches(2.15), size=15, col=INK)
cards(s, [
    ("One-button sweep",
     "Starts the 30 s stimulus run, sends the marker, shows progress, renders the "
     "verdict and per-device evidence."),
    ("Tethered or wireless",
     "Boards report over USB serial or over WiFi via UDP, so a board can sit in the "
     "room while the operator stays outside."),
    ("Survives interruptions",
     "A dropped serial port reconnects on its own; the page shows which feeds are live "
     "and which have gone quiet."),
    ("Readable under noise",
     "RSSI wobbles constantly. Sorting on raw values made rows swap every second; "
     "bucketing by 3 dB keeps it legible."),
], Inches(2.85), h=Inches(1.9))
teammate(s, "[ Justin's work ]   Dashboard design and implementation. Both feeds — "
            "WiFi sniffer and BLE scanner — land in this one view.", Inches(4.95))
slot(s, "[ 截图位 ]  Justin 的界面截图 —— 扫查进行中或出结果的那一刻", Inches(5.7), Inches(1.1))

# ══════════════════════════════════════════════════ 10 BLE
s = slide("BLE 不参与摄像头判定,因为蓝牙带宽远低于视频,摄像头的视频数据在蓝牙上根本不存在。\n\n"
          "但队友解决的三个问题是真实的工程判断,值得讲:地址轮换产生幽灵设备、"
          "表满了会挤掉真实设备、BLE和WiFi抢内存。\n\n"
          "这个''我们评估过并排除了''的回答是加分的,显示做过取舍而不是漏了没做。")
eyebrow(s, "Complementary sensing")
title(s, "Bluetooth presence — and why it stays\nout of the verdict", size=30)
cards(s, [
    ("What it does",
     "A second ESP32 scans BLE advertisements and reports every nearby device — "
     "address, signal, manufacturer, name, privacy — over WiFi to the same dashboard."),
    ("Why it never feeds the verdict",
     "BLE throughput is orders of magnitude below live video, so a streaming camera's "
     "payload never appears there. A BLE-capable camera reveals only that it HAS a BLE "
     "radio, typically used once at setup."),
], Inches(2.45), h=Inches(1.5), cols=2)
text(s, M, Inches(4.15), CONTENT_W, Inches(0.3),
     [("WHAT MADE IT HARD", 10, DIM, True, SANS, 0)])
cards(s, [
    ("Phones rotate their address",
     "A BLE address changes every ~15 min, so a naive table fills with devices that no "
     "longer exist. Entries now expire after 20 s."),
    ("A full table hides real devices",
     "With the table full, every new device evicts an old one. An eviction counter makes "
     "that visible."),
    ("BLE and WiFi compete for memory",
     "Both stacks together is tight. Free heap is reported each pass; reporting is "
     "skipped below a floor rather than crashing."),
], Inches(4.5), h=Inches(1.55))
teammate(s, "[ Justin's work ]   BLE scanner, its memory and address-rotation handling, "
            "and the wireless reporting path.", Inches(6.2))

# ══════════════════════════════════════════════════ 11 limits
s = slide("这一页是加分项。我们的贡献里有一条就是''量化方法何时失效'',现在有数字了。\n\n"
          "主动讲 802.11ax:6.6 MB/s 下载只恢复了 0.03%,同时验证了接收链路健康"
          "(信道正确、beacon按100ms正常到达)—— 这种''证明自己没测错''的严谨性评委会看到。\n\n"
          "务必主动说 SoftAP 是演示装置不是产品形态,被问出来会很被动。")
eyebrow(s, "What we measured about failure")
title(s, "Where the method stops working")
metrics(s, [("0.03%", "Of an 802.11ax link recovered",
             "A 6.6 MB/s download came back as ~2 KB/s. Receive path verified healthy "
             "at the same time — correct channel, beacons on schedule. A hardware "
             "ceiling, not a bug.", ALERT),
            ("0", "5 GHz signals visible",
             "The radio has no 5 GHz front end. Closing both gaps needs different "
             "silicon — a dual-band part such as the ESP32-C5 — not different software.",
             ALERT)],
        Inches(2.3), h=Inches(1.9))
cards(s, [
    ("Local-storage cameras",
     "Recording to an SD card emits nothing. Undetectable by any RF method, ours "
     "included."),
    ("Wired cameras", "Equally invisible."),
    ("RSSI is not distance",
     "We report signal strength but never localize. Walls and body blocking move RSSI "
     "as much as range does."),
], Inches(4.4), h=Inches(1.45))
body(s, "Our demo rig is not our deployment mode. We ran the board as its own 802.11n "
        "access point to force the camera onto a PHY this radio can read. A real hidden "
        "camera sits on the room's network and would never join ours.",
     Inches(6.05), size=13, col=INK)

# ══════════════════════════════════════════════════ 12 difficulties
s = slide("我们花了大量时间在一个看起来简单、实际很难的问题上:找一个合格的''替身摄像头''。\n\n"
          "三个原因都是实测出来的。而且我们确认了机制本身有效 —— MJPEG模式下完全遮住镜头,"
          "码率从700 KB/s掉到1 KB,300倍。\n\n"
          "还可以讲两个bug:混杂模式过滤器丢失导致垃圾帧冲垮设备表;浮点分箱误差把相关性从1.00拉到0.71。")
eyebrow(s, "Difficulties")
title(s, "The hardest problem was finding\na valid test camera", size=30)
body(s, "The correlation closed in simulation and in the network-side pilot, but not "
        "over the air — because a phone is a poor stand-in for a camera, in three "
        "specific ways.", Inches(2.35), size=15, col=INK)
cards(s, [
    ("Constant bitrate",
     "The app streamed a fixed ~550 KB/s regardless of what the lens saw, so scene "
     "changes never reached the bitrate."),
    ("Auto-gain",
     "Darkening a scene doesn't simplify it — the sensor amplifies until it returns a "
     "noisy, high-detail image. Bitrate sometimes went UP."),
    ("Dual-camera compositing",
     "The app forced a front+rear pair. The front sensor kept imaging the room and held "
     "the floor at ~430 KB/s."),
], Inches(3.25), h=Inches(1.75))
metrics(s, [("300×", "Swing when fully occluded",
             "In MJPEG mode ~700 KB/s dropped to ~1 KB — the mechanism is sound; the "
             "stand-in was not.", GOOD)], Inches(5.2), h=Inches(1.35))
text(s, M + Inches(4.6), Inches(5.35), Inches(7.2), Inches(1.1),
     [("What a valid stand-in needs", 14, INK, True, SANS, 6),
      ("Fixed exposure, a single sensor, and variable-bitrate encoding — which is "
       "exactly what a real IP camera is. That is the next thing we would buy.",
       12, DIM, False, SANS, 0)], spacing=1.2)

# ══════════════════════════════════════════════════ 13 extensions
s = slide("同样的''谁在单向大量上传''这个信号,换个场景就有别的用途。\n\n"
          "这一页讲得轻松一点,展示想象力。但别吹太满 —— 说清楚这些是设想,不是已实现的功能。")
eyebrow(s, "Beyond the washroom")
title(s, "The same signal answers other questions")
cards(s, [
    ("Confidential meetings",
     "Before a sensitive discussion, sweep the room for any device streaming out of it. "
     "The stimulus is the room's own lighting."),
    ("Classrooms and exams",
     "Sustained one-way uplink from a seat is a different signature from ordinary "
     "browsing — a proctoring aid that reads traffic shape, not content."),
], Inches(2.4), h=Inches(1.55), cols=2)
cards(s, [
    ("Changing rooms and clinics",
     "Any space where people undress or are examined, and where staff cannot inspect "
     "fixtures before every use."),
    ("Property handover",
     "A landlord or cleaning crew can sweep between guests, turning a reactive "
     "complaint process into a routine check."),
], Inches(4.1), h=Inches(1.55), cols=2)
body(s, "All reuse the same primitive: who is sending far more than they receive, and "
        "does it respond to this room?  None require decrypting anything.",
     Inches(5.85), size=14, col=INK)
text(s, M, Inches(6.5), CONTENT_W, Inches(0.3),
     [("These are directions, not shipped features.", 11, FAINT, False, MONO, 0)])

# ══════════════════════════════════════════════════ 14 conclusion
s = slide("我们没有声称机制新颖 —— 先行工作已经存在。我们的贡献是低成本独立实现、"
          "进门扫查场景、用房间自己的灯开关做刺激,以及量化了方法在什么情况下失效。\n\n"
          "结尾落在诚实上:能说清楚每一步的边界在哪,比声称''全做完了''更可信。")
eyebrow(s, "Conclusion")
title(s, "What we claim, and what we don't")
cards(s, [
    ("We do not claim novelty of the mechanism",
     "Traffic-analysis camera detection is established prior art — DeWiCam, CSI:DeSpy, "
     "SnoopDog, Lumos, LocCams."),
    ("What we contribute",
     "A low-cost ESP32 sensing front end with laptop-assisted analysis · the entry-sweep "
     "scenario · the room's own light switch as the stimulus · and measured numbers for "
     "where it fails."),
], Inches(2.3), h=Inches(1.6), cols=2)
metrics(s, [("Closed", "Asymmetry signal", "Demonstrated live over real RF", GOOD),
            ("Partial", "Stimulus correlation",
             "Simulation and pilot only — stand-in fidelity blocked the RF close", ACCENT),
            ("Bounded", "Coverage", "2.4 GHz, 802.11b/g/n, streaming cameras only", ALERT)],
        Inches(4.15), h=Inches(1.55))
body(s, "We would rather state the boundary precisely than overclaim past it — a sweeper "
        "that says UNKNOWN when it cannot see is more useful than one that says SAFE "
        "when it guessed.", Inches(5.95), size=15, col=INK)

# ══════════════════════════════════════════════════ 15 thanks
s = slide("准备好的问答在 PRESENTATION_NOTES.md 里:为什么相关性没做出来、"
          "怎么知道真摄像头是802.11n、视频通话算不算摄像头、频段限制怎么解决、"
          "扫描要多久、大流量上传会不会误报。")
eyebrow(s, "Thank you", Inches(1.9))
title(s, "Questions?", Inches(2.35), size=46)
cards(s, [
    ("Team",
     "David (Jiacheng) Wang — detection pipeline: ESP32 sniffer, uplink/downlink split, "
     "host correlator\n\nJustin Fang — BLE scanner and wireless reporting, live dashboard"),
    ("Code",
     "github.com/DavidWang1231/WashroomSweep\n\ngithub.com/justinfang37/washroom_security"),
], Inches(3.7), h=Inches(1.9), cols=2)

import sys
out = sys.argv[1] if len(sys.argv) > 1 else "WashroomSweep.pptx"
prs.save(out)
print(f"wrote {out} — {len(prs.slides.__iter__.__self__._sldIdLst)} slides")
