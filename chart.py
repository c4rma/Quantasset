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

def cur_interval():
    return INTERVALS[state.interval_idx] if hasattr(state, "interval_idx") else INTERVALS[0]

def cur_resolution():
    """Phemex resolution in seconds for current interval."""
    return cur_interval()[1]

def cur_kraken_interval():
    """Kraken interval in minutes."""
    return cur_interval()[2]

def cur_ws_interval():
    """WebSocket subscription interval in minutes (Kraken v2 ohlc channel)."""
    return cur_interval()[3]

def cur_label():
    return cur_interval()[0]

MAX_CANDLES   = 2000    # expanded for scroll-back
REST_LIMIT    = 500     # fetch up to 500 candles per request
REFRESH_DELAY = 0.04    # ~25 fps (slightly faster for smoother scroll)

# Interval table: label → (phemex_resolution_secs, kraken_interval_mins, ws_interval_mins)
INTERVALS = [
    ("1m",   60,     1,   1),
    ("3m",   180,    3,   3),
    ("15m",  900,    15,  15),
    ("1H",   3600,   60,  60),
    ("4H",   14400,  240, 240),
    ("1D",   86400,  1440,1440),
]
INTERVAL_IDX = 0   # default: 1m

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
C_LINE      = 15   # line chart
C_VWAP      = 16   # VWAP line
C_VWAP_BAND = 17   # 0.5σ shaded band
C_VWAP_SD2  = 18   # 2σ / 2.5σ bands

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
        # TV-style navigation: two independent values
        # view_offset: how far viewport is panned left (>=0, 0=live)
        # cursor_col_idx: cursor column within visible window
        self.cursor_offset    = 0   # keep for compat, unused now
        self.view_offset      = 0   # pan: candles hidden on right
        self.cursor_col_idx   = -1  # -1 = no cursor (live mode)
        self.color_scheme  = "bw"   # "bw" = blue/white  |  "rg" = red/green
        self.show_vp       = True    # volume profile overlay toggle
        self.show_vwap    = True    # VWAP + SD bands overlay toggle
        self.interval_idx  = 0       # index into INTERVALS list
        self.history_loading = False # True while background history fetch running
        self.chart_mode    = "candle" # "candle" | "line"

state = ChartState()

# ── Phemex REST history ────────────────────────────────────────────────────────
def fetch_phemex(asset: str, before_ts: int = 0) -> list:
    """
    Fetch up to REST_LIMIT candles ending at before_ts (0 = latest).
    Uses /kline/last for latest, /kline/list with to= for historical scroll-back.
    Row: [timestamp, interval, lastClose, open, high, low, close, volume, turnover]
    """
    symbol     = PHEMEX_SYMBOLS[asset]
    resolution = cur_resolution()

    if before_ts:
        path  = "/exchange/public/md/v2/kline/list"
        query = (f"symbol={symbol}&resolution={resolution}"
                 f"&from={before_ts - REST_LIMIT * resolution}&to={before_ts}"
                 f"&limit={REST_LIMIT}")
    else:
        path  = "/exchange/public/md/v2/kline/last"
        query = f"symbol={symbol}&resolution={resolution}&limit={REST_LIMIT}"

    url  = f"{PHEMEX_REST_URL}{path}?{query}"
    hdrs = _phemex_headers(path, query)

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

        # /kline/last returns newest-first; /kline/list returns oldest-first
        if before_ts:
            ordered = rows
        else:
            ordered = list(reversed(rows))

        candles = []
        for row in ordered:
            candles.append(Candle(
                ts=row[0], o=row[3], h=row[4], l=row[5],
                c=row[6],  v=row[7], closed=True,
            ))
        with state.lock:
            state.error = ""
        return candles

    except Exception as e:
        with state.lock:
            state.error = f"REST: {type(e).__name__}: {str(e)[:40]}"
        return []

