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

MAX_CANDLES   = 3000    # 48h × 60 = 2880 candles at 1m, plus headroom
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
C_PRICE_LBL  = 10   # price label badge (white on blue)
C_PRICE_LINE = 19   # price dashed line (yellow)
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
        self.n_vis        = 0       # visible candle count, set by draw()

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

    # Auto-preload history so VP/VWAP values are based on full session data.
    # 1m: preload 48h (covers two full futures sessions).
    # All other intervals: preload 500 candles.
    with state.lock:
        state.history_loading = True
    if cur_resolution() == 60:
        threading.Thread(
            target=preload_48h, args=(my_session,), daemon=True).start()
    else:
        threading.Thread(
            target=preload_500, args=(my_session,), daemon=True).start()


def preload_500(session: int):
    """
    Background: fetch until we have at least 500 candles loaded.
    Used for all intervals except 1m (which uses preload_48h).
    """
    TARGET = 500
    for _ in range(10):   # max 10 fetches
        with state.lock:
            if state.session != session:
                state.history_loading = False
                return
            n        = len(state.candles)
            if n >= TARGET:
                state.history_loading = False
                state.error = ""
                return
            oldest_ts = state.candles[0].ts if state.candles else 0
            my_feed   = state.feed
            asset     = state.asset

        if oldest_ts == 0:
            break

        with state.lock:
            state.error = f"Preloading... {n}/{TARGET}"

        candles = (fetch_kraken(asset, before_ts=oldest_ts)
                   if my_feed == "kraken"
                   else fetch_phemex(asset, before_ts=oldest_ts))
        candles = [c for c in candles if c.ts < oldest_ts]
        if not candles:
            break

        with state.lock:
            if state.session != session:
                state.history_loading = False
                return
            new_dq = collections.deque(candles, maxlen=MAX_CANDLES)
            for c in state.candles:
                new_dq.append(c)
            state.candles = new_dq

        time.sleep(0.2)

    with state.lock:
        if state.session == session:
            state.history_loading = False
            state.error = ""


