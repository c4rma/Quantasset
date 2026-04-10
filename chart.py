#!/usr/bin/env python3
"""
Quantasset Terminal Chart
TradingView-style terminal charting for ETH and BTC
Feeds: Phemex REST + WebSocket (hedged USDT perps)  [default, IP-bound]
       Kraken  REST + WebSocket v2 (USD pairs)       [use on Termux / non-whitelisted IPs]
Switch feeds live with [F]. Auth: reads PHEMEX_API_KEY / PHEMEX_API_SECRET from .env.
Flicker-free double-buffer curses rendering
"""

import curses
import hashlib
import hmac
import json
import math
import os
import sys
import time
import threading
import collections
from datetime import datetime, timezone
from pathlib import Path

# ── auto-install deps ──────────────────────────────────────────────────────────
def _ensure(pkg, import_as=None):
    import importlib, subprocess
    name = import_as or pkg.replace("-", "_")
    try:
        return importlib.import_module(name)
    except ImportError:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", pkg, "-q",
            "--break-system-packages"
        ])
        return importlib.import_module(name)

requests  = _ensure("requests")
websocket = _ensure("websocket-client", "websocket")

# ── .env loader ────────────────────────────────────────────────────────────────
def load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()

PHEMEX_API_KEY    = os.environ.get("PHEMEX_API_KEY", "")
PHEMEX_API_SECRET = os.environ.get("PHEMEX_API_SECRET", "")

# ── Phemex endpoints (updated Oct 2023) ───────────────────────────────────────
PHEMEX_REST_URL = "https://api.phemex.com"
PHEMEX_WS_URL   = "wss://ws.phemex.com"   # no path suffix — Phemex requirement

# ── asset config ───────────────────────────────────────────────────────────────
PHEMEX_SYMBOLS = {"ETH": "ETHUSDT", "BTC": "BTCUSDT"}

# Kraken config (no auth needed — fully public, no IP restrictions)
KRAKEN_REST_URL   = "https://api.kraken.com"
KRAKEN_WS_URL     = "wss://ws.kraken.com/v2"
KRAKEN_REST_PAIRS = {"ETH": "ETHUSD",  "BTC": "XBTUSD"}
KRAKEN_WS_PAIRS   = {"ETH": "ETH/USD", "BTC": "BTC/USD"}

FEEDS = ("phemex", "kraken")   # cycle order for [F] key

MAX_CANDLES   = 300
REST_LIMIT    = 200
RESOLUTION    = 60      # 1-minute candles (seconds)
REFRESH_DELAY = 0.05    # ~20 fps

# ── Phemex auth ────────────────────────────────────────────────────────────────
def _phemex_headers(path: str, query: str = "", body: str = "") -> dict:
    """Build signed headers. Kline REST is public but we send auth for rate-limit headroom."""
    if not PHEMEX_API_KEY:
        return {}
    expiry = str(int(time.time()) + 60)
    msg    = path + query + expiry + body
    sig    = hmac.new(PHEMEX_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "x-phemex-access-token":      PHEMEX_API_KEY,
        "x-phemex-request-expiry":    expiry,
        "x-phemex-request-signature": sig,
        "Content-Type":               "application/json",
    }

# ── color pairs ────────────────────────────────────────────────────────────────
C_BULL      = 1   # bullish  → white
C_BEAR      = 2   # bearish  → blue
C_AXIS      = 3
C_LABEL     = 4
C_HEADER    = 5
C_VOL_BULL  = 6   # blue
C_VOL_BEAR  = 7   # white/dim
C_ASSET_SEL = 9
C_PRICE_LBL = 10
C_CURSOR    = 11
C_VP_NORM   = 12   # volume profile bar (dim)
C_VP_POC    = 13   # point of control (brightest level)
C_VP_VA     = 14   # value area high/low lines

# ── data structures ────────────────────────────────────────────────────────────
class Candle:
    __slots__ = ("ts", "o", "h", "l", "c", "v", "closed")
    def __init__(self, ts, o, h, l, c, v, closed=False):
        self.ts     = int(ts)
        self.o      = float(o)
        self.h      = float(h)
        self.l      = float(l)
        self.c      = float(c)
        self.v      = float(v)
        self.closed = closed

class ChartState:
    def __init__(self):
        self.asset         = "ETH"
        self.feed          = "phemex"  # "phemex" | "kraken"
        self.candles       = collections.deque(maxlen=MAX_CANDLES)
        self.live          = None
        self.lock          = threading.Lock()
        self.ws            = None
        self.ws_thread     = None
        self.last_price    = 0.0
        self.status        = "Connecting..."
        self.error         = ""       # last REST/WS error for display
        self.session       = 0
        self.cursor_offset = 0
        self.color_scheme  = "bw"   # "bw" = blue/white  |  "rg" = red/green
        self.show_vp       = True    # volume profile overlay toggle

state = ChartState()