# ── Kraken REST history ────────────────────────────────────────────────────────
def fetch_kraken(asset: str, before_ts: int = 0) -> list:
    """
    GET /0/public/OHLC with since= for historical scroll-back.
    Row: [time, open, high, low, close, vwap, volume, count]
    Kraken returns oldest-first from `since` timestamp.
    """
    pair     = KRAKEN_REST_PAIRS[asset]
    interval = cur_kraken_interval()
    url      = f"{KRAKEN_REST_URL}/0/public/OHLC"
    params   = {"pair": pair, "interval": interval}
    if before_ts:
        # Fetch starting from enough back to fill REST_LIMIT candles
        params["since"] = before_ts - REST_LIMIT * interval * 60
    try:
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
        errs = data.get("error", [])
        if errs:
            with state.lock:
                state.error = f"Kraken REST: {errs[0]}"
            return []
        result = data.get("result", {})
        rows   = next((v for k, v in result.items() if k != "last"), [])
        if before_ts:
            rows = [r for r in rows if r[0] < before_ts]
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
            "params": {"channel": "ohlc", "symbol": [pair], "interval": cur_ws_interval()},
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
            "params": [symbol, cur_resolution()]
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
def start_feed(asset: str, feed: str = "", interval_idx: int = -1):
    """Start (or restart) the data feed. feed/interval_idx="" means keep current."""
    old_ws = state.ws
    if old_ws:
        try:
            old_ws.close()
        except Exception:
            pass

    with state.lock:
        if feed:
            state.feed = feed
        if interval_idx >= 0:
            state.interval_idx = interval_idx
        my_feed             = state.feed
        state.session      += 1
        my_session          = state.session
        state.status        = "Loading..."
        state.error         = ""
        state.candles.clear()
        state.live          = None
        state.last_price    = 0.0
        state.history_loading = False

    candles = fetch_kraken(asset) if my_feed == "kraken" else fetch_phemex(asset)

    with state.lock:
        if state.session != my_session:
            return
        for c in candles:
            state.candles.append(c)
        if candles:
            state.last_price = candles[-1].c
        state.status = "Connecting..." if candles else "REST failed"

    ws_fn = ws_kraken if my_feed == "kraken" else ws_phemex
    t = threading.Thread(target=ws_fn, args=(asset, my_session), daemon=True)
    t.start()
    state.ws_thread = t


def fetch_history_before(session: int):
    """Background: prepend older candles when user scrolls past the left edge."""
    with state.lock:
        if not state.candles or state.session != session:
            state.history_loading = False
            return
        oldest_ts = state.candles[0].ts
        my_feed   = state.feed
        asset     = state.asset

    candles = (fetch_kraken(asset, before_ts=oldest_ts)
               if my_feed == "kraken"
               else fetch_phemex(asset, before_ts=oldest_ts))

    # Filter to only truly older candles
    candles = [c for c in candles if c.ts < oldest_ts]

    with state.lock:
        if state.session != session:
            state.history_loading = False
            return
        # Prepend — deque maxlen will drop from the right (newest) if needed
        new_dq = collections.deque(candles, maxlen=MAX_CANDLES)
        for c in state.candles:
            new_dq.append(c)
        state.candles = new_dq
        state.history_loading = False

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
    curses.init_pair(C_LINE,      curses.COLOR_CYAN,   -1)
    curses.init_pair(C_VP_NORM,   curses.COLOR_CYAN,   -1)
    curses.init_pair(C_VP_POC,    curses.COLOR_YELLOW, -1)
    curses.init_pair(C_VP_VA,     curses.COLOR_MAGENTA,-1)
    curses.init_pair(C_VWAP,      curses.COLOR_WHITE,  -1)
    curses.init_pair(C_VWAP_BAND, curses.COLOR_CYAN,   -1)
    curses.init_pair(C_VWAP_SD2,  curses.COLOR_YELLOW, -1)

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


