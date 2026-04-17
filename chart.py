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
# Global mode asset colors (pairs 20-27)
C_G_BTC     = 20
C_G_ETH     = 21
C_G_USOIL   = 22
C_G_ZB      = 23
C_G_SPX     = 24
C_G_USDJPY  = 25
C_G_XAUUSD  = 26
C_G_NAS100  = 27
C_G_SHADE   = 28
C_BTD_BUY   = 29   # Big Trade buy signal
C_BTD_SELL  = 30   # Big Trade sell signal
# Session indicator border colors
C_SESS_NDO  = 31   # NDO         00:00-03:30  dark blue
C_SESS_MORN = 32   # Morning     08:30-10:30  cyan
C_SESS_EXCL = 33   # Exclusion   09:00-10:00  red (Wed/Thu)
C_SESS_LUNCH= 34   # Lunchtime   11:30-13:30  yellow
C_SESS_PWR  = 35   # Power Hour  14:00-15:00  magenta
C_SESS_EOD  = 36   # EOD/EEOD    18:30-00:00  green
# Alert color
C_ALERT     = 37   # alert box / triggered
C_VWAP      = 16   # VWAP line
C_VWAP_BAND = 17   # 0.5σ shaded band
C_VWAP_SD2  = 18   # 2σ / 2.5σ bands