# ── Phemex REST history ────────────────────────────────────────────────────────
def fetch_phemex(asset: str) -> list:
    """
    GET /exchange/public/md/v2/kline/last
    Row format: [timestamp, interval, lastClose, open, high, low, close, volume, turnover]
    Phemex returns rows newest-first; we reverse to chronological order.
    """
    symbol = PHEMEX_SYMBOLS[asset]
    path   = "/exchange/public/md/v2/kline/last"
    query  = f"symbol={symbol}&resolution={RESOLUTION}&limit={REST_LIMIT}"
    url    = f"{PHEMEX_REST_URL}{path}?{query}"
    hdrs   = _phemex_headers(path, query)

    try:
        r = requests.get(url, headers=hdrs, timeout=12)
        r.raise_for_status()
        data = r.json()

        code = data.get("code", -1)
        if code != 0:
            with state.lock:
                state.error = f"REST code {code}: {data.get('msg','')}"
            return []

        rows = data.get("data", {}).get("rows", [])
        if not rows:
            with state.lock:
                state.error = "REST: empty rows"
            return []

        # rows are newest-first — reverse for chronological order
        candles = []
        for row in reversed(rows):
            # [ts, interval, lastClose, open, high, low, close, volume, turnover]
            candles.append(Candle(
                ts     = row[0],
                o      = row[3],
                h      = row[4],
                l      = row[5],
                c      = row[6],
                v      = row[7],
                closed = True,
            ))
        with state.lock:
            state.error = ""
        return candles

    except Exception as e:
        with state.lock:
            state.error = f"REST: {type(e).__name__}: {str(e)[:40]}"
        return []

# ── Kraken REST history ────────────────────────────────────────────────────────
def fetch_kraken(asset: str) -> list:
    """
    GET /0/public/OHLC — public, no auth, no IP restrictions.
    Row: [time, open, high, low, close, vwap, volume, count]
    Kraken returns up to 720 rows oldest-first.
    """
    pair = KRAKEN_REST_PAIRS[asset]
    url  = f"{KRAKEN_REST_URL}/0/public/OHLC"
    try:
        r = requests.get(url, params={"pair": pair, "interval": 1}, timeout=12)
        r.raise_for_status()
        data = r.json()
        errs = data.get("error", [])
        if errs:
            with state.lock:
                state.error = f"Kraken REST: {errs[0]}"
            return []
        result = data.get("result", {})
        rows   = next((v for k, v in result.items() if k != "last"), [])
        candles = []
        for row in rows[-REST_LIMIT:]:
            candles.append(Candle(
                ts=row[0], o=row[1], h=row[2], l=row[3],
                c=row[4], v=row[6], closed=True,
            ))
        with state.lock:
            state.error = ""
        return candles
    except Exception as e:
        with state.lock:
            state.error = f"Kraken REST: {type(e).__name__}: {str(e)[:40]}"
        return []

# ── Kraken WebSocket v2 ────────────────────────────────────────────────────────
def ws_kraken(asset: str, session: int):
    """
    wss://ws.kraken.com/v2 — public OHLC channel, 1-minute interval.
    Sends snapshot on subscribe, then incremental updates.
    Each item: { timestamp_open, open, high, low, close, volume, ... }
    No heartbeat required — Kraken manages keepalive via WS-level ping.
    """
    pair = KRAKEN_WS_PAIRS[asset]

    def stale() -> bool:
        return state.session != session

    def on_open(ws):
        ws.send(json.dumps({
            "method": "subscribe",
            "params": {"channel": "ohlc", "symbol": [pair], "interval": 1},
        }))
        with state.lock:
            if stale(): return
            state.status = "Live"
            state.error  = ""

    def on_message(ws, message):
        if stale(): return
        try:
            msg = json.loads(message)
        except Exception:
            return

        if msg.get("channel") != "ohlc":
            return
        msg_type = msg.get("type", "")
        if msg_type not in ("snapshot", "update"):
            return

        for item in msg.get("data", []):
            if stale(): return
            ts_str = item.get("timestamp_open", "")
            try:
                ts_open = int(datetime.fromisoformat(
                    ts_str.replace("Z", "+00:00")).timestamp())
            except Exception:
                ts_open = int(time.time()) // 60 * 60

            c = Candle(
                ts     = ts_open,
                o      = item["open"],
                h      = item["high"],
                l      = item["low"],
                c      = item["close"],
                v      = item["volume"],
                closed = False,
            )
            with state.lock:
                if stale(): return
                state.last_price = c.c
                if state.candles and state.candles[-1].ts == ts_open:
                    state.candles[-1] = c
                    state.live = None
                else:
                    if state.live and state.live.ts != ts_open:
                        prev        = state.live
                        prev.closed = True
                        state.candles.append(prev)
                    state.live = c
                state.status = "Live"
                state.error  = ""

    def on_error(ws, err):
        if stale(): return
        with state.lock:
            if stale(): return
            state.status = "WS Err"
            state.error  = str(err)[:50]

    def on_close(ws, code, msg):
        if stale(): return
        with state.lock:
            if stale(): return
            state.status = "Reconnecting..."

    backoff = 1
    while not stale():
        ws_app = websocket.WebSocketApp(
            KRAKEN_WS_URL,
            on_open    = on_open,
            on_message = on_message,
            on_error   = on_error,
            on_close   = on_close,
        )
        state.ws = ws_app
        ws_app.run_forever(ping_interval=30, ping_timeout=10)

        if stale():
            break
        with state.lock:
            if stale(): break
            state.status = f"Reconnecting... ({backoff}s)"
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