# ── session boundary helper ───────────────────────────────────────────────────
def session_bounds():
    """
    Return (prev_start, prev_end, curr_start) as Unix timestamps.
    Sessions run 19:00 CT → 19:00 CT.
    prev_end == curr_start.
    """
    import datetime as _dt
    now = datetime.now()
    # floor to today 19:00
    today_open = datetime(now.year, now.month, now.day, 19, 0, 0)
    if now < today_open:
        curr_start = (today_open - _dt.timedelta(days=1)).timestamp()
    else:
        curr_start = today_open.timestamp()
    prev_start = curr_start - 86400   # exactly 24 hours earlier
    prev_end   = curr_start
    return prev_start, prev_end, curr_start

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
        color_scheme      = state.color_scheme
        show_vp           = state.show_vp
        show_vwap         = state.show_vwap
        interval_idx      = state.interval_idx
        history_loading   = state.history_loading
        chart_mode        = state.chart_mode

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

    n_all = len(all_candles)

    # ── viewport + TV-style cursor ────────────────────────────────────────────
    # view_offset : how many candles are hidden off the RIGHT edge (pan left).
    #               0 = live end is rightmost visible candle.
    # cursor_col_idx : cursor column within the visible window [0, chart_w-1].
    #               -1 = no cursor / live mode.
    with state.lock:
        view_offset    = state.view_offset
        cursor_col_idx = state.cursor_col_idx

    # Clamp view_offset so we never go past the oldest candle
    view_offset = max(0, min(n_all - 1, view_offset))

    # Slice visible window: chart_w candles ending at (n_all - view_offset)
    right_idx = n_all - view_offset
    left_idx  = max(0, right_idx - chart_w)
    visible   = all_candles[left_idx:right_idx] if all_candles else []
    n_vis     = len(visible)

    # Clamp cursor_col_idx to actual visible window
    if cursor_col_idx < 0:
        cursor_col_idx = -1          # live mode: no crosshair
    else:
        cursor_col_idx = max(0, min(n_vis - 1, cursor_col_idx))

    # Write clamped values back (draw() is the single authority on clamping)
    with state.lock:
        state.view_offset    = view_offset
        state.cursor_col_idx = cursor_col_idx

    if visible and cursor_col_idx >= 0:
        cursor_idx = cursor_col_idx
        # Map candle index → screen column (candles fill from right)
        start_col_base = chart_r - n_vis
        cursor_col     = start_col_base + cursor_idx
        selected       = visible[cursor_idx]
    elif visible:
        cursor_idx = n_vis - 1
        cursor_col = chart_r - 1
        selected   = visible[-1]
    else:
        cursor_idx = 0
        cursor_col = -1
        selected   = None

    in_cursor_mode = (cursor_col_idx >= 0)

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
    secs_left   = cur_resolution() - (now_epoch % cur_resolution())
    countdown   = f"  {secs_left:02d}s" if status == "Live" else ""
    ivl_label  = INTERVALS[interval_idx][0]
    status_str  = f"| {status}{countdown}  {ivl_label}  {now_str} "
    db.puts(0, cols - len(status_str) - 1, status_str,
            C_BULL if status == "Live" else C_BEAR, curses.A_BOLD)

    col = 2
    for a in ("ETH", "BTC"):
        lbl = f" {a}/USDT "
        db.puts(1, col, lbl, C_ASSET_SEL if a == asset else C_HEADER, curses.A_BOLD)
        col += len(lbl) + 1
    db.puts(1, col + 1, f"[E]TH  [B]TC  [F]eed  [C]olor  [W]AP  [V]P  [I]{ivl_label}  [L]ine  [<][>]cursor  [Esc]live  [Prt]shot  [Q]uit", C_HEADER)

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

    # ── VOLUME PROFILE — current + previous session ─────────────────────────
    if show_vp and visible and chart_h > 0:
        _prev_start, _prev_end, _curr_start = session_bounds()

        # Candle lists for each session
        prev_sess_candles = [c for c in all_candles
                             if _prev_start <= c.ts < _prev_end]
        session_candles   = [c for c in all_candles if c.ts >= _curr_start]

        # ── shared VP compute function ────────────────────────────────────────
        VP_BUCKETS  = 200
        price_range = hi_p - lo_p

        def compute_vp(candles):
            """Returns (vp_buckets, poc_price, vah_price, val_price) or None."""
            if not candles or price_range <= 0:
                return None
            vp = [0.0] * VP_BUCKETS
            def ptb(p):
                return max(0, min(VP_BUCKETS-1,
                    int((p - lo_p) / price_range * (VP_BUCKETS - 1))))
            for c in candles:
                body_hi = max(c.o, c.c);  body_lo = min(c.o, c.c)
                # 50% over wick range
                wv = c.v * 0.5
                bw, bh = ptb(max(lo_p, c.l)), ptb(min(hi_p, c.h))
                sw = max(1, bh - bw + 1)
                for b in range(bw, bh + 1): vp[b] += wv / sw
                # 50% over body range
                bv = c.v * 0.5
                bl, bb = ptb(max(lo_p, body_lo)), ptb(min(hi_p, body_hi))
                sb = max(1, bb - bl + 1)
                if sb > 1:
                    for b in range(bl, bb + 1): vp[b] += bv / sb
                else:
                    vp[ptb(c.c)] += bv
            mx = max(vp) or 1.0
            tv = sum(vp)
            pi = vp.index(mx)
            poc_p = lo_p + (pi / (VP_BUCKETS - 1)) * price_range
            # Value area 70%
            tgt = tv * 0.70; acc = vp[pi]; lo_b = pi; hi_b = pi
            while acc < tgt:
                ab = vp[hi_b+1] if hi_b < VP_BUCKETS-1 else 0.0
                bb = vp[lo_b-1] if lo_b > 0            else 0.0
                if ab == 0 and bb == 0: break
                if ab >= bb: hi_b += 1; acc += ab
                else:        lo_b -= 1; acc += bb
            vah_p = lo_p + (hi_b / (VP_BUCKETS - 1)) * price_range
            val_p = lo_p + (lo_b / (VP_BUCKETS - 1)) * price_range
            return vp, poc_p, vah_p, val_p

        def price_to_row(p):
            if price_range <= 0: return chart_h // 2
            r = int((1.0 - (p - lo_p) / price_range) * (chart_h - 1))
            return max(0, min(chart_h - 1, r))

        def draw_vp_bars(vp, poc_p, vah_p, val_p, alpha_dim=False):
            """Render VP bars. alpha_dim=True for previous session (fainter)."""
            row_vol = [0.0] * chart_h
            for b, vol in enumerate(vp):
                price = lo_p + (b / (VP_BUCKETS - 1)) * price_range
                row   = price_to_row(price)
                row_vol[row] += vol
            mrv     = max(row_vol) or 1.0
            VP_MAX_W = max(4, chart_w // 10 if alpha_dim else chart_w // 5)
            poc_row = price_to_row(poc_p)
            vah_row = price_to_row(vah_p)
            val_row = price_to_row(val_p)
            for b, bvol in enumerate(row_vol):
                if bvol <= 0: continue
                bar_w = max(1, int(bvol / mrv * VP_MAX_W))
                row   = chart_top + b
                if not (chart_top <= row < chart_bot): continue
                in_va = (vah_row <= b <= val_row)
                if alpha_dim:
                    pair, attrs, char = C_VP_NORM, curses.A_DIM, "."
                elif b == poc_row:
                    pair, attrs, char = C_VP_POC, curses.A_BOLD, "="
                elif in_va:
                    pair, attrs, char = C_VP_NORM, curses.A_NORMAL, "-"
                else:
                    pair, attrs, char = C_VP_NORM, curses.A_DIM, "-"
                for c2 in range(bar_w):
                    if 0 <= c2 < chart_r:
                        db.put(row, c2, char, pair, attrs)

        def draw_level_line(price, char, pair, attrs, label, label_suffix=""):
            """Draw a full-width horizontal line and axis label."""
            row = chart_top + price_to_row(price)
            if not (chart_top <= row < chart_bot): return
            for c2 in range(chart_r):
                if db.buf[row][c2][0] in (" ", ".", "-", char):
                    db.put(row, c2, char, pair, attrs)
            db.puts(row, chart_r, "+", pair, attrs)
            lbl = f"{label}{label_suffix} {price_fmt(price, asset)}"
            db.puts(row, chart_r + 1, f"{lbl:<{PRICE_W}}", pair, attrs)

        def virgin_line(price, char, pair, attrs, label,
                        start_col, candles_after, col_offset):
            """
            Draw a virgin level: extends from start_col rightward until
            a candle's high/low trades through the price level.
            """
            row = chart_top + price_to_row(price)
            if not (chart_top <= row < chart_bot): return
            end_col = chart_r  # default: extend to right edge
            for j, c in enumerate(candles_after):
                col = col_offset + j
                if col < 0 or col >= chart_r: continue
                # Level is "touched" when candle high crosses above or low crosses below
                if c.h >= price >= c.l:
                    end_col = col
                    break
            for c2 in range(max(0, start_col), min(chart_r, end_col + 1)):
                if db.buf[row][c2][0] in (" ", ".", "-"):
                    db.put(row, c2, char, pair, attrs)
            # Axis label only if line reaches right edge (still virgin)
            if end_col >= chart_r - 1:
                db.puts(row, chart_r, "+", pair, attrs)
                lbl = f"p{label} {price_fmt(price, asset)}"
                db.puts(row, chart_r + 1, f"{lbl:<{PRICE_W}}", pair, attrs)

        # ── PREVIOUS SESSION VP ───────────────────────────────────────────────
        prev_result = compute_vp(prev_sess_candles)
        if prev_result:
            pvp, p_poc, p_vah, p_val = prev_result
            draw_vp_bars(pvp, p_poc, p_vah, p_val, alpha_dim=True)

            # Find the column index where the current session starts in visible[]
            curr_start_col = chart_r  # default: off right edge
            for _i, _vc in enumerate(visible):
                if _vc.ts >= _curr_start:
                    curr_start_col = (chart_r - n_vis) + _i
                    break

            # Candles in visible[] that belong to current session
            curr_vis_candles = [c for c in visible if c.ts >= _curr_start]
            curr_col_offset  = chart_r - len(curr_vis_candles)

            # Draw previous session fixed lines (over prev-session columns only)
            prev_end_col = curr_start_col - 1
            for c2 in range(chart_r - n_vis, min(chart_r, prev_end_col + 1)):
                # Draw prev POC/VAH/VAL only over prev-session area
                pass  # handled by virgin_line extending from prev_end_col

            # Virgin POC/VAH/VAL — extend into current session until touched
            virgin_line(p_poc, "=", C_VP_POC, curses.A_DIM,
                        "POC", curr_start_col, curr_vis_candles, curr_col_offset)
            virgin_line(p_vah, "~", C_VP_VA, curses.A_DIM,
                        "VAH", curr_start_col, curr_vis_candles, curr_col_offset)
            virgin_line(p_val, "~", C_VP_VA, curses.A_DIM,
                        "VAL", curr_start_col, curr_vis_candles, curr_col_offset)

            # Draw prev-session lines over their own columns (solid, dim)
            for _px, _lbl in [(p_vah, "pVAH"), (p_val, "pVAL"), (p_poc, "pPOC")]:
                row = chart_top + price_to_row(_px)
                if not (chart_top <= row < chart_bot): continue
                _ch = "=" if _lbl == "pPOC" else "~"
                _pr = C_VP_POC if _lbl == "pPOC" else C_VP_VA
                for c2 in range(chart_r - n_vis, min(chart_r, curr_start_col)):
                    if db.buf[row][c2][0] in (" ", ".", "-", _ch):
                        db.put(row, c2, _ch, _pr, curses.A_DIM)

        # ── CURRENT SESSION VP ────────────────────────────────────────────────
        curr_result = compute_vp(session_candles)
        if curr_result:
            vp, poc_price, vah_price, val_price = curr_result
            draw_vp_bars(vp, poc_price, vah_price, val_price, alpha_dim=False)
            draw_level_line(vah_price, "~", C_VP_VA, curses.A_BOLD, "VAH")
            draw_level_line(val_price, "~", C_VP_VA, curses.A_BOLD, "VAL")
            draw_level_line(poc_price, "=", C_VP_POC, curses.A_BOLD, "POC")

    # ── VWAP + STANDARD DEVIATION BANDS — current + previous session ────────
    if show_vwap and visible and chart_h > 0:
        _pstart, _pend, _cstart = session_bounds()

        # All candles for each session (not just visible — for accurate VWAP)
        prev_vwap_candles = [c for c in all_candles if _pstart <= c.ts < _pend]
        session_all       = [c for c in all_candles if c.ts >= _cstart]

        def build_vwap_map(candles):
            """Compute cumulative VWAP + σ per candle. Returns {ts: (vwap, sd)}."""
            _cum_tpv = _cum_vol = _cum_tp2v = 0.0
            _map = {}
            for _c in candles:
                _tp = (_c.h + _c.l + _c.c) / 3.0
                _v  = _c.v
                _cum_tpv  += _tp * _v
                _cum_vol  += _v
                _cum_tp2v += _tp * _tp * _v
                if _cum_vol > 0:
                    _vw  = _cum_tpv / _cum_vol
                    _var = max(0.0, _cum_tp2v / _cum_vol - _vw * _vw)
                    _sd  = _var ** 0.5
                else:
                    _vw, _sd = 0.0, 0.0
                _map[_c.ts] = (_vw, _sd)
            return _map

        def draw_vwap_on_candles(candles_subset, vwap_map, dim=False):
            """Draw VWAP lines for the given candle subset using precomputed map."""
            _start_c = chart_r - n_vis
            _prev_r  = None
            for _i, _candle in enumerate(visible):
                if _candle not in candles_subset and _candle.ts not in {c.ts for c in candles_subset}:
                    _prev_r = None
                    continue
                _col = _start_c + _i
                if not (0 <= _col < chart_r):
                    _prev_r = None
                    continue
                if _candle.ts not in vwap_map:
                    _prev_r = None
                    continue
                _vw, _sd = vwap_map[_candle.ts]
                if _vw <= 0:
                    continue
                def _pr(p):
                    if hi_p == lo_p: return chart_h // 2
                    r = int((1.0 - (p - lo_p) / (hi_p - lo_p)) * (chart_h - 1))
                    return max(chart_top, min(chart_bot - 1, chart_top + r))
                _r_vwap  = _pr(_vw)
                _r_sd05u = _pr(_vw + 0.5 * _sd);  _r_sd05l = _pr(_vw - 0.5 * _sd)
                _r_sd2u  = _pr(_vw + 2.0 * _sd);  _r_sd2l  = _pr(_vw - 2.0 * _sd)
                _r_sd25u = _pr(_vw + 2.5 * _sd);  _r_sd25l = _pr(_vw - 2.5 * _sd)
                _band_a  = curses.A_DIM
                _line_a  = curses.A_DIM if dim else curses.A_BOLD
                # 0.5σ band
                for _r in range(min(_r_sd05u,_r_sd05l), max(_r_sd05u,_r_sd05l)+1):
                    if chart_top <= _r < chart_bot and db.buf[_r][_col][0] == " ":
                        db.put(_r, _col, ":", C_VWAP_BAND, _band_a)
                # VWAP line
                if chart_top <= _r_vwap < chart_bot:
                    if db.buf[_r_vwap][_col][0] in (" ", ":"):
                        db.put(_r_vwap, _col, "-", C_VWAP, _line_a)
                # 2σ / 2.5σ lines
                for _r in (_r_sd2u, _r_sd2l):
                    if chart_top <= _r < chart_bot and db.buf[_r][_col][0] in (" ", ":"):
                        db.put(_r, _col, "~", C_VWAP_SD2, curses.A_DIM if dim else curses.A_NORMAL)
                for _r in (_r_sd25u, _r_sd25l):
                    if chart_top <= _r < chart_bot and db.buf[_r][_col][0] in (" ", ":"):
                        db.put(_r, _col, "~", C_VWAP_SD2, curses.A_DIM)

        # Previous session VWAP (dim)
        if prev_vwap_candles:
            _prev_map = build_vwap_map(prev_vwap_candles)
            _prev_ts_set = {c.ts for c in prev_vwap_candles}
            _prev_vis = [c for c in visible if c.ts in _prev_ts_set]
            draw_vwap_on_candles(_prev_vis, _prev_map, dim=True)

        # Current session VWAP (full brightness)
        if session_all:
            # Build running sums over session candles ordered chronologically
            _cum_tpv = 0.0   # Σ tp×vol
            _cum_vol = 0.0   # Σ vol
            _cum_tp2v= 0.0   # Σ vol×tp²  (for σ via Welford-style: E[x²]-E[x]²)
            # Map ts → (vwap, sigma) for every session candle
            _vwap_map = {}
            for _c in session_all:
                _tp   = (_c.h + _c.l + _c.c) / 3.0
                _v    = _c.v
                _cum_tpv  += _tp * _v
                _cum_vol  += _v
                _cum_tp2v += _tp * _tp * _v
                if _cum_vol > 0:
                    _vw = _cum_tpv / _cum_vol
                    _var = max(0.0, _cum_tp2v / _cum_vol - _vw * _vw)
                    _sd = _var ** 0.5
                else:
                    _vw, _sd = 0.0, 0.0
                _vwap_map[_c.ts] = (_vw, _sd)

            # Draw current session VWAP using the helper
            _curr_ts_set = {c.ts for c in session_all}
            _curr_vis    = [c for c in visible if c.ts in _curr_ts_set]
            draw_vwap_on_candles(_curr_vis, _vwap_map, dim=False)

            # ── VWAP price axis labels at rightmost visible current candle ─────
            _last_curr = next((c for c in reversed(visible)
                               if c.ts in _vwap_map), None)
            if _last_curr:
                _vw_last, _sd_last = _vwap_map[_last_curr.ts]
                if _vw_last > 0:
                    def _pr_ax(p):
                        if hi_p == lo_p: return chart_h // 2
                        r = int((1.0 - (p - lo_p) / (hi_p - lo_p)) * (chart_h - 1))
                        return max(chart_top, min(chart_bot - 1, chart_top + r))
                    def _axis_lbl(price, label, pair, attrs=curses.A_BOLD):
                        _gr = _pr_ax(price)
                        if chart_top <= _gr < chart_bot:
                            db.puts(_gr, chart_r, "+", pair, attrs)
                            db.puts(_gr, chart_r + 1,
                                    f"{label + ' ' + price_fmt(price, asset):<{PRICE_W}}",
                                    pair, attrs)
                    _axis_lbl(_vw_last,                "VW",  C_VWAP)
                    _axis_lbl(_vw_last + 0.5*_sd_last, ".5s", C_VWAP_BAND, curses.A_DIM)
                    _axis_lbl(_vw_last - 0.5*_sd_last, ".5s", C_VWAP_BAND, curses.A_DIM)
                    _axis_lbl(_vw_last + 2.0*_sd_last, "2s",  C_VWAP_SD2)
                    _axis_lbl(_vw_last - 2.0*_sd_last, "2s",  C_VWAP_SD2)
                    _axis_lbl(_vw_last + 2.5*_sd_last, "2.5s",C_VWAP_SD2, curses.A_DIM)
                    _axis_lbl(_vw_last - 2.5*_sd_last, "2.5s",C_VWAP_SD2, curses.A_DIM)

    # ── PERIOD SEPARATOR — 19:00 CT vertical line ───────────────────────────
    # Mark each candle column whose local timestamp crosses the 19:00 session
    # open. Draws a dim `:` column behind candles.
    if visible:
        start_col_sep = chart_r - n_vis  # same as main start_col
        for i, candle in enumerate(visible):
            col_s = start_col_sep + i
            if not (0 <= col_s < chart_r):
                continue
            dt_c = datetime.fromtimestamp(candle.ts)
            if dt_c.hour == 19 and dt_c.minute == 0:
                for r in range(chart_top, chart_bot):
                    if db.buf[r][col_s][0] == " ":
                        db.put(r, col_s, ":", C_AXIS, curses.A_DIM)
                # Label at top of separator
                db.puts(chart_top, col_s, "S", C_LABEL, curses.A_DIM)

    # ── CANDLES / LINE CHART ─────────────────────────────────────────────────
    start_col = chart_r - n_vis
    clamp = lambda v: max(chart_top, min(chart_bot - 1, v))

    if chart_mode == "line":
        # Line chart: connect each candle's close price with a dot/dash.
        # Vertical connector drawn between consecutive close rows so the
        # line is continuous even over large price moves.
        prev_row = None
        for i, candle in enumerate(visible):
            col = start_col + i
            if not (0 <= col < chart_r):
                prev_row = None
                continue
            is_selected = (i == cursor_idx and in_cursor_mode)
            pair  = C_CURSOR if is_selected else C_LINE
            attrs = curses.A_BOLD if is_selected else curses.A_NORMAL
            r_c   = clamp(chart_top + p2r(candle.c))
            # Point at close
            db.put(r_c, col, "*", pair, attrs)
            # Vertical connector to previous close (fills gaps on big moves)
            if prev_row is not None and col > 0:
                r_from = min(prev_row, r_c)
                r_to   = max(prev_row, r_c)
                for r in range(r_from, r_to):
                    if db.buf[r][col - 1][0] == " ":
                        db.put(r, col - 1, "|", pair, curses.A_DIM)
            prev_row = r_c
            # Cursor crosshair
            if is_selected:
                for r in range(chart_top, chart_bot):
                    if db.buf[r][col][0] == " ":
                        db.put(r, col, ":", C_AXIS, curses.A_DIM)
    else:
        # Candlestick mode
        for i, candle in enumerate(visible):
            col = start_col + i
            if not (0 <= col < chart_r):
                continue
            is_selected = (i == cursor_idx and in_cursor_mode)
            bull        = candle.c >= candle.o
            body_pair   = C_CURSOR if is_selected else (C_BULL if bull else C_BEAR)
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
        with state.lock:
            _cci = state.cursor_col_idx
            _vo  = state.view_offset
        cinfo = f"  cur:{_cci} pan:{_vo}" if in_cursor_mode else ""
        auth_tag = f"/{('auth' if PHEMEX_API_KEY else 'no-auth')}" if feed == "phemex" else ""
        err_str  = f"  ! {error}" if error and visible else ""
        scheme_tag = "B/W" if color_scheme == "bw" else "R/G"
        vp_tag     = "VP:ON" if show_vp else "VP:OFF"
        hist_tag = "  [loading history...]" if history_loading else ""
        mode_tag  = "LINE" if chart_mode == "line" else "CANDLE"
        vwap_tag  = "W:ON" if show_vwap else "W:OFF"
        footer   = (f" [E] ETH  [B] BTC  [F] Feed: {feed.upper()}  [C] {scheme_tag}  [I] {ivl_label}  [L] {mode_tag}  [W] {vwap_tag}  [V] {vp_tag}  [Q] Quit"
                    f"   {len(all_candles)} candles  {feed.upper()}{auth_tag}{cinfo}{err_str}{hist_tag}")
        db.puts(footer_row, 0, footer.ljust(cols)[:cols], C_AXIS)


# ── screenshot ────────────────────────────────────────────────────────────────
def take_screenshot(db: "DoubleBuffer"):
    """Dump the current rendered buffer to screenshots/<timestamp>.txt"""
    folder = os.path.join(os.path.dirname(__file__), "screenshots")
    os.makedirs(folder, exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    fn  = os.path.join(folder, f"quantasset_{ts}.txt")
    buf = db.prev or db.buf   # use last flushed frame; fall back to current
    lines = []
    for row in buf:
        lines.append("".join(cell[0] for cell in row).rstrip())
    with open(fn, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return fn

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
        # ── Drain all pending keypresses for smooth scrolling on Windows ──────
        keys = []
        while True:
            k = stdscr.getch()
            if k == -1:
                break
            keys.append(k)
        if not keys:
            keys = [-1]   # still run draw/refresh even with no input

        restart_feed = False
        new_interval_idx = -1

        for key in keys:
            if key in (ord("q"), ord("Q")):
                curses.endwin()
                return

            new_asset = None
            new_feed  = None
            if   key in (ord("e"), ord("E")) and state.asset != "ETH":
                new_asset = "ETH"
            elif key in (ord("b"), ord("B")) and state.asset != "BTC":
                new_asset = "BTC"
            elif key in (ord("f"), ord("F")):
                idx      = FEEDS.index(state.feed)
                new_feed = FEEDS[(idx + 1) % len(FEEDS)]
            elif key in (ord("i"), ord("I")):
                new_interval_idx = (state.interval_idx + 1) % len(INTERVALS)
            elif key in (ord("c"), ord("C")):
                with state.lock:
                    state.color_scheme = "rg" if state.color_scheme == "bw" else "bw"
                init_colors(state.color_scheme)
                db.prev = None
                stdscr.clear()
            elif key in (ord("v"), ord("V")):
                with state.lock:
                    state.show_vp = not state.show_vp
                db.prev = None
                stdscr.clear()
            elif key in (ord("l"), ord("L")):
                with state.lock:
                    state.chart_mode = "line" if state.chart_mode == "candle" else "candle"
                db.prev = None
                stdscr.clear()
            elif key in (ord("w"), ord("W")):
                with state.lock:
                    state.show_vwap = not state.show_vwap
                db.prev = None
                stdscr.clear()
            elif key in (ord("p"), ord("P"), curses.KEY_PRINT):
                fn = take_screenshot(db)
                # Brief flash in footer — handled next draw frame
                with state.lock:
                    state.error = f"Screenshot: {os.path.basename(fn)}"
            elif key == curses.KEY_LEFT:
                with state.lock:
                    cci = state.cursor_col_idx
                    if cci < 0:
                        # Enter cursor mode: place cursor at rightmost candle
                        state.cursor_col_idx = max(0, len(state.candles) - 1)
                    elif cci > 0:
                        # Move cursor left within window
                        state.cursor_col_idx -= 1
                    else:
                        # Cursor already at left edge — pan viewport left
                        state.view_offset += 1
            elif key == curses.KEY_RIGHT:
                with state.lock:
                    cci = state.cursor_col_idx
                    vo  = state.view_offset
                    if cci < 0:
                        pass   # already live, nothing to do
                    elif vo > 0 and cci >= len(state.candles) - 1:
                        # Cursor at right edge and viewport panned — pan right
                        state.view_offset = max(0, vo - 1)
                    elif cci >= 0:
                        # Move cursor right within window
                        n_loaded = len(state.candles)
                        new_cci  = cci + 1
                        if vo == 0 and new_cci >= n_loaded:
                            # Reached live end — exit cursor mode
                            state.cursor_col_idx = -1
                            state.view_offset    = 0
                        else:
                            state.cursor_col_idx = new_cci
            elif key == 27:  # Escape — snap back to live
                with state.lock:
                    state.cursor_col_idx = -1
                    state.view_offset    = 0


            if new_asset:
                state.asset = new_asset
                restart_feed = True
            if new_feed:
                state.feed = new_feed
                restart_feed = True
            if new_interval_idx >= 0:
                state.interval_idx = new_interval_idx
                restart_feed = True

        if restart_feed:
            threading.Thread(
                target=start_feed, args=(state.asset,), daemon=True).start()

        # ── Auto-load history when cursor reaches left edge ───────────────────
        with state.lock:
            vo         = state.view_offset
            n_loaded   = len(state.candles)
            is_loading = state.history_loading
            cur_sess   = state.session
        # Trigger history fetch when viewport is within 20 candles of oldest loaded
        if (not is_loading and n_loaded > 0
                and vo >= n_loaded - 20):
            with state.lock:
                state.history_loading = True
            threading.Thread(
                target=fetch_history_before, args=(cur_sess,), daemon=True).start()

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