def preload_48h(session: int):
    """
    Background: fetch history until we have 48 hours of 1m candles.
    Only runs when interval is 1m. Fires multiple REST requests back-to-back,
    each prepending a batch, until either 2880 candles are loaded or the feed
    is stale (user switched asset/interval/feed).
    Updates status with progress so the user can see loading is happening.
    """
    TARGET_SECS = 48 * 3600   # 48 hours in seconds
    resolution  = cur_resolution()

    while True:
        with state.lock:
            if state.session != session:
                state.history_loading = False
                return
            if not state.candles:
                state.history_loading = False
                return
            oldest_ts  = state.candles[0].ts
            newest_ts  = state.candles[-1].ts
            n_loaded   = len(state.candles)
            my_feed    = state.feed
            asset      = state.asset

        covered = newest_ts - oldest_ts
        if covered >= TARGET_SECS:
            with state.lock:
                if state.session == session:
                    state.history_loading = False
                    state.error = ""
            return

        # Show progress
        pct = min(99, int(covered / TARGET_SECS * 100))
        with state.lock:
            if state.session == session:
                state.error = f"Preloading 48h... {pct}%"

        candles = (fetch_kraken(asset, before_ts=oldest_ts)
                   if my_feed == "kraken"
                   else fetch_phemex(asset, before_ts=oldest_ts))

        candles = [c for c in candles if c.ts < oldest_ts]
        if not candles:
            # Nothing more to fetch
            with state.lock:
                if state.session == session:
                    state.history_loading = False
                    state.error = ""
            return

        with state.lock:
            if state.session != session:
                state.history_loading = False
                return
            new_dq = collections.deque(candles, maxlen=MAX_CANDLES)
            for c in state.candles:
                new_dq.append(c)
            state.candles = new_dq

        # Small sleep to avoid hammering the API
        time.sleep(0.3)

    with state.lock:
        state.history_loading = False


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
    curses.init_pair(C_PRICE_LBL,  curses.COLOR_WHITE,  curses.COLOR_BLUE)
    curses.init_pair(C_PRICE_LINE, curses.COLOR_YELLOW, -1)
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
    with state.lock:
        state.n_vis = n_vis   # expose to key handler

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

    db.puts(0, 2, "Q U A N T A S S E T  |  ChartHacker", C_HEADER, curses.A_BOLD)

    badge = f" {feed.upper()} "
    db.puts(0, cols - len(badge) - 22, badge, C_ASSET_SEL, curses.A_BOLD)

    now_str = datetime.now().strftime("%m/%d/%Y  %H:%M:%S")
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
    db.puts(1, col + 1, f"[E]TH  [B]TC  [F]eed  [C]olor  [W]AP  [V]P  [I]{ivl_label}  [L]ine  [G]oto  [<][>]x1  [[]x10  [{{}}]x50  [Esc]live  [P]shot  [Q]uit", C_HEADER)

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
        # Cursor time: always show full date + time
        if in_cursor_mode and selected:
            ts_lbl = datetime.fromtimestamp(selected.ts).strftime("%m/%d/%Y  %H:%M:%S")
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

    # ── VOLUME PROFILE — current + previous session ─────────────────────────
    if show_vp and visible and chart_h > 0:
        _prev_start, _prev_end, _curr_start = session_bounds()
        _is_1m = (cur_resolution() == 60)

        # Split ALL loaded candles into complete 19:00 CT → 19:00 CT sessions.
        # Each session boundary is at _curr_start - N*86400.
        # "Current session" = started at _curr_start (may still be open).
        # "Previous session" = the one immediately before, for virgin lines.
        # All older sessions are drawn as historical profiles.
        import datetime as _dtmod3
        def _session_boundaries(candles):
            """Return list of session start timestamps covering all candles,
            sorted oldest→newest. Each boundary is a 19:00 CT Unix timestamp."""
            if not candles:
                return []
            oldest = candles[0].ts
            boundaries = []
            t = _curr_start
            while t > oldest - 86400:
                boundaries.append(t)
                t -= 86400
            return sorted(boundaries)

        _boundaries    = _session_boundaries(all_candles)
        # Build session candle lists: each session is [boundary, next_boundary)
        _all_sessions  = []
        for _si, _sb in enumerate(_boundaries):
            _se = _boundaries[_si + 1] if _si + 1 < len(_boundaries) else float("inf")
            _sc = [c for c in all_candles if _sb <= c.ts < _se]
            if _sc:
                _all_sessions.append((_sb, _sc))

        # Current session = last boundary; previous = second-to-last
        session_candles   = [c for c in all_candles if c.ts >= _curr_start]
        prev_sess_candles = ([c for c in all_candles
                              if _prev_start <= c.ts < _curr_start]
                             if len(_boundaries) >= 2 else [])
        # Historical sessions = everything older than prev session
        hist_sessions = _all_sessions[:-2] if len(_all_sessions) >= 2 else []

        # ── shared VP compute function ────────────────────────────────────────
        # Strategy:
        #   COMPUTE  — use the session's own fixed high/low as bucket boundaries.
        #              This makes POC/VAH/VAL stable regardless of scroll position.
        #   RENDER   — re-bin the stable buckets into the VISIBLE price range for
        #              display, so bars are proportional to what's on screen and
        #              don't collapse when the session range >> visible range.
        VP_BUCKETS = 200

        def compute_vp(candles):
            """
            Compute VP using session candles' own high/low as fixed bucket bounds.
            Returns (vp_buckets, s_lo, s_hi, poc_price, vah_price, val_price).
            """
            if not candles:
                return None
            s_lo    = min(c.l for c in candles)
            s_hi    = max(c.h for c in candles)
            s_range = s_hi - s_lo
            if s_range <= 0:
                return None
            vp = [0.0] * VP_BUCKETS

            def ptb(p):
                return max(0, min(VP_BUCKETS - 1,
                    int((p - s_lo) / s_range * (VP_BUCKETS - 1))))

            for c in candles:
                body_hi = max(c.o, c.c)
                body_lo = min(c.o, c.c)
                wv  = c.v * 0.5               # 50% over wick range
                bw  = ptb(c.l);  bh = ptb(c.h)
                sw  = max(1, bh - bw + 1)
                for b in range(bw, bh + 1):
                    vp[b] += wv / sw
                bv  = c.v * 0.5               # 50% over body range
                bl  = ptb(body_lo);  bb = ptb(body_hi)
                sb  = max(1, bb - bl + 1)
                if sb > 1:
                    for b in range(bl, bb + 1):
                        vp[b] += bv / sb
                else:
                    vp[ptb(c.c)] += bv

            mx    = max(vp) or 1.0
            tv    = sum(vp)
            pi    = vp.index(mx)
            poc_p = s_lo + (pi / (VP_BUCKETS - 1)) * s_range

            tgt  = tv * 0.70
            acc  = vp[pi]
            lo_b = hi_b = pi
            while acc < tgt:
                ab = vp[hi_b + 1] if hi_b < VP_BUCKETS - 1 else 0.0
                bb = vp[lo_b - 1] if lo_b > 0               else 0.0
                if ab == 0 and bb == 0:
                    break
                if ab >= bb:
                    hi_b += 1;  acc += ab
                else:
                    lo_b -= 1;  acc += bb

            vah_p = s_lo + (hi_b / (VP_BUCKETS - 1)) * s_range
            val_p = s_lo + (lo_b / (VP_BUCKETS - 1)) * s_range
            return vp, s_lo, s_hi, poc_p, vah_p, val_p

        def price_to_row(p):
            """Map a price to a chart row using the VISIBLE price range."""
            if hi_p == lo_p: return chart_h // 2
            r = int((1.0 - (p - lo_p) / (hi_p - lo_p)) * (chart_h - 1))
            return max(0, min(chart_h - 1, r))

        def draw_vp_bars(vp, s_lo, s_hi, poc_p, vah_p, val_p,
                         alpha_dim=False, col_start=0):
            """
            Render VP bars anchored to col_start (the session's first visible column).
            Bars grow rightward from col_start up to VP_MAX_W columns wide.
            Only buckets whose price falls within the visible range are shown.
            Bar width normalised to visible-row max so the profile always fills well.
            """
            s_range = s_hi - s_lo
            if s_range <= 0:
                return

            row_vol = [0.0] * chart_h
            for b, vol in enumerate(vp):
                price = s_lo + (b / (VP_BUCKETS - 1)) * s_range
                if price < lo_p or price > hi_p:
                    continue
                row = price_to_row(price)
                if 0 <= row < chart_h:
                    row_vol[row] += vol

            mrv      = max(row_vol) or 1.0
            VP_MAX_W = max(4, chart_w // 10 if alpha_dim else chart_w // 5)
            poc_row  = price_to_row(poc_p)
            vah_row  = price_to_row(vah_p)
            val_row  = price_to_row(val_p)

            for b, bvol in enumerate(row_vol):
                if bvol <= 0:
                    continue
                bar_w = max(1, int(bvol / mrv * VP_MAX_W))
                row   = chart_top + b
                if not (chart_top <= row < chart_bot):
                    continue
                in_va = (vah_row <= b <= val_row)
                if alpha_dim:
                    pair, attrs, char = C_VP_NORM, curses.A_DIM, "."
                elif b == poc_row:
                    pair, attrs, char = C_VP_POC, curses.A_BOLD, "="
                elif in_va:
                    pair, attrs, char = C_VP_NORM, curses.A_NORMAL, "-"
                else:
                    pair, attrs, char = C_VP_NORM, curses.A_DIM, "-"
                for c2 in range(col_start, min(chart_r, col_start + bar_w)):
                    db.put(row, c2, char, pair, attrs)

        def draw_level_line(price, char, pair, attrs, label,
                            label_suffix="", col_start=0):
            """Draw a horizontal line from col_start to the right edge + axis label."""
            row = chart_top + price_to_row(price)
            if not (chart_top <= row < chart_bot): return
            for c2 in range(col_start, chart_r):
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

        # ── Compute session column boundaries in visible window ───────────────
        start_col_base = chart_r - n_vis   # column of oldest visible candle

        # Column where the PREVIOUS session's first visible candle appears
        prev_col_start = chart_r  # off-screen by default
        for _i, _vc in enumerate(visible):
            if _vc.ts >= _prev_start:
                prev_col_start = start_col_base + _i
                break

        # Column where the CURRENT session starts (19:00 boundary)
        curr_col_start = chart_r  # off-screen if current session not yet visible
        for _i, _vc in enumerate(visible):
            if _vc.ts >= _curr_start:
                curr_col_start = start_col_base + _i
                break

        # Candles in visible[] belonging to the current session
        curr_vis_candles = [c for c in visible if c.ts >= _curr_start]
        curr_col_offset  = chart_r - len(curr_vis_candles)

        # ── PREVIOUS SESSION VP ───────────────────────────────────────────────
        prev_result = compute_vp(prev_sess_candles)
        if prev_result:
            pvp, p_slo, p_shi, p_poc, p_vah, p_val = prev_result

            # Bars anchored to the previous session's first visible column
            draw_vp_bars(pvp, p_slo, p_shi, p_poc, p_vah, p_val,
                         alpha_dim=True, col_start=max(0, prev_col_start))

            # Prev-session level lines only over prev-session columns
            for _px, _ch, _pr in [
                (p_vah, "~", C_VP_VA),
                (p_val, "~", C_VP_VA),
                (p_poc, "=", C_VP_POC),
            ]:
                _row = chart_top + price_to_row(_px)
                if not (chart_top <= _row < chart_bot): continue
                for c2 in range(max(0, prev_col_start),
                                min(chart_r, curr_col_start)):
                    if db.buf[_row][c2][0] in (" ", ".", "-", _ch):
                        db.put(_row, c2, _ch, _pr, curses.A_DIM)

            # Virgin POC/VAH/VAL — extend from curr session start until touched
            virgin_line(p_poc, "=", C_VP_POC, curses.A_DIM,
                        "POC", curr_col_start, curr_vis_candles, curr_col_offset)
            virgin_line(p_vah, "~", C_VP_VA, curses.A_DIM,
                        "VAH", curr_col_start, curr_vis_candles, curr_col_offset)
            virgin_line(p_val, "~", C_VP_VA, curses.A_DIM,
                        "VAL", curr_col_start, curr_vis_candles, curr_col_offset)

        # ── HISTORICAL SESSIONS VP (older than prev session) ─────────────────
        # Draw each historical session's profile anchored to its own start col.
        # No virgin lines for these — just bars and dim level markers.
        for _h_start, _h_candles in hist_sessions:
            _h_result = compute_vp(_h_candles)
            if not _h_result:
                continue
            _hvp, _h_slo, _h_shi, _h_poc, _h_vah, _h_val = _h_result
            # Find start column for this historical session
            _h_col_start = chart_r
            for _hi, _hc in enumerate(visible):
                if _hc.ts >= _h_start:
                    _h_col_start = start_col_base + _hi
                    break
            if _h_col_start >= chart_r:
                continue  # this session is off-screen
            # Find end column (next session boundary)
            _h_col_end = chart_r
            for _hi, _hc in enumerate(visible):
                if _hc.ts >= _h_start + 86400:
                    _h_col_end = start_col_base + _hi
                    break
            draw_vp_bars(_hvp, _h_slo, _h_shi, _h_poc, _h_vah, _h_val,
                         alpha_dim=True, col_start=max(0, _h_col_start))
            # Dim level lines over historical session columns only
            for _hpx, _hch, _hpr in [
                (_h_vah, "~", C_VP_VA), (_h_val, "~", C_VP_VA),
                (_h_poc, "=", C_VP_POC),
            ]:
                _hr = chart_top + price_to_row(_hpx)
                if not (chart_top <= _hr < chart_bot): continue
                for c2 in range(max(0, _h_col_start), min(chart_r, _h_col_end)):
                    if db.buf[_hr][c2][0] in (" ", ".", "-", _hch):
                        db.put(_hr, c2, _hch, _hpr, curses.A_DIM)

        # ── CURRENT SESSION VP ────────────────────────────────────────────────
        curr_result = compute_vp(session_candles)
        if curr_result:
            vp, c_slo, c_shi, poc_price, vah_price, val_price = curr_result
            # Bars and lines anchored to the current session's start column
            draw_vp_bars(vp, c_slo, c_shi, poc_price, vah_price, val_price,
                         alpha_dim=False, col_start=max(0, curr_col_start))
            draw_level_line(vah_price, "~", C_VP_VA, curses.A_BOLD, "VAH",
                            col_start=max(0, curr_col_start))
            draw_level_line(val_price, "~", C_VP_VA, curses.A_BOLD, "VAL",
                            col_start=max(0, curr_col_start))
            draw_level_line(poc_price, "=", C_VP_POC, curses.A_BOLD, "POC",
                            col_start=max(0, curr_col_start))

    # ── VWAP + STANDARD DEVIATION BANDS — current + previous session ────────
    if show_vwap and visible and chart_h > 0:
        _pstart, _pend, _cstart = session_bounds()
        _is_1m_vwap = (cur_resolution() == 60)

        if _is_1m_vwap:
            # 1m: session-anchored VWAP (19:00 CT)
            prev_vwap_candles = [c for c in all_candles if _pstart <= c.ts < _pend]
            session_all       = [c for c in all_candles if c.ts >= _cstart]
        else:
            # Other intervals: VWAP over all loaded candles
            prev_vwap_candles = []
            session_all       = list(all_candles)

        def build_vwap_map(candles):
            """
            Compute cumulative VWAP + σ per candle. Returns {ts: (vwap, sd)}.
            Uses the same formula as TradingView:
              VWAP = Σ(tp*vol) / Σ(vol)
              σ²   = Σ(vol*(tp - VWAP)²) / Σ(vol)
            Computed via Welford's online algorithm for numerical stability.
            """
            _cum_tpv  = 0.0   # Σ tp*vol
            _cum_vol  = 0.0   # Σ vol
            _cum_dev2 = 0.0   # Σ vol*(tp - VWAP)²  (updated each step)
            _map = {}
            for _c in candles:
                _tp  = (_c.h + _c.l + _c.c) / 3.0
                _v   = _c.v
                _old_vol = _cum_vol
                _cum_tpv += _tp * _v
                _cum_vol += _v
                if _cum_vol > 0:
                    _vw  = _cum_tpv / _cum_vol
                    # Welford update for volume-weighted variance
                    # Δ = tp - new_VWAP; add _v*(tp - old_VWAP)*(tp - new_VWAP)
                    if _old_vol > 0:
                        _old_vw = (_cum_tpv - _tp * _v) / _old_vol
                        _cum_dev2 += _v * (_tp - _old_vw) * (_tp - _vw)
                    _var = max(0.0, _cum_dev2 / _cum_vol)
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
            # Use the same Welford build function for consistency
            _vwap_map = build_vwap_map(session_all)

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

    # ── LIVE PRICE LINE (drawn after candles so it's always visible) ──────────
    # Overwrites VP bars, VWAP lines, etc. but never erases candle characters.
    # The axis label always renders on top with a contrasting background.
    _CANDLE_CHARS = {"#", "|"}   # never overwrite these
    # ── PRICE / CURSOR LINE (drawn last — always on top) ─────────────────────
    # The axis label overwrites everything at that row unconditionally so it's
    # always readable even when VWAP/VP labels land on the same price level.
    if not in_cursor_mode and last_price and lo_p < last_price < hi_p:
        pr = chart_top + p2r(last_price)
        if chart_top <= pr < chart_bot:
            # Full highlighted row — every cell gets yellow-on-black reversed
            # so the line is a solid bright band across the entire chart width
            _pl_attrs = curses.A_BOLD | curses.A_REVERSE
            for c2 in range(chart_r):
                _cur_ch = db.buf[pr][c2][0]
                # Keep candle body chars but apply highlight attrs over them
                _draw_ch = _cur_ch if _cur_ch not in (" ", "-", ".") else "-"
                db.put(pr, c2, _draw_ch, C_PRICE_LINE, _pl_attrs)
            # Right axis label — white-on-blue bold, full width
            lbl_str = f" {price_fmt(last_price, asset):<{PRICE_W - 1}}"
            db.put(pr, chart_r, ">", C_PRICE_LBL, curses.A_BOLD | curses.A_REVERSE)
            for _ci, _ch in enumerate(lbl_str):
                db.put(pr, chart_r + 1 + _ci, _ch, C_PRICE_LBL, curses.A_BOLD)
            # Left-edge floating badge
            _badge = f"{price_fmt(last_price, asset)}"
            for _ci, _ch in enumerate(_badge):
                db.put(pr, _ci, _ch, C_PRICE_LBL, curses.A_BOLD)

    if in_cursor_mode and selected and lo_p < selected.c < hi_p:
        pr = chart_top + p2r(selected.c)
        if chart_top <= pr < chart_bot:
            for c2 in range(chart_r):
                if db.buf[pr][c2][0] not in _CANDLE_CHARS:
                    db.put(pr, c2, "-", C_CURSOR, curses.A_BOLD)
            lbl_str = f" {price_fmt(selected.c, asset):<{PRICE_W - 1}}"
            db.put(pr, chart_r, ">", C_CURSOR, curses.A_BOLD)
            for _ci, _ch in enumerate(lbl_str):
                db.put(pr, chart_r + 1 + _ci, _ch, C_CURSOR, curses.A_BOLD)

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
                    # At midnight show the date; otherwise show HH:MM
                    if dt.hour == 0 and dt.minute == 0:
                        lbl = dt.strftime("%m/%d/%Y")
                    else:
                        lbl = dt.strftime("%H:%M")
                    db.puts(time_row, lbl_col, lbl, C_LABEL, curses.A_BOLD)
                    last_label_col = lbl_col

            # Cursor label — include date when not today
            if in_cursor_mode and selected and 0 <= cursor_col <= chart_r - 5:
                _c_dt   = datetime.fromtimestamp(selected.ts)
                _today  = datetime.now().date()
                if _c_dt.date() == _today:
                    lbl = _c_dt.strftime("%H:%M")
                else:
                    lbl = _c_dt.strftime("%m/%d/%Y %H:%M")
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
        # Count session candles for VP/VWAP diagnostic
        _sb = session_bounds()
        _n_sess = sum(1 for c in all_candles if c.ts >= _sb[2])
        footer   = (f" [E] ETH  [B] BTC  [F] Feed: {feed.upper()}  [C] {scheme_tag}  [I] {ivl_label}  [L] {mode_tag}  [W] {vwap_tag}  [V] {vp_tag}  [Q] Quit"
                    f"   {len(all_candles)} candles  sess:{_n_sess}  {feed.upper()}{auth_tag}{cinfo}{err_str}{hist_tag}")
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


# ── jump-to dialog ────────────────────────────────────────────────────────────
def jump_to_dialog(stdscr, db: "DoubleBuffer", rows: int, cols: int) -> str:
    """
    Draw a small input box in the centre of the screen and collect a
    date/time string from the user.  Returns the entered string (stripped)
    or "" if the user cancelled with Escape.
    Uses raw curses input so the main nodelay loop doesn't interfere.
    """
    prompt  = " Jump to (YYYY-MM-DD HH:MM or HH:MM):  "
    box_w   = len(prompt) + 20
    box_h   = 3
    box_y   = rows // 2 - box_h // 2
    box_x   = max(0, cols // 2 - box_w // 2)

    # Temporarily switch to blocking input with echo
    curses.curs_set(1)
    curses.echo()
    stdscr.nodelay(False)

    # Draw box
    for r in range(box_h):
        stdscr.addstr(box_y + r, box_x,
                      " " * min(box_w, cols - box_x),
                      curses.color_pair(C_CURSOR) | curses.A_BOLD)
    stdscr.addstr(box_y + 1, box_x, prompt,
                  curses.color_pair(C_CURSOR) | curses.A_BOLD)
    stdscr.refresh()

    # Collect input character by character
    input_buf = []
    input_x   = box_x + len(prompt)
    while True:
        try:
            ch = stdscr.get_wch()
        except Exception:
            break
        if isinstance(ch, str):
            code = ord(ch)
        else:
            code = ch
        if code in (27,):                      # Escape — cancel
            input_buf = []
            break
        elif code in (10, 13, curses.KEY_ENTER):  # Enter — confirm
            break
        elif code in (curses.KEY_BACKSPACE, 127, 8):
            if input_buf:
                input_buf.pop()
                # Erase last char on screen
                stdscr.addstr(box_y + 1, input_x + len(input_buf), " ",
                              curses.color_pair(C_CURSOR) | curses.A_BOLD)
        elif 32 <= code <= 126 and len(input_buf) < 20:
            input_buf.append(chr(code))
            stdscr.addstr(box_y + 1, input_x + len(input_buf) - 1,
                          chr(code), curses.color_pair(C_CURSOR) | curses.A_BOLD)
        stdscr.refresh()

    # Restore non-blocking + no echo
    curses.noecho()
    curses.curs_set(0)
    stdscr.nodelay(True)
    return "".join(input_buf).strip()


def parse_jump_target(s: str) -> int:
    """
    Parse user input into a UTC Unix timestamp.
    Accepts: YYYY-MM-DD HH:MM   or   HH:MM (assumes today local time).
    Returns 0 on parse failure.
    """
    s = s.strip()
    fmts = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%m/%d/%Y %H:%M",
        "%d/%m/%Y %H:%M",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.timestamp())
        except ValueError:
            pass
    # Try HH:MM — assume today local
    try:
        t = datetime.strptime(s, "%H:%M")
        now = datetime.now()
        dt  = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        return int(dt.timestamp())
    except ValueError:
        pass
    return 0


def jump_to_ts(target_ts: int, session: int):
    """
    Background: ensure target_ts is in loaded history, then position the
    viewport so the target candle is visible and the cursor is on it.
    Fetches history backwards if the target is older than what's loaded.
    """
    with state.lock:
        if state.session != session:
            return
        asset   = state.asset
        my_feed = state.feed

    # Fetch back until we have the target timestamp
    MAX_FETCHES = 20
    for _ in range(MAX_FETCHES):
        with state.lock:
            if state.session != session:
                return
            candles  = list(state.candles)
            n        = len(candles)
            oldest   = candles[0].ts  if n else 0
            newest   = candles[-1].ts if n else 0

        if oldest <= target_ts <= newest:
            break   # target is already loaded

        if target_ts < oldest:
            # Need to fetch older data
            with state.lock:
                state.error = f"Fetching history..."
            new_c = (fetch_kraken(asset, before_ts=oldest)
                     if my_feed == "kraken"
                     else fetch_phemex(asset, before_ts=oldest))
            new_c = [c for c in new_c if c.ts < oldest]
            if not new_c:
                break
            with state.lock:
                if state.session != session: return
                new_dq = collections.deque(new_c, maxlen=MAX_CANDLES)
                for c in state.candles:
                    new_dq.append(c)
                state.candles = new_dq
            time.sleep(0.2)
        else:
            break   # target is in the future — nothing to fetch

    # Now position the viewport
    with state.lock:
        if state.session != session:
            return
        candles = list(state.candles)
        n       = len(candles)

    if n == 0:
        return

    # Find the index of the candle closest to target_ts
    best_idx  = 0
    best_diff = abs(candles[0].ts - target_ts)
    for i, c in enumerate(candles):
        diff = abs(c.ts - target_ts)
        if diff < best_diff:
            best_diff = diff
            best_idx  = i

    # Place the target candle at the RIGHT edge of the viewport so it's
    # immediately visible, with the cursor on it (rightmost column = n_vis-1).
    # view_offset = n - best_idx - 1 puts target at the right edge.
    # cursor_col_idx = state.n_vis - 1 puts cursor on that rightmost candle.
    new_vo  = max(0, n - best_idx - 1)
    with state.lock:
        if state.session != session: return
        n_vis_cur = max(1, state.n_vis)
        state.view_offset    = new_vo
        state.cursor_col_idx = n_vis_cur - 1
        state.error          = ""

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
            elif key in (ord("g"), ord("G")):
                # Temporarily pause and open jump-to dialog
                user_input = jump_to_dialog(stdscr, db, rows, cols)
                if user_input:
                    target_ts = parse_jump_target(user_input)
                    if target_ts > 0:
                        with state.lock:
                            cur_sess = state.session
                        threading.Thread(
                            target=jump_to_ts,
                            args=(target_ts, cur_sess),
                            daemon=True).start()
                    else:
                        with state.lock:
                            state.error = "Invalid date. Use YYYY-MM-DD HH:MM or HH:MM"
                # Force full redraw after dialog closes
                db.prev = None
                stdscr.clear()
            elif key in (curses.KEY_LEFT,
                          curses.KEY_SLEFT,   # Shift+Left (most terminals)
                          541, 545,            # Ctrl+Left (xterm / Windows)
                          ord("["), ord("{")): # fallback skip keys
                # Determine step size
                if key in (curses.KEY_SLEFT, ord("[")):
                    step = 10
                elif key in (541, 545, ord("{")):
                    step = 50
                else:
                    step = 1
                with state.lock:
                    cci = state.cursor_col_idx
                    if cci < 0:
                        # Enter cursor mode at rightmost, then apply step
                        state.cursor_col_idx = max(0, len(state.candles) - 1)
                        cci = state.cursor_col_idx
                    # Move cursor left by step; overflow into pan
                    move = step
                    if cci >= move:
                        state.cursor_col_idx -= move
                    else:
                        state.view_offset    += move - cci
                        state.cursor_col_idx  = 0
            elif key in (curses.KEY_RIGHT,
                         curses.KEY_SRIGHT,   # Shift+Right
                         560, 564,             # Ctrl+Right (xterm / Windows)
                         ord("]"), ord("}")):  # fallback skip keys
                if key in (curses.KEY_SRIGHT, ord("]")):
                    step = 10
                elif key in (560, 564, ord("}")):
                    step = 50
                else:
                    step = 1
                with state.lock:
                    cci        = state.cursor_col_idx
                    vo         = state.view_offset
                    n_vis_now  = state.n_vis   # actual visible cols from last draw
                    n_loaded   = len(state.candles)
                    if cci < 0:
                        pass   # already live, nothing to do
                    else:
                        # Rightmost cursor position within the current visible window
                        right_edge = max(0, n_vis_now - 1)
                        move       = step
                        room_right = right_edge - cci  # steps until cursor hits wall
                        if move <= room_right:
                            # Cursor moves within window, no pan needed
                            state.cursor_col_idx = cci + move
                        else:
                            # Cursor reaches right edge, leftover steps become pan
                            move -= room_right
                            state.cursor_col_idx = right_edge
                            new_vo = max(0, vo - move)
                            if new_vo == 0:
                                # Fully unscrolled — snap to live
                                state.cursor_col_idx = -1
                                state.view_offset    = 0
                            else:
                                state.view_offset = new_vo
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