# ── Phemex WebSocket ───────────────────────────────────────────────────────────
def ws_phemex(asset: str, session: int):
    """
    WebSocket endpoint: wss://ws.phemex.com  (updated Oct 2023, no path suffix)
    Subscribe: kline_p.subscribe for hedged USDT perps.

    Message format (snapshot + incremental):
    {
      "kline_p": [[ts, interval, lastClose, open, high, low, close, volume, turnover], ...],
      "symbol": "ETHUSDT",
      "type": "snapshot" | "incremental"
    }

    Snapshot arrives newest-first. Incremental is the current open candle (single row).
    Heartbeat: send server.ping every 20s or DataGW drops the connection.
    """
    symbol = PHEMEX_SYMBOLS[asset]

    def stale() -> bool:
        return state.session != session

    def on_open(ws):
        sub = json.dumps({
            "id": 1,
            "method": "kline_p.subscribe",
            "params": [symbol, RESOLUTION]
        })
        ws.send(sub)
        with state.lock:
            if stale(): return
            state.status = "Live"
            state.error  = ""

    def on_message(ws, message):
        if stale(): return
        try:
            msg = json.loads(message)
        except Exception:
            return

        # Subscription ack
        if msg.get("result") == {"status": "success"}:
            return

        # Pong
        if msg.get("result") == "pong":
            return

        klines   = msg.get("kline_p")
        msg_type = msg.get("type", "incremental")
        sym      = msg.get("symbol", "")

        if not klines:
            return
        if sym and sym != symbol:
            return

        with state.lock:
            if stale(): return

            if msg_type == "snapshot":
                # Snapshot: newest-first → sort ascending by timestamp
                rows = sorted(klines, key=lambda r: r[0])
                for row in rows[-REST_LIMIT:]:
                    ts = row[0]
                    c  = Candle(ts=ts, o=row[3], h=row[4], l=row[5],
                                c=row[6], v=row[7], closed=True)
                    if not state.candles or ts > state.candles[-1].ts:
                        state.candles.append(c)
                    elif ts == state.candles[-1].ts:
                        state.candles[-1] = c
                if state.candles:
                    state.last_price = state.candles[-1].c
                state.status = "Live"
                state.error  = ""

            else:
                # Incremental: single current open candle row
                for row in klines:
                    ts = row[0]
                    c  = Candle(ts=ts, o=row[3], h=row[4], l=row[5],
                                c=row[6], v=row[7], closed=False)
                    state.last_price = c.c

                    if state.candles and state.candles[-1].ts == ts:
                        state.candles[-1] = c
                        state.live = None
                    else:
                        if state.live and state.live.ts != ts:
                            prev        = state.live
                            prev.closed = True
                            state.candles.append(prev)
                        state.live = c
                    state.status = "Live"

    # ── app-level heartbeat ───────────────────────────────────────────────────
    # Phemex DataGW requires a JSON server.ping every <30s on the data channel.
    # websocket-client's built-in ping_interval sends a WS-level ping frame,
    # which is separate — Phemex needs the app-level JSON ping as well.
    _ping_ws   = [None]   # mutable ref so the timer can access current ws_app
    _ping_stop = threading.Event()

    def _heartbeat():
        while not _ping_stop.wait(timeout=20):
            if stale():
                break
            ws = _ping_ws[0]
            if ws:
                try:
                    ws.send(json.dumps({"id": 0, "method": "server.ping", "params": []}))
                except Exception:
                    pass

    hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    hb_thread.start()

    def on_error(ws, err):
        if stale(): return
        with state.lock:
            if stale(): return
            state.status = "WS Err"
            state.error  = str(err)[:50]

    def on_close(ws, code, msg):
        if stale(): return
        with state.lock:
            if stale(): return
            state.status = "Reconnecting..."

    # ── reconnect loop ────────────────────────────────────────────────────────
    # run_forever() returns when the socket drops. We loop with backoff so the
    # connection is automatically restored without any user intervention.
    backoff = 1
    while not stale():
        ws_app = websocket.WebSocketApp(
            PHEMEX_WS_URL,
            on_open    = on_open,
            on_message = on_message,
            on_error   = on_error,
            on_close   = on_close,
        )
        state.ws    = ws_app
        _ping_ws[0] = ws_app

        ws_app.run_forever(
            ping_interval = 25,    # WS-level keepalive frame
            ping_timeout  = 10,
        )

        # run_forever returned — connection dropped
        if stale():
            break

        with state.lock:
            if stale(): break
            state.status = f"Reconnecting... ({backoff}s)"

        time.sleep(backoff)
        backoff = min(backoff * 2, 30)   # cap at 30s

    _ping_stop.set()
    _ping_ws[0] = None