# Global mode assets:
#   (label, phemex_symbol, kraken_pair, yahoo_symbol, color_pair)
# BTC/ETH → Phemex (primary) / Kraken (fallback)
# TradFi (indices, commodities, forex) → Yahoo Finance (free, no auth)
# Phemex CFD symbols tried first for TradFi assets (more reliable intraday data)
# Yahoo Finance used as fallback — note Yahoo CL=F can show roll artifacts
GLOBAL_ASSETS = [
    ("BTC",    "BTCUSDT",     "XBT/USD",  "BTC-USD",   C_G_BTC),
    ("ETH",    "ETHUSDT",     "ETH/USD",  "ETH-USD",   C_G_ETH),
    ("XAUUSD", "XAUUSDT",     "XAU/USD",  "GC=F",      C_G_XAUUSD),
    ("USDJPY", "uUSDJPYUSDT", None,       "JPY=X",     C_G_USDJPY),
    ("USOIL",  "XTIUSDT",     None,       "CL=F",      C_G_USOIL),
    ("SPX500", "SP500USDT",   None,       "^GSPC",     C_G_SPX),
    ("NAS100", "NAS100USDT",  None,       "^NDX",      C_G_NAS100),
    ("DXY",    "uDXYUSDT",    None,       "DX-Y.NYB",  C_G_ZB),
]

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
        self.show_help    = False   # help overlay visible
        self.help_scroll  = 0       # scroll offset in help overlay
        self.show_btd     = True    # Big Trade Detector overlay
        self.btd_lookback = 10      # lookback bars (matches TV default)
        self.btd_sigma    = 3.0     # sensitivity sigma (matches TV default)
        self.show_sessions = True   # Sessions indicator
        # Alerts: list of dicts with keys:
        #   name, condition, value, message, triggered, active, sound
        self.alerts        = []     # list of alert dicts
        self.alert_triggered  = []   # recently fired alerts for display
        self.show_alert_popup = False  # True = show latest alert banner
        self.show_alert_list  = False  # non-blocking alert list overlay
        self.alert_list_sel   = 0      # selected row in alert list
        self.show_econ_cal    = False  # economic calendar overlay
        self.econ_events      = []     # list of event dicts
        self.econ_loading     = False
        self.econ_impact_filter = {1, 2, 3}  # show * ** *** (all by default)
        self.econ_date_range  = "today"  # yesterday/today/tomorrow/week
        self.econ_scroll      = 0        # scroll offset in event list
        # Live trade lines drawn on chart
        # Each dict: {side, entry, sl, tp, size_phemex, size_xl, status}
        self.trade_lines    = []    # active/pending trade levels to draw
        self.trade_monitor  = False # True while trade monitor thread running
        # Indicator levels written by draw() for alert_monitor to read
        self.indicator_levels = {}  # {vwap, sd_p05, sd_m05, sd_p2, sd_m2, sd_p25, sd_m25, vah, val, poc}
        self.btd_buy_fired   = False  # set True when BTD buy fires on candle close
        self.btd_sell_fired  = False  # set True when BTD sell fires on candle close
        # Position tool: list of {entry,sl,tp,anchor_idx,side,active}
        self.pos_tools = []
        # Horizontal line drawings: [{price, label, alert, active}]
        self.hlines     = []
        self.show_hline_list = False
        self.hline_sel       = 0
        self.n_vis        = 0       # visible candle count, set by draw()
        self.global_mode      = False
        self.global_data      = {}
        self.global_ts        = []
        self.global_loading   = False
        self.global_view_off  = 0   # pan: data-points hidden off right
        self.global_cursor    = -1  # cursor column in global chart (-1=live)
        self.global_n_vis     = 0   # visible columns last drawn, set by draw_global()
        self.global_next_refresh = 0.0  # unix timestamp of next auto-refresh

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
    curses.init_pair(C_ASSET_SEL, curses.COLOR_WHITE,  curses.COLOR_BLUE)
    curses.init_pair(C_PRICE_LBL,  curses.COLOR_WHITE,  curses.COLOR_BLUE)
    curses.init_pair(C_PRICE_LINE, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_CURSOR,    curses.COLOR_BLACK,  curses.COLOR_WHITE)
    curses.init_pair(C_LINE,      curses.COLOR_CYAN,   -1)
    curses.init_pair(C_VP_NORM,   curses.COLOR_YELLOW, -1)
    curses.init_pair(C_VP_POC,    curses.COLOR_YELLOW, -1)
    curses.init_pair(C_VP_VA,     curses.COLOR_MAGENTA,-1)
    # Global mode asset line colors
    curses.init_pair(C_G_BTC,    curses.COLOR_YELLOW,  -1)
    curses.init_pair(C_G_ETH,    curses.COLOR_CYAN,    -1)
    curses.init_pair(C_G_USOIL,  curses.COLOR_MAGENTA, -1)
    curses.init_pair(C_G_ZB,     curses.COLOR_WHITE,   -1)
    curses.init_pair(C_G_SPX,    curses.COLOR_GREEN,   -1)
    curses.init_pair(C_G_USDJPY, curses.COLOR_RED,     -1)
    curses.init_pair(C_G_XAUUSD, 3,                    -1)
    curses.init_pair(C_G_NAS100, curses.COLOR_BLUE,    -1)
    curses.init_pair(C_G_SHADE,  8,                    -1)
    # BTD uses cyan/magenta — independent of the candle color scheme
    curses.init_pair(C_BTD_BUY,  curses.COLOR_CYAN,    -1)
    curses.init_pair(C_BTD_SELL, curses.COLOR_MAGENTA,  -1)
    # Session border colors
    curses.init_pair(C_SESS_NDO,  curses.COLOR_BLUE,    -1)
    curses.init_pair(C_SESS_MORN, curses.COLOR_CYAN,    -1)
    curses.init_pair(C_SESS_EXCL, curses.COLOR_RED,     -1)
    curses.init_pair(C_SESS_LUNCH,curses.COLOR_YELLOW,  -1)
    curses.init_pair(C_SESS_PWR,  curses.COLOR_MAGENTA, -1)
    curses.init_pair(C_SESS_EOD,  curses.COLOR_GREEN,   -1)
    curses.init_pair(C_ALERT,     curses.COLOR_YELLOW,  curses.COLOR_RED)
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
    Anchor adapts to interval:
      1m/3m/15m/1H → daily   (19:00 CT boundaries)
      4H           → weekly  (Sunday 19:00 CT)
      1D           → monthly (1st of month 00:00 local)
    """
    import datetime as _dt
    now        = datetime.now()
    resolution = cur_resolution()

    if resolution >= 86400:
        # 1D → monthly anchor: 1st of current month
        curr_start = datetime(now.year, now.month, 1, 0, 0, 0).timestamp()
        # Previous month
        if now.month == 1:
            prev_dt = datetime(now.year - 1, 12, 1, 0, 0, 0)
        else:
            prev_dt = datetime(now.year, now.month - 1, 1, 0, 0, 0)
        prev_start = prev_dt.timestamp()
        prev_end   = curr_start
    elif resolution >= 14400:
        # 4H → weekly anchor: most recent Sunday 19:00 CT
        days_since_sun = (now.weekday() + 1) % 7  # Sun=0
        this_sun = datetime(now.year, now.month, now.day, 19, 0, 0) -                    _dt.timedelta(days=days_since_sun)
        if now < this_sun:
            this_sun -= _dt.timedelta(weeks=1)
        curr_start = this_sun.timestamp()
        prev_start = (this_sun - _dt.timedelta(weeks=1)).timestamp()
        prev_end   = curr_start
    else:
        # Daily anchor: 19:00 CT
        today_open = datetime(now.year, now.month, now.day, 19, 0, 0)
        if now < today_open:
            curr_start = (today_open - _dt.timedelta(days=1)).timestamp()
        else:
            curr_start = today_open.timestamp()
        prev_start = curr_start - 86400
        prev_end   = curr_start

    return prev_start, prev_end, curr_start


# ── Global mode data fetching ─────────────────────────────────────────────────
def fetch_global_asset_phemex(symbol: str, resolution: int = 60,
                               limit: int = 300) -> list:
    """Fetch close prices from Phemex kline endpoint. Does not raise on HTTP errors."""
    path_  = "/exchange/public/md/v2/kline/last"
    query_ = f"symbol={symbol}&resolution={resolution}&limit={limit}"
    url_   = f"{PHEMEX_REST_URL}{path_}?{query_}"
    hdrs_  = _phemex_headers(path_, query_)
    try:
        r = requests.get(url_, headers=hdrs_, timeout=10)
        # Don't raise_for_status — read JSON regardless to get Phemex error code
        try:
            data = r.json()
        except Exception:
            return []
        code = data.get("code", -1)
        if code != 0:
            return []   # symbol unsupported — caller will try Kraken
        rows = data.get("data", {}).get("rows", [])
        if not rows:
            return []
        # row layout: [ts, interval, open, high, low, close, vol, turnover]
        # index 5 = close price for Phemex linear/CFD symbols
        return [(int(row[0]), float(row[5])) for row in reversed(rows)]
    except Exception:
        return []


def fetch_global_asset_yahoo(symbol: str, resolution: int = 60,
                              limit: int = 300) -> list:
    """
    Fetch OHLC from Yahoo Finance — free, no auth required.
    Maps ChartHacker resolution (seconds) to Yahoo interval/range params.
    Returns [(unix_ts, close), ...] oldest-first.
    """
    # Map resolution in seconds → Yahoo interval string + range
    if resolution <= 60:
        interval, yrange = "1m", "1d"
    elif resolution <= 300:
        interval, yrange = "5m", "5d"
    elif resolution <= 900:
        interval, yrange = "15m", "5d"
    elif resolution <= 3600:
        interval, yrange = "1h", "1mo"
    elif resolution <= 14400:
        interval, yrange = "1h", "3mo"
    else:
        interval, yrange = "1d", "1y"

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    headers = {"User-Agent": "Mozilla/5.0"}
    params  = {"interval": interval, "range": yrange}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=12)
        try:
            data = r.json()
        except Exception:
            return []
        result = data.get("chart", {}).get("result", [])
        if not result:
            return []
        chart  = result[0]
        ts_raw = chart.get("timestamp", [])
        closes = chart.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        if not ts_raw or not closes:
            return []
        pairs = []
        for ts, cl in zip(ts_raw, closes):
            if cl is not None and cl > 0:
                pairs.append((int(ts), float(cl)))
        return pairs[-limit:]
    except Exception:
        return []


def fetch_global_asset_kraken(pair: str, interval: int = 1,
                               limit: int = 300) -> list:
    """Fetch close prices from Kraken for a given pair."""
    url_ = f"{KRAKEN_REST_URL}/0/public/OHLC"
    try:
        r = requests.get(url_, params={"pair": pair, "interval": interval},
                         timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            return []
        result = data.get("result", {})
        rows   = next((v for k, v in result.items() if k != "last"), [])
        return [(row[0], float(row[4])) for row in rows[-limit:]]
    except Exception:
        return []


def load_global_data(session: int, initial: bool = False):
    """
    Fetch today's data for all GLOBAL_ASSETS in parallel.
    initial=True shows the loading screen; background refreshes keep existing data visible.
    """
    import concurrent.futures

    _now           = datetime.now()
    _today0        = datetime(_now.year, _now.month, _now.day, 0, 0, 0)
    today_start_ts = int(_today0.timestamp())
    resolution     = 60
    kraken_interval= 1

    if initial:
        with state.lock:
            state.global_loading = True
            state.error = "Global: fetching all assets..."

    def _fetch_one(args):
        label, phemex_sym, kraken_pair, yahoo_sym, _color = args
        with state.lock:
            if not state.global_mode:
                return label, []
        data = []
        if phemex_sym:
            data = fetch_global_asset_phemex(phemex_sym, resolution=resolution, limit=1440)
        if not data and kraken_pair:
            data = fetch_global_asset_kraken(kraken_pair, interval=kraken_interval, limit=1440)
        if not data and yahoo_sym:
            data = fetch_global_asset_yahoo(yahoo_sym, resolution=resolution, limit=1440)
        data = [(ts, cl) for ts, cl in data if ts >= today_start_ts]
        return label, data

    raw    = {}
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(GLOBAL_ASSETS)) as ex:
        futures = {ex.submit(_fetch_one, asset): asset[0] for asset in GLOBAL_ASSETS}
        for fut in concurrent.futures.as_completed(futures):
            label, data = fut.result()
            if data:
                raw[label] = data
            else:
                errors.append(label)

    if not raw:
        with state.lock:
            state.global_loading = False
            state.error = "Global: no data — check connection"
        return

    ref_pairs = max(raw.values(), key=len)
    all_ts    = sorted(set(ts for ts, _ in ref_pairs))

    aligned = {}
    for label, pairs in raw.items():
        d      = dict(pairs)
        last   = 0.0
        closes = []
        for ts in all_ts:
            if ts in d and d[ts] > 0:
                last = d[ts]
            closes.append(last)
        aligned[label] = closes

    n_ok = len(raw)
    n_tot = len(GLOBAL_ASSETS)
    msg   = f"Global: {n_ok}/{n_tot} assets" + (f"  skip:{','.join(errors)}" if errors else "")
    with state.lock:
        if not state.global_mode:
            state.global_loading = False
            return
        state.global_ts      = all_ts
        state.global_data    = aligned
        state.global_loading = False
        state.error          = msg


GLOBAL_REFRESH_SECS = 30

def _global_refresh_loop(session: int):
    """Initial load then refresh every 30s while global_mode is active."""
    with state.lock:
        if not state.global_mode:
            return
    load_global_data(session, initial=True)
    while True:
        # Set the next-refresh timestamp for the countdown display
        next_at = time.time() + GLOBAL_REFRESH_SECS
        with state.lock:
            state.global_next_refresh = next_at
        for _ in range(GLOBAL_REFRESH_SECS):
            time.sleep(1)
            with state.lock:
                if not state.global_mode:
                    return
        with state.lock:
            if not state.global_mode:
                return
        load_global_data(session, initial=False)
        with state.lock:
            state.global_next_refresh = time.time() + GLOBAL_REFRESH_SECS


def start_global(session: int):
    """Start the global refresh loop (handles initial load + 30s auto-refresh)."""
    threading.Thread(target=_global_refresh_loop, args=(session,), daemon=True).start()

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
        show_help         = state.show_help
        help_scroll       = state.help_scroll
        show_btd          = state.show_btd
        btd_lookback      = state.btd_lookback
        btd_sigma         = state.btd_sigma
        show_sessions     = state.show_sessions
        alert_triggered   = list(state.alert_triggered)
        show_alert_popup  = state.show_alert_popup
        show_alert_list   = state.show_alert_list
        alert_list_sel    = state.alert_list_sel
        show_econ_cal     = state.show_econ_cal
        econ_events       = list(state.econ_events)
        econ_loading      = state.econ_loading
        econ_filter       = set(state.econ_impact_filter)
        econ_date_range   = state.econ_date_range
        econ_scroll       = state.econ_scroll
        trade_lines       = list(state.trade_lines)
        pos_tools         = list(state.pos_tools)
        hlines            = list(state.hlines)
        show_hline_list   = state.show_hline_list
        hline_sel         = state.hline_sel
        global_mode       = state.global_mode

    all_candles = candles_snap + ([live] if live else [])

    # In global mode, skip the normal chart and render the performance view
    if global_mode:
        draw_global(db, rows, cols)
        return

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

    # Current session badge — shows which session is active right now
    _now_h = datetime.now()
    _now_m = _now_h.hour * 60 + _now_h.minute
    _wday  = _now_h.weekday()  # Mon=0 Sun=6
    _SESS_DEFS = [
        ("Excl",  9*60,   10*60,  C_SESS_EXCL,  [2,3]),
        ("NDO",   0,       3*60+30, C_SESS_NDO,  None),
        ("Morn",  8*60+30, 10*60+30, C_SESS_MORN, None),
        ("Lunch", 11*60+30,13*60+30, C_SESS_LUNCH,None),
        ("PWR",   14*60,   15*60,   C_SESS_PWR,  None),
        ("EOD",   18*60+30,23*60+59, C_SESS_EOD,  None),
    ]
    _cur_sess_lbl  = ""
    _cur_sess_pair = C_HEADER
    for _sn, _ss, _se, _sc, _sdays in _SESS_DEFS:
        if _sdays and _wday not in _sdays:
            continue
        if _ss <= _now_m < _se:
            _cur_sess_lbl  = f" {_sn} "
            _cur_sess_pair = _sc
            break
    if _cur_sess_lbl:
        db.puts(1, col + 1, _cur_sess_lbl, _cur_sess_pair,
                curses.A_BOLD | curses.A_REVERSE)
        col += len(_cur_sess_lbl) + 2
    else:
        db.puts(1, col + 1, " NO SESSION ", C_SESS_EXCL,
                curses.A_BOLD | curses.A_REVERSE)
        col += 14

    db.puts(1, col + 1, f"[E]TH  [B]TC  [F]eed  [C]olor  [W]AP  [V]P  [T]rades  [I]{ivl_label}  [L]ine  [M]macro  [G]oto  [<][>]x1  [[]x10  [{{}}]x50  [Esc]live  [P]shot  [H]elp  [Q]uit", C_HEADER)

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
            # Write VP levels to state for alert_monitor
            with state.lock:
                _lvl = state.indicator_levels
                _lvl["vah"] = vah_price
                _lvl["val"] = val_price
                _lvl["poc"] = poc_price

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
                    _axis_lbl(_vw_last + 0.5*_sd_last, "+.5s", C_VWAP_BAND, curses.A_DIM)
                    _axis_lbl(_vw_last - 0.5*_sd_last, "-.5s", C_VWAP_BAND, curses.A_DIM)
                    _axis_lbl(_vw_last + 2.0*_sd_last, "+2s",  C_VWAP_SD2)
                    _axis_lbl(_vw_last - 2.0*_sd_last, "-2s",  C_VWAP_SD2)
                    _axis_lbl(_vw_last + 2.5*_sd_last, "+2.5s",C_VWAP_SD2, curses.A_DIM)
                    _axis_lbl(_vw_last - 2.5*_sd_last, "-2.5s",C_VWAP_SD2, curses.A_DIM)


    # ── SESSIONS INDICATOR ───────────────────────────────────────────────────
    # Shows session windows as price-range boxes with a bottom indicator bar.
    # Each box spans the session's actual high-low price range.
    # Bottom bar: a solid colored strip in the time axis row showing session span.
    # Hidden on 1H, 4H, 1D timeframes.
    _sess_resolution = cur_resolution()
    # Sessions only shown on 1m and 3m — on 15m+ a single candle spans 15+ min
    # which causes sessions to smear across the chart incorrectly.
    if show_sessions and visible and chart_h > 0 and _sess_resolution <= 180:
        SESSIONS = [
            ("NDO",   (0,  0), (3, 30), C_SESS_NDO,   None),
            ("Morn",  (8, 30), (10,30), C_SESS_MORN,  None),
            ("Excl",  (9,  0), (10, 0), C_SESS_EXCL,  [2, 3]),
            ("Lunch", (11,30), (13,30), C_SESS_LUNCH,  None),
            ("PWR",   (14, 0), (15, 0), C_SESS_PWR,   None),
            ("EOD",   (18,30), (23,59), C_SESS_EOD,   None),
        ]
        _sc_sess = chart_r - n_vis

        for _sname, _sstart, _send, _scol, _sdays in SESSIONS:
            _sh, _sm = _sstart;  _eh, _em = _send
            _s_mins = _sh * 60 + _sm;  _e_mins = _eh * 60 + _em

            # Group candles by calendar date so sessions from different days
            # don't merge into one giant box
            from itertools import groupby as _groupby
            def _date_key(pair):
                _i, _c = pair
                return datetime.fromtimestamp(_c.ts).date()

            _indexed = [(i, c) for i, c in enumerate(visible)]
            for _day, _day_group in _groupby(_indexed, _date_key):
                _day_candles = list(_day_group)
                _sess_cols = []
                _sess_hi   = None
                _sess_lo   = None

                for _i, _candle in _day_candles:
                    _col = _sc_sess + _i
                    if not (0 <= _col < chart_r):
                        continue
                    _dt   = datetime.fromtimestamp(_candle.ts)
                    _wday = _dt.weekday()
                    if _sdays and _wday not in _sdays:
                        continue
                    _mins = _dt.hour * 60 + _dt.minute
                    if _s_mins <= _mins < _e_mins:
                        _sess_cols.append(_col)
                        if _sess_hi is None or _candle.h > _sess_hi:
                            _sess_hi = _candle.h
                        if _sess_lo is None or _candle.l < _sess_lo:
                            _sess_lo = _candle.l

                if not _sess_cols or _sess_hi is None:
                    continue

                _c0  = _sess_cols[0]
                _c1  = _sess_cols[-1]

                _r_top = max(chart_top,     chart_top + p2r(_sess_hi))
                _r_bot = min(chart_bot - 1, chart_top + p2r(_sess_lo))

                for _bc in range(_c0, _c1 + 1):
                    if 0 <= _bc < chart_r:
                        db.put(_r_top, _bc, "-", _scol, curses.A_BOLD)
                if _r_bot != _r_top:
                    for _bc in range(_c0, _c1 + 1):
                        if 0 <= _bc < chart_r:
                            db.put(_r_bot, _bc, "-", _scol, curses.A_BOLD)
                for _r in range(_r_top, _r_bot + 1):
                    if chart_top <= _r < chart_bot and db.buf[_r][_c0][0] in (" ", ":", "."):
                        db.put(_r, _c0, "|", _scol, curses.A_BOLD)
                for _r in range(_r_top, _r_bot + 1):
                    if chart_top <= _r < chart_bot and db.buf[_r][_c1][0] in (" ", ":", "."):
                        db.put(_r, _c1, "|", _scol, curses.A_BOLD)
                if _c0 + 1 < chart_r:
                    db.puts(_r_top, _c0 + 1, _sname, _scol, curses.A_BOLD)
                for _bc in range(_c0, _c1 + 1):
                    if 0 <= _bc < chart_r:
                        db.put(time_row, _bc, "=", _scol, curses.A_BOLD | curses.A_REVERSE)


    # ── PERIOD SEPARATOR — 19:00 CT vertical line ───────────────────────────
    # Mark each candle column whose local timestamp crosses the 19:00 session
    # open. Draws a dim `:` column behind candles. Hidden on 1D timeframe.
    if visible and cur_resolution() < 86400:
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


    # ── BIG TRADE DETECTOR ───────────────────────────────────────────────────
    # Intrabar buy/sell volume intensity z-score anomaly detection.
    # Cooldown: after a signal fires, suppress the same side for 3 candles
    # to prevent double-prints on consecutive high-volume candles.
    # Colors: cyan buys / magenta sells — independent of candle color scheme.
    # Visibility tiers:
    #   T1 (>σ):     solid block █ bold — clearly distinct from candle chars
    #   T2 (>σ+1.5): double block ██ bold
    #   T3 (>σ+3.0): triple block ███ reversed — maximum contrast
    BTD_COOLDOWN = 1   # suppress only immediate repeat (same candle index)
    if show_btd and n_all >= btd_lookback + 2:
        _clist   = all_candles
        _buy_iv  = []
        _sell_iv = []
        for _c in _clist:
            _rng = _c.h - _c.l
            if _rng > 0:
                _buy_iv.append((_c.c - _c.l) / _rng * _c.v)
                _sell_iv.append((_c.h - _c.c) / _rng * _c.v)
            else:
                _buy_iv.append(0.0)
                _sell_iv.append(0.0)

        _sc = chart_r - n_vis
        _last_buy_signal  = -999   # index of last buy signal (for cooldown)
        _last_sell_signal = -999   # index of last sell signal

        for _i, _c in enumerate(visible):
            _col = _sc + _i
            if not (0 <= _col < chart_r):
                continue
            _ci = left_idx + _i
            if _ci < btd_lookback:
                continue

            _wb = _buy_iv[max(0, _ci - btd_lookback) : _ci]
            _ws = _sell_iv[max(0, _ci - btd_lookback) : _ci]
            if len(_wb) < 2:
                continue

            _n   = len(_wb)
            _mb  = sum(_wb) / _n
            _ms  = sum(_ws) / _n
            _sdb = (sum((x - _mb) ** 2 for x in _wb) / (_n - 1)) ** 0.5
            _sds = (sum((x - _ms) ** 2 for x in _ws) / (_n - 1)) ** 0.5

            _cb = _buy_iv[_ci]
            _cs = _sell_iv[_ci]

            _t1b = _mb + _sdb * btd_sigma
            _t2b = _mb + _sdb * (btd_sigma + 1.5)
            _t3b = _mb + _sdb * (btd_sigma + 3.0)
            _t1s = _ms + _sds * btd_sigma
            _t2s = _ms + _sds * (btd_sigma + 1.5)
            _t3s = _ms + _sds * (btd_sigma + 3.0)

            # Rows: buys just below wick low, sells just above wick high
            _row_b = min(chart_bot - 1, chart_top + p2r(_c.l) + 1)
            _row_s = max(chart_top,     chart_top + p2r(_c.h) - 1)

            _rev = curses.A_BOLD | curses.A_REVERSE   # highlighted block attr

            # ── Buy signal (cyan highlighted block below wick low) ────────────
            if (_cb > _t1b
                    and chart_top <= _row_b < chart_bot
                    and _ci - _last_buy_signal >= BTD_COOLDOWN):
                _last_buy_signal = _ci
                # Signal on live candle → notify alert_monitor
                if _ci >= len(all_candles) - 1:
                    state.btd_buy_fired  = True
                    state.btd_sell_fired = False
                if _cb > _t3b:
                    # T3: 3-wide × 2-tall — unmissable
                    for _dx in range(-1, 2):
                        if 0 <= _col + _dx < chart_r:
                            db.put(_row_b, _col + _dx, "#", C_BTD_BUY, _rev)
                            if _row_b + 1 < chart_bot:
                                db.put(_row_b + 1, _col + _dx, "#", C_BTD_BUY, _rev)
                elif _cb > _t2b:
                    # T2: 1-wide × 2-tall reversed
                    db.put(_row_b, _col, "#", C_BTD_BUY, _rev)
                    if _row_b + 1 < chart_bot:
                        db.put(_row_b + 1, _col, "#", C_BTD_BUY, _rev)
                else:
                    # T1: single reversed block
                    db.put(_row_b, _col, "#", C_BTD_BUY, _rev)

            # ── Sell signal (magenta highlighted block above wick high) ────────
            if (_cs > _t1s
                    and chart_top <= _row_s < chart_bot
                    and _ci - _last_sell_signal >= BTD_COOLDOWN):
                _last_sell_signal = _ci
                # Signal on live candle → notify alert_monitor
                if _ci >= len(all_candles) - 1:
                    state.btd_sell_fired  = True
                    state.btd_buy_fired   = False
                if _cs > _t3s:
                    # T3: 3-wide × 2-tall
                    for _dx in range(-1, 2):
                        if 0 <= _col + _dx < chart_r:
                            db.put(_row_s, _col + _dx, "#", C_BTD_SELL, _rev)
                            if _row_s - 1 >= chart_top:
                                db.put(_row_s - 1, _col + _dx, "#", C_BTD_SELL, _rev)
                elif _cs > _t2s:
                    # T2: 1-wide × 2-tall reversed
                    db.put(_row_s, _col, "#", C_BTD_SELL, _rev)
                    if _row_s - 1 >= chart_top:
                        db.put(_row_s - 1, _col, "#", C_BTD_SELL, _rev)
                else:
                    # T1: single reversed block
                    db.put(_row_s, _col, "#", C_BTD_SELL, _rev)


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
            _res = cur_resolution()
            # Label frequency and format by interval
            if _res <= 60:       # 1m  → every 15min
                _tick_mins = 15;  _lbl_fmt = "%H:%M"; _gap = 6
            elif _res <= 180:    # 3m  → every 30min
                _tick_mins = 30;  _lbl_fmt = "%H:%M"; _gap = 5
            elif _res <= 900:    # 15m → every hour
                _tick_mins = 60;  _lbl_fmt = "%H:%M"; _gap = 5
            elif _res <= 3600:   # 1H  → every 6h with date
                _tick_mins = 360; _lbl_fmt = "%m/%d %H:%M"; _gap = 12
            elif _res <= 14400:  # 4H  → each candle date
                _tick_mins = 240; _lbl_fmt = "%m/%d"; _gap = 6
            else:                # 1D  → weekly grouping
                _tick_mins = 1440;_lbl_fmt = "%m/%d"; _gap = 6

            MIN_LABEL_GAP  = _gap
            last_label_col = -MIN_LABEL_GAP - 1

            # On 1D every candle IS midnight, so "day break" would fire every
            # candle.  Only use day-break special treatment on sub-daily intervals.
            _sub_daily = (_res < 86400)

            # For 1D: label every Nth candle so labels don't collide.
            # Calculate how many candles fit per label based on label width.
            _lbl_sample = datetime.now().strftime(_lbl_fmt)
            _lbl_w      = max(MIN_LABEL_GAP, len(_lbl_sample) + 1)
            # cols per candle (floating)
            _cols_per_candle = max(1.0, chart_r / max(1, n_vis))
            # How many candles between labels
            _candle_stride = max(1, int(_lbl_w / _cols_per_candle))

            for i, candle in enumerate(visible):
                lbl_col = start_col + i
                if not (0 <= lbl_col <= chart_r - 8):
                    continue
                dt = datetime.fromtimestamp(candle.ts)
                _total_mins = dt.hour * 60 + dt.minute

                _is_day_break = _sub_daily and (
                    (dt.hour == 0 and dt.minute == 0) or
                    (i > 0 and datetime.fromtimestamp(visible[i-1].ts).date() != dt.date())
                )

                if _is_day_break and lbl_col - last_label_col >= 5:
                    lbl = dt.strftime("%m/%d")
                    db.puts(time_row, lbl_col, lbl, C_LABEL,
                            curses.A_BOLD | curses.A_UNDERLINE)
                    last_label_col = lbl_col
                elif (i % _candle_stride == 0
                      and lbl_col - last_label_col >= MIN_LABEL_GAP):
                    lbl = dt.strftime(_lbl_fmt)
                    db.puts(time_row, lbl_col, lbl, C_LABEL, curses.A_NORMAL)
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
        btd_tag  = "T:ON" if show_btd else "T:OFF"
        sess_tag = "S:ON" if show_sessions else "S:OFF"
        n_alerts = len(state.alerts)
        footer   = (f" [E] ETH  [B] BTC  [F] Feed: {feed.upper()}  [C] {scheme_tag}  [I] {ivl_label}  [L] {mode_tag}  [W] {vwap_tag}  [V] {vp_tag}  [T] {btd_tag}  [S] {sess_tag}  [A] {n_alerts}alrt  [Q] Quit"
                    f"   {len(all_candles)} candles  sess:{_n_sess}  {feed.upper()}{auth_tag}{cinfo}{err_str}{hist_tag}")
        db.puts(footer_row, 0, footer.ljust(cols)[:cols], C_AXIS)

    # Alert evaluation runs in alert_monitor() background thread
    # ── ALERT OVERLAY ────────────────────────────────────────────────────────
    if alert_triggered:
        _latest = alert_triggered[0]
        _abox_w = min(cols - 4, 54)
        _abox_y = chart_top + 1
        _abox_x = max(0, cols // 2 - _abox_w // 2)
        # Title
        db.puts(_abox_y, _abox_x,
                f" *** ALERT: {_latest['name']} @ {_latest['time']} ***".ljust(_abox_w)[:_abox_w],
                C_ALERT, curses.A_BOLD | curses.A_REVERSE)
        # Message
        db.puts(_abox_y + 1, _abox_x,
                f" {_latest['message'][:_abox_w - 2]}".ljust(_abox_w)[:_abox_w],
                C_ALERT, curses.A_BOLD)
        db.puts(_abox_y + 2, _abox_x,
                f" [A] Alert list  [Esc] Dismiss".ljust(_abox_w)[:_abox_w],
                C_AXIS, curses.A_DIM)

    # ── HORIZONTAL LINES ─────────────────────────────────────────────
    if hlines:
        draw_hlines(db, chart_top, chart_bot, chart_r, p2r, hlines)

    # ── POSITION TOOLS ───────────────────────────────────────────────
    if pos_tools:
        draw_pos_tools(db, chart_top, chart_bot, chart_r, p2r,
                       all_candles, left_idx, n_vis, pos_tools)

    # ── ALERT PRICE LINES ──────────────────────────────────────────
    # Draw a horizontal line on chart for each active alert price,
    # but only if no other indicator occupies that row.
    with state.lock:
        _alert_lines = list(state.alerts)
    for _al in _alert_lines:
        if not _al.get("active"):  continue
        _conds = _al.get("conditions", [])
        if not _conds:  continue
        _av = float(_conds[0].get("value", 0))
        if _av <= 0:  continue
        _ar = chart_top + p2r(_av)
        if not (chart_top <= _ar < chart_bot):  continue
        # Draw dashed line only on empty/background cells
        for _ac2 in range(0, chart_r, 2):
            if db.buf[_ar][_ac2][0] in (" ", ".", ":"):  
                db.put(_ar, _ac2, "-", C_ALERT, curses.A_DIM)
        # Label at right axis (only if row is not already occupied)
        _lbl_col = max(0, chart_r - 16)
        if db.buf[_ar][_lbl_col][0] in (" ", "-", "."):
            _al_lbl = f" A:{_av:.2f}"[:16]
            db.puts(_ar, _lbl_col, _al_lbl, C_ALERT, curses.A_BOLD)

    # ── HLINE LIST OVERLAY ──────────────────────────────────────────
    if show_hline_list:
        draw_hline_list_overlay(db, rows, cols, hlines, hline_sel)

    # ── ALERT LIST OVERLAY ──────────────────────────────────────────
    if show_alert_list:
        draw_alert_list_overlay(db, rows, cols, alert_list_sel)

    # ── ECON CALENDAR OVERLAY ────────────────────────────────────────
    if show_econ_cal:
        draw_econ_cal_overlay(db, rows, cols, econ_events, econ_loading,
                              econ_filter, econ_date_range, econ_scroll)

    # ── HELP OVERLAY (drawn last, on top of everything) ──────────────
    if show_help:
        draw_help_overlay(db, rows, cols, help_scroll)


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



# ── Global performance chart renderer ────────────────────────────────────────
def draw_global(db: "DoubleBuffer", rows: int, cols: int):
    """
    Comparative performance chart for today (00:00–23:59 CT).
    All data auto-fits into the terminal — no scrolling needed.
    Each asset normalised to 0% at the first data point of the day.
    Asia session (00:00–02:00 CT) is shaded.
    """
    with state.lock:
        g_ts     = list(state.global_ts)
        g_data   = {k: list(v) for k, v in state.global_data.items()}
        loading  = state.global_loading
        gerr     = state.error
        g_v_off  = state.global_view_off
        g_cursor     = state.global_cursor
        g_next_ref   = state.global_next_refresh

    # ── layout ────────────────────────────────────────────────────────────────
    HEADER_H = 2
    LEGEND_W = 32
    FOOTER_H = 1
    TIME_H   = 1
    chart_top = HEADER_H
    chart_bot = rows - FOOTER_H - TIME_H - 1
    chart_h   = max(1, chart_bot - chart_top)
    # Adaptive legend width: shrink if terminal too narrow
    _eff_legend = min(LEGEND_W, max(0, cols - 20))
    chart_r   = max(10, cols - _eff_legend - 2)
    time_row  = chart_bot
    footer_row= rows - 1

    # ── Header ────────────────────────────────────────────────────────────────
    for r in range(HEADER_H):
        db.puts(r, 0, " " * cols, C_HEADER, curses.A_BOLD)
    db.puts(0, 2, "Q U A N T A S S E T  |  ChartHacker  —  MACRO", C_HEADER, curses.A_BOLD)
    now_str = datetime.now().strftime("%m/%d/%Y  %H:%M:%S")
    db.puts(0, max(0, cols - len(now_str) - 2), now_str, C_LABEL, curses.A_BOLD)
    # Refresh countdown
    _secs_left = max(0, int(g_next_ref - time.time())) if g_next_ref > 0 else 0
    _cd_str    = f"  refresh in {_secs_left}s" if not loading else ""
    db.puts(1, 2, f"[M] Exit   [R] Refresh{_cd_str}   [P] Screenshot   [Q] Quit   Today 00:00-now CT", C_HEADER)

    # ── Loading / error state ─────────────────────────────────────────────────
    if loading or not g_ts:
        msg = "Fetching global data (parallel)..." if loading else "No data — press [R] to retry"
        db.puts(chart_top + chart_h // 2, max(0, cols // 2 - len(msg) // 2),
                msg, C_AXIS, curses.A_BOLD)
        if gerr and not loading:
            db.puts(chart_top + chart_h // 2 + 1,
                    max(0, cols // 2 - len(gerr) // 2),
                    gerr[:cols - 2], C_BEAR, curses.A_BOLD)
        db.puts(footer_row, 0, " MACRO MODE  [R] Retry  [M] Exit".ljust(cols)[:cols],
                C_ASSET_SEL, curses.A_BOLD)
        return

    n_total = len(g_ts)
    if n_total == 0:
        return

    # ── Viewport: pan with g_v_off (0 = rightmost/live end) ──────────────────
    g_v_off  = max(0, min(n_total - 1, g_v_off))
    with state.lock:
        state.global_view_off = g_v_off

    right_idx = n_total - g_v_off
    # Auto-fit: all data → chart_r cols when not panned; fewer when panned
    view_n    = right_idx              # data points to show (up to right_idx)
    step      = max(1.0, view_n / chart_r)

    col_ts     = []
    col_closes = {label: [] for label in g_data}
    for col in range(chart_r):
        raw_idx = int(col * step)
        idx     = min(right_idx - 1, max(0, raw_idx))
        col_ts.append(g_ts[idx])
        for label, closes in g_data.items():
            col_closes[label].append(closes[idx] if idx < len(closes) else 0.0)

    # ── Normalise to % change from the FIRST visible column ──────────────────
    pct_series = {}
    raw_series = {}   # actual close prices per column for each asset
    for label, col_cl in col_closes.items():
        base = next((v for v in col_cl if v > 0), 0.0)
        if base == 0:
            continue
        pct_series[label] = [(v / base - 1.0) * 100.0 if v > 0 else 0.0
                             for v in col_cl]
        raw_series[label] = col_cl

    if not pct_series:
        db.puts(chart_top + chart_h // 2, cols // 2 - 10,
                "No pct data", C_AXIS, curses.A_BOLD)
        return

    # ── Y range ───────────────────────────────────────────────────────────────
    all_vals = [v for pcts in pct_series.values() for v in pcts]
    y_min = min(all_vals);  y_max = max(all_vals)
    y_span = y_max - y_min or 0.01
    pad    = y_span * 0.08
    y_min -= pad;  y_max += pad;  y_span = y_max - y_min

    def pct_to_row(pct):
        frac = (pct - y_min) / y_span
        return max(chart_top, min(chart_bot - 1,
               chart_top + int((1.0 - frac) * (chart_h - 1))))

    # ── Asia session shading (00:00–02:00 CT) ─────────────────────────────────
    for col, ts in enumerate(col_ts):
        if not (0 <= col < chart_r):
            continue
        dt = datetime.fromtimestamp(ts)
        if 0 <= dt.hour < 2:
            for r in range(chart_top, chart_bot):
                if db.buf[r][col][0] == " ":
                    db.put(r, col, " ", C_G_SHADE, curses.A_DIM)

    # ── Zero line ─────────────────────────────────────────────────────────────
    zero_row = pct_to_row(0.0)
    for col in range(chart_r):
        if db.buf[zero_row][col][0] == " ":
            db.put(zero_row, col, "-", C_AXIS, curses.A_DIM)

    # ── Y-axis ────────────────────────────────────────────────────────────────
    for r in range(chart_top, chart_bot + 1):
        db.put(r, chart_r, "|", C_AXIS)
    n_ylbl = max(2, chart_h // 5)
    for yi in range(n_ylbl + 1):
        pct = y_min + (yi / n_ylbl) * y_span
        row = pct_to_row(pct)
        if chart_top <= row < chart_bot:
            db.puts(row, chart_r + 1, f"{pct:+.2f}%", C_LABEL)

    # ── Time axis ─────────────────────────────────────────────────────────────
    db.puts(time_row, 0, "-" * chart_r + "+", C_AXIS)
    lbl_every = max(1, chart_r // 10)
    for col in range(0, chart_r, lbl_every):
        if col < len(col_ts) and col + 4 < chart_r:
            dt  = datetime.fromtimestamp(col_ts[col])
            lbl = dt.strftime("%H:%M")
            db.puts(time_row, col, lbl, C_LABEL)

    # ── Asset lines ───────────────────────────────────────────────────────────
    final_pcts   = {}
    final_prices = {}
    in_cursor_g  = (g_cursor >= 0)
    for label, pcts in pct_series.items():
        color_pair = next((c for l, _p, _k, _y, c in GLOBAL_ASSETS if l == label), C_LABEL)
        prev_r = None
        for col, pct in enumerate(pcts):
            if not (0 <= col < chart_r):
                continue
            is_sel = in_cursor_g and col == g_cursor
            row    = pct_to_row(pct)
            char   = "+" if is_sel else "*"
            attrs  = curses.A_BOLD | curses.A_REVERSE if is_sel else curses.A_BOLD
            db.put(row, col, char, color_pair, attrs)
            if prev_r is not None and abs(row - prev_r) > 1:
                for r in range(min(row, prev_r) + 1, max(row, prev_r)):
                    if db.buf[r][col][0] in (" ", "-"):
                        db.put(r, col, "|", color_pair, curses.A_DIM)
            prev_r = row
        # Cursor or live end values for labels
        ref_col = g_cursor if in_cursor_g and 0 <= g_cursor < len(pcts) else len(pcts) - 1
        if pcts and ref_col >= 0:
            final_pcts[label]   = pcts[ref_col]
            # Try live ticker from Phemex for current price
            _base_price = raw_series.get(label, [0.0] * len(pcts))[ref_col]
            # Use live last_price for ETH/BTC if watching that asset
            with state.lock:
                _is_live_asset = (label in ("ETH","BTC") and
                                  label[:3] == state.asset and
                                  state.last_price > 0)
                _live_p = state.last_price if _is_live_asset else 0.0
            final_prices[label] = _live_p if _live_p > 0 else _base_price

    # ── Cursor crosshair ──────────────────────────────────────────────────────
    if in_cursor_g and 0 <= g_cursor < chart_r:
        for r in range(chart_top, chart_bot):
            if db.buf[r][g_cursor][0] in (" ", "-"):
                db.put(r, g_cursor, ":", C_AXIS, curses.A_DIM)
        if g_cursor < len(col_ts):
            cur_lbl = datetime.fromtimestamp(col_ts[g_cursor]).strftime("%H:%M")
            db.puts(time_row, max(0, g_cursor - 2), cur_lbl, C_CURSOR, curses.A_BOLD)

    # ── Legend: each label placed at its line's actual row on the right axis ──
    # Compute desired row for each asset, then resolve collisions by nudging.
    legend_x   = chart_r + 1
    label_rows = {}   # label → desired axis row
    for label, pct in final_pcts.items():
        label_rows[label] = pct_to_row(pct)

    # Resolve collisions: sort by desired row, nudge duplicates ±1
    used_rows  = {}   # row → label already placed
    placed     = {}   # label → final row
    for label in sorted(label_rows, key=lambda l: label_rows[l]):
        desired = label_rows[label]
        row     = desired
        # Search outward for a free row
        for delta in [0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5]:
            candidate = desired + delta
            if chart_top <= candidate < chart_bot and candidate not in used_rows:
                row = candidate
                break
        used_rows[row] = label
        placed[label]  = row

    # Fetch live ticker prices for crypto assets directly from state
    _live_price_map = {}
    with state.lock:
        _live_price_map["ETH"] = state.last_price if state.asset == "ETH" else 0.0
        _live_price_map["BTC"] = state.last_price if state.asset == "BTC" else 0.0

    for label, row in placed.items():
        pct        = final_pcts[label]
        color_pair = next((c for l, _p, _k, _y, c in GLOBAL_ASSETS if l == label), C_LABEL)
        price      = _live_price_map.get(label) or final_prices.get(label, 0.0)
        price_str  = f"(${price:,.2f})" if price > 0 else ""
        lbl        = f"{label:<7}{pct:+.2f}% {price_str}"
        true_row   = label_rows[label]
        if chart_top <= true_row < chart_bot:
            db.put(true_row, chart_r, "+", color_pair, curses.A_BOLD)
        if chart_top <= row < chart_bot:
            db.puts(row, legend_x, lbl, color_pair, curses.A_BOLD)

    # ── Footer ────────────────────────────────────────────────────────────────
    with state.lock:
        _macro_err = state.error
    _macro_footer_base = (f" MACRO  {n_total} pts  {len(pct_series)}/{len(GLOBAL_ASSETS)} assets"
                          f"  next refresh:{_secs_left}s  [R] refresh  [P] shot  [M] exit  [Q] quit")
    if _macro_err:
        _macro_footer = _macro_footer_base + f"  | {_macro_err}"
    else:
        _macro_footer = _macro_footer_base
    db.puts(footer_row, 0, _macro_footer.ljust(cols)[:cols], C_ASSET_SEL, curses.A_BOLD)



# ── Trade dialog ─────────────────────────────────────────────────────────────
def trade_dialog(stdscr, rows: int, cols: int) -> dict | None:
    """
    Multi-step dialog to build and submit a copycat trade order.
    Returns submitted trade dict or None if cancelled.
    """
    import subprocess, os
    stdscr.nodelay(False)
    curses.curs_set(0)

    box_w = min(cols - 4, 80)
    box_h = 26
    box_y = max(0, rows // 2 - box_h // 2)
    box_x = max(0, cols // 2 - box_w // 2)

    def _draw_box(title="", lines_=None):
        for r in range(box_h):
            try:
                stdscr.addstr(box_y + r, box_x, " " * box_w,
                              curses.color_pair(C_CURSOR) | curses.A_BOLD)
            except curses.error:
                pass
        try:
            stdscr.addstr(box_y, box_x,
                          f" Trading — {title} ".center(box_w),
                          curses.color_pair(C_ALERT) | curses.A_BOLD | curses.A_REVERSE)
            stdscr.addstr(box_y + box_h - 1, box_x + 1,
                          "  [↑↓] select   [Enter] next   [Esc] cancel"[:box_w - 2],
                          curses.color_pair(C_AXIS) | curses.A_DIM)
        except curses.error:
            pass
        if lines_:
            for li, (txt, pair, attr) in enumerate(lines_):
                try:
                    stdscr.addstr(box_y + 2 + li, box_x + 2,
                                  txt[:box_w - 4],
                                  curses.color_pair(pair) | attr)
                except curses.error:
                    pass
        stdscr.refresh()

    def _menu(title, options, default=0):
        idx = default
        while True:
            items = []
            for i, opt in enumerate(options):
                items.append((f"{'>' if i==idx else ' '} {opt}",
                              C_ASSET_SEL if i == idx else C_CURSOR,
                              curses.A_BOLD if i == idx else curses.A_NORMAL))
            _draw_box(title, items)
            k = stdscr.getch()
            if k == 27: return None
            if k == curses.KEY_UP:   idx = (idx - 1) % len(options)
            elif k == curses.KEY_DOWN: idx = (idx + 1) % len(options)
            elif k in (10, 13): return idx

    def _inp(title, prompt, default="", context_lines=None):
        _draw_box(title, context_lines or [])
        try:
            stdscr.addstr(box_y + 2 + len(context_lines or []), box_x + 2,
                          prompt[:box_w - 4],
                          curses.color_pair(C_LABEL) | curses.A_BOLD)
        except curses.error:
            pass
        result = _input_field(stdscr,
                              box_y + 3 + len(context_lines or []),
                              box_x + 4, box_w - 8, default)
        return result  # None = Esc

    # ── Step 1: Order type ────────────────────────────────────────────────────
    order_types = ["Buy Market", "Sell Market", "Buy Limit", "Sell Limit"]
    ot_idx = _menu("Order Type", order_types)
    if ot_idx is None: stdscr.nodelay(True); return None
    order_type = order_types[ot_idx]
    side = "buy" if "Buy" in order_type else "sell"
    is_limit = "Limit" in order_type

    # ── Step 2: Limit price (only for limit orders) ───────────────────────────
    limit_price = None
    if is_limit:
        cur_p = str(round(state.last_price, 2))
        lp_str = _inp("Limit Price", f"Limit price (current: {cur_p}):", cur_p)
        if lp_str is None: stdscr.nodelay(True); return None
        try: limit_price = float(lp_str)
        except: limit_price = state.last_price

    entry = limit_price if limit_price else state.last_price

    # ── Step 3: Stop Loss ────────────────────────────────────────────────────
    ctx = [(f"Order: {order_type}" + (f" @ {limit_price}" if limit_price else ""),
            C_LABEL, curses.A_BOLD)]
    sl_type_idx = _menu("Stop Loss Type", ["Points", "Price"], default=0)
    if sl_type_idx is None: stdscr.nodelay(True); return None

    sl_def = "50" if sl_type_idx == 0 else str(round(
        entry - 50 if side == "buy" else entry + 50, 2))
    sl_str = _inp("Stop Loss Value", "SL value:", sl_def, ctx)
    if sl_str is None: stdscr.nodelay(True); return None
    try: sl_val = float(sl_str)
    except: sl_val = 50.0
    sl_price = (entry - sl_val if side == "buy" else entry + sl_val)                if sl_type_idx == 0 else sl_val

    # ── Step 4: Take Profit ──────────────────────────────────────────────────
    ctx2 = ctx + [(f"SL: {sl_price:.2f}", C_BTD_SELL, curses.A_BOLD)]
    tp_type_idx = _menu("Take Profit Type", ["Points", "Price"], default=0)
    if tp_type_idx is None: stdscr.nodelay(True); return None

    tp_def = "100" if tp_type_idx == 0 else str(round(
        entry + 100 if side == "buy" else entry - 100, 2))
    tp_str = _inp("Take Profit Value", "TP value:", tp_def, ctx2)
    if tp_str is None: stdscr.nodelay(True); return None
    try: tp_val = float(tp_str)
    except: tp_val = 100.0
    tp_price = (entry + tp_val if side == "buy" else entry - tp_val)                if tp_type_idx == 0 else tp_val

    # ── Step 5: Position sizes ────────────────────────────────────────────────
    ctx3 = ctx2 + [(f"TP: {tp_price:.2f}", C_BTD_BUY, curses.A_BOLD)]
    sp_str = _inp("Position Size", "Phemex size (contracts):", "1", ctx3)
    if sp_str is None: stdscr.nodelay(True); return None
    try: sp = float(sp_str)
    except: sp = 1.0

    sx_str = _inp("Position Size", "XLTRADE size (lots):", "0.01",
                  ctx3 + [(f"Phemex: {sp}", C_LABEL, curses.A_NORMAL)])
    if sx_str is None: stdscr.nodelay(True); return None
    try: sx = float(sx_str)
    except: sx = 0.01

    # ── Step 6: Confirm ───────────────────────────────────────────────────────
    cmd_parts = [side, "market" if not is_limit else "limit"]
    if is_limit:
        cmd_parts += ["-p", str(limit_price)]
    cmd_parts += ["-sp", f"{float(sp):.2f}", "-sx", f"{float(sx):.2f}",
                  "-tp", f"{float(tp_price):.2f}"]
    cmd_str = "python copycat.py " + " ".join(cmd_parts)

    # Show full order summary with the confirm menu underneath
    confirm_lines = [
        (f"  Order:   {order_type}", C_LABEL, curses.A_BOLD),
        (f"  Entry:   {'MARKET' if not is_limit else f'{limit_price:.2f} (limit)'}",
         C_LABEL, curses.A_NORMAL),
        (f"  SL:      {sl_price:.2f}", C_BTD_SELL, curses.A_BOLD),
        (f"  TP:      {tp_price:.2f}", C_BTD_BUY, curses.A_BOLD),
        (f"  Phemex:  {sp} contracts", C_LABEL, curses.A_NORMAL),
        (f"  XLTRADE: {sx} lots", C_LABEL, curses.A_NORMAL),
        ("", C_LABEL, curses.A_NORMAL),
        (f"  {cmd_str[:box_w-6]}", C_AXIS, curses.A_DIM),
        (f"  {cmd_str[box_w-6:(box_w-6)*2]}", C_AXIS, curses.A_DIM),
        (f"  {cmd_str[(box_w-6)*2:(box_w-6)*3]}", C_AXIS, curses.A_DIM),
        ("", C_LABEL, curses.A_NORMAL),
    ]

    # Custom confirm step: draw summary then ask
    _draw_box("Confirm Order", confirm_lines)
    # Draw the two choices below the summary
    _choice = 0
    while True:
        for _ci, _opt in enumerate(["[Y] SUBMIT", "[N] Cancel"]):
            _attr = curses.color_pair(C_ASSET_SEL) | curses.A_BOLD if _ci == _choice                     else curses.color_pair(C_CURSOR)
            try:
                stdscr.addstr(box_y + 2 + len(confirm_lines) + _ci,
                              box_x + 4,
                              f"{'>' if _ci==_choice else ' '} {_opt}",
                              _attr)
            except curses.error:
                pass
        stdscr.refresh()
        _k = stdscr.getch()
        if _k == 27: stdscr.nodelay(True); return None
        if _k == curses.KEY_UP: _choice = (_choice - 1) % 2
        elif _k == curses.KEY_DOWN: _choice = (_choice + 1) % 2
        elif _k in (10, 13): break

    if _choice == 1:
        stdscr.nodelay(True); return None

    _draw_box("Submitting...", confirm_lines)
    stdscr.refresh()

    # ── Submit via copycat ────────────────────────────────────────────────────
    import sys as _sys
    script_dir = os.path.dirname(os.path.abspath(__file__))
    copycat_py = os.path.join(script_dir, "copycat.py")
    log_path   = os.path.join(script_dir, "trade_debug.log")

    if not os.path.exists(copycat_py):
        with state.lock:
            state.error = f"Trade failed: copycat.py not found at {copycat_py}"
        stdscr.nodelay(True)
        return None
    full_cmd = [_sys.executable, copycat_py] + cmd_parts

    # Always log the full command and output for debugging
    import datetime as _dt_trade
    _ts_str = _dt_trade.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        _env = dict(os.environ)
        _env["PYTHONIOENCODING"] = "utf-8"
        # Use stdin=PIPE so copycat can receive interactive prompts if needed
        proc = subprocess.Popen(
            full_cmd, cwd=script_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stdout+stderr
            stdin=subprocess.PIPE,
            env=_env,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        # Send 'y' to any confirmation prompts copycat may show
        out_bytes, _ = proc.communicate(input=b"y\n", timeout=60)
        ok = proc.returncode == 0
        out_text = out_bytes.decode("utf-8", errors="replace")
        err_text = ""
    except subprocess.TimeoutExpired:
        proc.kill()
        out_text = ""
        err_text = "copycat timed out after 60s"
        ok = False
    except Exception as _e:
        out_text = ""
        err_text = str(_e)
        ok = False

    # Write full debug log
    try:
        with open(log_path, "a", encoding="utf-8") as _lf:
            _lf.write(f"\n{'='*60}\n")
            _lf.write(f"[{_ts_str}] TRADE SUBMITTED\n")
            _lf.write(f"CMD: {' '.join(full_cmd)}\n")
            _lf.write(f"CWD: {script_dir}\n")
            _lf.write(f"EXIT: {proc.returncode if hasattr(proc,'returncode') else 'N/A'}\n")
            _lf.write(f"OUTPUT:\n{out_text}\n")
            if err_text:
                _lf.write(f"ERROR:\n{err_text}\n")
    except Exception:
        pass

    combined = (out_text + "\n" + err_text).strip()
    if not ok:
        _elines = [l.strip() for l in combined.splitlines() if l.strip()]
        _short  = _elines[-1] if _elines else "copycat failed — see trade_debug.log"
        with state.lock:
            state.error = f"Trade failed: {_short[:180]}  [full log: trade_debug.log]"
        stdscr.nodelay(True)
        return None

    # Show output summary in footer even on success
    _elines_ok = [l.strip() for l in combined.splitlines() if l.strip()]
    _summary   = _elines_ok[-1] if _elines_ok else "submitted"
    with state.lock:
        state.error = f"Trade OK: {_summary[:120]}"

    stdscr.nodelay(True)
    return {
        "side":         side,
        "order_type":   order_type,
        "entry":        entry,
        "sl":           sl_price,
        "tp":           tp_price,
        "size_phemex":  sp,
        "size_xl":      sx,
        "limit_price":  limit_price,
        "status":       "active",
    }



# ── Position Tool ──────────────────────────────────────────────────────────────
C_POS_ENTRY = C_LABEL
C_POS_TP    = C_BTD_BUY
C_POS_SL    = C_BTD_SELL

def pos_tool_dialog(stdscr, rows: int, cols: int, anchor_price: float) -> dict | None:
    """Ask user for Entry/SL/TP prices. Returns tool dict or None."""
    stdscr.nodelay(False)
    curses.curs_set(0)
    box_w = min(cols - 4, 52)
    box_h = 14
    box_y = max(0, rows // 2 - box_h // 2)
    box_x = max(0, cols // 2 - box_w // 2)

    def _clr(title):
        for r in range(box_h):
            try: stdscr.addstr(box_y + r, box_x, " " * box_w,
                               curses.color_pair(C_CURSOR) | curses.A_BOLD)
            except curses.error: pass
        try:
            stdscr.addstr(box_y, box_x,
                          f" Position Tool — {title} ".center(box_w),
                          curses.color_pair(C_ALERT) | curses.A_BOLD | curses.A_REVERSE)
            stdscr.addstr(box_y + box_h - 1, box_x + 1,
                          "  [Enter] confirm   [Esc] cancel"[:box_w-2],
                          curses.color_pair(C_AXIS) | curses.A_DIM)
        except curses.error: pass
        stdscr.refresh()

    def _inp(title, label, default):
        _clr(title)
        try: stdscr.addstr(box_y + 3, box_x + 2, label,
                           curses.color_pair(C_LABEL) | curses.A_BOLD)
        except curses.error: pass
        return _input_field(stdscr, box_y + 4, box_x + 4, box_w - 8, str(default))

    ep = _inp("Entry Price", "Entry price (default=candle open):", f"{anchor_price:.2f}")
    if ep is None: stdscr.nodelay(True); return None
    try:    entry = float(ep)
    except: entry = anchor_price

    tp_s = _inp("Take Profit", "TP price (green):", f"{entry + 50:.2f}")
    if tp_s is None: stdscr.nodelay(True); return None
    try:    tp = float(tp_s)
    except: tp = entry + 50

    sl_s = _inp("Stop Loss", "SL price (red):", f"{entry - 15:.2f}")
    if sl_s is None: stdscr.nodelay(True); return None
    try:    sl = float(sl_s)
    except: sl = entry - 25

    # Ask whether to create alerts
    _clr("Create Alerts?")
    try:
        stdscr.addstr(box_y + 3, box_x + 2,
                      f"Create TP/SL alerts automatically?",
                      curses.color_pair(C_LABEL) | curses.A_BOLD)
    except curses.error:
        pass
    _alert_choice = 0
    while True:
        for _ci2, _opt2 in enumerate(["Yes — create TP+SL alerts", "No  — lines only"]):
            _attr2 = curses.color_pair(C_ASSET_SEL) | curses.A_BOLD                      if _ci2 == _alert_choice else curses.color_pair(C_CURSOR)
            try: stdscr.addstr(box_y + 5 + _ci2, box_x + 4,
                               f"{'>' if _ci2==_alert_choice else ' '} {_opt2}", _attr2)
            except curses.error: pass
        stdscr.refresh()
        _ka = stdscr.getch()
        if _ka == 27: stdscr.nodelay(True); return None
        if _ka == curses.KEY_UP: _alert_choice = (_alert_choice - 1) % 2
        elif _ka == curses.KEY_DOWN: _alert_choice = (_alert_choice + 1) % 2
        elif _ka in (10, 13): break
    create_alerts = (_alert_choice == 0)

    stdscr.nodelay(True)
    return {"entry": entry, "tp": tp, "sl": sl, "active": True,
            "_anchor_idx": 0, "_stop_idx": None, "create_alerts": create_alerts}


def draw_pos_tools(db, chart_top, chart_bot, chart_r, p2r,
                   all_candles, left_idx, n_vis, pos_tools):
    """Draw entry/SL/TP lines. Entry=white, TP=green, SL=red. Stop at hit."""
    if not pos_tools:
        return
    start_col = chart_r - n_vis

    def _row(price):
        return max(chart_top, min(chart_bot - 1, chart_top + p2r(price)))

    for pt in pos_tools:
        if not pt.get("active"):
            continue
        entry = pt["entry"];  tp = pt["tp"];  sl = pt["sl"]
        anchor_ci = pt.get("_anchor_idx", 0)

        # Find where TP or SL was hit
        stop_ci = pt.get("_stop_idx")
        if stop_ci is None:
            for ci in range(anchor_ci, len(all_candles)):
                c = all_candles[ci]
                hit_tp = (tp > entry and c.h >= tp) or (tp < entry and c.l <= tp)
                hit_sl = (sl < entry and c.l <= sl) or (sl > entry and c.h >= sl)
                if hit_tp or hit_sl:
                    pt["_stop_idx"] = ci
                    stop_ci = ci
                    break

        end_ci  = stop_ci if stop_ci is not None else len(all_candles) - 1
        col_s   = max(0, start_col + (anchor_ci - left_idx))
        col_e   = min(chart_r - 1, start_col + (end_ci - left_idx))

        if col_s > col_e:
            continue

        row_e = _row(entry);  row_t = _row(tp);  row_s = _row(sl)

        for col in range(col_s, col_e + 1):
            if not (0 <= col < chart_r):
                continue
            if chart_top <= row_e < chart_bot:
                db.put(row_e, col, "-", C_POS_ENTRY, curses.A_BOLD)
            if chart_top <= row_t < chart_bot:
                db.put(row_t, col, "-", C_POS_TP, curses.A_BOLD)
            if chart_top <= row_s < chart_bot:
                db.put(row_s, col, "-", C_POS_SL, curses.A_BOLD)

        lbl_col = min(col_e + 1, chart_r - 18)
        if chart_top <= row_t < chart_bot:
            db.puts(row_t, lbl_col, f" TP {tp:.2f}", C_POS_TP,
                    curses.A_BOLD | curses.A_REVERSE)
        if chart_top <= row_e < chart_bot:
            db.puts(row_e, lbl_col, f" Entry {entry:.2f}", C_POS_ENTRY,
                    curses.A_BOLD | curses.A_REVERSE)
        if chart_top <= row_s < chart_bot:
            db.puts(row_s, lbl_col, f" SL {sl:.2f}", C_POS_SL,
                    curses.A_BOLD | curses.A_REVERSE)



# ── Horizontal Line Tool ──────────────────────────────────────────────────────
def hline_dialog(stdscr, rows: int, cols: int, default_price: float) -> dict | None:
    """Ask for price, label, and optional alert. Returns hline dict or None."""
    stdscr.nodelay(False)
    curses.curs_set(0)
    box_w = min(cols - 4, 52)
    box_h = 16
    box_y = max(0, rows // 2 - box_h // 2)
    box_x = max(0, cols // 2 - box_w // 2)

    def _clr(title):
        for r in range(box_h):
            try: stdscr.addstr(box_y + r, box_x, " " * box_w,
                               curses.color_pair(C_CURSOR) | curses.A_BOLD)
            except curses.error: pass
        try:
            stdscr.addstr(box_y, box_x,
                          f" Horizontal Line — {title} ".center(box_w),
                          curses.color_pair(C_ALERT) | curses.A_BOLD | curses.A_REVERSE)
            stdscr.addstr(box_y + box_h - 1, box_x + 1,
                          "  [Enter] confirm   [Esc] cancel"[:box_w - 2],
                          curses.color_pair(C_AXIS) | curses.A_DIM)
        except curses.error: pass
        stdscr.refresh()

    def _inp(title, label, default=""):
        _clr(title)
        try: stdscr.addstr(box_y + 3, box_x + 2, label,
                           curses.color_pair(C_LABEL) | curses.A_BOLD)
        except curses.error: pass
        return _input_field(stdscr, box_y + 4, box_x + 4, box_w - 8, str(default))

    # Price
    ps = _inp("Price", "Price level:", f"{default_price:.2f}")
    if ps is None: stdscr.nodelay(True); return None
    try:    price = float(ps)
    except: price = default_price

    # Label (optional)
    ls = _inp("Label", "Label (optional, Enter to skip):", "")
    if ls is None: stdscr.nodelay(True); return None
    label = ls.strip()

    # Alert?
    _clr("Create Alert?")
    try: stdscr.addstr(box_y + 3, box_x + 2, f"Create alert at {price:.2f}?",
                       curses.color_pair(C_LABEL) | curses.A_BOLD)
    except curses.error: pass
    _ac = 1  # default No
    while True:
        for _ci, _o in enumerate(["Yes — create alert", "No  — line only"]):
            _attr = curses.color_pair(C_ASSET_SEL) | curses.A_BOLD                     if _ci == _ac else curses.color_pair(C_CURSOR)
            try: stdscr.addstr(box_y + 5 + _ci, box_x + 4,
                               f"{'>' if _ci==_ac else ' '} {_o}", _attr)
            except curses.error: pass
        stdscr.refresh()
        _k = stdscr.getch()
        if _k == 27: stdscr.nodelay(True); return None
        if _k == curses.KEY_UP: _ac = (_ac - 1) % 2
        elif _k == curses.KEY_DOWN: _ac = (_ac + 1) % 2
        elif _k in (10, 13): break

    stdscr.nodelay(True)
    return {"price": price, "label": label, "alert": (_ac == 0), "active": True}


def draw_hlines(db, chart_top, chart_bot, chart_r, p2r, hlines):
    """Draw all active horizontal lines across the full chart width."""
    for hl in hlines:
        if not hl.get("active"):
            continue
        price = hl["price"]
        label = hl.get("label", "")
        row   = chart_top + p2r(price)
        if not (chart_top <= row < chart_bot):
            continue
        for col in range(chart_r):
            if db.buf[row][col][0] in (" ", ".", ":"):
                db.put(row, col, "-", C_VWAP, curses.A_DIM)
        # Label at right
        lbl_str = f" {label} {price:.2f}" if label else f" {price:.2f}"
        lbl_col = max(0, chart_r - len(lbl_str) - 1)
        if db.buf[row][lbl_col][0] in (" ", "-"):
            db.puts(row, lbl_col, lbl_str[:chart_r - lbl_col],
                    C_VWAP, curses.A_BOLD | curses.A_REVERSE)


def draw_hline_list_overlay(db, rows, cols, hlines, sel):
    """Non-blocking drawing list overlay."""
    box_w = min(cols - 4, 60)
    box_h = min(rows - 4, 20)
    box_y = rows // 2 - box_h // 2
    box_x = max(0, cols // 2 - box_w // 2)

    for r in range(box_h):
        for c in range(box_w):
            db.put(box_y + r, box_x + c, " ", C_HEADER)

    title = " Drawings  [N]new  [D]del  [E]edit  [UpDn]sel  [`]close "
    db.puts(box_y, box_x, title.center(box_w)[:box_w], C_ALERT,
            curses.A_BOLD | curses.A_REVERSE)
    db.puts(box_y + 1, box_x + 1,
            f" Price        Alrt  Label"[:box_w - 2], C_LABEL, curses.A_BOLD)

    if not hlines:
        db.puts(box_y + 3, box_x + 3, "No lines — press [N] to draw one",
                C_AXIS, curses.A_DIM)
    for hi, hl in enumerate(hlines[:box_h - 4]):
        pair  = C_ASSET_SEL if hi == sel else C_HEADER
        attrs = curses.A_BOLD if hi == sel else curses.A_NORMAL
        mrk   = ">" if hi == sel else " "
        lbl   = hl.get("label", "")
        alrt  = "[A]" if hl.get("alert") else "   "
        lbl_s = f"{mrk} {hl['price']:>10.2f}  {alrt}  {lbl[:25]}"
        db.puts(box_y + 2 + hi, box_x + 1, lbl_s[:box_w - 2], pair, attrs)

    db.puts(box_y + box_h - 1, box_x + 1,
            f" {len(hlines)} line(s)   [N]new  [D]del  [E]edit  [`]close"[:box_w - 2],
            C_AXIS, curses.A_DIM)

# ── Draw active trade lines ────────────────────────────────────────────────────
def draw_trade_lines(db, chart_top, chart_bot, chart_r, p2r, trade_lines):
    """
    Draw entry, SL, TP horizontal lines for all active trades.
    Long = green entry/TP, Short = red entry/TP.  SL always red.
    """
    for tl in trade_lines:
        if tl.get("status") not in ("active", "pending"):
            continue
        side    = tl["side"]
        entry   = tl["entry"]
        sl      = tl["sl"]
        tp      = tl["tp"]
        clr     = C_BTD_BUY if side == "buy" else C_BTD_SELL
        side_lbl= "LONG" if side == "buy" else "SHORT"

        def _row(price):
            return max(chart_top, min(chart_bot - 1, chart_top + p2r(price)))

        row_e = _row(entry)
        row_sl= _row(sl)
        row_tp= _row(tp)

        for col in range(chart_r):
            if chart_top <= row_e < chart_bot:
                db.put(row_e,  col, "-", clr,       curses.A_BOLD)
            if chart_top <= row_sl < chart_bot:
                db.put(row_sl, col, "-", C_BTD_SELL, curses.A_BOLD)
            if chart_top <= row_tp < chart_bot:
                db.put(row_tp, col, "-", clr,       curses.A_BOLD)

        # Labels on right axis
        lbl_col = max(0, chart_r - 20)
        if chart_top <= row_tp < chart_bot:
            db.puts(row_tp, lbl_col, f" TP {tp:.2f}", clr, curses.A_BOLD | curses.A_REVERSE)
        if chart_top <= row_e < chart_bot:
            db.puts(row_e, lbl_col,
                    f" {side_lbl} {entry:.2f}", clr, curses.A_BOLD | curses.A_REVERSE)
        if chart_top <= row_sl < chart_bot:
            db.puts(row_sl, lbl_col,
                    f" SL {sl:.2f}", C_BTD_SELL, curses.A_BOLD | curses.A_REVERSE)


# ── Trade monitor thread ───────────────────────────────────────────────────────
def trade_monitor_loop():
    """
    Always-on background monitor that:
    1. Polls Phemex positions + active orders every 3s continuously.
    2. Auto-creates trade lines for any open position or pending order found,
       including SL/TP levels if available.
    3. Creates a one-shot alert at the entry/limit price of new detections.
    4. Removes lines when position closes or order cancels.
    Never stops — runs for the life of the application.
    """
    _known_ids: set = set()   # track which positions/orders we've already plotted

    while True:
        time.sleep(3)

        try:
            sym = PHEMEX_SYMBOLS.get(state.asset, "ETHUSDT")

            # ── Fetch open positions ───────────────────────────────────────────
            path_  = "/g-accounts/accountPositions"
            query_ = "currency=USDT"
            r = requests.get(f"{PHEMEX_REST_URL}{path_}?{query_}",
                             headers=_phemex_headers(path_, query_), timeout=8)
            _pjson = r.json() or {}
            pos_data = (_pjson.get("data") or {}).get("positions", []) or []

            # ── Fetch active orders (limit orders waiting to fill) ─────────────
            # Try g-orders (USDT perp) first, fallback to orders
            opath_  = "/g-orders/activeList"
            oquery_ = f"symbol={sym}&currency=USDT"
            or_ = requests.get(f"{PHEMEX_REST_URL}{opath_}?{oquery_}",
                               headers=_phemex_headers(opath_, oquery_), timeout=8)
            _ord_json = or_.json()
            # g-orders returns data.rows, legacy returns data directly
            _od = (_ord_json or {}).get("data") or {}
            ord_data = (_od.get("rows") if isinstance(_od, dict) else
                        (_od if isinstance(_od, list) else [])) or []

        except Exception as _me:
            # Store monitor error briefly so it shows in footer
            with state.lock:
                state.error = f"Monitor: {type(_me).__name__}: {str(_me)[:60]}"
            continue

        with state.lock:
            existing_lines = {tl.get("_id"): tl for tl in state.trade_lines
                              if tl.get("_id")}
            existing_alerts = {a.get("_trade_id") for a in state.alerts
                               if a.get("_trade_id")}

        new_lines  = []
        active_ids = set()

        # ── Process open positions ─────────────────────────────────────────────
        for p in pos_data:
            if p.get("symbol") != sym:
                continue
            size = float(p.get("size", 0) or 0)
            if size == 0:
                continue
            pos_side  = p.get("posSide", "Long")
            side      = "buy" if pos_side == "Long" else "sell"
            entry     = float(p.get("avgEntryPriceRp", 0) or 0)
            sl        = float(p.get("stopLossPriceRp",  0) or 0)
            tp        = float(p.get("takeProfitPriceRp", 0) or 0)
            _id       = f"pos_{sym}_{pos_side}"
            active_ids.add(_id)

            if _id in existing_lines:
                # Update in-place
                tl = existing_lines[_id]
                if entry > 0: tl["entry"] = entry
                if sl    > 0: tl["sl"]    = sl
                if tp    > 0: tl["tp"]    = tp
                tl["status"] = "active"
                new_lines.append(tl)
            else:
                # New position detected — create line
                tl = {
                    "_id": _id, "side": side, "order_type": f"{pos_side} Position",
                    "entry": entry if entry > 0 else state.last_price,
                    "sl": sl, "tp": tp,
                    "size_phemex": size, "size_xl": 0,
                    "limit_price": None, "status": "active",
                }
                new_lines.append(tl)
                # Alert at entry if not already set
                if entry > 0 and _id not in existing_alerts:
                    _dir = "price_cross_up" if side == "buy" else "price_cross_down"
                    with state.lock:
                        state.alerts.append({
                            "_trade_id": _id,
                            "name": f"{pos_side} @ {entry:.2f}",
                            "conditions": [{"type": _dir, "value": entry}],
                            "message": f"Position {pos_side} filled @ {entry:.2f}",
                            "active": True, "sound": True, "_last_state": False,
                        })

        # ── Process pending limit orders ───────────────────────────────────────
        for o in ord_data:
            ord_side  = o.get("side", "")
            ord_type  = o.get("ordType") or o.get("orderType", "")
            price     = float(o.get("priceRp") or o.get("price") or 0)
            sl        = float(o.get("stopLossRp") or o.get("stopLoss") or 0)
            tp_v      = float(o.get("takeProfitRp") or o.get("takeProfit") or 0)
            cl_id     = o.get("clOrdID") or o.get("orderID") or o.get("id", "")
            status    = o.get("ordStatus") or o.get("status", "")
            if status in ("Filled", "Canceled", "Rejected"):
                continue
            _id = f"ord_{cl_id}"
            active_ids.add(_id)
            side = "buy" if ord_side == "Buy" else "sell"

            if _id in existing_lines:
                tl = existing_lines[_id]
                if price > 0: tl["entry"] = price
                if sl    > 0: tl["sl"]    = sl
                if tp_v  > 0: tl["tp"]    = tp_v
                new_lines.append(tl)
            else:
                tl = {
                    "_id": _id, "side": side, "order_type": f"Limit {ord_side}",
                    "entry": price if price > 0 else state.last_price,
                    "sl": sl, "tp": tp_v,
                    "size_phemex": float(o.get("orderQtyRq") or o.get("qty") or 0), "size_xl": 0,
                    "limit_price": price, "status": "pending",
                }
                new_lines.append(tl)
                if price > 0 and _id not in existing_alerts:
                    # Alert direction based on price relative to current
                    _lp  = state.last_price
                    _dir = "price_cross_up" if price > _lp else "price_cross_down"
                    with state.lock:
                        state.alerts.append({
                            "_trade_id": _id,
                            "name": f"Limit {ord_side} @ {price:.2f}",
                            "conditions": [{"type": _dir, "value": price}],
                            "message": f"Limit order filled @ {price:.2f}",
                            "active": True, "sound": True, "_last_state": False,
                        })

        # ── Keep manual lines that aren't from monitor (no _id set by monitor) ─
        with state.lock:
            manual = [tl for tl in state.trade_lines if not tl.get("_id")]
            state.trade_lines = new_lines + manual


# ── Alert system ──────────────────────────────────────────────────────────────
ALERT_CONDITIONS = [
    ("price_cross_up",      "Price crosses UP through value"),
    ("price_cross_down",    "Price crosses DOWN through value"),
    ("touch_vwap",          "Price touches VWAP"),
    ("touch_sd_plus05",     "Price touches +0.5 SD"),
    ("touch_sd_minus05",    "Price touches -0.5 SD"),
    ("touch_sd_plus2",      "Price touches +2 SD"),
    ("touch_sd_minus2",     "Price touches -2 SD"),
    ("touch_sd_plus25",     "Price touches +2.5 SD"),
    ("touch_sd_minus25",    "Price touches -2.5 SD"),
    ("touch_vah",           "Price touches VAH"),
    ("touch_val",           "Price touches VAL"),
    ("touch_poc",           "Price touches POC"),
    ("big_trade_buy",       "Big Buy signal (any tier, candle close)"),
    ("big_trade_sell",      "Big Sell signal (any tier, candle close)"),
    ("over_ext_up",         "Over-Extension Up (price >= +2sd + Big Sell)"),
    ("over_ext_down",       "Over-Extension Down (price <= -2sd + Big Buy)"),
]

def _input_field(stdscr, row, col, width, default=""):
    """Collect a single line of input directly via stdscr. Returns string or None on Esc."""
    buf = list(default)
    curses.curs_set(1)
    while True:
        display = "".join(buf)[-width:]
        try:
            stdscr.addstr(row, col, display.ljust(width)[:width],
                          curses.color_pair(C_CURSOR) | curses.A_BOLD)
            stdscr.move(row, col + min(len(buf), width - 1))
        except curses.error:
            pass
        stdscr.refresh()
        try:
            ch = stdscr.get_wch()
        except Exception:
            continue
        code = ord(ch) if isinstance(ch, str) else ch
        if code == 27:
            curses.curs_set(0)
            return None
        if code in (10, 13, curses.KEY_ENTER):
            curses.curs_set(0)
            return "".join(buf).strip()
        if code in (curses.KEY_BACKSPACE, 127, 8) and buf:
            buf.pop()
        elif 32 <= code <= 126 and len(buf) < width - 1:
            buf.append(chr(code))


def alert_create_dialog(stdscr, rows: int, cols: int, existing: dict = None):
    """Create alert dialog using the same safe pattern as jump_to_dialog."""
    stdscr.nodelay(False)
    curses.curs_set(0)

    box_w = min(cols - 4, 62)
    box_h = 16
    box_y = max(0, rows // 2 - box_h // 2)
    box_x = max(0, cols // 2 - box_w // 2)

    def draw_box(cond_idx, step, snd):
        for r in range(box_h):
            try:
                stdscr.addstr(box_y + r, box_x, " " * box_w,
                              curses.color_pair(C_CURSOR) | curses.A_BOLD)
            except curses.error:
                pass
        try:
            stdscr.addstr(box_y, box_x,
                          " Create Alert ".center(box_w),
                          curses.color_pair(C_ALERT) | curses.A_BOLD | curses.A_REVERSE)
        except curses.error:
            pass
        for ci, (_, clabel) in enumerate(ALERT_CONDITIONS):
            attr = (curses.color_pair(C_ASSET_SEL) | curses.A_BOLD
                    if ci == cond_idx else curses.color_pair(C_CURSOR))
            marker = ">" if ci == cond_idx else " "
            try:
                stdscr.addstr(box_y + 2 + ci, box_x + 2,
                              f"{marker} {clabel:<40}"[:box_w - 3], attr)
            except curses.error:
                pass
        hints = {
            0: "  [↑↓] Select condition   [Enter] next   [Esc] cancel",
            1: "  Enter price value   [Enter] next   [Esc] cancel",
            2: "  Enter alert name    [Enter] next   [Esc] cancel",
            3: "  Enter message       [Enter] next   [Esc] cancel",
            4: "  [←→] toggle sound   [Enter] confirm   [Esc] cancel",
        }
        try:
            stdscr.addstr(box_y + box_h - 1, box_x + 1,
                          hints.get(step, "")[:box_w - 2],
                          curses.color_pair(C_AXIS) | curses.A_DIM)
        except curses.error:
            pass
        stdscr.refresh()

    # Pre-fill from existing alert if editing
    _prefill_cond  = 0
    _prefill_val   = str(round(state.last_price, 2))
    _prefill_name  = ""
    _prefill_msg   = ""
    _prefill_snd   = True
    if existing:
        _ec = existing.get("conditions", [{}])[0]
        _et = _ec.get("type", "")
        _prefill_cond = next((i for i,(t,_) in enumerate(ALERT_CONDITIONS) if t==_et), 0)
        _prefill_val  = str(_ec.get("value", round(state.last_price,2)))
        _prefill_name = existing.get("name", "")
        _prefill_msg  = existing.get("message", "")
        _prefill_snd  = existing.get("sound", True)
    # Step 0: choose condition
    cond_idx = _prefill_cond
    while True:
        draw_box(cond_idx, 0, True)
        try:
            k = stdscr.get_wch()
        except Exception:
            continue
        kc = ord(k) if isinstance(k, str) else k
        if kc == 27:
            stdscr.nodelay(True)
            return None
        if kc == curses.KEY_UP:
            cond_idx = (cond_idx - 1) % len(ALERT_CONDITIONS)
        elif kc == curses.KEY_DOWN:
            cond_idx = (cond_idx + 1) % len(ALERT_CONDITIONS)
        elif kc in (10, 13):
            break

    cond_type, cond_label = ALERT_CONDITIONS[cond_idx]

    # Step 1-4: text fields
    draw_box(cond_idx, 1, True)
    try:
        stdscr.addstr(box_y + 8, box_x + 2,
                      f"Condition: {cond_label}",
                      curses.color_pair(C_LABEL) | curses.A_BOLD)
        stdscr.addstr(box_y + 9, box_x + 2, "Price value: ",
                      curses.color_pair(C_LABEL) | curses.A_BOLD)
    except curses.error:
        pass
    val_str = _input_field(stdscr, box_y + 9, box_x + 15, 20, _prefill_val)
    if val_str is None:
        stdscr.nodelay(True)
        return None
    try:
        val = float(val_str)
    except ValueError:
        val = state.last_price

    try:
        stdscr.addstr(box_y + 10, box_x + 2, "Alert name:  ",
                      curses.color_pair(C_LABEL) | curses.A_BOLD)
    except curses.error:
        pass
    name = _input_field(stdscr, box_y + 10, box_x + 15,
                        30, _prefill_name or f"Alert @ {val_str}")
    if name is None:
        stdscr.nodelay(True)
        return None

    try:
        stdscr.addstr(box_y + 11, box_x + 2, "Message:     ",
                      curses.color_pair(C_LABEL) | curses.A_BOLD)
    except curses.error:
        pass
    msg = _input_field(stdscr, box_y + 11, box_x + 15,
                       40, _prefill_msg or f"{cond_label} {val_str}")
    if msg is None:
        stdscr.nodelay(True)
        return None

    # Step 4: sound toggle
    snd = _prefill_snd
    while True:
        draw_box(cond_idx, 4, snd)
        snd_str = f"  Sound: {'[ON ]' if snd else '[OFF]'}   ← → toggle"
        try:
            stdscr.addstr(box_y + 12, box_x + 2, snd_str,
                          curses.color_pair(C_CURSOR) | curses.A_BOLD)
        except curses.error:
            pass
        stdscr.refresh()
        try:
            k = stdscr.get_wch()
        except Exception:
            continue
        kc = ord(k) if isinstance(k, str) else k
        if kc == 27:
            stdscr.nodelay(True)
            return None
        if kc in (curses.KEY_LEFT, curses.KEY_RIGHT):
            snd = not snd
        elif kc in (10, 13):
            break

    stdscr.nodelay(True)
    return {
        "name":        name or f"Alert @ {val}",
        "conditions":  [{"type": cond_type, "value": val}],
        "message":     msg or f"{cond_label} {val}",
        "active":      True,
        "sound":       snd,
        "_last_state": False,
    }


def alert_list_dialog(stdscr, rows: int, cols: int):
    """Show list of active + triggered alerts. [N] new, [D] delete, [Esc] close."""
    stdscr.nodelay(False)
    sel = 0

    box_w = min(cols - 4, 66)
    box_h = min(rows - 4, 24)
    box_y = rows // 2 - box_h // 2
    box_x = max(0, cols // 2 - box_w // 2)

    while True:
        with state.lock:
            _alts = list(state.alerts)
    
        for r in range(box_h):
            try:
                stdscr.addstr(box_y + r, box_x, " " * box_w,
                              curses.color_pair(C_CURSOR) | curses.A_BOLD)
            except curses.error:
                pass
        try:
            stdscr.addstr(box_y, box_x,
                          " Alert List — [D] delete  [N] new  [Esc] close ".center(box_w),
                          curses.color_pair(C_ALERT) | curses.A_BOLD | curses.A_REVERSE)
            stdscr.addstr(box_y + 1, box_x + 1, "ACTIVE ALERTS:",
                          curses.color_pair(C_LABEL) | curses.A_BOLD)
        except curses.error:
            pass

        for ai, alt in enumerate(_alts[:9]):
            ctype = alt["conditions"][0]["type"] if alt["conditions"] else ""
            val   = alt["conditions"][0]["value"] if alt["conditions"] else 0
            snd   = "♪" if alt.get("sound") else " "
            lbl   = f"{'>' if ai == sel else ' '} {snd} {alt['name'][:24]:<24} {ctype} {val}"
            attr  = (curses.color_pair(C_ASSET_SEL) | curses.A_BOLD
                     if ai == sel else curses.color_pair(C_CURSOR))
            try:
                stdscr.addstr(box_y + 2 + ai, box_x + 1, lbl[:box_w - 2], attr)
            except curses.error:
                pass

        if not _alts:
            try:
                stdscr.addstr(box_y + 2, box_x + 3,
                              "No active alerts — press [N] to create one",
                              curses.color_pair(C_AXIS) | curses.A_DIM)
            except curses.error:
                pass

        try:
            stdscr.addstr(box_y + 12, box_x + 1, "TRIGGERED HISTORY:",
                          curses.color_pair(C_LABEL) | curses.A_BOLD)
        except curses.error:
            pass
        for ti, trig in enumerate(_trig[:9]):
            lbl = f"  {trig['time']}  {trig['name'][:22]}  {trig['message'][:18]}"
            try:
                stdscr.addstr(box_y + 13 + ti, box_x + 1,
                              lbl[:box_w - 2],
                              curses.color_pair(C_VWAP_SD2))
            except curses.error:
                pass

        stdscr.refresh()
        try:
            k = stdscr.getch()
        except Exception:
            break
        if k in (27, ord("a"), ord("A")):
            break
        elif k == curses.KEY_UP:
            sel = max(0, sel - 1)
        elif k == curses.KEY_DOWN:
            sel = min(max(0, len(_alts) - 1), sel + 1)
        elif k in (ord("d"), ord("D")):
            if 0 <= sel < len(_alts):
                with state.lock:
                    if sel < len(state.alerts):
                        state.alerts.pop(sel)
                sel = max(0, sel - 1)
        elif k in (ord("n"), ord("N")):
            new_alt = alert_create_dialog(stdscr, rows, cols)
            if new_alt:
                with state.lock:
                    state.alerts.append(new_alt)
            # Re-enter blocking mode for list
            stdscr.nodelay(False)

    stdscr.nodelay(True)


# ── Non-blocking alert list overlay ──────────────────────────────────────────
def draw_alert_list_overlay(db, rows, cols, sel):
    """Render alert list into double-buffer (chart stays live behind it)."""
    with state.lock:
        _alts = list(state.alerts)

    box_w = min(cols - 4, 68)
    box_h = min(rows - 4, 26)
    box_y = rows // 2 - box_h // 2
    box_x = max(0, cols // 2 - box_w // 2)

    # Background
    for r in range(box_h):
        for c in range(box_w):
            db.put(box_y + r, box_x + c, " ", C_HEADER)

    # Title
    title = " Alert List   [N] new  [D] delete  [E] edit  [↑↓] select  [A] close "
    db.puts(box_y, box_x, title.center(box_w)[:box_w], C_ALERT,
            curses.A_BOLD | curses.A_REVERSE)

    db.puts(box_y + 1, box_x + 1, "ACTIVE ALERTS:", C_LABEL, curses.A_BOLD)
    for ai, alt in enumerate(_alts[:9]):
        ctype = alt["conditions"][0]["type"] if alt["conditions"] else ""
        val   = alt["conditions"][0].get("value", 0) if alt["conditions"] else 0
        snd   = "*" if alt.get("sound") else " "
        lbl   = f"{'>' if ai == sel else ' '} {snd} {alt['name'][:24]:<24} {ctype:<20} @ {val}"
        pair  = C_ASSET_SEL if ai == sel else C_HEADER
        attrs = curses.A_BOLD if ai == sel else curses.A_NORMAL
        db.puts(box_y + 2 + ai, box_x + 1, lbl[:box_w - 2], pair, attrs)

    if not _alts:
        db.puts(box_y + 2, box_x + 3,
                "No active alerts — press [N] to create one", C_AXIS, curses.A_DIM)




# ── Economic calendar overlay ─────────────────────────────────────────────────
def draw_econ_cal_overlay(db, rows, cols, events, loading,
                          impact_filter=None, date_range="today", scroll=0):
    """
    Render econ calendar overlay into double-buffer.
    Features: impact filter, date range selector, scrollable list,
    color-coded rows, countdown to next event.
    """
    if impact_filter is None:
        impact_filter = {1, 2, 3}
    now_ts = time.time()
    now_dt = datetime.now()

    box_w = min(cols - 2, 96)
    box_h = min(rows - 2, 34)
    box_y = max(0, rows // 2 - box_h // 2)
    box_x = max(0, cols // 2 - box_w // 2)

    for r in range(box_h):
        for c in range(box_w):
            db.put(box_y + r, box_x + c, " ", C_HEADER)

    now_str = now_dt.strftime("%H:%M CT")
    title   = f" Economic Calendar — US Events  [{now_str}]  [K]close  [R]refresh  [P]shot "
    db.puts(box_y, box_x, title.center(box_w)[:box_w], C_ALERT,
            curses.A_BOLD | curses.A_REVERSE)

    # ── Row 1: Date range selector ─────────────────────────────────────────────
    ranges = [("yesterday","Yest"), ("today","Today"), ("tomorrow","Tmrw"), ("week","Week")]
    rsel_x = box_x + 2
    db.puts(box_y + 1, rsel_x - 1, "Range:", C_LABEL, curses.A_BOLD)
    rsel_x += 6
    for rkey, rlbl in ranges:
        badge = f" {rlbl} "
        pair  = C_ASSET_SEL if rkey == date_range else C_HEADER
        db.puts(box_y + 1, rsel_x, badge, pair, curses.A_BOLD)
        rsel_x += len(badge) + 1

    # ── Row 2: Impact filter ───────────────────────────────────────────────────
    f1 = "[x]" if 1 in impact_filter else "[ ]"
    f2 = "[x]" if 2 in impact_filter else "[ ]"
    f3 = "[x]" if 3 in impact_filter else "[ ]"
    fl = f"  Filter: {f1} * [1]   {f2} ** [2]   {f3} *** [3]   [Y]est [T]oday [W]eek [N]ext"
    db.puts(box_y + 2, box_x, fl[:box_w], C_LABEL, curses.A_BOLD)

    if loading:
        db.puts(box_y + box_h // 2, box_x + box_w // 2 - 10,
                "Loading calendar...", C_AXIS, curses.A_BOLD)
        return

    # ── Impact filter helper ───────────────────────────────────────────────────
    def _imp_count(ev):
        imp = ev.get("impact", 1)
        return imp if isinstance(imp, int) else (imp.count("★") or 1)

    filtered = [ev for ev in events if _imp_count(ev) in impact_filter]

    if not filtered:
        msg = ("No matching events — adjust filter [1][2][3]"
               if events else "No events — press [R] to refresh")
        db.puts(box_y + box_h // 2, box_x + max(0, box_w // 2 - len(msg) // 2),
                msg[:box_w], C_AXIS, curses.A_BOLD)
        return

    # ── Header ─────────────────────────────────────────────────────────────────
    hdr = f" {'Time':<7} {'Imp':<5} {'Event':<55}"
    db.puts(box_y + 3, box_x, hdr[:box_w], C_LABEL, curses.A_BOLD | curses.A_UNDERLINE)

    # ── Countdown to next event ────────────────────────────────────────────────
    next_event = next((ev for ev in filtered if ev.get("ts", 0) > now_ts), None)
    if next_event:
        secs_until = max(0, int(next_event["ts"] - now_ts))
        h_, r_ = divmod(secs_until, 3600)
        m_, s_ = divmod(r_, 60)
        cd_str = f"{h_:02d}:{m_:02d}:{s_:02d}" if h_ else f"{m_:02d}:{s_:02d}"
        cd_lbl = f" ▶ Next: {next_event['name'][:38]}  in {cd_str}"
        db.puts(box_y + 4, box_x, cd_lbl[:box_w], C_ALERT, curses.A_BOLD | curses.A_REVERSE)
        ev_start = 5
    else:
        ev_start = 4

    # ── Scrollable event list ──────────────────────────────────────────────────
    max_rows   = box_h - ev_start - 1   # leave 1 row for scroll hint
    n_filtered = len(filtered)
    scroll     = max(0, min(scroll, max(0, n_filtered - max_rows)))
    visible_evs = filtered[scroll: scroll + max_rows]

    # Scroll indicator
    if n_filtered > max_rows:
        db.puts(box_y + box_h - 1, box_x + 2,
                f" ↑↓ scroll  {scroll+1}-{min(scroll+max_rows, n_filtered)}/{n_filtered} "
                "  [<][>] also scrolls ",
                C_AXIS, curses.A_DIM)

    _last_day_shown = None
    _row_y_offset   = 0   # extra rows used by day separators
    for li, ev in enumerate(visible_evs):
        row_y = box_y + ev_start + li + _row_y_offset
        if row_y >= box_y + box_h - 1:
            break
        ev_ts     = ev.get("ts", 0)
        ev_time   = ev.get("time", "")
        ev_name   = ev.get("name", "")[:52]
        imp_n     = _imp_count(ev)
        imp_str   = "***" if imp_n >= 3 else "** " if imp_n == 2 else "*  "

        # Day separator for week view
        if date_range == "week" and ev_ts > 0:
            _ev_day = datetime.fromtimestamp(ev_ts).strftime("%A %b %d")
            if _ev_day != _last_day_shown:
                _last_day_shown = _ev_day
                _sep = f" --- {_ev_day} ---"
                db.puts(row_y, box_x, _sep.ljust(box_w)[:box_w], C_VWAP, curses.A_BOLD)
                _row_y_offset += 1
                row_y = box_y + ev_start + li + _row_y_offset
                if row_y >= box_y + box_h - 1:
                    break

        # Color by state
        if ev_ts > 0 and ev_ts < now_ts:
            pair, attrs = C_AXIS, curses.A_DIM
        elif ev == next_event:
            pair, attrs = C_ALERT, curses.A_BOLD | curses.A_REVERSE
        elif imp_n >= 3:
            pair, attrs = C_BTD_SELL, curses.A_BOLD
        elif imp_n == 2:
            pair, attrs = C_VWAP_SD2, curses.A_BOLD
        else:
            pair, attrs = C_LABEL, curses.A_NORMAL

        lbl = f" {ev_time:<7} {imp_str:<5} {ev_name:<55}"
        db.puts(row_y, box_x, lbl[:box_w], pair, attrs)

# ── help dialog ───────────────────────────────────────────────────────────────
HELP_SECTIONS = [
    ("ASSETS & FEED", [
        ("[E]",        "Switch to ETH/USDT"),
        ("[B]",        "Switch to BTC/USDT"),
        ("[F]",        "Cycle data feed  (PHEMEX ↔ KRAKEN)"),
        ("[I]",        "Cycle interval   (1m → 3m → 15m → 1H → 4H → 1D)"),
    ]),
    ("NAVIGATION", [
        ("[←] [→]",   "Move cursor 1 candle at a time"),
        ("[[] []]",   "Move cursor 10 candles at a time"),
        ("[{] [}]",   "Move cursor 50 candles at a time"),
        ("[Shift+←/→]","Move cursor 10 candles (if terminal supports)"),
        ("[Ctrl+←/→]", "Move cursor 50 candles (if terminal supports)"),
        ("[Esc]",      "Snap back to live (exit cursor mode)"),
        ("[G]",        "Jump to date/time  (YYYY-MM-DD HH:MM or HH:MM)"),
    ]),
    ("CHART DISPLAY", [
        ("[L]",        "Toggle chart mode  (CANDLE ↔ LINE)"),
        ("[C]",        "Toggle candle colors  (B/W ↔ R/G)"),
        ("[W]",        "Toggle VWAP + standard deviation bands"),
        ("[V]",        "Toggle Volume Profile overlay"),
        ("[T]",        "Toggle Big Trade Detector  (volume anomaly circles)"),
    ]),
    ("INDICATORS", [
        ("VWAP",       "Volume Weighted Avg Price (session-anchored 19:00 CT)"),
        ("+.5s/-.5s",  "±0.5 std dev band (cyan shading)"),
        ("+2s/-2s",    "±2 std dev lines (yellow)"),
        ("+2.5s/-2.5s","±2.5 std dev lines (dim yellow)"),
        ("POC",        "Point of Control (highest volume price, yellow)"),
        ("VAH/VAL",    "Value Area High/Low (70% of session volume, magenta)"),
        ("pPOC/pVAH/pVAL","Previous session levels — extend as virgin lines"),
        ("S",          "Period separator (19:00 CT session open)"),
    ]),
    ("ALERT CONDITIONS", [
        ("Price Cross", "Alert when price crosses a set level up or down"),
        ("Indicator",   "Alert when price touches VWAP/SD/VAH/VAL/POC (no line drawn)"),
        ("Big Trade",   "Alert on any BTD signal candle close (no line drawn)"),
        ("Over-Ext Up", "Price >= +2sd AND Big Sell closes (no line drawn)"),
        ("Over-Ext Dn", "Price <= -2sd AND Big Buy closes (no line drawn)"),
        ("Auto-delete", "Price cross alerts delete after firing; others persist"),
    ]),
    ("DRAWINGS", [
        ("[`]",        "Toggle Drawings list (backtick key)"),
        ("[|]",        "Draw horizontal line (pipe = Shift+backslash)"),
        ("[\\]",       "Position tool (backslash) — entry/SL/TP lines"),
        ("[N]",        "New line (when drawings list open)"),
        ("[D]",        "Delete selected line"),
        ("[E]",        "Edit selected line"),
        ("[Z]",        "Clear all trade lines and position tools"),
    ]),
    ("EXTRA", [
        ("VWAP",       "Volume Weighted Avg Price  (session-anchored 19:00 CT)"),
        ("±0.5σ",      "0.5 std dev band  (shaded cyan region around VWAP)"),
        ("±2σ",        "2 std dev lines   (yellow)"),
        ("±2.5σ",      "2.5 std dev lines (dim yellow)"),
        ("POC",        "Point of Control  (highest volume price, yellow)"),
        ("VAH/VAL",    "Value Area High/Low  (70% of session volume, magenta)"),
        ("pPOC/pVAH/pVAL", "Previous session levels — extend as virgin lines"),
        ("S",          "Period separator  (19:00 CT session open)"),
    ]),
    ("SESSIONS & ALERTS", [
        ("[S]",        "Toggle Sessions indicator (NDO/Morning/Excl/Lunch/PWR/EOD)"),
        ("[A]",        "Open/close Alert list overlay (chart stays live)"),
        ("[N]",        "New alert (only when alert list is open)"),
        ("[D]",        "Delete selected alert (only when alert list is open)"),
        ("[E]",        "Edit selected alert (only when alert list is open)"),
        ("[.]",        "Dismiss alert popup"),
        ("[↑][↓]",     "Navigate alert list / drawings list / econ calendar"),
        ("[Esc]",      "Close calendar / dismiss popup / snap to live"),
    ]),
    ("DRAWINGS", [
        ("[`]",       "Toggle Drawings list (backtick key)"),
        ("[|]",        "Draw horizontal line (pipe key  Shift+\\)"),
        ("[N]",        "New line (when drawings list open)"),
        ("[D]",        "Delete selected line (when drawings list open)"),
        ("[↑][↓]",     "Navigate drawings list"),
        ("Line+Alert", "Optional alert created at line price"),
    ]),
    ("POSITION TOOL", [
        ("[\\]",       "Open position tool (backslash key)"),
        ("Entry",      "Defaults to cursor candle open price"),
        ("SL",         "Defaults to entry - 15 pts"),
        ("TP",         "Defaults to entry + 50 pts"),
        ("Alerts",     "Optional TP/SL alerts auto-created"),
        ("[Z]",        "Clear all position tools and trade lines"),
    ]),
    ("ECONOMIC CALENDAR", [
        ("[K]",        "Open/close Economic Calendar  (US events)"),
        ("[R]",        "Refresh data (when calendar open)"),
        ("[Y]",        "Switch to Yesterday events"),
        ("[T]",        "Switch to Today events  (default)"),
        ("[N]ext",     "Switch to Tomorrow events"),
        ("[W]eek",     "Switch to This Week events"),
        ("[1][2][3]",  "Toggle * / ** / *** impact filter"),
        ("[↑][↓]",     "Scroll event list"),
        ("[P]",        "Screenshot of calendar"),
    ]),
    ("TRADING  [X]", [
        ("[X]",        "Open trade dialog (Buy/Sell Market or Limit)"),
        ("[Z]",        "Clear all trade lines from chart"),
        ("Sizes",      "Enter Phemex contracts + XLTRADE lots"),
        ("SL/TP",      "Enter as points or exact price"),
        ("Submit",     "Runs copycat.py — lines appear on chart immediately"),
        ("Monitor",    "Trade lines auto-update from Phemex position data"),
    ]),
    ("GLOBAL / MACRO MODE", [
        ("[M]",        "Toggle Global performance chart (all 8 assets vs BTC baseline)"),
        ("[R]",        "Refresh global data (when in global mode)"),
        ("[P]",        "Screenshot in global mode"),
    ]),
    ("UTILITIES", [
        ("[P]",        "Screenshot → screenshots/quantasset_YYYYMMDD_HHMMSS.txt"),
        ("[G]",        "Jump to date/time  (opens input box, YYYY-MM-DD HH:MM or HH:MM)"),
        ("[H] / [?]",  "Toggle this help box"),
        ("[Q]",        "Quit ChartHacker"),
    ]),
]

# Pre-build help lines once at module level
def _build_help_lines():
    lines = []
    lines.append("  C H A R T H A C K E R  —  Key Reference")
    lines.append("")
    for section, entries in HELP_SECTIONS:
        lines.append(f"  ── {section} " + "─" * max(0, 44 - len(section)))
        for key, desc in entries:
            lines.append(f"    {key:<18}  {desc}")
        lines.append("")
    lines.append("  [H] / [?] / [Esc]  Close help")
    return lines

HELP_LINES = _build_help_lines()
HELP_BOX_W = max((len(l) for l in HELP_LINES), default=40) + 4


def draw_help_overlay(db: "DoubleBuffer", rows: int, cols: int, scroll: int = 0):
    """
    Render the help box into the double buffer. Scrollable with ↑↓.
    Chart continues live behind it.
    """
    box_w    = min(cols - 4, HELP_BOX_W)
    box_h    = min(rows - 4, 36)   # fixed visible height
    box_y    = max(0, (rows - box_h) // 2)
    box_x    = max(0, (cols - box_w) // 2)
    n_lines  = len(HELP_LINES)
    max_rows = box_h - 3   # title + bottom hint

    # Clamp scroll
    scroll = max(0, min(scroll, max(0, n_lines - max_rows)))

    # Background
    for r in range(box_h):
        for c in range(box_w):
            db.put(box_y + r, box_x + c, " ", C_CURSOR)

    # Title bar
    title = "  C H A R T H A C K E R  —  Key Reference  "
    for ci, ch in enumerate(f"{title:^{box_w}}"[:box_w]):
        db.put(box_y, box_x + ci, ch, C_ASSET_SEL, curses.A_BOLD)

    # Scroll hint at bottom
    pct = int(scroll / max(1, n_lines - max_rows) * 100)
    hint = f" ↑↓ scroll  {scroll+1}-{min(scroll+max_rows,n_lines)}/{n_lines}  {pct}%  [H] close "
    db.puts(box_y + box_h - 1, box_x + 1, hint[:box_w - 2], C_AXIS, curses.A_DIM)

    # Content lines with scroll offset
    visible = HELP_LINES[scroll: scroll + max_rows]
    for li, line in enumerate(visible):
        row  = box_y + 1 + li
        text = line[:box_w - 2].ljust(box_w - 2)
        if line.startswith("  ──"):
            pair, attrs = C_VWAP, curses.A_BOLD
        elif line.startswith("  C H A R T") or line.strip().startswith("—"):
            pair, attrs = C_VP_POC, curses.A_BOLD
        elif "  [H]" in line:
            pair, attrs = C_AXIS, curses.A_DIM
        else:
            pair, attrs = C_CURSOR, curses.A_NORMAL
        for ci, ch in enumerate(text):
            db.put(row, box_x + 1 + ci, ch, pair, attrs)


# ── Alert monitor (background thread) ────────────────────────────────────────
def alert_monitor():
    """
    Polls state.last_price every 100ms and evaluates all active alerts.
    Runs entirely on live state objects (not draw snapshots) so:
    - _last_state persists correctly between evaluations
    - alerts trigger the instant price crosses, not on candle close
    - sound fires immediately via terminal bell
    """
    _prev_price = 0.0
    while True:
        time.sleep(0.1)
        with state.lock:
            _cur  = state.last_price
            _alts = state.alerts

        if _cur <= 0 or not _alts:
            _prev_price = _cur
            continue

        _newly = []
        for _alt in _alts:
            if not _alt.get("active", True):
                continue
            _all_met = True
            for _cond in _alt.get("conditions", []):
                _ct  = _cond.get("type", "")
                _cv  = float(_cond.get("value", 0.0))
                if _ct == "price_above":
                    _m = _cur > _cv
                elif _ct == "price_below":
                    _m = _cur < _cv
                elif _ct == "price_cross_up":
                    _m = (_prev_price > 0 and _prev_price <= _cv < _cur)
                elif _ct == "price_cross_down":
                    _m = (_prev_price > 0 and _prev_price >= _cv > _cur)
                # ── Indicator touch alerts (no line drawn) ──────────────────
                elif _ct.startswith("touch_"):
                    with state.lock:
                        _lvls = dict(state.indicator_levels)
                    _tgt_map = {
                        "touch_vwap":   _lvls.get("vwap", 0),
                        "touch_sd_plus05": _lvls.get("sd_p05", 0),
                        "touch_sd_minus05": _lvls.get("sd_m05", 0),
                        "touch_sd_plus2":  _lvls.get("sd_p2", 0),
                        "touch_sd_minus2": _lvls.get("sd_m2", 0),
                        "touch_sd_plus25": _lvls.get("sd_p25", 0),
                        "touch_sd_minus25":_lvls.get("sd_m25", 0),
                        "touch_vah":    _lvls.get("vah", 0),
                        "touch_val":    _lvls.get("val", 0),
                        "touch_poc":    _lvls.get("poc", 0),
                    }
                    _tgt = _tgt_map.get(_ct, 0)
                    # "Touch" = price within 0.1% of target level
                    _m = (_tgt > 0 and abs(_cur - _tgt) / _tgt < 0.001)
                # ── BTD + Over-Extension alerts (candle close based) ────────
                elif _ct == "big_trade_buy":
                    with state.lock:
                        _m = state.btd_buy_fired
                elif _ct == "big_trade_sell":
                    with state.lock:
                        _m = state.btd_sell_fired
                elif _ct == "over_ext_up":
                    with state.lock:
                        _sd2u = state.indicator_levels.get("sd_p2", 0)
                        _bsell = state.btd_sell_fired
                    _m = (_sd2u > 0 and _cur >= _sd2u and _bsell)
                elif _ct == "over_ext_down":
                    with state.lock:
                        _sd2d = state.indicator_levels.get("sd_m2", 0)
                        _bbuy = state.btd_buy_fired
                    _m = (_sd2d > 0 and _cur <= _sd2d and _bbuy)
                else:
                    _m = False
                if not _m:
                    _all_met = False
                    break

            if _all_met and not _alt.get("_last_state", False):
                _alt["_last_state"] = True
                _newly.append(_alt)
            elif not _all_met:
                _alt["_last_state"] = False

        if _newly:
            with state.lock:
                for _ta in _newly:
                    state.alert_triggered.insert(0, {
                        "name":    _ta.get("name", "Alert"),
                        "message": _ta.get("message", "Condition met"),
                        "time":    datetime.now().strftime("%H:%M:%S"),
                    })
                    # Price-cross alerts fire once and delete themselves
                    # BTD/Over-ext/Indicator-touch alerts PERSIST (remain active)
                    _ct0 = _ta.get("conditions", [{}])[0].get("type", "")
                    _auto_delete = _ct0 in ("price_cross_up", "price_cross_down",
                                            "touch_vwap", "touch_sd_plus05",
                                            "touch_sd_minus05", "touch_sd_plus2",
                                            "touch_sd_minus2", "touch_sd_plus25",
                                            "touch_sd_minus25", "touch_vah",
                                            "touch_val", "touch_poc")
                    if _auto_delete and _ta in state.alerts:
                        state.alerts.remove(_ta)
                state.alert_triggered = state.alert_triggered[:20]
                state.show_alert_popup = True
            # Sound alert — winsound on Windows, terminal bell on Unix
            if any(a.get("sound", True) for a in _newly):
                try:
                    import winsound; winsound.Beep(880, 400)
                except Exception:
                    try:
                        import sys, os; os.write(sys.stdout.fileno(), b"\a")
                    except Exception:
                        pass

        _prev_price = _cur


# ── Economic calendar fetcher ─────────────────────────────────────────────────
def fetch_econ_calendar(date_range="today"):
    """
    Fetch US economic events by scraping Investing.com's economic calendar page.
    Parses the datatable-v2 HTML table which contains actual/forecast/previous
    as plain text in <td> cells. Times returned are in ET; convert to CT (-1h).
    date_range: yesterday/today/tomorrow/week
    """
    from html.parser import HTMLParser
    import re as _re, calendar as _cal

    today   = datetime.now()
    import datetime as _dt2
    if date_range == "yesterday":
        _target = today - _dt2.timedelta(days=1)
    elif date_range == "tomorrow":
        _target = today + _dt2.timedelta(days=1)
    else:
        _target = today

    # Investing.com page URL with date param
    date_fmt = _target.strftime("%Y-%m-%d")
    url = f"https://www.investing.com/economic-calendar/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
    }

    # Use the AJAX endpoint which is more reliable
    payload = (
        f"country%5B%5D=5"
        f"&dateFrom={date_fmt}"
        f"&dateTo={date_fmt}"
        f"&timeZone=8"       # New York time (ET)
        f"&timeFilter=timeOnly"
        f"&currentTab={'today' if date_range in ('today','') else date_range.replace('week','thisWeek')}"
        f"&limit_from=0"
    )
    if date_range == "week":
        _wend = (_target + _dt2.timedelta(days=6)).strftime("%Y-%m-%d")
        payload = payload.replace(f"dateTo={date_fmt}", f"dateTo={_wend}")
        payload = payload.replace("currentTab=week", "currentTab=thisWeek")

    ajax_headers = dict(headers)
    ajax_headers.update({
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/javascript, */*",
    })

    try:
        r    = requests.post(
            "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData",
            data=payload, headers=ajax_headers, timeout=15)
        data = r.json()
        html = data.get("data", "")
        if not html:
            return []

        events = []

        class _P(HTMLParser):
            def __init__(self):
                super().__init__()
                self._ev    = None
                self._field = None
                self._depth = 0

            def handle_starttag(self, tag, attrs):
                d = dict(attrs)
                cls = d.get("class", "")
                row_id = d.get("id", "")

                # New event row: id like "123-456789-US-7"
                if tag == "tr" and "js-event-item" in cls:
                    self._ev = {
                        "time": d.get("data-event-datetime", ""),
                        "name": "", "impact": 1,
                        "actual": "", "forecast": "", "previous": "", "ts": 0,
                    }
                    self._field = None
                    return

                if self._ev is None:
                    return

                # Impact from bull icon
                imgkey = d.get("data-img_key", "")
                if imgkey.startswith("bull"):
                    try: self._ev["impact"] = int(imgkey[-1])
                    except: pass

                if tag == "td":
                    cls_l = cls.lower()
                    # Map td class to field
                    if   "js-time"  in cls_l or "first" in cls_l: self._field = "time_td"
                    elif "event"    in cls_l:                      self._field = "event"
                    elif "actual"   in cls_l:                      self._field = "actual"
                    elif "fore"     in cls_l:                      self._field = "forecast"
                    elif "prev"     in cls_l:                      self._field = "previous"
                    # Also detect by position using data-* attrs
                    for attr, fld in [("data-actual","actual"),
                                      ("data-forecast","forecast"),
                                      ("data-previous","previous")]:
                        v = d.get(attr,"").strip().strip("\xa0")
                        if v and v != "&nbsp;":
                            self._ev[fld] = v
                    else:
                        if self._field is None: self._field = None

                if tag == "a" and self._field == "event":
                    self._field = "event_a"

            def handle_data(self, data):
                data = data.strip().strip("\xa0")
                if not data or self._ev is None:
                    return
                if   self._field == "event_a"  and not self._ev["name"]:     self._ev["name"]     = data
                elif self._field == "event"     and not self._ev["name"]:     self._ev["name"]     = data
                elif self._field == "time_td"   and not self._ev["time"] and _re.match(r"\d{1,2}:\d{2}", data):
                    self._ev["time"] = data
                elif self._field == "actual"    and not self._ev["actual"]:   self._ev["actual"]   = data
                elif self._field == "forecast"  and not self._ev["forecast"]: self._ev["forecast"] = data
                elif self._field == "previous"  and not self._ev["previous"]: self._ev["previous"] = data

            def handle_endtag(self, tag):
                if self._ev is None: return
                if tag == "a" and self._field == "event_a": self._field = "event"
                if tag == "tr" and self._ev.get("name"):
                    events.append(dict(self._ev))
                    self._ev = None

        _P().feed(html)

        # Convert times: data-event-datetime is UTC from server (timeZone=8 = ET)
        # ET = UTC-4 (EDT in April). CT = ET - 1h.
        CT_OFFSET = 14400  # Investing.com timeZone=8 returns data; +14400 aligns to CT
        for ev in events:
            raw    = ev.get("time", "")
            m_full = _re.search(r"(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})", raw)
            m_hm   = _re.search(r"(\d{1,2}):(\d{2})", raw)
            if m_full:
                yr,mo,dy = int(m_full.group(1)),int(m_full.group(2)),int(m_full.group(3))
                h,mn,sc  = int(m_full.group(4)),int(m_full.group(5)),int(m_full.group(6))
                et_ts    = _cal.timegm((yr,mo,dy,h,mn,sc,0,0,0))
                ct_ts    = et_ts + CT_OFFSET
                ev["ts"] = ct_ts
                ev["time"] = datetime.fromtimestamp(ct_ts).strftime("%H:%M")
            elif m_hm:
                h, mn  = int(m_hm.group(1)), int(m_hm.group(2))
                et_ts  = datetime(_target.year,_target.month,_target.day,h,mn,0).timestamp()
                ct_ts  = et_ts + CT_OFFSET
                ev["ts"]   = ct_ts
                ev["time"] = datetime.fromtimestamp(ct_ts).strftime("%H:%M")

        return sorted([e for e in events if e.get("name")],
                      key=lambda x: x.get("ts", 0))

    except Exception as _exc:
        return []

def load_econ_calendar():
    """Background: fetch calendar and keep it refreshed every 60s while open."""
    with state.lock:
        state.econ_loading = True
        _dr = state.econ_date_range
    events = fetch_econ_calendar(date_range=_dr)
    with state.lock:
        state.econ_events  = events
        state.econ_loading = False
    # Auto-refresh loop: re-fetch every 60s while calendar is open
    while True:
        for _ in range(60):
            time.sleep(1)
            with state.lock:
                if not state.show_econ_cal:
                    return
        with state.lock:
            if not state.show_econ_cal:
                return
        with state.lock:
            _dr2 = state.econ_date_range
        fresh = fetch_econ_calendar(date_range=_dr2)
        with state.lock:
            state.econ_events = fresh


# ── main loop ────────────────────────────────────────────────────────────────
def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    init_colors(state.color_scheme)

    rows, cols = stdscr.getmaxyx()
    db = DoubleBuffer(rows, cols)

    threading.Thread(target=start_feed, args=(state.asset,), daemon=True).start()
    threading.Thread(target=alert_monitor, daemon=True).start()
    # Start trade monitor immediately — auto-plots any existing positions/orders
    if PHEMEX_API_KEY:
        with state.lock:
            state.trade_monitor = True
        threading.Thread(target=trade_monitor_loop, daemon=True).start()

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
            if key == curses.KEY_RESIZE:
                # Force resize handling on next frame
                new_rows, new_cols = stdscr.getmaxyx()
                rows, cols = new_rows, new_cols
                db = DoubleBuffer(rows, cols)
                db.prev = None
                curses.resizeterm(rows, cols)
                stdscr.clearok(True)
                stdscr.clear()
                continue
            if key in (ord("q"), ord("Q")):
                try: curses.endwin()
                except: pass
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
            elif key in (ord("t"), ord("T")) if False else False:
                pass  # placeholder — T handled below with context check
            elif key in (ord("w"), ord("W")):
                with state.lock:
                    _cal_open_w = state.show_econ_cal
                if _cal_open_w:
                    with state.lock:
                        state.econ_date_range = "week"
                        state.econ_scroll = 0
                    threading.Thread(target=load_econ_calendar, daemon=True).start()
                else:
                    with state.lock:
                        state.show_vwap = not state.show_vwap
                    db.prev = None
                    stdscr.clear()
            elif key in (ord("t"), ord("T")):
                with state.lock:
                    _cal_open_t = state.show_econ_cal
                if _cal_open_t:
                    with state.lock:
                        state.econ_date_range = "today"
                        state.econ_scroll = 0
                    threading.Thread(target=load_econ_calendar, daemon=True).start()
                else:
                    with state.lock:
                        state.show_btd = not state.show_btd
                    db.prev = None
                    stdscr.clear()
            elif key in (ord("s"), ord("S")):
                with state.lock:
                    state.show_sessions = not state.show_sessions
                db.prev = None
                stdscr.clear()
            elif key in (ord("x"), ord("X")):
                # Open trade dialog
                _trade = trade_dialog(stdscr, rows, cols)
                try: curses.flushinp()
                except: pass
                stdscr.nodelay(True)
                if _trade:
                    with state.lock:
                        state.trade_lines.append(_trade)
                        if not state.trade_monitor:
                            state.trade_monitor = True
                            threading.Thread(target=trade_monitor_loop,
                                             daemon=True).start()
                db.prev = None; stdscr.clearok(True); stdscr.clear()
            elif key in (ord("z"), ord("Z")):
                # Clear all trade lines AND position tools
                with state.lock:
                    state.trade_lines = []
                    state.pos_tools   = []
                db.prev = None; stdscr.clear()
            elif key in (ord("l"), ord("L")) if state.show_hline_list else False:
                pass  # L handled by line chart toggle when list closed
            elif key in (ord("l"), ord("L")):
                # L = toggle hline list if open, else toggle line chart
                with state.lock:
                    _hl_open = state.show_hline_list
                if _hl_open:
                    with state.lock:
                        state.show_hline_list = False
                else:
                    with state.lock:
                        state.chart_mode = "line" if state.chart_mode == "candle" else "candle"
                    db.prev = None; stdscr.clear()
            elif key == 96:  # backtick ` = toggle drawings list
                with state.lock:
                    state.show_hline_list = not state.show_hline_list
            elif key == ord("|"):
                # | (pipe) = open horizontal line drawing tool
                with state.lock:
                    _hlp = state.last_price
                _hl = hline_dialog(stdscr, rows, cols, _hlp)
                curses.flushinp()
                stdscr.nodelay(True)
                if _hl:
                    with state.lock:
                        state.hlines.append(_hl)
                        if _hl.get("alert"):
                            _hdir = "price_cross_up" if _hl["price"] > state.last_price else "price_cross_down"
                            state.alerts.append({
                                "name": _hl.get("label") or f"Line {_hl['price']:.2f}",
                                "conditions": [{"type": _hdir, "value": _hl["price"]}],
                                "message": f"Price reached {_hl['price']:.2f}",
                                "active": True, "sound": True, "_last_state": False,
                            })
                db.prev = None; stdscr.clearok(True); stdscr.clear()
            elif key == ord("\\"):
                # Position tool — open dialog anchored to current cursor candle
                with state.lock:
                    _cci  = state.cursor_col_idx
                    _lo   = state.view_offset
                    _nv   = state.n_vis
                    _nall = len(state.candles)
                    _clist = list(state.candles)
                # anchor = index of candle at cursor position
                if _cci >= 0:
                    _anchor = max(0, _nall - _lo - _nv + _cci)
                else:
                    _anchor = max(0, _nall - 1 - _lo)
                _anchor = min(_anchor, _nall - 1)
                _ac = _clist[_anchor] if _clist else None
                if _ac:
                    _pt = pos_tool_dialog(stdscr, rows, cols, _ac.o)
                    curses.flushinp()
                    stdscr.nodelay(True)
                    if _pt:
                        _pt["_anchor_idx"] = min(_anchor, _nall-1)
                        _tp_dir = "price_cross_up" if _pt["tp"] > _pt["entry"] else "price_cross_down"
                        _sl_dir = "price_cross_down" if _pt["sl"] < _pt["entry"] else "price_cross_up"
                        with state.lock:
                            state.pos_tools.append(_pt)
                            if _pt.get("create_alerts", True):
                                state.alerts.append({
                                    "name": f"TP {_pt['tp']:.2f}",
                                    "conditions": [{"type": _tp_dir, "value": _pt["tp"]}],
                                    "message": f"TP hit at {_pt['tp']:.2f}",
                                    "active": True, "sound": True, "_last_state": False,
                                })
                                state.alerts.append({
                                    "name": f"SL {_pt['sl']:.2f}",
                                    "conditions": [{"type": _sl_dir, "value": _pt["sl"]}],
                                    "message": f"SL hit at {_pt['sl']:.2f}",
                                    "active": True, "sound": True, "_last_state": False,
                                })
                db.prev = None; stdscr.clearok(True); stdscr.clear()
            elif key in (ord("a"), ord("A")):
                with state.lock:
                    state.show_alert_list = not state.show_alert_list
            elif key in (ord("n"), ord("N")):
                with state.lock:
                    _hl_open2 = state.show_hline_list
                    _al_open  = state.show_alert_list
                    _cal_open = state.show_econ_cal
                if _hl_open2:
                    with state.lock: _hlp2 = state.last_price
                    _hl2 = hline_dialog(stdscr, rows, cols, _hlp2)
                    if _hl2:
                        with state.lock:
                            state.hlines.append(_hl2)
                            if _hl2.get("alert"):
                                _hdir2 = "price_cross_up" if _hl2["price"] > state.last_price else "price_cross_down"
                            state.alerts.append({
                                    "name": _hl2.get("label") or f"Line {_hl2['price']:.2f}",
                                    "conditions": [{"type": _hdir2, "value": _hl2["price"]}],
                                    "message": f"Price reached {_hl2['price']:.2f}",
                                    "active": True, "sound": True, "_last_state": False,
                                })
                    db.prev = None; stdscr.clearok(True); stdscr.clear()
                    continue
                # Fall through to alert/cal N handling below
                if _cal_open:
                    with state.lock:
                        state.econ_date_range = "tomorrow"
                        state.econ_scroll = 0
                    threading.Thread(target=load_econ_calendar, daemon=True).start()
                elif _al_open:
                    new_alt = alert_create_dialog(stdscr, rows, cols)
                    if new_alt:
                        with state.lock:
                            state.alerts.append(new_alt)
                    db.prev = None
                    stdscr.clearok(True)
                    stdscr.clear()
            elif key in (ord("d"), ord("D")):
                with state.lock:
                    if state.show_hline_list and 0 <= state.hline_sel < len(state.hlines):
                        state.hlines.pop(state.hline_sel)
                        state.hline_sel = max(0, state.hline_sel - 1)
                    elif state.show_alert_list and 0 <= state.alert_list_sel < len(state.alerts):
                        _del_name = state.alerts[state.alert_list_sel].get("name","")
                        state.alerts.pop(state.alert_list_sel)
                        state.alert_list_sel = max(0, state.alert_list_sel - 1)
                        # Clear related popup if no alerts remain
                        if not state.alerts:
                            state.alert_triggered = []
                            state.show_alert_popup = False
            elif key in (ord("e"), ord("E")):
                with state.lock:
                    _hl_edit_open = state.show_hline_list
                    _hl_edit_sel  = state.hline_sel
                    _hl_existing  = (state.hlines[_hl_edit_sel]
                                     if _hl_edit_open and 0 <= _hl_edit_sel < len(state.hlines)
                                     else None)
                    _al_open = state.show_alert_list
                    _sel = state.alert_list_sel
                    _existing = state.alerts[_sel] if _al_open and 0 <= _sel < len(state.alerts) else None
                if _hl_existing:
                    _hl_new = hline_dialog(stdscr, rows, cols, _hl_existing["price"])
                    curses.flushinp(); stdscr.nodelay(True)
                    if _hl_new:
                        with state.lock:
                            state.hlines[_hl_edit_sel] = _hl_new
                    db.prev = None; stdscr.clearok(True); stdscr.clear()
                if _existing:
                    _edited = alert_create_dialog(stdscr, rows, cols, existing=_existing)
                    if _edited:
                        with state.lock:
                            if 0 <= _sel < len(state.alerts):
                                state.alerts[_sel] = _edited
                    db.prev = None
                    stdscr.clearok(True)
                    stdscr.clear()
            elif key in (ord("k"), ord("K")):
                with state.lock:
                    state.show_econ_cal = not state.show_econ_cal
                    if state.show_econ_cal and not state.econ_events:
                        threading.Thread(target=load_econ_calendar, daemon=True).start()
            elif key in (ord("1"), ord("2"), ord("3")):
                with state.lock:
                    if state.show_econ_cal:
                        _tier = key - ord("0")
                        if _tier in state.econ_impact_filter:
                            # Only remove if at least one tier remains
                            if len(state.econ_impact_filter) > 1:
                                state.econ_impact_filter.discard(_tier)
                        else:
                            state.econ_impact_filter.add(_tier)
            elif key in (ord("y"), ord("Y")):
                with state.lock:
                    _cal_open_y = state.show_econ_cal
                if _cal_open_y:
                    with state.lock:
                        state.econ_date_range = "yesterday"
                        state.econ_scroll = 0
                    threading.Thread(target=load_econ_calendar, daemon=True).start()

            elif key in (ord("m"), ord("M")):
                with state.lock:
                    state.global_mode = not state.global_mode
                    _enter_global = state.global_mode
                    _g_sess = state.session
                    _has_data = bool(state.global_data)
                if _enter_global:   # always refresh on entry
                    start_global(_g_sess)
                db.prev = None
                stdscr.clear()
            elif key in (ord("r"), ord("R")):
                with state.lock:
                    _in_global = state.global_mode
                    _in_econ   = state.show_econ_cal
                    _g_sess    = state.session
                if _in_global:
                    start_global(_g_sess)
                elif _in_econ:
                    threading.Thread(target=load_econ_calendar, daemon=True).start()
            elif key in (ord("h"), ord("H"), ord("?")):
                with state.lock:
                    state.show_help = not state.show_help
                    if not state.show_help:
                        state.help_scroll = 0
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
            elif key == curses.KEY_UP:
                with state.lock:
                    if state.show_help:
                        state.help_scroll = max(0, state.help_scroll - 1)
                    elif state.show_hline_list:
                        state.hline_sel = max(0, state.hline_sel - 1)
                    elif state.show_alert_list:
                        state.alert_list_sel = max(0, state.alert_list_sel - 1)
                    elif state.show_econ_cal:
                        state.econ_scroll = max(0, state.econ_scroll - 1)
            elif key == curses.KEY_DOWN:
                with state.lock:
                    if state.show_hline_list:
                        state.hline_sel = min(max(0, len(state.hlines)-1),
                                              state.hline_sel + 1)
                    elif state.show_alert_list:
                        n_a = len(state.alerts)
                        state.alert_list_sel = min(max(0, n_a-1), state.alert_list_sel + 1)
                    elif state.show_help:
                        state.help_scroll = state.help_scroll + 1
                    elif state.show_econ_cal:
                        state.econ_scroll = state.econ_scroll + 1
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
                    if state.global_mode:
                        pass  # scrolling disabled in global mode
                    else:
                        cci = state.cursor_col_idx
                        if cci < 0:
                            state.cursor_col_idx = max(0, len(state.candles) - 1)
                            cci = state.cursor_col_idx
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
                    if state.global_mode:
                        pass  # scrolling disabled in global mode
                        continue
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
            elif key == 27:  # Escape — close overlays or snap to live
                with state.lock:
                    if state.show_help:
                        state.show_help = False
                        state.help_scroll = 0
                    elif state.show_econ_cal:
                        state.show_econ_cal = False
                    else:
                        state.cursor_col_idx = -1
                        state.view_offset    = 0
            elif key == ord("."):  # period — dismiss alert popup
                with state.lock:
                    if state.alert_triggered:
                        state.alert_triggered.pop(0)
                    if not state.alert_triggered:
                        state.show_alert_popup = False


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
            db.prev = None  # force full redraw on resize
            curses.resizeterm(rows, cols)
            stdscr.clearok(True)
            stdscr.clear()

        draw(stdscr, db, rows, cols)
        db.flush(stdscr)
        stdscr.refresh()
        time.sleep(REFRESH_DELAY)

if __name__ == "__main__":
    curses.wrapper(main)