# ── feed manager ──────────────────────────────────────────────────────────────
def start_feed(asset: str, feed: str = ""):
    """Start (or restart) the data feed. feed="" means keep state.feed unchanged."""
    old_ws = state.ws
    if old_ws:
        try:
            old_ws.close()
        except Exception:
            pass

    with state.lock:
        if feed:
            state.feed = feed
        my_feed        = state.feed
        state.session      += 1
        my_session          = state.session
        state.status        = "Loading..."
        state.error         = ""
        state.candles.clear()
        state.live          = None
        state.last_price    = 0.0
        state.cursor_offset = 0

    # Route REST fetch to the right source
    if my_feed == "kraken":
        candles = fetch_kraken(asset)
    else:
        candles = fetch_phemex(asset)

    with state.lock:
        if state.session != my_session:
            return
        for c in candles:
            state.candles.append(c)
        if candles:
            state.last_price = candles[-1].c
        state.status = "Connecting..." if candles else "REST failed"

    # Route WS to the right source
    ws_fn = ws_kraken if my_feed == "kraken" else ws_phemex
    t = threading.Thread(target=ws_fn, args=(asset, my_session), daemon=True)
    t.start()
    state.ws_thread = t

# ── colors ────────────────────────────────────────────────────────────────────
def init_colors(scheme: str = "bw"):
    """scheme: 'bw' = white bull / blue bear  |  'rg' = green bull / red bear"""
    curses.start_color()
    curses.use_default_colors()
    if scheme == "rg":
        bull_fg, bear_fg = curses.COLOR_GREEN, curses.COLOR_RED
    else:
        bull_fg, bear_fg = curses.COLOR_WHITE, curses.COLOR_BLUE
    curses.init_pair(C_BULL,      bull_fg,             -1)
    curses.init_pair(C_BEAR,      bear_fg,             -1)
    curses.init_pair(C_AXIS,      8,                   -1)
    curses.init_pair(C_LABEL,     curses.COLOR_WHITE,  -1)
    curses.init_pair(C_HEADER,    curses.COLOR_WHITE,  curses.COLOR_BLACK)
    curses.init_pair(C_VOL_BULL,  bull_fg,             -1)
    curses.init_pair(C_VOL_BEAR,  bear_fg,             -1)
    curses.init_pair(C_ASSET_SEL, curses.COLOR_BLACK,  curses.COLOR_CYAN)
    curses.init_pair(C_PRICE_LBL, curses.COLOR_BLACK,  curses.COLOR_CYAN)
    curses.init_pair(C_CURSOR,    curses.COLOR_BLACK,  curses.COLOR_WHITE)
    curses.init_pair(C_VP_NORM,   curses.COLOR_CYAN,   -1)
    curses.init_pair(C_VP_POC,    curses.COLOR_YELLOW, -1)
    curses.init_pair(C_VP_VA,     curses.COLOR_MAGENTA,-1)

# ── formatting ────────────────────────────────────────────────────────────────
def price_fmt(p: float, asset: str) -> str:
    return f"{p:,.1f}" if asset == "BTC" else f"{p:,.2f}"

def vol_fmt(v: float) -> str:
    if v >= 1_000_000: return f"{v/1_000_000:.2f}M"
    if v >= 1_000:     return f"{v/1_000:.1f}K"
    return f"{v:.2f}"

def smart_price_labels(lo: float, hi: float, asset: str) -> list:
    span = hi - lo
    if span <= 0:
        return []
    raw_step  = span / 7
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step      = magnitude * min([1, 2, 2.5, 5, 10],
                                key=lambda x: abs(x * magnitude - raw_step))
    first     = math.ceil(lo / step) * step
    labels, p = [], first
    while p <= hi + step * 0.01:
        labels.append((p, price_fmt(p, asset)))
        p = round(p + step, 10)
    return labels

# ── double-buffer ──────────────────────────────────────────────────────────────
EMPTY_CELL = (" ", 0, 0)

class DoubleBuffer:
    def __init__(self, rows, cols):
        self.rows = rows
        self.cols = cols
        self.buf  = [[EMPTY_CELL] * cols for _ in range(rows)]
        self.prev = None

    def put(self, row, col, ch, pair=0, attrs=0):
        if 0 <= row < self.rows and 0 <= col < self.cols:
            self.buf[row][col] = (ch, pair, attrs)

    def puts(self, row, col, s, pair=0, attrs=0):
        for i, ch in enumerate(s):
            self.put(row, col + i, ch, pair, attrs)

    def flush(self, win):
        prev = self.prev
        for r in range(self.rows):
            for c in range(self.cols):
                cell = self.buf[r][c]
                if prev is None or prev[r][c] != cell:
                    ch, pair, attrs = cell
                    try:
                        win.addch(r, c, ch, curses.color_pair(pair) | attrs)
                    except curses.error:
                        pass
        self.prev = [row[:] for row in self.buf]
        self.buf  = [[EMPTY_CELL] * self.cols for _ in range(self.rows)]

# ── draw frame ────────────────────────────────────────────────────────────────
def draw(win, db: DoubleBuffer, rows: int, cols: int):
    with state.lock:
        candles_snap  = list(state.candles)
        live          = state.live
        asset         = state.asset
        feed          = state.feed
        last_price    = state.last_price
        status        = state.status
        error         = state.error
        cursor_offset = state.cursor_offset
        color_scheme  = state.color_scheme
        show_vp       = state.show_vp

    all_candles = candles_snap + ([live] if live else [])

    # ── layout ────────────────────────────────────────────────────────────────
    HEADER_H = 3
    PRICE_W  = 12
    VOL_H    = 6
    TIME_H   = 1
    FOOTER_H = 1

    chart_top  = HEADER_H
    chart_bot  = rows - VOL_H - TIME_H - FOOTER_H - 2
    chart_h    = max(1, chart_bot - chart_top)
    chart_r    = cols - PRICE_W - 1
    chart_w    = max(1, chart_r)
    # Layout (rows, top→bottom):
    #   chart_top … chart_bot   chart area
    #   chart_bot               separator line
    #   chart_bot+1             time axis labels  ← between chart and VOL
    #   chart_bot+2 … +2+VOL_H volume bars
    #   rows-1                  footer
    time_row   = chart_bot + 1
    vol_top    = chart_bot + 2
    vol_bot    = vol_top + VOL_H
    footer_row = rows - 1

    visible    = all_candles[-chart_w:] if all_candles else []

    n_vis = len(visible)


    # ── cursor ────────────────────────────────────────────────────────────────
    max_offset    = -(len(visible) - 1) if visible else 0
    cursor_offset = max(max_offset, min(0, cursor_offset))
    with state.lock:
        state.cursor_offset = cursor_offset

    if visible:
        cursor_idx = len(visible) - 1 + cursor_offset
        cursor_idx = max(0, min(len(visible) - 1, cursor_idx))
        cursor_col = chart_r - len(visible) + cursor_idx
        selected   = visible[cursor_idx]
    else:
        cursor_idx = 0
        cursor_col = -1
        selected   = None

    in_cursor_mode = cursor_offset != 0

    # ── price range ───────────────────────────────────────────────────────────
    if visible:
        lo_p = min(c.l for c in visible)
        hi_p = max(c.h for c in visible)
        span = hi_p - lo_p
        pad  = max(span * 0.05, hi_p * 0.0005)
        lo_p -= pad
        hi_p += pad
    else:
        lo_p, hi_p = 0.0, 1.0

    def p2r(price):
        if hi_p == lo_p: return chart_h // 2
        return int((1.0 - (price - lo_p) / (hi_p - lo_p)) * (chart_h - 1))

    max_vol = max((c.v for c in visible), default=1) or 1

    # ── HEADER ────────────────────────────────────────────────────────────────
    for r in range(HEADER_H):
        db.puts(r, 0, " " * cols, C_HEADER, curses.A_BOLD)

    db.puts(0, 2, "Q U A N T A S S E T  |  Terminal Chart", C_HEADER, curses.A_BOLD)

    badge = f" {feed.upper()} "
    db.puts(0, cols - len(badge) - 22, badge, C_ASSET_SEL, curses.A_BOLD)

    now_str = datetime.now().strftime("%H:%M:%S")
    # Countdown: seconds remaining until next 1-min candle close
    now_epoch   = int(time.time())
    secs_left   = RESOLUTION - (now_epoch % RESOLUTION)
    countdown   = f"  {secs_left:02d}s" if status == "Live" else ""
    status_str  = f"| {status}{countdown}  1m  {now_str} "
    db.puts(0, cols - len(status_str) - 1, status_str,
            C_BULL if status == "Live" else C_BEAR, curses.A_BOLD)

    col = 2
    for a in ("ETH", "BTC"):
        lbl = f" {a}/USDT "
        db.puts(1, col, lbl, C_ASSET_SEL if a == asset else C_HEADER, curses.A_BOLD)
        col += len(lbl) + 1
    db.puts(1, col + 1, "[E]TH  [B]TC  [F]eed  [C]olor  [V]P  [<][>] cursor  [Esc] reset  [Q]uit", C_HEADER)

    # Error line — shown in row 2 when no candles, otherwise OHLCV
    if not visible and error:
        db.puts(2, 2, f"ERR: {error}", C_BEAR, curses.A_BOLD)
    elif not visible and status not in ("Live",):
        db.puts(2, 2, status, C_BEAR, curses.A_BOLD)

    display_candle = selected if selected else (visible[-1] if visible else None)
    if display_candle:
        lc    = display_candle
        bull  = lc.c >= lc.o
        chg   = lc.c - lc.o
        pct   = chg / lc.o * 100 if lc.o else 0
        arrow = "+" if bull else "-"
        # Cursor time: directly from the selected candle's timestamp (local time)
        if in_cursor_mode and selected:
            ts_lbl = datetime.fromtimestamp(selected.ts).strftime("%H:%M:%S")
        else:
            ts_lbl = ""
        cursor_tag = f"  [{ts_lbl}]" if in_cursor_mode else ""
        ohlcv = (f"  O {price_fmt(lc.o, asset)}"
                 f"  H {price_fmt(lc.h, asset)}"
                 f"  L {price_fmt(lc.l, asset)}"
                 f"  C {price_fmt(lc.c, asset)}"
                 f"  {arrow}{abs(chg):.2f} ({pct:+.2f}%)"
                 f"  Vol {vol_fmt(lc.v)}"
                 f"{cursor_tag}")
        ohlc_pair = C_CURSOR if in_cursor_mode else (C_BULL if bull else C_BEAR)
        db.puts(2, 2, ohlcv, ohlc_pair, curses.A_BOLD)

    # ── PRICE AXIS ────────────────────────────────────────────────────────────
    price_labels = smart_price_labels(lo_p, hi_p, asset)
    for r in range(chart_top, chart_bot + 1):
        db.put(r, chart_r, "|", C_AXIS)
    # (price axis | through vol pane drawn inside VOLUME section)

    for price, lbl in price_labels:
        gr = chart_top + p2r(price)
        if chart_top <= gr < chart_bot:
            db.put(gr, chart_r, "+", C_AXIS)
            db.puts(gr, chart_r + 1, f"{lbl:<{PRICE_W}}", C_LABEL)

    if not in_cursor_mode and last_price and lo_p < last_price < hi_p:
        pr = chart_top + p2r(last_price)
        if chart_top <= pr < chart_bot:
            for c2 in range(chart_r):
                if db.buf[pr][c2][0] == " ":
                    db.put(pr, c2, "-", C_PRICE_LBL)
            db.puts(pr, chart_r,     ">", C_PRICE_LBL, curses.A_BOLD)
            db.puts(pr, chart_r + 1, f"{price_fmt(last_price, asset):<{PRICE_W}}",
                    C_PRICE_LBL, curses.A_BOLD)

    if in_cursor_mode and selected and lo_p < selected.c < hi_p:
        pr = chart_top + p2r(selected.c)
        if chart_top <= pr < chart_bot:
            for c2 in range(chart_r):
                if db.buf[pr][c2][0] == " ":
                    db.put(pr, c2, "-", C_CURSOR)
            db.puts(pr, chart_r,     ">", C_CURSOR, curses.A_BOLD)
            db.puts(pr, chart_r + 1, f"{price_fmt(selected.c, asset):<{PRICE_W}}",
                    C_CURSOR, curses.A_BOLD)

    # ── VOLUME PROFILE (session: today 00:00 local time → now) ──────────────
    # Drawn behind candles so candles always overwrite profile bars.
    # Price buckets = chart rows; each candle's volume is distributed across
    # the rows it spans (high→low), proportional to the row count it covers.
    if show_vp and visible and chart_h > 0:
        # Session start: most recent 19:00 CT (futures session open).
        # If current local time is before 19:00 today, use yesterday's 19:00.
        _now_local = datetime.now()
        _session_today = datetime(
            _now_local.year, _now_local.month, _now_local.day, 19, 0, 0)
        if _now_local < _session_today:
            # Before 19:00 today — session started yesterday at 19:00
            import datetime as _dt
            _session_today -= _dt.timedelta(days=1)
        session_start = _session_today.timestamp()

        # Accumulate volume per price bucket using ALL loaded candles in session
        vp_buckets = [0.0] * chart_h   # index 0 = top row (high price)

        # Use full all_candles (not just visible) for session scope
        session_candles = [c for c in all_candles if c.ts >= session_start]

        if session_candles and lo_p < hi_p:
            for c in session_candles:
                b_hi = int((1.0 - (c.h - lo_p) / (hi_p - lo_p)) * (chart_h - 1))
                b_lo = int((1.0 - (c.l - lo_p) / (hi_p - lo_p)) * (chart_h - 1))
                b_hi = max(0, min(chart_h - 1, b_hi))
                b_lo = max(0, min(chart_h - 1, b_lo))
                span = max(1, b_lo - b_hi + 1)
                vol_per_row = c.v / span
                for b in range(b_hi, b_lo + 1):
                    vp_buckets[b] += vol_per_row

            max_vp    = max(vp_buckets) or 1.0
            total_vol = sum(vp_buckets)
            VP_MAX_W  = max(4, chart_w // 5)
            poc_row   = vp_buckets.index(max_vp)

            # ── Value Area (70% of session volume) ────────────────────────────
            # Standard algorithm: start at POC, expand up or down one bucket
            # at a time, always choosing the side with more volume, until
            # accumulated volume >= 70% of total.
            TARGET    = total_vol * 0.70
            va_accum  = vp_buckets[poc_row]
            va_top    = poc_row   # lowest bucket index = highest price (row 0 = top)
            va_bot    = poc_row   # highest bucket index = lowest price

            while va_accum < TARGET:
                # Volume available one step above (lower index) and below (higher index)
                above_vol = vp_buckets[va_top - 1] if va_top > 0                else 0.0
                below_vol = vp_buckets[va_bot + 1] if va_bot < chart_h - 1     else 0.0
                if above_vol == 0 and below_vol == 0:
                    break
                if above_vol >= below_vol:
                    va_top   -= 1
                    va_accum += above_vol
                else:
                    va_bot   += 1
                    va_accum += below_vol

            # va_top = row index of VAH (highest price in value area)
            # va_bot = row index of VAL (lowest price in value area)
            vah_row = va_top
            val_row = va_bot

            # Convert bucket rows back to prices for axis labels
            def row_to_price(b):
                return lo_p + (1.0 - b / (chart_h - 1)) * (hi_p - lo_p)

            vah_price = row_to_price(vah_row)
            val_price = row_to_price(val_row)

            # ── Draw profile bars ─────────────────────────────────────────────
            for b, bvol in enumerate(vp_buckets):
                if bvol <= 0:
                    continue
                bar_w = max(1, int(bvol / max_vp * VP_MAX_W))
                row   = chart_top + b
                if not (chart_top <= row < chart_bot):
                    continue
                if b == poc_row:
                    pair, attrs, char = C_VP_POC, curses.A_BOLD, "="
                elif vah_row <= b <= val_row:
                    pair, attrs, char = C_VP_NORM, curses.A_NORMAL, "-"
                else:
                    pair, attrs, char = C_VP_NORM, curses.A_DIM, "-"
                for c2 in range(bar_w):
                    if 0 <= c2 < chart_r:
                        db.put(row, c2, char, pair, attrs)

            # ── Draw VAH line ─────────────────────────────────────────────────
            vah_draw = chart_top + vah_row
            if chart_top <= vah_draw < chart_bot:
                for c2 in range(chart_r):
                    if db.buf[vah_draw][c2][0] in (" ", "-"):
                        db.put(vah_draw, c2, "~", C_VP_VA, curses.A_BOLD)
                db.puts(vah_draw, chart_r, "+", C_VP_VA, curses.A_BOLD)
                vah_lbl = f"VAH {price_fmt(vah_price, asset)}"
                db.puts(vah_draw, chart_r + 1, f"{vah_lbl:<{PRICE_W}}", C_VP_VA, curses.A_BOLD)

            # ── Draw VAL line ─────────────────────────────────────────────────
            val_draw = chart_top + val_row
            if chart_top <= val_draw < chart_bot and val_draw != vah_draw:
                for c2 in range(chart_r):
                    if db.buf[val_draw][c2][0] in (" ", "-"):
                        db.put(val_draw, c2, "~", C_VP_VA, curses.A_BOLD)
                db.puts(val_draw, chart_r, "+", C_VP_VA, curses.A_BOLD)
                val_lbl = f"VAL {price_fmt(val_price, asset)}"
                db.puts(val_draw, chart_r + 1, f"{val_lbl:<{PRICE_W}}", C_VP_VA, curses.A_BOLD)

            # ── Draw POC line (on top of bars, after VA lines) ────────────────
            poc_draw = chart_top + poc_row
            if chart_top <= poc_draw < chart_bot:
                for c2 in range(chart_r):
                    if db.buf[poc_draw][c2][0] in (" ", "-", "~", "="):
                        db.put(poc_draw, c2, "=", C_VP_POC, curses.A_BOLD)
                db.puts(poc_draw, chart_r, "+", C_VP_POC, curses.A_BOLD)
                poc_price = row_to_price(poc_row)
                poc_lbl   = f"POC {price_fmt(poc_price, asset)}"
                db.puts(poc_draw, chart_r + 1, f"{poc_lbl:<{PRICE_W}}", C_VP_POC, curses.A_BOLD)

    # ── CANDLES ───────────────────────────────────────────────────────────────
    start_col = chart_r - len(visible)
    for i, candle in enumerate(visible):
        col = start_col + i
        if not (0 <= col < chart_r):
            continue

        is_selected = (i == cursor_idx and in_cursor_mode)
        bull        = candle.c >= candle.o
        body_pair   = C_CURSOR if is_selected else (C_BULL if bull else C_BEAR)

        clamp = lambda v: max(chart_top, min(chart_bot - 1, v))
        r_hi  = clamp(chart_top + p2r(candle.h))
        r_top = clamp(chart_top + p2r(max(candle.o, candle.c)))
        r_bot = clamp(chart_top + p2r(min(candle.o, candle.c)))
        r_lo  = clamp(chart_top + p2r(candle.l))

        for r in range(r_hi, r_top):
            db.put(r, col, "|", body_pair)
        if r_top == r_bot:
            db.put(r_top, col, "-", body_pair, curses.A_BOLD)
        else:
            for r in range(r_top, r_bot + 1):
                db.put(r, col, "#", body_pair)
        for r in range(r_bot + 1, r_lo + 1):
            db.put(r, col, "|", body_pair)

        if is_selected:
            for r in range(chart_top, r_hi):
                db.put(r, col, ":", C_AXIS, curses.A_DIM)
            for r in range(r_lo + 1, chart_bot):
                db.put(r, col, ":", C_AXIS, curses.A_DIM)

    # ── SEPARATOR ─────────────────────────────────────────────────────────────
    db.puts(chart_bot, 0, "-" * chart_r + "+", C_AXIS)

    # ── VOLUME ────────────────────────────────────────────────────────────────
    # vol_top is now the bottom border of the time axis box.
    # VOL bars live in vol_top+1 … vol_bot.
    vol_bar_top = vol_top + 1
    db.puts(vol_bar_top, 0, "VOL", C_AXIS, curses.A_DIM)
    # Also extend the price axis | through the vol pane
    for r in range(vol_bar_top, min(vol_bot + 1, rows)):
        db.put(r, chart_r, "|", C_AXIS)
    for i, candle in enumerate(visible):
        col = start_col + i
        if not (0 <= col < chart_r):
            continue
        is_selected = (i == cursor_idx and in_cursor_mode)
        pair        = C_CURSOR if is_selected else \
                      (C_VOL_BULL if candle.c >= candle.o else C_VOL_BEAR)
        bar_h = max(1, int(candle.v / max_vol * VOL_H))
        for r in range(vol_bot - bar_h, vol_bot):
            if 0 <= r < rows:
                db.put(r, col, "#", pair)

    # ── TIME AXIS (between chart separator and VOL) ──────────────────────────
    # time_row = chart_bot+1: 15-min interval labels, bordered left/right.
    # vol_top  = chart_bot+2: bottom border line, then VOL bars below.
    if 0 <= time_row < rows:
        # Right border | on the time label row
        db.put(time_row, chart_r, "|", C_AXIS)

        if visible:
            MIN_LABEL_GAP  = 6
            last_label_col = -MIN_LABEL_GAP - 1

            for i, candle in enumerate(visible):
                lbl_col = start_col + i
                if not (0 <= lbl_col <= chart_r - 5):
                    continue
                # Use candle.ts directly — fromtimestamp converts UTC epoch to local time
                dt = datetime.fromtimestamp(candle.ts)
                if dt.minute % 15 == 0 and lbl_col - last_label_col >= MIN_LABEL_GAP:
                    lbl = dt.strftime("%H:%M")
                    db.puts(time_row, lbl_col, lbl, C_LABEL, curses.A_BOLD)
                    last_label_col = lbl_col

            # Cursor label — from selected candle's actual ts
            if in_cursor_mode and selected and 0 <= cursor_col <= chart_r - 5:
                lbl = datetime.fromtimestamp(selected.ts).strftime("%H:%M")
                db.puts(time_row, cursor_col, lbl, C_CURSOR, curses.A_BOLD)

    # Bottom border of the time axis box (top of VOL pane)
    if 0 <= vol_top < rows:
        db.puts(vol_top, 0, "-" * chart_r + "+", C_AXIS)

    # ── FOOTER ────────────────────────────────────────────────────────────────
    if 0 <= footer_row < rows:
        cinfo    = f"  cursor {cursor_offset:+d}" if in_cursor_mode else ""
        auth_tag = f"/{('auth' if PHEMEX_API_KEY else 'no-auth')}" if feed == "phemex" else ""
        err_str  = f"  ! {error}" if error and visible else ""
        scheme_tag = "B/W" if color_scheme == "bw" else "R/G"
        vp_tag     = "VP:ON" if show_vp else "VP:OFF"
        footer   = (f" [E] ETH  [B] BTC  [F] Feed: {feed.upper()}  [C] {scheme_tag}  [V] {vp_tag}  [Q] Quit"
                    f"   {len(all_candles)} candles  {feed.upper()}{auth_tag}{cinfo}{err_str}")
        db.puts(footer_row, 0, footer.ljust(cols)[:cols], C_AXIS)

# ── main loop ────────────────────────────────────────────────────────────────
def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    init_colors(state.color_scheme)

    rows, cols = stdscr.getmaxyx()
    db = DoubleBuffer(rows, cols)

    threading.Thread(target=start_feed, args=(state.asset,), daemon=True).start()

    while True:
        key = stdscr.getch()

        if key in (ord("q"), ord("Q")):
            break

        new_asset = None
        new_feed  = None
        if   key in (ord("e"), ord("E")) and state.asset != "ETH":
            new_asset = "ETH"
        elif key in (ord("b"), ord("B")) and state.asset != "BTC":
            new_asset = "BTC"
        elif key in (ord("f"), ord("F")):
            # Cycle to next feed
            idx      = FEEDS.index(state.feed)
            new_feed = FEEDS[(idx + 1) % len(FEEDS)]
        elif key in (ord("c"), ord("C")):
            with state.lock:
                state.color_scheme = "rg" if state.color_scheme == "bw" else "bw"
            # Reinit color pairs and invalidate the entire double-buffer cache
            # so every cell is redrawn fresh with the new scheme next frame.
            init_colors(state.color_scheme)
            db.prev = None
            stdscr.clear()
        elif key in (ord("v"), ord("V")):
            with state.lock:
                state.show_vp = not state.show_vp
            db.prev = None
            stdscr.clear()

        if new_asset:
            state.asset = new_asset
            threading.Thread(target=start_feed, args=(new_asset,), daemon=True).start()
        elif new_feed:
            threading.Thread(target=start_feed, args=(state.asset, new_feed), daemon=True).start()
        elif key == curses.KEY_LEFT:
            with state.lock:
                state.cursor_offset -= 1
        elif key == curses.KEY_RIGHT:
            with state.lock:
                state.cursor_offset = min(0, state.cursor_offset + 1)
        elif key == 27:  # Escape
            with state.lock:
                state.cursor_offset = 0

        new_rows, new_cols = stdscr.getmaxyx()
        if new_rows != rows or new_cols != cols:
            rows, cols = new_rows, new_cols
            db = DoubleBuffer(rows, cols)
            stdscr.clear()

        draw(stdscr, db, rows, cols)
        db.flush(stdscr)
        stdscr.refresh()
        time.sleep(REFRESH_DELAY)

if __name__ == "__main__":
    curses.wrapper(main)
