#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# athena.py — Blackjack/CCCCWIDE automated execution engine
#
# Watches status.py's daily snapshot log + gex.py's live export for 5 of the
# framework's 6 conditions (Session, Volatility, PCVR, HPLs, Targets) and
# footprint.py's 90s order-flow log for the 6th (Order Flow), for ETH and QQQ
# independently. Renders a 6-segment red/green readiness meter per instrument.
# Once all 5 status-derived lights are active AND a closed footprint candle
# confirms direction (POC + net delta both moved the same way as the current
# PCVR regime), places a real Phemex entry with a bracket (fixed-distance SL
# + a 2-target split take-profit) and manages the resulting position,
# including an immediate market-close if PCVR flips to the opposite extreme
# zone while the position is open.
#
# athena.py never imports status.py or footprint.py — both are scripts with
# top-level argv parsing / curses / network-connect side effects, not
# libraries. It only reads their on-disk output as data:
#   status_logs/<Y>/<M>/<D>/status_<MM_DD_YYYY>.jsonl   (status.py, ~60s cadence)
#   status_<ASSET>_gex.json                              (gex.py's live export)
#   data/footprint/<Y>/<M>/<D>/footprint_<SYM>_90s_*.jsonl (footprint.py)
#
# All trading is Phemex-only (ETHUSDT / QQQUSDT). No MT5/XLTRADE involvement.
#
# Usage:
#   python athena.py [--interval 1-3] [--pct FLOAT] [--dry-run] [--no-session]
#                     [--reset-sim] [--sim-balance FLOAT]
#     --interval SEC   poll cadence for the status/footprint logs, clamped to
#                       [1, 3] (default 2). The status.py log itself is only
#                       appended every ~60s regardless of this setting — a
#                       faster poll just reacts within that cycle sooner.
#     --pct FLOAT       % of Phemex USDT balance to size each entry at
#                       (default 1.0). Recomputed fresh at every entry.
#     --dry-run         Trade against a persisted paper account (see
#                       SimAccount) instead of real Phemex — same order/SL/
#                       TP/PCVR-flip-close logic, same state machine, just
#                       backed by a simulated ledger seeded at $10,000
#                       (sim_account.json + sim_logs/) instead of real money.
#     --no-session      24-hour mode: drop Session from the readiness gate,
#                       so Athena can arm/enter outside status.py's kill
#                       zones too. Session is still read and shown in the
#                       meter for information — it's just no longer required
#                       to be active. The other 4 status conditions
#                       (Volatility, PCVR, HPLs, Targets) still gate as usual.
#     --reset-sim       Wipe the paper account back to a fresh starting
#                       balance before starting (only meaningful with
#                       --dry-run; ignored otherwise). Combine with
#                       --sim-balance to reset to something other than
#                       $10,000. This is a startup-time reset — restart the
#                       process with this flag whenever you want a clean
#                       paper account; there's no way to reset a separate
#                       already-running instance from outside it.
#     --sim-balance FLOAT   Paper-account balance to reset to (with
#                       --reset-sim) or seed with on a brand-new paper
#                       account (default 10000). Ignored on an existing
#                       paper account unless --reset-sim is also given.
# ─────────────────────────────────────────────────────────────────────────────

import sys
import os
import re
import glob
import json
import math
import time
import hmac
import hashlib
import asyncio
import threading
from collections import deque
from datetime import datetime, timezone

try:
    import httpx
except ModuleNotFoundError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "httpx"], stdout=subprocess.DEVNULL)
    import httpx

try:
    import curses
except ModuleNotFoundError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "windows-curses"], stdout=subprocess.DEVNULL)
    import curses

try:
    import websocket
except ModuleNotFoundError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websocket-client"], stdout=subprocess.DEVNULL)
    import websocket

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

def _load_tz(name):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        try:
            import subprocess
            subprocess.check_call([sys.executable, "-m", "pip", "install", "tzdata"], stdout=subprocess.DEVNULL)
            return ZoneInfo(name)
        except Exception:
            return None

TZ_ET = _load_tz("America/New_York")
TZ_CT = _load_tz("America/Chicago")

# ── Load .env (same convention as copycat.py/status.py) ──────────────────────
def load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.exists(env_path):
        print('\033[91m✗ .env file not found. Copy .env.example to .env.\033[0m')
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── ANSI tags ─────────────────────────────────────────────────────────────────
# These are never written raw to the terminal (curses can't render escape
# sequences via addstr) — every console_log()/log message still tags text
# with them exactly as before, and ansi_segments() (defined near the curses
# setup below) parses them into curses color-pair segments at draw time, so
# none of the many existing call sites needed to change.
RED = '\033[91m'; GRN = '\033[92m'; YLW = '\033[93m'; CYN = '\033[96m'
BLD = '\033[1m';  DIM = '\033[2m';  RST = '\033[0m'

# ── Config ────────────────────────────────────────────────────────────────────
PHEMEX_API_KEY    = os.environ.get('PHEMEX_API_KEY', '')
PHEMEX_API_SECRET = os.environ.get('PHEMEX_API_SECRET', '')
PHEMEX_BASE_URL   = 'https://api.phemex.com'
PHEMEX_WS_URL     = 'wss://ws.phemex.com'   # public trade_p feed — no auth needed

# Dedicated Alpaca account for Athena's own QQQ live tape — per explicit
# user request: "the footprint chart and trade logic should be using QQQ
# data, and only the trade itself should use QQQUSDT." Separate credentials
# from footprint.py's own ALPACA_API_KEY_ID(_FOOTPRINT) so the two don't
# fight over Alpaca's free-IEX-plan one-connection-per-symbol limit.
ALPACA_API_KEY_ID     = os.environ.get('ALPACA_API_ATHENA_ID', '')
ALPACA_API_SECRET_KEY = os.environ.get('ALPACA_API_SECRET_KEY_ATHENA', '')
ALPACA_WS_URL         = 'wss://stream.data.alpaca.markets/v2/iex'

# ETH's live tape ALSO aggregates Kraken + Coinbase, matching footprint.py's
# own multi-exchange approach for ETH's closed bars exactly (ws_kraken/
# ws_coinbase there) — confirmed live 2026-07-23: a Phemex-only ETH live
# bar showed materially different o/c than the SAME bucket's real closed
# value once footprint.py's full 3-exchange aggregate caught up (e.g.
# o=1878.29 Phemex-only vs. o=1877.59 real — a print landed on Kraken or
# Coinbase FIRST, invisible to a Phemex-only feed), i.e. the exact "correct
# after close, wrong while forming" bug the user reported. Public feeds,
# no auth needed, same as Phemex's own.
KRAKEN_WS_URL   = 'wss://ws.kraken.com/v2'
KRAKEN_ETH_PAIR = 'ETH/USD'
COINBASE_WS_URL       = 'wss://ws-feed.exchange.coinbase.com'
COINBASE_ETH_PRODUCT  = 'ETH-USD'

SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
STATUS_LOG_DIR_BASE = os.path.join(SCRIPT_DIR, "status_logs")
FOOTPRINT_DATA_DIR  = os.path.join(SCRIPT_DIR, "data", "footprint")
ATHENA_LOG_DIR_BASE = os.path.join(SCRIPT_DIR, "athena_logs")
SIM_STATE_PATH      = os.path.join(SCRIPT_DIR, "sim_account.json")
SIM_LOG_DIR_BASE    = os.path.join(SCRIPT_DIR, "sim_logs")
SIM_DEFAULT_BALANCE = 10000.0

FOOTPRINT_INTERVAL_LABEL = "90s"
FOOTPRINT_BAR_SECS       = 90   # numeric form of FOOTPRINT_INTERVAL_LABEL, for bucket math
GEX_EXPORT_MAX_AGE       = 120   # matches status.py's GEX_STATUS_EXPORT_MAX_AGE

VALUE_AREA_FRACTION = 0.70

# Per-asset: Phemex symbol, footprint.py symbol arg, fixed SL distance ($),
# the key each instrument's block lives under in status.py's snapshot log,
# and the leverage used for --dry-run's own available-margin simulation
# (SimAccount.to_account_snapshot's totalUsedBalanceRv — real Phemex
# margin usage per the account's configured leverage per symbol).
ASSETS = {
    "ETH": {"phemex_symbol": "ETHUSDT", "footprint_symbol": "ETH", "sl": 10.00, "snap_key": "eth", "leverage": 100, "tick": 0.10},
    "QQQ": {"phemex_symbol": "QQQUSDT", "footprint_symbol": "QQQ", "sl": 0.75,  "snap_key": "qqq", "leverage": 10,  "tick": 0.05},
}
LEVERAGE_BY_SYMBOL = {cfg["phemex_symbol"]: cfg["leverage"] for cfg in ASSETS.values()}

# A TP leg targeting GEX Flip never sits exactly at the flip level — $3 on
# the near side (below for a long, above for a short), applied both at
# initial placement (_check_fill) and every time GEX Flip itself moves
# while the position is open (_manage_position), since status.py/gex.py
# recompute it continuously.
GEX_FLIP_TP_BUFFER = 3.00

# Best-effort qty-step fallback if Phemex's /public/products response shape
# doesn't match what we expect — always used as a floor (never sized up), so
# a wrong guess here means a smaller/rougher order, never an oversized one.
DEFAULT_QTY_STEP  = {"ETH": 0.01, "QQQ": 0.01}
DEFAULT_PRICE_STEP = {"ETH": 0.01, "QQQ": 0.01}

# ── CLI args ─────────────────────────────────────────────────────────────────
args = sys.argv[1:]
INTERVAL = 2
if "--interval" in args:
    i = args.index("--interval")
    try:
        INTERVAL = max(1, min(3, int(float(args[i + 1]))))
    except (IndexError, ValueError):
        pass
PCT = 1.0
if "--pct" in args:
    i = args.index("--pct")
    try:
        PCT = max(0.0, float(args[i + 1]))
    except (IndexError, ValueError):
        pass
DRY_RUN = "--dry-run" in args
NO_SESSION = "--no-session" in args
ATHENA_ENABLED = True   # [A] master on/off toggle — see process_cycle's own
                         # gating: OFF blocks WATCHING->ARMED->confirm (no
                         # NEW entries), but PENDING_FILL/IN_POSITION
                         # management (SL/TP fills, PCVR-flip-close,
                         # EOD-flatten) keeps running unconditionally either
                         # way — pausing must never mean abandoning an
                         # already-open position's own risk management.
RESET_SIM = "--reset-sim" in args
SIM_BALANCE_ARG = SIM_DEFAULT_BALANCE
if "--sim-balance" in args:
    i = args.index("--sim-balance")
    try:
        SIM_BALANCE_ARG = max(0.0, float(args[i + 1]))
    except (IndexError, ValueError):
        pass

STATUS_LIGHT_NAMES = ["Session", "Volatility", "PCVR", "HPLs", "Targets"]

def required_status_lights():
    """The subset of the 5 status.py-derived lights that must actually be
    active to arm — all 5 normally, or all but Session in --no-session
    (24-hour) mode. Session is still computed/displayed either way, just
    not required."""
    return [n for n in STATUS_LIGHT_NAMES if not (NO_SESSION and n == "Session")]

# ── Athena's own event log — athena_logs/YYYY/MM/DD/athena_MM_DD_YYYY.jsonl ──
def athena_log_path(dt=None):
    dt = dt or datetime.now()
    day_dir = os.path.join(ATHENA_LOG_DIR_BASE, f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}")
    os.makedirs(day_dir, exist_ok=True)
    return os.path.join(day_dir, f"athena_{dt.strftime('%m_%d_%Y')}.jsonl")

def log_event(asset, event, detail=None):
    row = {"ts": datetime.now().isoformat(), "asset": asset, "event": event}
    if detail is not None:
        row["detail"] = detail
    try:
        with open(athena_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass

EVENT_LOG_MAXLEN = 500   # dashboard's compact "Recent Activity" section only
                         # ever shows the last 2 of these, but [L] opens a
                         # scrollable popup over the FULL buffer — this was
                         # 8 (just enough for the compact view) before that
                         # existed, which would have made "full log" mean
                         # almost nothing. Still in-memory/session-only, not
                         # a permanent record — athena_logs/sim_logs on disk
                         # already serve that role (see the [D]ata view).
_event_log = deque(maxlen=EVENT_LOG_MAXLEN)

def console_log(msg):
    """Buffer, don't print. render() is a cursor-positioned redraw (\\033[H
    ... \\033[0J) that assumes it owns the whole screen every cycle — a
    stray plain print() (e.g. one fired the moment an order gets placed)
    advances the real cursor past what render() expects, and once that
    scrolls the terminal, \\033[H no longer points at the top of the
    dashboard, breaking the whole layout. Keeping every message in this
    bounded ring buffer and rendering it as part of render()'s own single
    write (see the Recent Activity section) means nothing ever writes to
    the terminal outside that one redraw."""
    ts = datetime.now().strftime("%H:%M:%S")
    _event_log.append(f"{DIM}[{ts}]{RST} {msg}")

# ── status.py snapshot log — tail last line ───────────────────────────────────
def status_log_path(dt=None):
    dt = dt or datetime.now()
    day_dir = os.path.join(STATUS_LOG_DIR_BASE, f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}")
    return os.path.join(day_dir, f"status_{dt.strftime('%m_%d_%Y')}.jsonl")

def read_last_status_snapshot():
    path = status_log_path()
    try:
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        if not lines:
            return None
        return json.loads(lines[-1])
    except Exception:
        return None

def read_gex_export(asset):
    path = os.path.join(SCRIPT_DIR, f"status_{asset}_gex.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("updated_at", 0) <= GEX_EXPORT_MAX_AGE:
            return data
    except Exception:
        pass
    return None

def reconstruct_targets(log_targets, gex_export):
    """[{'type','level'}, ...] in status.py render()'s exact Targets order:
    BT/ST, then GEX Flip, then every Cluster — status.py's own snapshot log
    only carries BT/ST + Cluster, so GEX Flip (read separately from the
    gex.py export) is spliced in as the 2nd element when present.

    Large gamma clusters take precedence over Medium ones (explicit user
    request 2026-07-23) — status.py's own log orders Cluster entries by
    STRIKE ascending (see `gamma_cluster_targets_directional`'s `above`/
    `below` sort), not by tier, so a closer Medium cluster can otherwise
    land ahead of a farther but more significant Large one. Each Cluster
    entry carries its own `"tier"` field ("Medium"/"Large" — see
    status.py's `magnitude_tier()`); a stable sort on tier (Large first)
    preserves status.py's own relative ordering WITHIN each tier, only
    promoting Large clusters ahead of Medium ones across tiers."""
    log_targets = log_targets or []
    full = []
    idx = 0
    if log_targets and log_targets[0].get("type") in ("BT", "ST"):
        full.append({"type": log_targets[0]["type"], "level": float(log_targets[0]["level"])})
        idx = 1
    gex_flip = gex_export.get("gex_flip") if gex_export else None
    if gex_flip is not None:
        full.append({"type": "GEX Flip", "level": float(gex_flip)})
    clusters = [t for t in log_targets[idx:] if t.get("type") == "Cluster"]
    clusters.sort(key=lambda t: 0 if t.get("tier") == "Large" else 1)
    for t in clusters:
        full.append({"type": "Cluster", "level": float(t["level"])})
    return full

def instrument_lights(snapshot, asset):
    """(lights_dict, regime, price, targets_full, market_closed) for one
    instrument, from the latest status.py snapshot + this cycle's gex
    export. regime is 'long' (PCVR<=0.98), 'short' (PCVR>=1.02), or None.

    market_closed (QQQ outside 08:45-15:00 CT weekdays — status.py's own
    qqq.market_closed, same field §_flatten_eod reuses) forces every light
    false and regime None rather than showing whatever partial/stale state
    (Session/Volatility/PCVR are computed independently of market hours and
    can easily still read green) would otherwise light up and look like
    real progress toward a trade that can't actually happen."""
    cfg = ASSETS[asset]
    lights = {"Session": False, "Volatility": False, "PCVR": False, "HPLs": False, "Targets": False}
    if snapshot is None:
        return lights, None, None, [], False

    inst = snapshot.get(cfg["snap_key"]) or {}
    price = inst.get("price")
    market_closed = bool(inst.get("market_closed"))
    if market_closed:
        return lights, None, price, [], True

    lights["Session"] = bool((snapshot.get("session") or {}).get("in_session"))

    if asset == "ETH":
        lights["Volatility"] = snapshot.get("dvol") is not None
    else:
        lights["Volatility"] = snapshot.get("vxn") is not None

    pcvr = snapshot.get("pcvr") or {}
    ratio = pcvr.get("ratio")
    regime = None
    if ratio is not None:
        if ratio <= 0.98:
            regime = "long"
        elif ratio >= 1.02:
            regime = "short"
    lights["PCVR"] = bool(ratio is not None and (ratio <= 0.98 or ratio >= 1.02))

    lights["HPLs"] = bool(inst.get("any_active"))
    log_targets = inst.get("targets") or []
    lights["Targets"] = bool(log_targets)

    gex_export = read_gex_export(asset)
    targets_full = reconstruct_targets(log_targets, gex_export)
    return lights, regime, price, targets_full, False

# ── footprint.py bar log — tail last CLOSED bar ───────────────────────────────
def footprint_log_glob(asset):
    """footprint.py fixes its own day-folder (TODAY_STR) at process start
    and never recomputes it, so a footprint.py instance that's been running
    across one or more local midnights keeps writing into a folder that's
    however many days old — a single long-lived instance can drift multiple
    days behind actual "today" (confirmed live: a process still running
    from 07/21 was found writing into that same folder two days later, on
    07/23 — an earlier version of this function only checked today/
    yesterday and missed it entirely). Search the WHOLE data/footprint tree
    recursively rather than guessing how many days back to look, and pick
    whichever matching file is actually most recently modified — correct
    for any gap length, and still cheap (this repo's footprint history is a
    few dozen files at most, and this only runs once per engine cycle, not
    per redraw)."""
    pattern = os.path.join(FOOTPRINT_DATA_DIR, "**",
                            f"footprint_{ASSETS[asset]['footprint_symbol']}_{FOOTPRINT_INTERVAL_LABEL}_*.jsonl")
    matches = glob.glob(pattern, recursive=True)
    if not matches:
        return None
    matches.sort(key=os.path.getmtime, reverse=True)
    return matches[0]

# ── Live trade tape (self-contained — no dependency on footprint.py at all) ──
# Athena's still-forming bar previously either (a) approximated O/H/L/C from
# polled ticker prices with no per-level data, or (b) read a snapshot file
# footprint.py exported — both worked but tied the live chart to something
# outside Athena's own control (a poll cadence, or another process's uptime/
# restart timing). This instead subscribes directly to Phemex's own public
# trade-print WS feed for EXACTLY the instrument Athena trades (ETHUSDT/
# QQQUSDT) — same `trade_p.subscribe` channel/message shape footprint.py's
# own ws_phemex() already uses in production (ported, not reinvented) — and
# folds prints into a live per-price-level bar with zero external file or
# process dependency. Closed-bar history (used for confirmation logic and
# the historical chart columns) still comes from footprint.py's on-disk log
# — that's a different, separate, already-necessary dependency (Order Flow
# confirmation reads footprint.py's own closed bars) and is unaffected.
class LiveTape:
    def __init__(self):
        self.lock = threading.Lock()
        self.bars = {a: None for a in ASSETS}
        self.status = {a: "connecting…" for a in ASSETS}

    def ingest(self, asset, ts, price, qty, is_buy, tick):
        """A brand-new bucket's O/H/L/C all start at the triggering trade's
        own price — matches footprint.py's own `_new_bar()` exactly (it
        does NOT seed from the previous bar's close either; verified by
        reading its source directly). An earlier version of this seeded
        from the last CLOSED bar's close instead, on the assumption that a
        new candle's open should always connect to the previous candle's
        close — reasonable as a general charting convention, but NOT what
        footprint.py itself does, so it actively made Athena's live bar
        DIVERGE from footprint.py's own (organic, sometimes genuinely
        gappy) behavior instead of matching it. Reverted 2026-07-23 after
        the user reported Athena's live O/H/L/C no longer matching
        footprint.py's own values for the same bar."""
        bucket_ts = int(ts // FOOTPRINT_BAR_SECS) * FOOTPRINT_BAR_SECS
        with self.lock:
            bar = self.bars[asset]
            if bar is None or bar["ts"] != bucket_ts:
                bar = {"ts": bucket_ts, "o": price, "h": price, "l": price, "c": price,
                       "delta": 0.0, "tick": tick, "levels": {}, "live": True}
            bar["h"] = max(bar["h"], price)
            bar["l"] = min(bar["l"], price)
            bar["c"] = price
            bar["tick"] = tick
            lvl = round(price / tick)
            cell = bar["levels"].setdefault(lvl, [0.0, 0.0])
            if is_buy:
                cell[1] += qty
            else:
                cell[0] += qty
            bar["delta"] = bar["delta"] + (qty if is_buy else -qty)
            self.bars[asset] = bar

    def snapshot(self, asset):
        """A fully independent copy (levels dict AND its inner [bid,ask]
        lists all copied) — the WS thread keeps mutating the live original
        after this returns, so the consumer (curses draw thread, via
        AppState) must never hold a reference into it."""
        with self.lock:
            bar = self.bars.get(asset)
            if bar is None:
                return None
            copy = dict(bar)
            copy["levels"] = {k: list(v) for k, v in bar["levels"].items()}
            return copy

    def set_status(self, asset, text):
        with self.lock:
            self.status[asset] = text

LIVE_TAPE = LiveTape()

def _current_tick(asset):
    """Athena's own live tape ALWAYS groups at a fixed per-asset tick
    (ASSETS[asset]["tick"] — $0.10 ETH / $0.05 QQQ), per explicit user
    request, regardless of whatever footprint.py itself is currently
    configured with. This ALSO fully resolves an earlier startup-race bug
    (a fast, no-auth WS feed could deliver its first trade before
    AppState.footprint_bars was populated, and the old dynamic
    "read the last closed bar's own tick" approach had no real value to
    read yet in that window) — there's simply nothing left to wait for or
    guess anymore, the tick is known upfront."""
    return ASSETS[asset]["tick"]

def _phemex_trade_ws(asset, stop_evt):
    """Background thread: subscribes to Phemex's public trade_p feed for
    this asset's own traded symbol and folds every print into LIVE_TAPE.
    Reconnects with backoff on any disconnect/error — same convention as
    footprint.py's own ws_phemex(), trimmed down (no multi-symbol session
    staleness tracking needed; this thread only ever serves one asset for
    its whole lifetime, stopped via stop_evt at quit)."""
    symbol = ASSETS[asset]["phemex_symbol"]

    def on_open(ws):
        ws.send(json.dumps({"id": 1, "method": "trade_p.subscribe", "params": [symbol]}))
        LIVE_TAPE.set_status(asset, "live")

    def on_message(ws, message):
        try:
            msg = json.loads(message)
        except Exception:
            return
        if msg.get("type") == "snapshot":
            return   # Phemex replays historical trades on (re)connect — not new prints
        trades = msg.get("trades_p")
        if not trades:
            return
        if msg.get("symbol") and msg["symbol"] != symbol:
            return
        tick = _current_tick(asset)
        for row in trades:
            try:
                ts = row[0] / 1e9
                is_buy = row[1] == "Buy"
                price = float(row[2])
                qty = float(row[3])
            except Exception:
                continue
            LIVE_TAPE.ingest(asset, ts, price, qty, is_buy, tick)
        LIVE_TAPE.set_status(asset, "live")

    def on_error(ws, err):
        LIVE_TAPE.set_status(asset, f"err: {str(err)[:30]}")

    def on_close(ws, code, msg):
        if not stop_evt.is_set():
            LIVE_TAPE.set_status(asset, "reconnecting…")

    _ping_ws = [None]

    def _heartbeat():
        while not stop_evt.wait(timeout=20):
            ws = _ping_ws[0]
            if ws:
                try:
                    ws.send(json.dumps({"id": 0, "method": "server.ping", "params": []}))
                except Exception:
                    pass

    threading.Thread(target=_heartbeat, daemon=True).start()

    backoff = 1
    while not stop_evt.is_set():
        ws_app = websocket.WebSocketApp(PHEMEX_WS_URL, on_open=on_open, on_message=on_message,
                                         on_error=on_error, on_close=on_close)
        _ping_ws[0] = ws_app
        try:
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            LIVE_TAPE.set_status(asset, f"crashed: {str(e)[:30]}")
        if stop_evt.is_set():
            break
        LIVE_TAPE.set_status(asset, f"reconnecting… ({backoff}s)")
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

def _parse_rfc3339(ts_str):
    """Ported verbatim from footprint.py — Alpaca trims trailing zeros off
    the fractional-seconds part inconsistently (e.g. "...30.01172" is 5
    digits), which fromisoformat() rejects unless zero-padded to exactly
    6 first."""
    ts_str = ts_str.rstrip("Z")
    if "." in ts_str:
        base, frac = ts_str.split(".")
        ts_str = f"{base}.{(frac + '000000')[:6]}"
    return datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc).timestamp()

def classify_one_trade(price, bid, ask, prev_price, prev_side):
    """Ported verbatim from footprint.py — quote-rule (Lee-Ready)
    classification for a venue with no native buy/sell trade tag (Alpaca,
    same as most non-crypto venues): at/above the prevailing ask is
    buyer-initiated, at/below the bid is seller-initiated, falling back to
    a tick-rule vs. the previous trade when it prints inside the spread or
    no quote is available yet, and "assume buy" for the very first trade
    with neither."""
    if ask is not None and price >= ask:
        return True
    if bid is not None and price <= bid:
        return False
    if prev_price is not None:
        return price > prev_price if price != prev_price else prev_side
    return True

def _alpaca_trade_ws(asset, stop_evt):
    """Background thread: real-time IEX trades+quotes for this asset's REAL
    symbol (QQQ, not QQQUSDT) via Athena's own dedicated Alpaca account —
    per explicit user request, so the footprint chart's live candle and
    the entry/confirmation decision price (see reference_price()) both
    reflect the actual QQQ ETF, not Phemex's thinner QQQUSDT perp, while
    order EXECUTION still goes through Phemex/QQQUSDT unchanged. Ported
    from footprint.py's own ws_alpaca() (auth flow, quote-rule trade
    classification, RFC3339 timestamp parsing) — same reasoning as
    _phemex_trade_ws being ported from ws_phemex(): proven, don't
    reinvent. Alpaca has no native buy/sell tag on trades, hence
    maintaining a running bid/ask from the quote stream to classify each
    trade live via classify_one_trade()."""
    symbol = ASSETS[asset]["footprint_symbol"]   # "QQQ", not the Phemex symbol
    latest = {"bid": None, "ask": None, "prev_price": None, "prev_side": True}

    def on_open(ws):
        ws.send(json.dumps({"action": "auth", "key": ALPACA_API_KEY_ID, "secret": ALPACA_API_SECRET_KEY}))

    def on_message(ws, message):
        try:
            msgs = json.loads(message)
        except Exception:
            return
        if not isinstance(msgs, list):
            msgs = [msgs]
        for msg in msgs:
            mtype = msg.get("T")
            if mtype == "success" and msg.get("msg") == "authenticated":
                ws.send(json.dumps({"action": "subscribe", "trades": [symbol], "quotes": [symbol]}))
                LIVE_TAPE.set_status(asset, "live")
            elif mtype == "error":
                LIVE_TAPE.set_status(asset, f"err: {str(msg.get('msg', ''))[:30]}")
            elif mtype == "q":
                latest["bid"] = msg.get("bp")
                latest["ask"] = msg.get("ap")
            elif mtype == "t":
                try:
                    ts = _parse_rfc3339(msg["t"])
                    price = float(msg["p"])
                    qty = float(msg["s"])
                except Exception:
                    continue
                is_buy = classify_one_trade(price, latest["bid"], latest["ask"],
                                             latest["prev_price"], latest["prev_side"])
                latest["prev_price"], latest["prev_side"] = price, is_buy
                tick = _current_tick(asset)
                LIVE_TAPE.ingest(asset, ts, price, qty, is_buy, tick)
                LIVE_TAPE.set_status(asset, "live")

    last_err = {"text": ""}

    def on_error(ws, err):
        last_err["text"] = str(err)[:30]
        LIVE_TAPE.set_status(asset, f"err: {last_err['text']}")

    def on_close(ws, code, msg):
        if not stop_evt.is_set():
            LIVE_TAPE.set_status(asset, "reconnecting…")

    backoff = 1
    while not stop_evt.is_set():
        ws_app = websocket.WebSocketApp(ALPACA_WS_URL, on_open=on_open, on_message=on_message,
                                         on_error=on_error, on_close=on_close)
        try:
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            last_err["text"] = str(e)[:30]
            LIVE_TAPE.set_status(asset, f"crashed: {last_err['text']}")
        if stop_evt.is_set():
            break
        # "connection limit exceeded" means Alpaca's server still thinks a
        # previous connection is live (e.g. a killed process it hasn't
        # detected as dead yet) — retrying fast just gets rejected again,
        # so floor the wait well above the normal ramp (same as footprint.py's
        # own ws_alpaca()).
        if "connection limit" in last_err["text"].lower():
            backoff = max(backoff, 15)
        LIVE_TAPE.set_status(asset, f"reconnecting… ({backoff}s)")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)

def _kraken_trade_ws(asset, stop_evt):
    """Background thread: Kraken's public trade feed for ETH/USD — one of
    the two additional exchanges (with Coinbase) that make up ETH's live
    tape aggregate, matching footprint.py's own ws_kraken() exactly (same
    subscribe message, same message shape). No auth needed."""
    def on_open(ws):
        ws.send(json.dumps({"method": "subscribe", "params": {"channel": "trade", "symbol": [KRAKEN_ETH_PAIR]}}))
        LIVE_TAPE.set_status(asset, "live")

    def on_message(ws, message):
        try:
            msg = json.loads(message)
        except Exception:
            return
        if msg.get("channel") != "trade" or msg.get("type") != "update":
            return
        tick = _current_tick(asset)
        for t in msg.get("data", []):
            try:
                ts_str = t["timestamp"].replace("Z", "+00:00")
                ts = datetime.fromisoformat(ts_str).timestamp()
                price = float(t["price"])
                qty = float(t["qty"])
                is_buy = t["side"] == "buy"
            except Exception:
                continue
            LIVE_TAPE.ingest(asset, ts, price, qty, is_buy, tick)
        LIVE_TAPE.set_status(asset, "live")

    def on_error(ws, err):
        LIVE_TAPE.set_status(asset, f"err: {str(err)[:30]}")

    def on_close(ws, code, msg):
        if not stop_evt.is_set():
            LIVE_TAPE.set_status(asset, "reconnecting…")

    backoff = 1
    while not stop_evt.is_set():
        ws_app = websocket.WebSocketApp(KRAKEN_WS_URL, on_open=on_open, on_message=on_message,
                                         on_error=on_error, on_close=on_close)
        try:
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            LIVE_TAPE.set_status(asset, f"crashed: {str(e)[:30]}")
        if stop_evt.is_set():
            break
        LIVE_TAPE.set_status(asset, f"reconnecting… ({backoff}s)")
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

def _coinbase_trade_ws(asset, stop_evt):
    """Background thread: Coinbase's public "matches" feed for ETH-USD —
    the other of the two additional exchanges making up ETH's live tape
    aggregate, matching footprint.py's own ws_coinbase() exactly. No auth
    needed."""
    def on_open(ws):
        ws.send(json.dumps({"type": "subscribe",
                             "channels": [{"name": "matches", "product_ids": [COINBASE_ETH_PRODUCT]}]}))
        LIVE_TAPE.set_status(asset, "live")

    def on_message(ws, message):
        try:
            msg = json.loads(message)
        except Exception:
            return
        mtype = msg.get("type")
        if mtype == "error":
            LIVE_TAPE.set_status(asset, f"err: {str(msg.get('message', ''))[:30]}")
            return
        if mtype != "match":
            return
        try:
            ts = _parse_rfc3339(msg["time"])
            price = float(msg["price"])
            qty = float(msg["size"])
            is_buy = msg["side"] == "sell"   # maker's side flipped -> taker/aggressor
        except Exception:
            return
        tick = _current_tick(asset)
        LIVE_TAPE.ingest(asset, ts, price, qty, is_buy, tick)
        LIVE_TAPE.set_status(asset, "live")

    def on_error(ws, err):
        LIVE_TAPE.set_status(asset, f"err: {str(err)[:30]}")

    def on_close(ws, code, msg):
        if not stop_evt.is_set():
            LIVE_TAPE.set_status(asset, "reconnecting…")

    backoff = 1
    while not stop_evt.is_set():
        ws_app = websocket.WebSocketApp(COINBASE_WS_URL, on_open=on_open, on_message=on_message,
                                         on_error=on_error, on_close=on_close)
        try:
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            LIVE_TAPE.set_status(asset, f"crashed: {str(e)[:30]}")
        if stop_evt.is_set():
            break
        LIVE_TAPE.set_status(asset, f"reconnecting… ({backoff}s)")
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

async def reference_price(asset, phemex_symbol):
    """The price used for ENTRY/CONFIRMATION decisions (target-distance
    viability, market-vs-limit order_type, market-fill entry_price) — NOT
    for order execution itself, which always goes through the real Phemex
    symbol regardless (place_entry/place_sl/place_tp_leg/market_close all
    still take phemex_symbol directly, untouched). For QQQ specifically,
    per explicit user request ("trade logic should be using QQQ data, only
    the trade itself should use QQQUSDT"), prefers the real QQQ price from
    Athena's own Alpaca-fed LIVE_TAPE over Phemex's thinner QQQUSDT perp.
    Every other asset — and QQQ too, if Alpaca hasn't populated LIVE_TAPE
    yet (startup, or the feed is down) — falls back to the existing
    Phemex-based fetch_last_price(), unchanged."""
    if asset == "QQQ":
        bar = LIVE_TAPE.snapshot("QQQ")
        if bar is not None:
            return bar["c"]
    return await fetch_last_price(phemex_symbol)

def read_last_two_footprint_bars(asset):
    """(prev_bar, cur_bar) — the two most recent CLOSED bars, read fresh
    from disk every call. Reading directly off the file (rather than
    trusting an in-memory "last seen bar" that gets updated every cycle
    regardless of state) is deliberate: it means the confirmation check
    always compares the two most recent bars currently on disk, so a bar
    that closes right as — or just before — an instrument arms still gets
    evaluated against its own predecessor instead of being silently
    absorbed as an already-seen baseline. cur_bar is None if the log is
    empty; prev_bar is None if there's only one bar so far."""
    path = footprint_log_glob(asset)
    if not path:
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        if not lines:
            return None, None
        cur = json.loads(lines[-1])
        prev = json.loads(lines[-2]) if len(lines) >= 2 else None
        return prev, cur
    except Exception:
        return None, None

def compute_poc(levels):
    if not levels:
        return None
    return max(levels, key=lambda lvl: levels[lvl][0] + levels[lvl][1])

def compute_value_area(levels, poc_g, target_frac=VALUE_AREA_FRACTION):
    if not levels or poc_g is None:
        return poc_g, poc_g
    total = sum(c[0] + c[1] for c in levels.values())
    if total <= 0:
        return poc_g, poc_g
    lo = hi = poc_g
    poc_cell = levels.get(poc_g, [0.0, 0.0])
    acc = poc_cell[0] + poc_cell[1]
    target = total * target_frac
    while acc < target:
        below = levels.get(lo - 1)
        above = levels.get(hi + 1)
        below_vol = (below[0] + below[1]) if below else -1.0
        above_vol = (above[0] + above[1]) if above else -1.0
        if below_vol < 0 and above_vol < 0:
            break
        if above_vol >= below_vol:
            hi += 1
            acc += above_vol
        else:
            lo -= 1
            acc += below_vol
    return lo, hi

def bar_poc_vah_val(bar):
    """(poc_price, vah_price, val_price) for one serialized footprint.py bar,
    reimplementing footprint.py's own compute_poc/compute_value_area math
    (see module docstring for why this isn't just imported)."""
    tick = bar.get("tick", 1.0)
    levels = {int(k): v for k, v in (bar.get("levels") or {}).items()}
    poc_g = compute_poc(levels)
    if poc_g is None:
        return None, None, None
    lo, hi = compute_value_area(levels, poc_g)
    return poc_g * tick, hi * tick, lo * tick

def footprint_confirmation(prev_bar, new_bar, regime):
    """True if new_bar confirms `regime` against prev_bar: POC and net delta
    both moved the same direction the regime needs (both up for long, both
    down for short) — per the user's exact spec, a value/sign comparison
    against the previous bar, not an absolute threshold."""
    prev_poc, _, _ = bar_poc_vah_val(prev_bar)
    new_poc, new_vah, new_val = bar_poc_vah_val(new_bar)
    if prev_poc is None or new_poc is None:
        return False, None, None
    prev_delta = prev_bar.get("delta", 0.0)
    new_delta = new_bar.get("delta", 0.0)
    if regime == "long":
        ok = new_poc > prev_poc and new_delta > prev_delta
        return ok, new_vah, new_val
    if regime == "short":
        ok = new_poc < prev_poc and new_delta < prev_delta
        return ok, new_vah, new_val
    return False, new_vah, new_val

# ── Simulated ("paper") account for --dry-run ─────────────────────────────────
# sim_logs/YYYY/MM/DD/sim_MM_DD_YYYY.jsonl — append-only ledger of every
# simulated fill/close, same per-day-folder convention as status_logs/
# data/footprint/athena_logs, for later recall/analysis.
def sim_log_path(dt=None):
    dt = dt or datetime.now()
    day_dir = os.path.join(SIM_LOG_DIR_BASE, f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}")
    os.makedirs(day_dir, exist_ok=True)
    return os.path.join(day_dir, f"sim_{dt.strftime('%m_%d_%Y')}.jsonl")

def sim_log_event(event, detail):
    row = {"ts": datetime.now().isoformat(), "event": event}
    row.update(detail)
    try:
        with open(sim_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass

class SimAccount:
    """Paper-trading ledger used in --dry-run mode, persisted to
    sim_account.json. Exposes place_entry/place_sl/place_tp_leg/cancel_all/
    market_close with the same shapes as the real Phemex calls, plus
    to_account_snapshot() shaped exactly like Phemex's own
    /g-accounts/accountPositions response — so account_available_balance()/
    account_position() (and therefore the ENTIRE PENDING_FILL/IN_POSITION
    state machine in AthenaInstrument) work unchanged against either a real
    or simulated account, with no separate simulated code path to maintain.
    Fills/closes are matched against Phemex's own live public ticker price
    (fetch_last_price) so a paper trade fills at the same price a real one
    would have, even though no real order is ever sent."""

    def __init__(self, default_balance=SIM_DEFAULT_BALANCE):
        self.balance = default_balance
        self.day = datetime.now().strftime("%m_%d_%Y")
        self.realized_pnl_today = 0.0
        self.positions = {}   # symbol -> {"pos_side","qty","avg_entry"}
        self.orders = {}      # clOrdID -> order dict
        self._seq = 0
        self._load()

    def _load(self):
        try:
            with open(SIM_STATE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            self.balance = data.get("balance", self.balance)
            self.day = data.get("day", self.day)
            self.realized_pnl_today = data.get("realized_pnl_today", 0.0)
            self.positions = data.get("positions", {})
            self.orders = data.get("orders", {})
        except Exception:
            pass
        self._roll_day()

    def _roll_day(self):
        today = datetime.now().strftime("%m_%d_%Y")
        if today != self.day:
            self.day = today
            self.realized_pnl_today = 0.0

    def _save(self):
        self._roll_day()
        try:
            with open(SIM_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"balance": self.balance, "day": self.day,
                           "realized_pnl_today": self.realized_pnl_today,
                           "positions": self.positions, "orders": self.orders}, f, indent=2)
        except Exception:
            pass

    def reset(self, balance):
        self.balance = balance
        self.day = datetime.now().strftime("%m_%d_%Y")
        self.realized_pnl_today = 0.0
        self.positions = {}
        self.orders = {}
        self._save()
        sim_log_event("reset", {"balance": balance})

    def _next_id(self, prefix):
        self._seq += 1
        return f"sim_{prefix}_{int(time.time() * 1000)}_{self._seq}"

    def _apply_fill(self, symbol, pos_side, qty, price, order_type):
        pos = self.positions.get(symbol)
        if pos and pos.get("pos_side") == pos_side:
            total = pos["qty"] + qty
            pos["avg_entry"] = (pos["avg_entry"] * pos["qty"] + price * qty) / total
            pos["qty"] = total
        else:
            self.positions[symbol] = {"pos_side": pos_side, "qty": qty, "avg_entry": price}
        sim_log_event("filled", {"symbol": symbol, "pos_side": pos_side, "qty": qty,
                                  "price": price, "order_type": order_type})

    def _close(self, symbol, pos_side, qty, price, reason):
        pos = self.positions.get(symbol)
        if not pos:
            return
        qty = min(qty, pos["qty"])
        entry = pos["avg_entry"]
        pnl = (price - entry) * qty if pos_side == "Long" else (entry - price) * qty
        self.balance += pnl
        self._roll_day()
        self.realized_pnl_today += pnl
        pos["qty"] -= qty
        sim_log_event("closed", {"symbol": symbol, "pos_side": pos_side, "qty": qty, "price": price,
                                  "entry": entry, "pnl": pnl, "reason": reason, "balance": self.balance})
        if pos["qty"] <= 1e-9:
            del self.positions[symbol]
            for oid in [k for k, o in self.orders.items() if o["symbol"] == symbol]:
                del self.orders[oid]

    async def place_entry(self, symbol, pos_side, order_type, qty_str, price_str):
        qty = float(qty_str)
        if order_type == "market":
            price = await fetch_last_price(symbol)
            if price is None:
                return {"code": -1, "msg": "sim: no live price available"}
            self._apply_fill(symbol, pos_side, qty, price, "market")
        else:
            oid = self._next_id("entry")
            self.orders[oid] = {"symbol": symbol, "kind": "entry", "posSide": pos_side,
                                 "qty": qty, "price": float(price_str)}
            sim_log_event("order_placed", {"symbol": symbol, "kind": "entry", "pos_side": pos_side,
                                            "qty": qty, "price": float(price_str)})
        self._save()
        return {"code": 0}

    async def place_sl(self, symbol, pos_side, sl_price):
        oid = self._next_id("sl")
        self.orders[oid] = {"symbol": symbol, "kind": "sl", "posSide": pos_side, "stopPx": float(sl_price)}
        self._save()
        return {"code": 0}

    async def place_tp_leg(self, symbol, pos_side, qty_str, price, suffix, target_type="?"):
        oid = self._next_id(f"tp{suffix}")
        self.orders[oid] = {"symbol": symbol, "kind": "tp", "posSide": pos_side,
                             "qty": float(qty_str), "price": float(price), "type": target_type}
        self._save()
        return {"code": 0}

    async def cancel_all(self, symbol):
        for oid in [k for k, o in self.orders.items() if o["symbol"] == symbol]:
            del self.orders[oid]
        self._save()
        return {"code": 0}

    async def market_close(self, symbol, pos_side, qty, reason="flip"):
        price = await fetch_last_price(symbol)
        if price is None:
            return {"code": -1, "msg": "sim: no live price available"}
        self._close(symbol, pos_side, qty, price, reason)
        self._save()
        return {"code": 0}

    async def tick_matching(self):
        """Check every resting sim order against the live Phemex price and
        fill/close whatever now qualifies. Called every time fetch_account()
        is polled — mirrors how real fills/SL/TP are only ever discovered
        by polling the real exchange, so the same AthenaInstrument code
        works for both."""
        symbols = {o["symbol"] for o in self.orders.values()} | set(self.positions.keys())
        for symbol in symbols:
            price = await fetch_last_price(symbol)
            if price is None:
                continue
            for oid, o in list(self.orders.items()):
                if o["symbol"] != symbol or o["kind"] != "entry":
                    continue
                pos_side = o["posSide"]
                if (pos_side == "Long" and price <= o["price"]) or (pos_side == "Short" and price >= o["price"]):
                    self._apply_fill(symbol, pos_side, o["qty"], price, "limit")
                    del self.orders[oid]
            pos = self.positions.get(symbol)
            if not pos:
                continue
            pos_side = pos["pos_side"]
            for oid, o in list(self.orders.items()):
                if o["symbol"] != symbol:
                    continue
                if o["kind"] == "sl":
                    hit = (pos_side == "Long" and price <= o["stopPx"]) or (pos_side == "Short" and price >= o["stopPx"])
                    if hit:
                        self._close(symbol, pos_side, pos["qty"], price, "sl")
                        for k in [k2 for k2, o2 in self.orders.items() if o2["symbol"] == symbol]:
                            self.orders.pop(k, None)
                        break
                elif o["kind"] == "tp":
                    hit = (pos_side == "Long" and price >= o["price"]) or (pos_side == "Short" and price <= o["price"])
                    if hit:
                        self._close(symbol, pos_side, o["qty"], price, "tp")
                        self.orders.pop(oid, None)
        self._save()

    async def to_account_snapshot(self):
        """totalUsedBalanceRv is real per-position margin usage — notional
        (qty * LIVE price, not entry price, so "Available" tracks the
        position's actual current margin draw as price moves) divided by
        that symbol's configured leverage (LEVERAGE_BY_SYMBOL — 100x ETH/
        10x QQQ) — was hardcoded "0" before, meaning Available always
        equaled Balance regardless of any open position, per explicit user
        request to make it reflect real margin usage."""
        positions = []
        used = 0.0
        for symbol, pos in self.positions.items():
            if pos["qty"] > 0:
                positions.append({"symbol": symbol, "posSide": pos["pos_side"],
                                   "size": str(pos["qty"]), "avgEntryPriceRp": str(pos["avg_entry"])})
                price = await fetch_last_price(symbol)
                if price is None:
                    price = pos["avg_entry"]
                leverage = LEVERAGE_BY_SYMBOL.get(symbol, 1)
                used += (pos["qty"] * price) / leverage
        return {"data": {"account": {"accountBalanceRv": str(self.balance), "totalUsedBalanceRv": str(used)},
                          "positions": positions}}

_sim_account = None

def get_sim_account():
    global _sim_account
    if _sim_account is None:
        _sim_account = SimAccount(SIM_BALANCE_ARG)
    return _sim_account

# ── Phemex REST (HMAC signing/order shapes copied from copycat.py) ───────────
def _phemex_sign(path, query='', body=''):
    expiry = str(int(time.time()) + 60)
    msg = path + query + expiry + body
    sig = hmac.new(PHEMEX_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return expiry, sig

def _phemex_headers(path, query='', body=''):
    expiry, sig = _phemex_sign(path, query, body)
    return {
        'x-phemex-access-token': PHEMEX_API_KEY,
        'x-phemex-request-expiry': expiry,
        'x-phemex-request-signature': sig,
        'Content-Type': 'application/json',
    }

async def phemex_request(method, path, params=None, body=None):
    query = '&'.join(f'{k}={v}' for k, v in params.items()) if params else ''
    body_str = json.dumps(body) if body else ''
    headers = _phemex_headers(path, query, body_str)
    url = f"{PHEMEX_BASE_URL}{path}" + (f"?{query}" if query else '')
    async with httpx.AsyncClient(timeout=10) as client:
        if method == 'GET':
            r = await client.get(url, headers=headers)
        elif method == 'PUT':
            r = await client.put(url, headers=headers, content=body_str) if body else await client.put(url, headers=headers)
        elif method == 'POST':
            r = await client.post(url, headers=headers, content=body_str)
        elif method == 'DELETE':
            r = await client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        return r.json()

async def fetch_account():
    if DRY_RUN:
        sim = get_sim_account()
        await sim.tick_matching()
        return await sim.to_account_snapshot()
    return await phemex_request('GET', '/g-accounts/accountPositions', params={'currency': 'USDT'})

def account_available_balance(acc_data):
    acc = (acc_data or {}).get('data', {}).get('account', {})
    bal = float(acc.get('accountBalanceRv') or 0)
    used = float(acc.get('totalUsedBalanceRv') or 0)
    return max(0.0, bal - used)

def account_balance_fields(acc_data):
    """(balance, available) for the dashboard's ACCOUNT line."""
    acc = (acc_data or {}).get('data', {}).get('account', {})
    bal = float(acc.get('accountBalanceRv') or 0)
    used = float(acc.get('totalUsedBalanceRv') or 0)
    return bal, max(0.0, bal - used)

def account_realized_pnl_today(acc_data, symbols):
    """Real mode only: Phemex already tracks today's realized PnL per
    position (curTermRealisedPnlRv) — sum it across our symbols rather than
    re-deriving it ourselves. Sim mode uses SimAccount.realized_pnl_today
    instead (see recent_closed_trades/render)."""
    total = 0.0
    for p in (acc_data or {}).get('data', {}).get('positions', []) or []:
        if p.get('symbol') in symbols:
            total += float(p.get('curTermRealisedPnlRv') or 0)
    return total

def account_position(acc_data, symbol, pos_side=None):
    for p in ((acc_data or {}).get('data', {}).get('positions', []) or []):
        if p.get('symbol') != symbol:
            continue
        if pos_side and p.get('posSide') != pos_side:
            continue
        if abs(float(p.get('size', 0) or 0)) > 0:
            return p
    return None

_contract_spec_cache = {}

async def fetch_contract_spec(symbol):
    if symbol in _contract_spec_cache:
        return _contract_spec_cache[symbol]
    qty_step = price_step = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{PHEMEX_BASE_URL}/public/products")
        rows = (r.json().get('data') or {})
        products = rows.get('perpProductsV2') or rows.get('products') or []
        for p in products:
            if p.get('symbol') == symbol:
                for key in ('qtyStepSize', 'lotSize', 'baseQtyStepSize', 'qtyStep'):
                    if p.get(key):
                        qty_step = float(p[key]); break
                for key in ('tickSize', 'priceStep'):
                    if p.get(key):
                        price_step = float(p[key]); break
                break
    except Exception:
        pass
    spec = (qty_step, price_step)
    _contract_spec_cache[symbol] = spec
    return spec

def _decimals_for_step(step):
    s = f"{step:.10f}".rstrip('0')
    return len(s.split('.')[1]) if '.' in s else 0

def floor_to_step(value, step):
    if step <= 0:
        return value
    steps = math.floor(value / step + 1e-9)
    return steps * step

async def place_entry(symbol, pos_side, order_type, qty_str, price_str=None):
    if DRY_RUN:
        return await get_sim_account().place_entry(symbol, pos_side, order_type, qty_str, price_str)
    side = 'Buy' if pos_side == 'Long' else 'Sell'
    params = {
        'symbol': symbol, 'clOrdID': f'athena_{int(time.time() * 1000)}',
        'side': side, 'posSide': pos_side, 'orderQtyRq': qty_str,
        'ordType': 'Market' if order_type == 'market' else 'Limit',
        'reduceOnly': 'false', 'timeInForce': 'GoodTillCancel',
    }
    if order_type == 'limit':
        params['priceRp'] = price_str
    return await phemex_request('PUT', '/g-orders/create', params=params)

async def place_sl(symbol, pos_side, sl_price, price_decimals):
    if DRY_RUN:
        return await get_sim_account().place_sl(symbol, pos_side, sl_price)
    close_side = 'Sell' if pos_side == 'Long' else 'Buy'
    params = {
        'symbol': symbol, 'clOrdID': f'athena_sl_{int(time.time() * 1000)}',
        'side': close_side, 'posSide': pos_side, 'orderQtyRq': '0',
        'ordType': 'Stop', 'stopPxRp': f'{sl_price:.{price_decimals}f}',
        'triggerType': 'ByMarkPrice', 'closeOnTrigger': 'true',
        'reduceOnly': 'true', 'timeInForce': 'GoodTillCancel',
    }
    return await phemex_request('PUT', '/g-orders/create', params=params)

async def place_tp_leg(symbol, pos_side, qty_str, price, price_decimals, suffix, target_type="?"):
    if DRY_RUN:
        return await get_sim_account().place_tp_leg(symbol, pos_side, qty_str, price, suffix, target_type)
    close_side = 'Sell' if pos_side == 'Long' else 'Buy'
    params = {
        'symbol': symbol, 'clOrdID': f'athena_tp{suffix}_{int(time.time() * 1000)}',
        'side': close_side, 'posSide': pos_side, 'orderQtyRq': qty_str,
        'ordType': 'Limit', 'priceRp': f'{price:.{price_decimals}f}',
        'reduceOnly': 'true', 'timeInForce': 'GoodTillCancel',
    }
    return await phemex_request('PUT', '/g-orders/create', params=params)

async def cancel_all(symbol):
    if DRY_RUN:
        return await get_sim_account().cancel_all(symbol)
    return await phemex_request('DELETE', '/g-orders/all', params={'symbol': symbol, 'untriggered': 'false'})

async def market_close(symbol, pos_side, qty, reason="flip"):
    # `reason` only matters for --dry-run's own sim_logs "closed" reason tag
    # (real mode's athena_logs event NAME already distinguishes pcvr_flip_close
    # vs. eod_flatten at the call site — see AthenaInstrument._manage_position/
    # _flatten_eod) — a real Phemex market-close order carries no such field.
    if DRY_RUN:
        return await get_sim_account().market_close(symbol, pos_side, qty, reason)
    close_side = 'Sell' if pos_side == 'Long' else 'Buy'
    params = {
        'symbol': symbol, 'clOrdID': f'athena_close_{int(time.time() * 1000)}',
        'side': close_side, 'posSide': pos_side, 'orderQtyRq': str(qty),
        'ordType': 'Market', 'reduceOnly': 'true', 'timeInForce': 'GoodTillCancel',
    }
    return await phemex_request('PUT', '/g-orders/create', params=params)

async def fetch_last_price(symbol):
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(f"{PHEMEX_BASE_URL}/md/v2/ticker/24hr", params={'symbol': symbol})
        data = r.json().get('result') or r.json().get('data') or {}
        # closeRp (this endpoint's real last-TRADED price) is preferred over
        # markPriceRp (an oracle/index-derived synthetic price) — CRITICAL
        # fix 2026-07-23: for QQQUSDT specifically, markPriceRp/indexPriceRp
        # were confirmed live to diverge from closeRp by $0.50-1.15+, and
        # this function feeds BOTH SimAccount.tick_matching()'s SL/TP
        # trigger checks (DRY_RUN) AND _check_confirmation's entry decisions
        # — using mark price there caused SL closes to fire late/at an
        # unrealistic price (nothing on the real tape ever traded there) and
        # TP legs to sit un-filled well after the real price had already
        # crossed them, since mark price was lagging on the wrong side of
        # the trigger. markPriceRp is now only a last-resort fallback for
        # when closeRp/lastRp are genuinely missing.
        last = data.get('closeRp') or data.get('lastRp') or data.get('markPriceRp')
        return float(last) if last is not None else None
    except Exception:
        return None

# ── Per-instrument state machine ──────────────────────────────────────────────
class AthenaInstrument:
    def __init__(self, asset):
        self.asset = asset
        self.cfg = ASSETS[asset]
        self.state = "WATCHING"
        self.lights = {"Session": False, "Volatility": False, "PCVR": False, "HPLs": False, "Targets": False, "Order Flow": False}
        self.regime = None
        self.price = None
        self.market_closed = False
        self._last_checked_bar_ts = None   # ts of the last bar we've already run a confirmation check against
        self.pending = None     # while PENDING_FILL
        self.position = None    # while IN_POSITION
        self.note = ""

    async def reconcile_startup(self):
        # CRITICAL: seed the "already evaluated" baseline with whatever bar
        # is CURRENTLY the latest closed one for this asset — otherwise, if
        # the 5-light gate already happens to be satisfied the moment
        # Athena starts (common in --no-session/24H mode), process_cycle's
        # WATCHING->ARMED transition deliberately falls through and
        # confirms in the SAME cycle (see that comment) against whatever
        # bar is currently last — which, on a fresh restart, could have
        # closed minutes ago, well before this run even started. Confirmed
        # live 2026-07-23: a QQQ position appeared within 2 seconds of
        # restart, entered off a bar that had already closed before Athena
        # started this run — not a live signal, a stale one. Seeding this
        # here means the first REAL confirmation after a restart requires a
        # genuinely NEW bar to close first, same as it would mid-session.
        try:
            _, last_bar = read_last_two_footprint_bars(self.asset)
            if last_bar is not None:
                self._last_checked_bar_ts = last_bar.get("ts")
        except Exception:
            pass

        try:
            acc = await fetch_account()
        except Exception as e:
            console_log(f"{self.asset}: startup reconciliation failed ({e}) — assuming flat")
            return
        pos = account_position(acc, self.cfg["phemex_symbol"])
        if pos:
            pos_side = pos.get("posSide", "Long")
            size = abs(float(pos.get("size") or 0))
            fill_price = float(pos.get("avgEntryPriceRp") or 0)
            qty_step, price_step = await fetch_contract_spec(self.cfg["phemex_symbol"])
            qd = _decimals_for_step(qty_step or DEFAULT_QTY_STEP[self.asset])
            pd = _decimals_for_step(price_step or DEFAULT_PRICE_STEP[self.asset])

            # Restore the resting SL/TP too, not just the bare position —
            # previously this only restored pos_side/qty/fill_price, so a
            # restart silently lost the SL/TP display (dashboard showed
            # "SL n/a") even though the real resting orders were still
            # there protecting the position the whole time. DRY_RUN has
            # authoritative order data (SimAccount.orders); real mode has
            # no "list my resting orders" call wired up here, so it stays
            # an honest gap (sl_price/tp_legs empty) rather than a guess —
            # same documented limitation as elsewhere.
            sl_price, tp_legs = None, []
            if DRY_RUN:
                sim_orders = get_sim_account().orders
                symbol = self.cfg["phemex_symbol"]
                for o in sim_orders.values():
                    if o["symbol"] != symbol or o.get("posSide") != pos_side:
                        continue
                    if o["kind"] == "sl":
                        sl_price = o["stopPx"]
                    elif o["kind"] == "tp":
                        # Which target (BT/ST/GEX Flip/Cluster) this leg
                        # came from is now persisted on the sim order itself
                        # (place_tp_leg's target_type param) — falls back to
                        # "?" only for orders placed before that field
                        # existed (a pre-upgrade sim_account.json).
                        leg_type = o.get("type", "?")
                        tp_legs.append({"level": o["price"], "qty": o["qty"],
                                        "tracks_gex_flip": leg_type == "GEX Flip", "type": leg_type})
                tp_legs.sort(key=lambda leg: leg["level"], reverse=(pos_side == "Short"))

            self.position = {"pos_side": pos_side, "qty": size, "fill_price": fill_price,
                              "sl_price": sl_price, "tp_legs": tp_legs,
                              "price_decimals": pd, "qty_decimals": qd}
            self.state = "IN_POSITION"
            self.lights["Order Flow"] = True
            console_log(f"{self.asset}: found existing {pos_side} position ({size}) on restart — resuming IN_POSITION"
                        + ("" if DRY_RUN else f" {DIM}(SL/TP display unavailable after restart in real mode){RST}"))
            log_event(self.asset, "reconciled_existing_position", {"pos_side": pos_side, "qty": size,
                                                                     "fill_price": fill_price, "sl_price": sl_price,
                                                                     "tp_legs": tp_legs})

    async def process_cycle(self, snapshot):
        lights5, regime, price, targets_full, market_closed = instrument_lights(snapshot, self.asset)
        self.lights.update(lights5)
        self.regime = regime
        self.price = price
        self.market_closed = market_closed

        # QQQ-only: status.py's own qqq.market_closed already zeroes
        # HPLs/Targets outside 08:45-15:00 CT (weekdays), so the 5-light
        # gate already blocks new entries — this just cleans up anything
        # already resting/open the moment the window closes, regardless of
        # PCVR. Reuses status.py's own boundary/weekend logic rather than
        # re-deriving a CT-hour check here.
        if (self.asset == "QQQ" and snapshot and (snapshot.get(self.cfg["snap_key"]) or {}).get("market_closed")
                and self.state in ("PENDING_FILL", "IN_POSITION")):
            await self._flatten_now("eod")
            return

        required = required_status_lights()
        gate_ok = all(lights5[n] for n in required)

        if self.state == "WATCHING":
            self.lights["Order Flow"] = False
            if not gate_ok or not ATHENA_ENABLED:
                return
            self.state = "ARMED"
            console_log(f"{self.asset}: all required conditions active — ARMED, watching order flow ({regime})"
                        + (" [24H MODE]" if NO_SESSION else ""))
            log_event(self.asset, "armed", {"regime": regime, "no_session": NO_SESSION})
            # Fall through into the ARMED check below in this same cycle —
            # otherwise a bar that already closed (confirming or not) right
            # as/just before arming would get silently treated as the
            # "seen" baseline next cycle without ever itself being checked
            # against its predecessor (see read_last_two_footprint_bars).

        if self.state == "ARMED":
            if not gate_ok or not ATHENA_ENABLED:
                self.state = "WATCHING"
                self.lights["Order Flow"] = False
                reason = "Athena paused ([A])" if not ATHENA_ENABLED else "a required condition dropped"
                console_log(f"{self.asset}: {reason} — back to WATCHING")
                log_event(self.asset, "disarmed", {"lights": lights5, "no_session": NO_SESSION,
                                                     "athena_enabled": ATHENA_ENABLED})
                return
            prev_bar, bar = read_last_two_footprint_bars(self.asset)
            is_fresh_pair = (bar is not None and prev_bar is not None
                             and bar.get("ts") != self._last_checked_bar_ts)
            if is_fresh_pair and regime in ("long", "short"):
                self._last_checked_bar_ts = bar.get("ts")
                await self._check_confirmation(prev_bar, bar, regime, price, targets_full)
            return

        if self.state == "PENDING_FILL":
            await self._check_fill()
            return

        if self.state == "IN_POSITION":
            await self._manage_position(regime)
            return

    async def _check_confirmation(self, prev_bar, new_bar, regime, live_price_fallback, targets_full):
        confirmed, vah, val = footprint_confirmation(prev_bar, new_bar, regime)
        if not confirmed:
            return

        entry_price = vah if regime == "long" else val
        if entry_price is None:
            log_event(self.asset, "confirmation_no_entry_price", {"regime": regime})
            return

        nearest_target = targets_full[0]["level"] if targets_full else None
        R = self.cfg["sl"]
        live_price = await reference_price(self.asset, self.cfg["phemex_symbol"])
        if live_price is None:
            live_price = new_bar.get("c", live_price_fallback)

        if nearest_target is None or abs(nearest_target - live_price) < R:
            dist = None if nearest_target is None else abs(nearest_target - live_price)
            dist_txt = "n/a" if dist is None else f"{dist:.2f}"
            console_log(f"{self.asset}: order flow confirmed {regime.upper()} but rejected — "
                        f"distance to nearest target ({dist_txt}) < {R}R")
            log_event(self.asset, "confirmation_rejected_viability", {"regime": regime, "nearest_target": nearest_target,
                                                                        "distance": dist, "R": R})
            return

        order_type = "market" if ((regime == "long" and live_price <= entry_price) or
                                   (regime == "short" and live_price >= entry_price)) else "limit"

        try:
            acc = await fetch_account()
            balance = account_available_balance(acc)
        except Exception as e:
            console_log(f"{self.asset}: balance fetch failed ({e}) — skipping entry")
            log_event(self.asset, "entry_skipped_balance_error", {"error": str(e)})
            return

        qty_step, price_step = await fetch_contract_spec(self.cfg["phemex_symbol"])
        qty_step = qty_step or DEFAULT_QTY_STEP[self.asset]
        price_step = price_step or DEFAULT_PRICE_STEP[self.asset]
        price_decimals = _decimals_for_step(price_step)

        # Per spec: size = trade_risk / sl_distance, where trade_risk =
        # accountBalance * riskPercentage and sl_distance is THIS asset's
        # own fixed SL distance (R, $10 ETH / $0.75 QQQ — self.cfg["sl"],
        # already fetched above). NOT a hardcoded /10 — that was ETH-
        # specific (ETH's own SL distance happens to be $10, which is why
        # a flat /10 looked correct there) and silently UNDERSIZED every
        # QQQ trade by ~13x (dividing by 10 instead of 0.75 means the
        # actual dollar risk realized on a QQQ stop-out was only qty*0.75
        # = ~7.5% of the intended trade_risk, not the full risk budget).
        # Not a price-based notional/price conversion either; this is the
        # sizing rule itself, independent of the asset's current price.
        raw_qty = (balance * (PCT / 100.0)) / R
        qty = floor_to_step(raw_qty, qty_step)
        if qty <= 0:
            console_log(f"{self.asset}: sized qty is 0 (balance ${balance:.2f}, pct {PCT}%) — skipping entry")
            log_event(self.asset, "entry_skipped_zero_size", {"balance": balance, "pct": PCT})
            return

        qty_decimals = _decimals_for_step(qty_step)
        pos_side = "Long" if regime == "long" else "Short"
        qty_str = f"{qty:.{qty_decimals}f}"
        price_str = f"{entry_price:.{price_decimals}f}" if order_type == "limit" else None

        detail = {"regime": regime, "order_type": order_type, "pos_side": pos_side, "qty": qty_str,
                   "price": price_str or f"market (~{live_price})", "targets": targets_full, "sl_distance": R}

        # DRY_RUN and real trading share this exact same path from here on —
        # place_entry()/place_sl()/place_tp_leg()/cancel_all()/market_close()
        # each dispatch internally to SimAccount when DRY_RUN, so the state
        # machine (PENDING_FILL -> IN_POSITION -> closed, SL/TP, PCVR-flip
        # close) runs identically either way, against a paper ledger instead
        # of real money.
        result = await place_entry(self.cfg["phemex_symbol"], pos_side, order_type, qty_str, price_str)
        if not (isinstance(result, dict) and result.get("code") == 0):
            console_log(f"{self.asset}: entry order FAILED — {result}")
            log_event(self.asset, "entry_order_failed", {"result": result, **detail})
            return

        self.pending = {"pos_side": pos_side, "qty": qty, "qty_decimals": qty_decimals,
                         "price_decimals": price_decimals, "targets": targets_full, "regime": regime,
                         "order_type": order_type, "entry_price": entry_price if order_type == "limit" else live_price}
        self.state = "PENDING_FILL"
        self.lights["Order Flow"] = True
        tag = " (sim)" if DRY_RUN else ""
        console_log(f"{self.asset}: entry order placed{tag} — {pos_side} {order_type} {qty_str} @ "
                    f"{price_str or 'market'}")
        log_event(self.asset, "entry_order_placed", {"result": result, "sim": DRY_RUN, **detail})

    async def _check_fill(self):
        p = self.pending
        try:
            acc = await fetch_account()
        except Exception as e:
            console_log(f"{self.asset}: fill check failed ({e})")
            return
        pos = account_position(acc, self.cfg["phemex_symbol"], p["pos_side"])
        if not pos:
            return
        fill_price = float(pos.get("avgEntryPriceRp") or 0)
        qty = abs(float(pos.get("size") or p["qty"]))
        if fill_price <= 0:
            return

        R = self.cfg["sl"]
        sl_price = fill_price - R if p["pos_side"] == "Long" else fill_price + R
        symbol = self.cfg["phemex_symbol"]
        await place_sl(symbol, p["pos_side"], sl_price, p["price_decimals"])

        targets = p["targets"]
        qd = p["qty_decimals"]
        step = 10 ** (-qd) if qd else 1.0
        total_steps = round(qty / step)

        def _tp_level(target):
            # GEX Flip is a moving target (status.py/gex.py recompute it
            # every cycle) — per spec, a TP tracking it never sits exactly
            # AT the flip level, always $3 on the near side (below for a
            # long, above for a short). _manage_position keeps this synced
            # for as long as the position stays open.
            if target["type"] == "GEX Flip":
                return target["level"] - GEX_FLIP_TP_BUFFER if p["pos_side"] == "Long" else target["level"] + GEX_FLIP_TP_BUFFER
            return target["level"]

        def _valid_tp(level):
            # A TP must sit at least R past the ACTUAL fill price on the
            # profit side — without this, a target that's genuinely close
            # to price (or a GEX-Flip target the $3 buffer pushes past
            # price when GEX Flip itself is within $3 of the fill) becomes
            # instantly triggerable the moment price ticks at all, closing
            # the position within seconds at (near-)break-even. Confirmed
            # live 2026-07-23: GEX Flip $1922.10, -$3 buffer -> $1919.10,
            # BELOW a $1920.83 long fill — that TP leg filled in 6 seconds.
            return level >= fill_price + R if p["pos_side"] == "Long" else level <= fill_price - R

        candidates = [(_tp_level(t), t) for t in targets]
        valid = [(lvl, t) for lvl, t in candidates if _valid_tp(lvl)]
        dropped = [t for lvl, t in candidates if not _valid_tp(lvl)]
        if dropped:
            console_log(f"{self.asset}: {YLW}{len(dropped)} target(s) within ${R} of fill ${fill_price:.2f} — dropped from TP{RST}")
            log_event(self.asset, "tp_target_dropped_too_close", {"fill_price": fill_price, "R": R, "dropped": dropped})

        if len(valid) >= 2 and total_steps >= 2:
            tp1_steps = total_steps // 2
            tp2_steps = total_steps - tp1_steps
            tp1_qty = tp1_steps * step
            tp2_qty = tp2_steps * step
            (tp1_level, tp1_t), (tp2_level, tp2_t) = valid[0], valid[1]
            await place_tp_leg(symbol, p["pos_side"], f"{tp1_qty:.{qd}f}", tp1_level, p["price_decimals"], "1", tp1_t["type"])
            await place_tp_leg(symbol, p["pos_side"], f"{tp2_qty:.{qd}f}", tp2_level, p["price_decimals"], "2", tp2_t["type"])
            tp_legs = [{"level": tp1_level, "qty": tp1_qty, "tracks_gex_flip": tp1_t["type"] == "GEX Flip", "type": tp1_t["type"]},
                       {"level": tp2_level, "qty": tp2_qty, "tracks_gex_flip": tp2_t["type"] == "GEX Flip", "type": tp2_t["type"]}]
        elif valid:
            tp1_level, tp1_t = valid[0]
            await place_tp_leg(symbol, p["pos_side"], f"{qty:.{qd}f}", tp1_level, p["price_decimals"], "1", tp1_t["type"])
            tp_legs = [{"level": tp1_level, "qty": qty, "tracks_gex_flip": tp1_t["type"] == "GEX Flip", "type": tp1_t["type"]}]
        else:
            tp_legs = []
            if targets:
                console_log(f"{self.asset}: {YLW}no valid TP target — position has SL only{RST}")
        tp_desc = [leg["level"] for leg in tp_legs]

        self.position = {"pos_side": p["pos_side"], "qty": qty, "fill_price": fill_price,
                          "sl_price": sl_price, "tp_legs": tp_legs,
                          "price_decimals": p["price_decimals"], "qty_decimals": qd}
        self.pending = None
        self.state = "IN_POSITION"
        tag = " (sim)" if DRY_RUN else ""
        console_log(f"{self.asset}: filled{tag} @ {fill_price} — SL {sl_price}, TP {tp_desc}")
        log_event(self.asset, "filled", {"fill_price": fill_price, "qty": qty, "sl": sl_price, "tp": tp_desc, "sim": DRY_RUN})

    async def _manage_position(self, regime):
        symbol = self.cfg["phemex_symbol"]
        pos_side = self.position["pos_side"]
        try:
            acc = await fetch_account()
        except Exception as e:
            console_log(f"{self.asset}: position check failed ({e})")
            return
        pos = account_position(acc, symbol, pos_side)
        if not pos:
            console_log(f"{self.asset}: position flat — cleaning up resting orders")
            try:
                await cancel_all(symbol)
            except Exception:
                pass
            # Athena doesn't query the exchange's own fill/close records, so
            # for a REAL close this exit price (and therefore PnL) is only
            # an approximation off the current live price, not the actual
            # fill — labeled accordingly. A DRY_RUN close already has an
            # exact, authoritative entry in sim_logs (SimAccount._close),
            # this is just a matching note in athena_logs for one combined
            # blotter view.
            exit_price = await fetch_last_price(symbol)
            entry = self.position.get("fill_price")
            qty = self.position.get("qty")
            pnl_approx = None
            if exit_price is not None and entry:
                pnl_approx = (exit_price - entry) * qty if pos_side == "Long" else (entry - exit_price) * qty
            log_event(self.asset, "position_closed", {"pos_side": pos_side, "qty": qty, "entry": entry,
                                                        "exit_approx": exit_price, "pnl_approx": pnl_approx,
                                                        "sim": DRY_RUN})
            self.position = None
            self.state = "WATCHING"
            self.lights["Order Flow"] = False
            return

        # A partial TP fill reduces the exchange's own position without it
        # going fully flat — keep the displayed size (and, in --dry-run
        # where we have authoritative order data, the resting TP legs) in
        # sync, or the dashboard/open-PnL calc would keep showing the
        # original full size and both TP legs forever. Real mode has no
        # "list my resting orders" call wired up here, so its tp_legs
        # display can go stale after a partial fill even though the size
        # itself is now correct — a known, minor display-only gap.
        self.position["qty"] = abs(float(pos.get("size") or 0))
        if DRY_RUN:
            sim_orders = get_sim_account().orders
            old_legs = {round(leg["level"], 2): (leg.get("tracks_gex_flip", False), leg.get("type", "?"))
                        for leg in (self.position.get("tp_legs") or [])}
            self.position["tp_legs"] = [
                {"level": o["price"], "qty": o["qty"],
                 "tracks_gex_flip": old_legs.get(round(o["price"], 2), (False, "?"))[0],
                 "type": old_legs.get(round(o["price"], 2), (False, "?"))[1]}
                for o in sim_orders.values() if o["symbol"] == symbol and o["kind"] == "tp"]

        flipped = (pos_side == "Long" and regime == "short") or (pos_side == "Short" and regime == "long")
        if flipped:
            qty = abs(float(pos.get("size") or 0))
            entry = self.position.get("fill_price")
            console_log(f"{self.asset}: {RED}PCVR flipped against open {pos_side} position — emergency close{RST}")
            try:
                await cancel_all(symbol)
            except Exception:
                pass
            try:
                await market_close(symbol, pos_side, qty, reason="flip")
            except Exception as e:
                console_log(f"{self.asset}: emergency close FAILED — {e}")
                log_event(self.asset, "pcvr_flip_close_failed", {"error": str(e)})
                return
            exit_price = await fetch_last_price(symbol)   # approximate, same caveat as position_closed
            pnl_approx = None
            if exit_price is not None and entry:
                pnl_approx = (exit_price - entry) * qty if pos_side == "Long" else (entry - exit_price) * qty
            log_event(self.asset, "pcvr_flip_close", {"pos_side": pos_side, "qty": qty, "entry": entry,
                                                        "exit_approx": exit_price, "pnl_approx": pnl_approx,
                                                        "sim": DRY_RUN})
            self.position = None
            self.state = "WATCHING"
            self.lights["Order Flow"] = False
            return

        await self._sync_gex_flip_tp(symbol, pos_side)

    async def _sync_gex_flip_tp(self, symbol, pos_side):
        """GEX Flip is recomputed every status.py/gex.py cycle, not fixed at
        entry — any TP leg tracking it needs its resting order price kept
        $3 on the near side of whatever GEX Flip currently is, for as long
        as the position stays open. Full bracket refresh (cancel
        everything, re-place SL + all TP legs at current prices) rather
        than a selective single-order amend/cancel — Phemex's real API for
        editing just one resting order isn't verified/wired up here, and
        this is simpler and doesn't risk touching the wrong order; SL's own
        price is unchanged so re-placing it is a harmless no-op. Trade-off,
        worth knowing: the position briefly has no resting SL between the
        cancel and re-place calls."""
        tp_legs = self.position.get("tp_legs") or []
        if not any(leg.get("tracks_gex_flip") for leg in tp_legs):
            return
        gex_export = read_gex_export(self.asset)
        gex_flip = gex_export.get("gex_flip") if gex_export else None
        if gex_flip is None:
            return
        new_level = gex_flip - GEX_FLIP_TP_BUFFER if pos_side == "Long" else gex_flip + GEX_FLIP_TP_BUFFER
        stale = any(leg.get("tracks_gex_flip") and abs(leg["level"] - new_level) > 0.005 for leg in tp_legs)
        if not stale:
            return

        # Same guard as the initial placement (_check_fill) — if GEX Flip
        # has drifted close enough to the fill price that the buffered
        # level would land within R of it (or past it), refreshing would
        # create the exact instant-trigger bug this buffer exists to
        # avoid. Skip this cycle's refresh (leave the currently-resting
        # order as-is) and retry once GEX Flip moves back to a safe
        # distance, rather than ever placing an unsafe level.
        fill_price = self.position.get("fill_price")
        R = self.cfg["sl"]
        safe = (new_level >= fill_price + R) if pos_side == "Long" else (new_level <= fill_price - R)
        if fill_price is not None and not safe:
            console_log(f"{self.asset}: {YLW}GEX Flip too close to fill (${fmt_num(new_level)}) — TP refresh skipped this cycle{RST}")
            return

        pd = self.position.get("price_decimals", 2)
        qd = self.position.get("qty_decimals", 2)
        new_legs = [{"level": (new_level if leg.get("tracks_gex_flip") else leg["level"]),
                     "qty": leg["qty"], "tracks_gex_flip": leg.get("tracks_gex_flip", False),
                     "type": leg.get("type", "?")}
                    for leg in tp_legs]
        try:
            await cancel_all(symbol)
        except Exception as e:
            console_log(f"{self.asset}: GEX Flip TP refresh — cancel failed ({e})")
            return
        sl_price = self.position.get("sl_price")
        if sl_price is not None:
            await place_sl(symbol, pos_side, sl_price, pd)
        for i, leg in enumerate(new_legs, start=1):
            await place_tp_leg(symbol, pos_side, f"{leg['qty']:.{qd}f}", leg["level"], pd, str(i))
        self.position["tp_legs"] = new_legs
        console_log(f"{self.asset}: {YLW}GEX Flip moved — TP refreshed to {fmt_num(new_level)}{RST}")
        log_event(self.asset, "tp_gex_flip_adjusted", {"new_level": new_level, "legs": new_legs})

    async def _flatten_now(self, reason):
        """Force-flat regardless of state/regime — cancels any resting
        entry/SL/TP order and market-closes an open position. Two callers:
        `reason="eod"` (QQQ-only, 15:00 CT regular close, unchanged
        behavior) and `reason="manual"` ([F]latten All, any asset/state,
        triggered by the user). Mirrors the PCVR-flip-close branch of
        _manage_position above (same approximate-exit/PnL caveat for real
        trades — see that method's own comment). Event names stay distinct
        per reason ("eod_flatten"/"manual_flatten") — same one-event-name-
        per-cause convention "pcvr_flip_close" already established, so the
        trades table/blotter/chart markers can tell them apart."""
        symbol = self.cfg["phemex_symbol"]
        label = "15:00 CT — flattening for EOD" if reason == "eod" else "manual Flatten All"
        console_log(f"{self.asset}: {YLW}{label}{RST}")
        event_name = f"{reason}_flatten"
        try:
            await cancel_all(symbol)
        except Exception:
            pass

        if self.state == "IN_POSITION" and self.position:
            pos_side = self.position["pos_side"]
            qty = self.position.get("qty")
            entry = self.position.get("fill_price")
            try:
                acc = await fetch_account()
                pos = account_position(acc, symbol, pos_side)
                if pos:
                    qty = abs(float(pos.get("size") or 0))
            except Exception:
                pass
            if qty:
                try:
                    await market_close(symbol, pos_side, qty, reason=reason)
                except Exception as e:
                    console_log(f"{self.asset}: {label} close FAILED — {e}")
                    log_event(self.asset, f"{event_name}_failed", {"error": str(e)})
                    return
            exit_price = await fetch_last_price(symbol)   # approximate, same caveat as position_closed
            pnl_approx = None
            if exit_price is not None and entry and qty:
                pnl_approx = (exit_price - entry) * qty if pos_side == "Long" else (entry - exit_price) * qty
            log_event(self.asset, event_name, {"pos_side": pos_side, "qty": qty, "entry": entry,
                                                "exit_approx": exit_price, "pnl_approx": pnl_approx,
                                                "sim": DRY_RUN})
        else:
            log_event(self.asset, event_name, {"note": "cancelled pending/resting order, no open position"})

        self.pending = None
        self.position = None
        self.state = "WATCHING"
        self.lights["Order Flow"] = False

# ── Dashboard data helpers ─────────────────────────────────────────────────────
# Full canonical set, always shown in the detail line (Session included for
# information even in --no-session mode). The bar/count only reflect what's
# actually required to arm — see gated_light_names().
LIGHT_ORDER = ["Session", "Volatility", "PCVR", "HPLs", "Targets", "Order Flow"]
BOX_W = 78

def gated_light_names():
    return required_status_lights() + ["Order Flow"]

def fmt_num(x, decimals=2, default="n/a"):
    try:
        return f"{float(x):.{decimals}f}"
    except (TypeError, ValueError):
        return default

def fmt_money(x, default="n/a"):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return default
    return f"{'+' if x >= 0 else ''}${x:,.2f}"

def pnl_color(x):
    try:
        return GRN if float(x) >= 0 else RED
    except (TypeError, ValueError):
        return DIM

def pnl_pair(x):
    """Same green/red-by-sign convention as pnl_color(), but returns a
    curses color-PAIR int (P_GREEN/P_RED/P_DIM) for plain db.puts() calls —
    pnl_color()'s ANSI-string return only works with puts_ansi()/f-strings;
    passing it straight to puts()'s `pair` arg gets stored verbatim into
    the DoubleBuffer cell and blows up in flush() the first time it tries
    curses.color_pair() on a string ("cannot be interpreted as an
    integer") — confirmed live, see draw_trades_table's own history."""
    try:
        return P_GREEN if float(x) >= 0 else P_RED
    except (TypeError, ValueError):
        return P_DIM

def instrument_open_pnl(inst, live_price):
    if not inst.position or live_price is None:
        return None
    p = inst.position
    try:
        if p["pos_side"] == "Long":
            return (live_price - p["fill_price"]) * p["qty"]
        return (p["fill_price"] - live_price) * p["qty"]
    except (TypeError, KeyError):
        return None

def recent_closed_trades(n=5):
    """Last n closed-trade rows for the blotter — from sim_logs when
    DRY_RUN (exact, authoritative PnL straight from SimAccount), or from
    today's athena_logs 'position_closed'/'pcvr_flip_close'/'eod_flatten'
    events otherwise (approximate exit/PnL — see _manage_position's own
    caveat)."""
    rows = []
    path = sim_log_path() if DRY_RUN else athena_log_path()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if DRY_RUN and d.get("event") == "closed":
                    rows.append(d)
                elif not DRY_RUN and d.get("event") in ("position_closed", "pcvr_flip_close", "eod_flatten", "manual_flatten"):
                    rows.append(d)
    except Exception:
        pass
    return rows[-n:]

def recent_trade_pairs(n=6):
    """Like recent_closed_trades, but paired with each trade's own ENTRY
    (timestamp + price), not just its exit — needed for the chart's
    entry+exit arrow markers (build_trade_markers), which the blotter
    itself doesn't need since it only ever shows the exit side. Correlates
    each close event with the most recent preceding 'filled' event for the
    same symbol/pos_side (sim) or asset (real) — logs are append-only and
    strictly time-ordered, so a straightforward most-recent-fill-before-
    this-close scan is exact, not a heuristic."""
    path = sim_log_path() if DRY_RUN else athena_log_path()
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception:
        return []

    pairs = []
    last_fill = {}
    if DRY_RUN:
        for d in rows:
            ev = d.get("event")
            if ev == "filled":
                last_fill[(d.get("symbol"), d.get("pos_side"))] = (d.get("ts"), d.get("price"))
            elif ev == "closed":
                fill = last_fill.get((d.get("symbol"), d.get("pos_side")))
                pairs.append({"symbol": d.get("symbol"), "pos_side": d.get("pos_side"),
                               "entry_ts": fill[0] if fill else None,
                               "entry_price": fill[1] if fill else d.get("entry"),
                               "exit_ts": d.get("ts"), "exit_price": d.get("price"),
                               "reason": d.get("reason", "close")})
    else:
        reason_by_event = {"pcvr_flip_close": "flip", "eod_flatten": "eod", "manual_flatten": "manual",
                           "position_closed": "close"}
        for d in rows:
            ev, asset = d.get("event"), d.get("asset")
            if ev == "filled":
                det = d.get("detail") or {}
                last_fill[asset] = (d.get("ts"), det.get("fill_price"))
            elif ev in ("position_closed", "pcvr_flip_close", "eod_flatten", "manual_flatten"):
                det = d.get("detail") or {}
                fill = last_fill.get(asset)
                pairs.append({"asset": asset, "pos_side": det.get("pos_side"),
                               "entry_ts": fill[0] if fill else None,
                               "entry_price": fill[1] if fill else det.get("entry"),
                               "exit_ts": d.get("ts"), "exit_price": det.get("exit_approx"),
                               "reason": reason_by_event.get(ev, "close")})
    return pairs[-n:]

# ── [D] Data view — all-time trading statistics + PnL equity curve ───────────
def scan_all_trade_events(source):
    """Every closed-trade PnL value across ALL daily log files (not just
    today) — source 'sim' reads sim_logs (exact PnL, straight from
    SimAccount), 'real' reads athena_logs (approximate PnL — same caveat as
    the Recent Closed Trades blotter, Athena has no authoritative
    fill/close price source wired up for real trades). Sorted oldest-first
    for the equity curve."""
    base = SIM_LOG_DIR_BASE if source == "sim" else ATHENA_LOG_DIR_BASE
    pattern = os.path.join(base, "**", "*.jsonl")
    events = []
    for path in glob.glob(pattern, recursive=True):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    if source == "sim":
                        if d.get("event") == "closed" and d.get("pnl") is not None:
                            events.append({"ts": d.get("ts"), "pnl": d.get("pnl")})
                    elif d.get("event") in ("position_closed", "pcvr_flip_close", "eod_flatten", "manual_flatten"):
                        pnl = (d.get("detail") or {}).get("pnl_approx")
                        if pnl is not None:
                            events.append({"ts": d.get("ts"), "pnl": pnl})
        except Exception:
            continue
    events.sort(key=lambda e: e.get("ts") or "")
    return events

PHEMEX_TAKER_FEE = 0.0006   # market orders — SL/PCVR-flip/EOD-flatten closes
PHEMEX_MAKER_FEE = 0.0001   # limit orders — TP leg fills
PHEMEX_SYMBOL_TO_ASSET = {cfg["phemex_symbol"]: a for a, cfg in ASSETS.items()}

def scan_all_trades_detailed():
    """Full per-trade records (entry+every closing leg+fees+duration+R:R),
    reconstructed from ALL sim_logs across every day — DRY_RUN only. Real
    mode's athena_logs has no order_type or per-leg granularity recorded
    (see _manage_position's own documented gap: no "list my resting
    orders" Phemex call wired up), so a real-mode version of this would be
    almost entirely blank/approximate — not built.

    Correlation: a 'filled' event starts a new trade for its (symbol,
    pos_side); every subsequent 'closed' event for that same key belongs to
    it until either the filled qty is fully accounted for, or a NEW
    'filled' for the same key appears (defensive fallback — Athena only
    ever holds one position per instrument at a time, so this ordering is
    exact in practice, not a heuristic). A trade still open at the end of
    the log (no matching close yet) is dropped — this table is CLOSED
    trades only, same scope as the equity curve above.

    TP1 vs TP2 leg attribution: sim_logs' 'closed' event records price/
    qty/reason but not which TP leg (1 or 2) it was — attributed by
    chronological order among reason=='tp' exits for the same trade (first
    tp-reason close = TP1, second = TP2), which matches the framework's own
    expectation that TP1 (the nearer target) fills before TP2 in a
    well-formed trade. A reasonable, documented heuristic, not guaranteed
    exact if fills raced.

    Fees: Phemex's own published taker/maker rates (PHEMEX_TAKER_FEE/
    PHEMEX_MAKER_FEE), applied per-leg on notional (qty * price) — the
    entry leg's rate depends on its recorded order_type (market/limit);
    each exit leg's rate is maker for a 'tp' reason (limit fill) and taker
    for 'sl'/'flip'/'eod' (stop/market close)."""
    pattern = os.path.join(SIM_LOG_DIR_BASE, "**", "*.jsonl")
    rows = []
    for path in glob.glob(pattern, recursive=True):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except Exception:
            continue
    rows.sort(key=lambda d: d.get("ts") or "")

    open_trades = {}
    closed_trades = []
    for d in rows:
        ev = d.get("event")
        key = (d.get("symbol"), d.get("pos_side"))
        if ev == "filled":
            if key in open_trades:
                closed_trades.append(open_trades.pop(key))   # defensive — see docstring
            open_trades[key] = {
                "symbol": d.get("symbol"), "pos_side": d.get("pos_side"),
                "entry_ts": d.get("ts"), "entry_price": d.get("price"),
                "qty": d.get("qty"), "entry_order_type": d.get("order_type"),
                "exits": [],
            }
        elif ev == "closed" and key in open_trades:
            t = open_trades[key]
            t["exits"].append({"ts": d.get("ts"), "price": d.get("price"), "qty": d.get("qty"),
                                "reason": d.get("reason"), "pnl": d.get("pnl"), "balance": d.get("balance")})
            if sum(x["qty"] for x in t["exits"]) >= t["qty"] - 1e-9:
                closed_trades.append(open_trades.pop(key))

    out = []
    for t in closed_trades:
        exits = t["exits"]
        if not exits:
            continue
        entry_qty, entry_price = t["qty"], t["entry_price"]
        entry_fee_rate = PHEMEX_MAKER_FEE if t.get("entry_order_type") == "limit" else PHEMEX_TAKER_FEE
        entry_fee = entry_qty * entry_price * entry_fee_rate
        exit_qty_total = sum(x["qty"] for x in exits)
        exit_price_avg = sum(x["price"] * x["qty"] for x in exits) / exit_qty_total if exit_qty_total else None
        gross_pnl = sum(x["pnl"] for x in exits)
        exit_fees = sum(x["qty"] * x["price"] * (PHEMEX_MAKER_FEE if x["reason"] == "tp" else PHEMEX_TAKER_FEE)
                         for x in exits)
        total_fees = entry_fee + exit_fees
        net_pnl = gross_pnl - total_fees
        # trade_risk = qty * sl_distance (THIS asset's own fixed SL
        # distance — $10 ETH / $0.75 QQQ), not a flat *10 — that was
        # ETH-specific and would understate QQQ's real R by ~13x, same
        # underlying mixup _check_confirmation's entry sizing itself had.
        asset = PHEMEX_SYMBOL_TO_ASSET.get(t["symbol"])
        sl_distance = ASSETS.get(asset, {}).get("sl", 10.0)
        r_dollars = entry_qty * sl_distance
        rr = (net_pnl / r_dollars) if r_dollars else None
        reasons = []
        for x in exits:
            r = (x.get("reason") or "close").upper()
            if r not in reasons:
                reasons.append(r)
        tp_exits = [x for x in exits if x.get("reason") == "tp"]
        out.append({
            "symbol": t["symbol"], "asset": asset or t["symbol"],
            "pos_side": t["pos_side"], "qty": entry_qty,
            "entry_ts": t["entry_ts"], "entry_price": entry_price,
            "exit_ts": exits[-1]["ts"], "exit_price": exit_price_avg,
            "reason": "+".join(reasons),
            "tp1": tp_exits[0] if len(tp_exits) >= 1 else None,
            "tp2": tp_exits[1] if len(tp_exits) >= 2 else None,
            "r_dollars": r_dollars, "fees": total_fees,
            "gross_pnl": gross_pnl, "net_pnl": net_pnl, "rr": rr,
            "balance": exits[-1].get("balance"),
        })
    out.sort(key=lambda t: t["exit_ts"] or "")
    return out

def compute_trade_stats(events):
    pnls = [e["pnl"] for e in events]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "count": n, "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / n * 100.0) if n else 0.0,
        "total_pnl": sum(pnls),
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "largest_win": max(wins) if wins else 0.0,
        "largest_loss": min(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win <= 0 else float("inf")),
    }

def _draw_equity_curve(db, y0, y1, cols, events):
    """The 'Cumulative PnL' line chart, bounded to rows [y0, y1) — split
    out of draw_data_view so it can share the screen with the trades table
    (top half chart / bottom half table) instead of taking the whole
    remaining screen, per explicit user request."""
    db.puts(y0, 0, "── Cumulative PnL ".ljust(min(cols, BOX_W), "─")[:cols], P_DIM)
    chart_top, chart_bottom = y0 + 1, y1 - 1
    chart_h = max(3, chart_bottom - chart_top)
    if not events:
        db.puts(y0 + 1, 0, "no closed trades yet", P_DIM)
        return

    cum, running = [], 0.0
    for e in events:
        running += e["pnl"]
        cum.append(running)
    lo, hi = min(cum + [0.0]), max(cum + [0.0])
    span = max(1e-9, hi - lo)
    axis_w = 12
    plot_w = max(1, cols - axis_w - 1)
    n = len(cum)
    # Downsample only when there are literally more trades than columns to
    # put them in — still keep the LAST point (current balance) even if
    # striding would otherwise have dropped it.
    step = max(1, -(-n // (plot_w + 1)))
    sampled = cum[::step]
    if sampled[-1] != cum[-1]:
        sampled.append(cum[-1])

    def _row_for(val):
        r = chart_top + int((hi - val) / span * (chart_h - 1))
        return max(chart_top, min(chart_bottom, r))

    # Evenly-spaced gridlines + $ labels across the ACTUAL value range
    # (previously only hi/lo/zero got a label, with nothing in between) —
    # per explicit user request to scale intervals evenly in proportion to
    # the data. Tick count adapts to the available height so labels never
    # collide on a short chart.
    n_ticks = min(6, max(3, chart_h // 3 + 1))
    seen_rows = set()
    for i in range(n_ticks):
        val = lo + i * (hi - lo) / (n_ticks - 1) if n_ticks > 1 else lo
        row = _row_for(val)
        if row in seen_rows:
            continue
        seen_rows.add(row)
        db.puts(row, axis_w, "─" * plot_w, P_DIM)
        db.puts(row, 0, fmt_money(val).rjust(axis_w - 2), P_DIM)

    # Y-axis line — a vertical rule separating the $ labels from the plot,
    # per explicit user request.
    for r in range(chart_top, chart_bottom + 1):
        db.put(r, axis_w - 1, "│", P_DIM)

    # $0 break-even reference line — drawn AFTER the evenly-spaced ticks
    # above (so it overrides a dim gridline at the same row) but before the
    # data line (so the data still draws on top and stays legible) — a
    # distinct color, since $0 is the one PnL level that always matters
    # regardless of how the rest of the axis happens to scale. Only drawn
    # when 0 actually falls within the displayed range (an all-winning or
    # all-losing stretch has nothing to mark).
    zero_row = _row_for(0.0)
    if chart_top <= zero_row <= chart_bottom:
        db.puts(zero_row, axis_w, "─" * plot_w, P_YELLOW)
        db.puts(zero_row, 0, "$0".rjust(axis_w - 2), P_YELLOW, curses.A_BOLD)

    # Spread points across the FULL plot width (previously placed 1 column
    # apart regardless of how many columns were actually available, which
    # left most of the pane empty whenever there were only a handful of
    # trades) and connect consecutive points with an interpolated line
    # (previously bare, unconnected dots) — a real line chart instead of a
    # scatter of dots, per explicit user request for "a much nicer-looking
    # graph."
    m = len(sampled)
    x_scale = plot_w / (m - 1) if m > 1 else 0
    prev_col = prev_row = None
    for i, val in enumerate(sampled):
        col = min(cols - 1, axis_w + round(i * x_scale))
        row = _row_for(val)
        pair = P_GREEN if val >= 0 else P_RED
        if prev_col is not None and col > prev_col:
            for c in range(prev_col + 1, col):
                t = (c - prev_col) / (col - prev_col)
                r = max(chart_top, min(chart_bottom, round(prev_row + t * (row - prev_row))))
                db.put(r, c, "·", pair, curses.A_DIM)
        db.put(row, col, "●", pair, curses.A_BOLD)
        prev_col, prev_row = col, row

def _fmt_ts_short(iso_ts):
    if not iso_ts:
        return "—"
    try:
        return datetime.fromisoformat(iso_ts).strftime("%m-%d %H:%M")
    except Exception:
        return "—"

def _fmt_duration(entry_ts, exit_ts):
    if not entry_ts or not exit_ts:
        return "—"
    try:
        secs = (datetime.fromisoformat(exit_ts) - datetime.fromisoformat(entry_ts)).total_seconds()
    except Exception:
        return "—"
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"

# Column order prioritizes the most important fields FIRST — a narrow
# terminal's [:cols] clip (same convention every other view in this file
# uses for wide content) drops the tail (TP1/TP2/duration) before anything
# essential, rather than clipping arbitrarily. NET PNL moved right after
# QTY/DIR (2026-07-23, was much further right at column ~93 — invisible on
# anything but a very wide terminal, which is why it looked "missing"
# despite already existing) since it's arguably the single most important
# number in the whole row.
TRADES_TABLE_COLS = [
    ("ENTRY", 12), ("EXIT", 12), ("SYM", 4), ("DIR", 5), ("QTY", 6),
    ("NET PNL", 10), ("R:R", 6), ("ENTRY$", 9), ("EXIT$", 9),
    ("REASON", 10), ("GROSS", 10), ("FEES", 7), ("BALANCE", 11),
    ("R$", 7), ("TP1", 9), ("TP2", 9), ("DUR", 7),
]

def _draw_trades_table(db, y0, y1, cols, source, trades, scroll):
    """The all-trades table, bounded to rows [y0, y1) — split out of the
    old full-screen draw_trades_table so it can share the screen with the
    equity curve (top half chart / bottom half table) per explicit user
    request, instead of a [T]-toggled full-screen alternate page. Returns
    (visible_rows, total) so the caller's footer/scroll-hint can report the
    EXACT window shown without duplicating this function's own row math."""
    y = y0
    db.puts(y, 0, f" All Trades — {'SIM (Paper)' if source == 'sim' else 'LIVE (Phemex)'} ".center(cols, "─")[:cols],
            P_CYAN, curses.A_BOLD)
    y += 1

    if source != "sim":
        db.puts(y, 0, "Detailed trade table is DRY_RUN/--dry-run only — real mode's "
                      "athena_logs has no order-type/TP-leg data to build it from.", P_DIM)
        return 0, 0

    header = ""
    for name, w in TRADES_TABLE_COLS:
        header += name.ljust(w + 1)
    db.puts(y, 0, header[:cols], P_DIM, curses.A_BOLD)
    y += 1
    db.puts(y, 0, "─" * min(cols, len(header)), P_DIM)
    y += 1

    table_top = y
    table_bottom = y1 - 1
    visible_rows = max(1, table_bottom - table_top)
    total = len(trades)
    if not trades:
        db.puts(y, 0, "no closed trades yet", P_DIM)
        return visible_rows, total

    scroll = max(0, min(scroll, max(0, total - visible_rows)))
    # Newest first — most recently closed trade at the top, matching every
    # other "recent" list in this app (Recent Closed Trades, Recent Activity).
    window = list(reversed(trades))[scroll:scroll + visible_rows]

    for i, t in enumerate(window):
        row_y = table_top + i
        cells = [
            (_fmt_ts_short(t["entry_ts"]), P_DEFAULT),
            (_fmt_ts_short(t["exit_ts"]), P_DEFAULT),
            (t["asset"], P_DEFAULT),
            (t["pos_side"] or "—", P_GREEN if t["pos_side"] == "Long" else P_RED),
            (fmt_num(t["qty"], 2), P_DEFAULT),
            (fmt_money(t["net_pnl"]), pnl_pair(t["net_pnl"])),
            (f"{t['rr']:.2f}" if t["rr"] is not None else "—", pnl_pair(t["rr"] or 0)),
            (fmt_num(t["entry_price"]), P_DEFAULT),
            (fmt_num(t["exit_price"]), P_DEFAULT),
            (t["reason"], P_DEFAULT),
            (fmt_money(t["gross_pnl"]), pnl_pair(t["gross_pnl"])),
            (fmt_money(-t["fees"]) if t["fees"] else "$0.00", P_DIM),
            (fmt_money(t["balance"]) if t["balance"] is not None else "—", P_DIM),
            (fmt_money(t["r_dollars"]), P_DIM),
            (fmt_num(t["tp1"]["price"]) if t["tp1"] else "—", P_DIM),
            (fmt_num(t["tp2"]["price"]) if t["tp2"] else "—", P_DIM),
            (_fmt_duration(t["entry_ts"], t["exit_ts"]), P_DIM),
        ]
        x = 0
        for (text, pair), (name, w) in zip(cells, TRADES_TABLE_COLS):
            if x >= cols:
                break
            db.puts(row_y, x, text.ljust(w)[:max(0, min(w, cols - x))], pair)
            x += w + 1

    return visible_rows, total

def draw_data_view(db, rows, cols, source, events, stats, trades, table_scroll):
    """Stats stay put (unchanged), then the screen splits in half: equity
    curve on top, all-trades table on the bottom — replaces the old
    [T]-toggled full-screen alternate page per explicit user request
    ("split in half... top half PnL chart, bottom half table")."""
    y = 0
    tag = "SIM (Paper)" if source == "sim" else "LIVE (Phemex)"
    db.puts(y, 0, f" DATA — {tag} — all-time ".center(cols, "─")[:cols], P_CYAN, curses.A_BOLD)
    y += 2

    db.puts_ansi(y, 0, f"{BLD}Trades{RST}    Total {stats['count']}   Wins {GRN}{stats['wins']}{RST}   "
                       f"Losses {RED}{stats['losses']}{RST}   Win Rate {stats['win_rate']:.1f}%")
    y += 1
    db.puts_ansi(y, 0, f"{BLD}PnL{RST}       Total {pnl_color(stats['total_pnl'])}{fmt_money(stats['total_pnl'])}{RST}   "
                       f"Avg Win {GRN}{fmt_money(stats['avg_win'])}{RST}   "
                       f"Avg Loss {RED}{fmt_money(-stats['avg_loss'] if stats['avg_loss'] else 0)}{RST}")
    y += 1
    pf = stats["profit_factor"]
    pf_txt = "n/a" if pf is None else ("inf" if pf == float("inf") else f"{pf:.2f}")
    db.puts_ansi(y, 0, f"{BLD}Extremes{RST}  Largest Win {GRN}{fmt_money(stats['largest_win'])}{RST}   "
                       f"Largest Loss {RED}{fmt_money(stats['largest_loss'])}{RST}   Profit Factor {pf_txt}")
    y += 2

    remaining_top, remaining_bottom = y, rows - 2
    mid = remaining_top + max(3, (remaining_bottom - remaining_top) // 2)
    _draw_equity_curve(db, remaining_top, mid, cols, events)
    return _draw_trades_table(db, mid, remaining_bottom, cols, source, trades, table_scroll)

# ── Curses app shell ─────────────────────────────────────────────────────────
# DoubleBuffer ported verbatim from charthacker.py — a plain (char,
# color_pair_id, attrs) cell grid; draw code calls put/puts into buf each
# frame, flush(win) diffs buf against the previous frame and only calls
# win.addch where a cell actually changed. This is what makes the redraw
# flicker-free, and is the same "no real curses sub-windows, just row/col
# math for sections" model charthacker.py itself uses throughout.
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

    def puts_ansi(self, row, col, s):
        """puts() for a string tagged with this module's RED/GRN/YLW/CYN/
        BLD/DIM/RST constants (every console_log()/f-string message already
        is) — parses via ansi_segments() so none of those call sites needed
        to change for the switch to curses."""
        c = col
        for text, pair, attrs in ansi_segments(s):
            self.puts(row, c, text, pair, attrs)
            c += len(text)
        return c

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

# Color pairs — same P_* naming/semantics as footprint.py's own
# init_colors(), pre-registered once at startup and never redefined at
# runtime (charthacker.py's memory log flags redefining a pair as the root
# cause of a slow-redraw bug on PDCurses — "swap which pair number gets
# used" instead).
P_DEFAULT, P_DIM, P_CYAN, P_YELLOW, P_GREEN, P_RED, P_STATUS, P_BLUE, P_MAGENTA = range(1, 10)

def init_curses_colors():
    curses.start_color()
    curses.use_default_colors()
    BG = -1
    curses.init_pair(P_DEFAULT, curses.COLOR_WHITE,  BG)
    curses.init_pair(P_DIM,     curses.COLOR_WHITE,  BG)
    curses.init_pair(P_CYAN,    curses.COLOR_CYAN,   BG)
    curses.init_pair(P_YELLOW,  curses.COLOR_YELLOW, BG)
    curses.init_pair(P_GREEN,   curses.COLOR_GREEN,  BG)
    curses.init_pair(P_RED,     curses.COLOR_RED,    BG)
    curses.init_pair(P_STATUS,  curses.COLOR_BLACK,  curses.COLOR_WHITE)
    curses.init_pair(P_BLUE,    curses.COLOR_BLUE,   BG)
    curses.init_pair(P_MAGENTA, curses.COLOR_MAGENTA, BG)

_ANSI_RE = re.compile(r'(\033\[[0-9;]*m)')

def ansi_segments(s):
    """Split an ANSI-tagged string into (text, pair, attrs) runs. DIM maps
    to the P_DIM COLOR pair rather than curses.A_DIM — footprint.py's own
    color setup already established A_DIM is a silent no-op on
    windows-curses/PDCurses."""
    parts = _ANSI_RE.split(s)
    segs = []
    pair, bold = P_DEFAULT, False
    for part in parts:
        if part == RED: pair = P_RED
        elif part == GRN: pair = P_GREEN
        elif part == YLW: pair = P_YELLOW
        elif part == CYN: pair = P_CYAN
        elif part == BLD: bold = True
        elif part == DIM: pair = P_DIM
        elif part == RST: pair, bold = P_DEFAULT, False
        elif part:
            segs.append((part, pair, curses.A_BOLD if bold else 0))
    return segs

# ── Live footprint chart — full per-price-level fidelity ──────────────────────
# Reimplemented (not imported — see module docstring's no-import rule) by
# porting footprint.py's own draw()/init_colors() algorithms faithfully off
# its actual source, so "what Athena sees" and "what footprint.py would
# show" are the same math against the same on-disk bar log, not a
# lookalike reconstruction.
CELL_TXT_W = 15
CHART_COL_W = 1 + CELL_TXT_W + 1
CHART_AXIS_W = 10
POC_MARKER = "◆"
OPEN_MARKER = "○"
CLOSE_MARKER = "●"
VP_BLOCK_FULL = "█"
VP_GHOST_CH = "|"
IMBALANCE_RATIO = 3.0
MIN_IMBALANCE_VOL = 0.0
STACK_COUNT = 3
BIG_TRADE_SIZE = 100.0
PROFILE_MODES = ("volume", "delta", "ohlc", "off")
DASHBOARD_H = 24   # fixed row budget so the chart region's top edge never
                   # jitters cycle to cycle regardless of how much optional
                   # content (pending order/position/TP-leg line) is present
                   # — 4 header/account rows + 2×6 per-instrument rows
                   # (rule/meter+price/lights/position/TP/spacer) + 4
                   # closed-trades (incl. spacer) + 4 activity (incl.
                   # trailing spacer before the chart) = 24. A single
                   # blank row separates each section, INCLUDING between
                   # Recent Activity and the chart below it (2026-07-23,
                   # explicit user request) — was 19 with no inter-section
                   # spacing before that. This budget sits ABOVE the chart
                   # on every screen, and its size directly determines how
                   # many price rows the chart gets (see
                   # draw_footprint_panel's plot_h/group_size) —
                   # see [H] for a toggle that drops this to 0 entirely when
                   # even 24 rows isn't enough.
CHART_HISTORY_BARS = 300   # how much bar history AppState loads so [<-]/[->]
                           # actually has somewhere to scroll back into —
                           # draw_footprint_panel only ever shows a small
                           # visible window sliced out of this

def compute_imbalances(levels, ratio=IMBALANCE_RATIO, min_vol=MIN_IMBALANCE_VOL):
    """Ported verbatim from footprint.py: diagonal footprint imbalance —
    ask volume at level L vs bid volume one tick below (buy imbalance), or
    bid at L vs ask one tick above (sell imbalance)."""
    out = {}
    for lvl, cell in levels.items():
        bid, ask = cell[0], cell[1]
        below_cell = levels.get(lvl - 1)
        above_cell = levels.get(lvl + 1)
        below_bid = below_cell[0] if below_cell is not None else None
        above_ask = above_cell[1] if above_cell is not None else None
        buy_ok = (ask > 0 and ask >= min_vol and below_bid is not None
                  and (below_bid == 0 or ask / below_bid >= ratio))
        sell_ok = (bid > 0 and bid >= min_vol and above_ask is not None
                   and (above_ask == 0 or bid / above_ask >= ratio))
        if buy_ok and sell_ok:
            r_buy = ask / below_bid if below_bid else float("inf")
            r_sell = bid / above_ask if above_ask else float("inf")
            out[lvl] = "buy" if r_buy >= r_sell else "sell"
        elif buy_ok:
            out[lvl] = "buy"
        elif sell_ok:
            out[lvl] = "sell"
    return out

def compute_stacks(imbalances, stack_count=STACK_COUNT):
    """Ported verbatim from footprint.py: consecutive tick levels flagged
    the same imbalance direction, grouped into runs >= stack_count."""
    stacked = set()
    if not imbalances:
        return stacked
    lvls = sorted(imbalances)
    i = 0
    while i < len(lvls):
        j = i
        while j + 1 < len(lvls) and lvls[j + 1] == lvls[j] + 1 and imbalances[lvls[j + 1]] == imbalances[lvls[i]]:
            j += 1
        if j - i + 1 >= stack_count:
            stacked.update(lvls[i:j + 1])
        i = j + 1
    return stacked

def group_levels(levels, group_size):
    """Ported verbatim from footprint.py."""
    if group_size <= 1:
        return {lvl: [cell[0], cell[1]] for lvl, cell in levels.items()}
    grouped = {}
    for lvl, cell in levels.items():
        g = lvl // group_size
        gc = grouped.setdefault(g, [0.0, 0.0])
        gc[0] += cell[0]
        gc[1] += cell[1]
    return grouped

def vp_bar_str(frac, max_width):
    """Ported verbatim from footprint.py."""
    frac = max(0.0, min(1.0, frac))
    return VP_BLOCK_FULL * round(frac * max_width)

def fmt_price(p):
    if p is None:
        return "—"
    return f"{p:,.2f}" if p >= 1 else f"{p:.5f}"

def fmt_lvl_qty(q):
    if q <= 0:
        return "0"
    if q >= 1000:
        return f"{q:,.0f}"
    if q >= 10:
        return f"{q:.1f}"
    return f"{q:.2f}"

def fmt_delta(d):
    if d == 0:
        return "0"
    sign = "+" if d > 0 else "-"
    mag = abs(d)
    if mag >= 1000:
        return f"{sign}{mag:,.0f}"
    if mag >= 10:
        return f"{sign}{mag:.1f}"
    return f"{sign}{mag:.2f}"

def read_last_n_footprint_bars(asset, n):
    path = footprint_log_glob(asset)
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        return [json.loads(l) for l in lines[-n:]]
    except Exception:
        return []

def compute_chart_rows(bars, plot_h):
    """(group_size, group_to_row) — simplified, non-scrolling version of
    footprint.py's rubber-band/group_size logic (no cursor/manual pan
    needed for a live-only embedded view): maps every level across the
    given bars into `plot_h` available rows, widening the group bucket only
    if the combined range doesn't fit, centered vertically."""
    all_levels = set()
    for bar in bars:
        for k in (bar.get("levels") or {}):
            all_levels.add(int(k))
    if not all_levels or plot_h <= 0:
        return 1, {}
    lo_g, hi_g = min(all_levels), max(all_levels)
    span = hi_g - lo_g + 1
    group_size = max(1, -(-span // plot_h))
    grouped = sorted(set(g // group_size for g in all_levels))
    lo, hi = grouped[0], grouped[-1]
    pad = max(0, (plot_h - (hi - lo + 1)) // 2)
    top_group = hi + pad
    return group_size, {top_group - i: i for i in range(plot_h)}

def _event_epoch(ts_val):
    """recent_trade_pairs' 'filled' events carry ts as a raw epoch float
    (log_event/sim_log_event both stamp datetime.now() — sim_log_event's
    own 'ts' field is actually an isoformat() STRING though, same as every
    other athena_logs/sim_logs row) — accept either shape."""
    if ts_val is None:
        return None
    if isinstance(ts_val, (int, float)):
        return ts_val
    try:
        return datetime.fromisoformat(ts_val).timestamp()
    except Exception:
        return None

def _nearest_bar_ts(bars, epoch_ts):
    if epoch_ts is None:
        return None
    nearest = None
    for bar in bars:
        if bar["ts"] <= epoch_ts:
            nearest = bar["ts"]
        else:
            break
    return nearest

def build_trade_markers(asset, bars, inst_snap, trade_pairs):
    """Text-label markers (per explicit user request — labels, not arrows)
    for HISTORICAL trades only, both --dry-run and real: "ENTRY" at the
    fill, and the actual close reason ("TP"/"SL"/"FLIP"/"EOD") at the exit
    — pulled straight from the log event rather than a generic marker, so
    you can tell at a glance why a past trade closed. Each matched to the
    nearest bar at/before its own event timestamp. Exact PnL/price in
    --dry-run via sim_logs, approximate exit in real mode — same caveat as
    the Recent Closed Trades blotter. The CURRENT live/pending position's
    Entry/SL/TP are drawn separately as full-width reference lines (see
    draw_footprint_panel's own position-levels pass) — a single point
    marker doesn't fit "show me where TP1/TP2 actually are right now"."""
    events = []
    if not bars:
        return events
    symbol = ASSETS[asset]["phemex_symbol"]
    for t in trade_pairs:
        if DRY_RUN:
            if t.get("symbol") != symbol:
                continue
        elif t.get("asset") != asset:
            continue
        pos_side = t.get("pos_side")
        if pos_side not in ("Long", "Short"):
            continue
        entry_pair = P_GREEN if pos_side == "Long" else P_RED

        entry_bar = _nearest_bar_ts(bars, _event_epoch(t.get("entry_ts")))
        if entry_bar is not None and t.get("entry_price") is not None:
            events.append({"bar_ts": entry_bar, "price": t["entry_price"], "label": "ENTRY", "pair": entry_pair})

        exit_bar = _nearest_bar_ts(bars, _event_epoch(t.get("exit_ts")))
        if exit_bar is not None and t.get("exit_price") is not None:
            events.append({"bar_ts": exit_bar, "price": t["exit_price"],
                            "label": t.get("reason", "close").upper(), "pair": P_MAGENTA})
    return events

def draw_footprint_panel(db, asset, bars, inst_snap, y0, y1, x0, x1, profile_mode, trade_events,
                          hscroll_bars=0, live_price=None, live_bar=None, focused=False):
    """One footprint chart pane within rows [y0,y1) / cols [x0,x1) — ported
    rendering from footprint.py's draw(), adapted to (a) write into a
    DoubleBuffer instead of directly to a curses window, (b) a fixed
    column range instead of full-terminal width (this is what makes the
    ETH/QQQ side-by-side split just two calls with different x0/x1), and
    (c) an added trade-marker overlay. O/H/L/C/Δ/VAH/VAL/POC row labels +
    time axis match footprint.py's own bottom-table convention.

    hscroll_bars is footprint.py's own "N bars back from the newest"
    convention — 0 is the live tail; the window bars are pulled from keeps
    updating in real time as new bars close (AppState republishes fresh
    data every engine cycle) even while scrolled back, exactly like
    footprint.py's own live+scrolled behavior. live_bar (only shown when
    hscroll_bars==0 — a forming bar has no place in a scrolled-back
    historical view) is Athena's own O/H/L/C-only approximation of the
    still-forming bar footprint.py never persists to disk — see
    engine_loop's live_bar_state tracking for why."""
    cols, rows = x1 - x0, y1 - y0
    if cols < CHART_AXIS_W + CHART_COL_W or rows < 10:
        db.puts(y0, x0, f"{asset}: pane too small"[:max(0, cols)], P_DIM)
        return

    scroll_tag = f"  [SCROLLED {hscroll_bars}b back]" if hscroll_bars else ""
    # Persistent on-chart focus indicator — a footer hint alone was easy to
    # miss (and on a narrow terminal, easy to lose entirely to truncation,
    # since it used to be appended at the very end of an already-long
    # footer string). This makes "which pane does [←/→] scroll" visible
    # right on the pane itself, not just in text you have to go read.
    focus_tag = "  ★ FOCUSED — [Tab] to switch" if focused else ""
    header = f" {asset} FOOTPRINT — {profile_mode.upper()}{scroll_tag}{focus_tag} "
    header_pair = P_YELLOW if focused else P_CYAN
    db.puts(y0, x0, header.center(cols, "─")[:cols], header_pair, curses.A_BOLD)
    top = y0 + 1
    bottom_reserved = 10   # divider + O/H/L/C/Δ/VAH/VAL/POC + time axis
    plot_bottom = y1 - bottom_reserved - 1
    plot_h = max(1, plot_bottom - top)
    plot_w = cols - CHART_AXIS_W
    n = max(1, plot_w // CHART_COL_W)

    show_live = hscroll_bars == 0 and live_bar is not None
    hist_n = max(0, n - 1) if show_live else n
    hscroll_bars = max(0, min(hscroll_bars, max(0, len(bars) - 1)))
    end = len(bars) - hscroll_bars
    visible = bars[max(0, end - hist_n):end] if bars else []
    if show_live:
        visible = visible + [live_bar]
    if not visible:
        db.puts(top, x0 + CHART_AXIS_W, "no bar data yet", P_DIM)
        return

    group_size, group_to_row = compute_chart_rows(visible, plot_h)
    tick = visible[-1].get("tick", 1.0)
    # Adaptive label stride (~12 labels regardless of plot_h) — matches
    # footprint.py's own axis convention exactly (its label_step = max(1,
    # plot_h // 12)). A FIXED stride here previously meant a short pane
    # (few rows — e.g. squeezed under the dashboard) showed almost no
    # labels while a tall one showed far more than 12, neither matching
    # footprint.py's own consistently-spaced axis.
    label_step = max(1, plot_h // 12)
    for g, r_i in group_to_row.items():
        row_y = top + r_i
        if r_i % label_step == 0:
            db.puts(row_y, x0, fmt_price(g * group_size * tick).rjust(CHART_AXIS_W - 1), P_DIM)

    col_x = [x0 + CHART_AXIS_W + i * CHART_COL_W for i in range(len(visible))]
    cell_w = min(CELL_TXT_W, CHART_COL_W - 2)
    # Live price reference line — footprint.py's own LIVE_LINE_CH, drawn
    # through every visible bar's column at the row for the CURRENT live
    # price, skipping any bar whose own traded range already has a real
    # cell sitting at that exact row (has_live_cell, tracked per bar below)
    # so it never overwrites actual bid/ask data.
    live_g = round(live_price / tick) // group_size if live_price is not None else None
    bar_stats = []
    has_live_cell = []
    for i, bar in enumerate(visible):
        levels = {int(k): v for k, v in (bar.get("levels") or {}).items()}
        cx = col_x[i]
        poc_g = va_low = va_high = None
        if levels:
            glevels = group_levels(levels, group_size)
            poc_g = compute_poc(glevels)
            va_low, va_high = compute_value_area(glevels, poc_g)
            has_live_cell.append(live_g is not None and live_g in glevels)
            display_levels = dict(glevels)
            for g in range(min(glevels), max(glevels) + 1):
                display_levels.setdefault(g, [0.0, 0.0])

            if profile_mode == "volume":
                max_vol = max((c[0] + c[1] for c in glevels.values()), default=0.0)
                va_span = max(1, max(poc_g - va_low, va_high - poc_g)) if poc_g is not None else 1
                for g, cell in display_levels.items():
                    r_i = group_to_row.get(g)
                    if r_i is None:
                        continue
                    row_y = top + r_i
                    vol = cell[0] + cell[1]
                    frac = (vol / max_vol) if max_vol > 0 else 0.0
                    bstr = vp_bar_str(frac, cell_w) or VP_GHOST_CH
                    if va_low <= g <= va_high:
                        dist = abs(g - poc_g) if poc_g is not None else 0
                        pair = P_CYAN if (1 - dist / va_span) <= 0.5 else P_BLUE
                    else:
                        pair = P_DIM
                    db.puts(row_y, cx + 1, bstr, pair)
                    if g == poc_g:
                        db.put(row_y, cx, POC_MARKER, P_YELLOW, curses.A_BOLD)
            elif profile_mode == "delta":
                max_abs_delta = max((abs(c[1] - c[0]) for c in glevels.values()), default=0.0)
                for g, cell in display_levels.items():
                    r_i = group_to_row.get(g)
                    if r_i is None:
                        continue
                    row_y = top + r_i
                    lvl_delta = cell[1] - cell[0]
                    frac = (abs(lvl_delta) / max_abs_delta) if max_abs_delta > 0 else 0.0
                    bstr = vp_bar_str(frac, cell_w) or VP_GHOST_CH
                    if lvl_delta > 0: pair, attrs = P_GREEN, curses.A_BOLD
                    elif lvl_delta < 0: pair, attrs = P_RED, curses.A_BOLD
                    else: pair, attrs = P_DEFAULT, 0
                    db.puts(row_y, cx + 1, bstr, pair, attrs)
                    if g == poc_g:
                        db.put(row_y, cx, POC_MARKER, P_YELLOW, curses.A_BOLD)
            elif profile_mode == "ohlc":
                # Traditional candlestick — solid body between open/close,
                # thin wick through the full high/low range. No per-level
                # volume/delta breakdown (that's what the volume/delta
                # modes are for); POC still marked as a reference point.
                o_g = round(bar["o"] / tick) // group_size
                h_g = round(bar["h"] / tick) // group_size
                l_g = round(bar["l"] / tick) // group_size
                c_g = round(bar["c"] / tick) // group_size
                pair = P_GREEN if bar["c"] >= bar["o"] else P_RED
                body_lo, body_hi = min(o_g, c_g), max(o_g, c_g)
                wick_col = cx + max(1, cell_w // 2)
                for g in range(l_g, h_g + 1):
                    r_i = group_to_row.get(g)
                    if r_i is None:
                        continue
                    row_y = top + r_i
                    if body_lo <= g <= body_hi:
                        db.puts(row_y, cx + 1, VP_BLOCK_FULL * cell_w, pair, curses.A_BOLD)
                    else:
                        db.put(row_y, wick_col, "│", pair, curses.A_BOLD)
                    if g == poc_g:
                        db.put(row_y, cx, POC_MARKER, P_YELLOW, curses.A_BOLD)
            else:
                imbalances = compute_imbalances(glevels)
                for g, cell in display_levels.items():
                    r_i = group_to_row.get(g)
                    if r_i is None:
                        continue
                    row_y = top + r_i
                    bid, ask = cell[0], cell[1]
                    txt = f"{fmt_lvl_qty(bid)}x{fmt_lvl_qty(ask)}"[:cell_w]
                    direction = imbalances.get(g)
                    if direction == "sell": pair, attrs = P_RED, curses.A_BOLD
                    elif direction == "buy": pair, attrs = P_GREEN, curses.A_BOLD
                    elif bid >= BIG_TRADE_SIZE or ask >= BIG_TRADE_SIZE: pair, attrs = P_CYAN, curses.A_BOLD
                    else: pair, attrs = P_DEFAULT, 0
                    db.puts(row_y, cx + 1, txt.center(cell_w), pair, attrs)
                    if g == poc_g:
                        db.put(row_y, cx, POC_MARKER, P_YELLOW, curses.A_BOLD)

            # ○/● open/close gutter markers are redundant on top of an
            # OHLC candle's own body (which already encodes both) — only
            # drawn for the per-level profile modes.
            if profile_mode != "ohlc":
                open_g = round(bar["o"] / tick) // group_size
                close_g = round(bar["c"] / tick) // group_size
                if open_g != poc_g:
                    r_i = group_to_row.get(open_g)
                    if r_i is not None:
                        db.put(top + r_i, cx, OPEN_MARKER, P_CYAN)
                if close_g != poc_g:
                    r_i = group_to_row.get(close_g)
                    if r_i is not None:
                        pair = P_GREEN if bar["c"] >= bar["o"] else P_RED
                        db.put(top + r_i, cx, CLOSE_MARKER, pair, curses.A_BOLD)
        else:
            has_live_cell.append(False)

        poc_price = poc_g * group_size * tick if poc_g is not None else None
        vah_price = va_high * group_size * tick if va_high is not None else None
        val_price = va_low * group_size * tick if va_low is not None else None
        bar_stats.append((poc_price, vah_price, val_price))

    if live_g is not None:
        live_row_y = group_to_row.get(live_g)
        if live_row_y is not None:
            live_row_y += top
            for i in range(len(visible)):
                if not has_live_cell[i]:
                    db.puts(live_row_y, col_x[i], "─" * (CHART_COL_W - 1), P_YELLOW)

    # Historical trade markers — short text labels ("ENTRY"/"TP"/"SL"/
    # "FLIP"/"EOD") at the specific bar+price they happened at.
    for i, bar in enumerate(visible):
        cx = col_x[i]
        for ev in trade_events:
            if ev["bar_ts"] != bar["ts"]:
                continue
            g = round(ev["price"] / tick) // group_size
            r_i = group_to_row.get(g)
            if r_i is not None:
                db.puts(top + r_i, cx + 1, ev["label"][:cell_w], ev["pair"], curses.A_BOLD | curses.A_REVERSE)

    # Current position/pending Entry/SL/TP1/TP2 — full-width dashed
    # reference lines with a labeled tag at the right edge (per explicit
    # user request: "Entry/SL/TP labels", not point markers, since these
    # are ongoing levels the position is actually resting against right
    # now, not one-off historical events).
    def _level_line(level, label, pair):
        if level is None or not col_x:
            return
        g = round(level / tick) // group_size
        r_i = group_to_row.get(g)
        if r_i is None:
            return
        row_y = top + r_i
        for cx2 in col_x:
            db.puts(row_y, cx2, "-" * (CHART_COL_W - 1), pair)
        tag = f" {label} "
        db.puts(row_y, col_x[-1] + 1, tag[:max(0, cols - (col_x[-1] + 1 - x0))], pair, curses.A_BOLD | curses.A_REVERSE)

    pos = inst_snap.get("position")
    pend = inst_snap.get("pending")
    if pos:
        _level_line(pos.get("fill_price"), "ENTRY", P_GREEN if pos["pos_side"] == "Long" else P_RED)
        _level_line(pos.get("sl_price"), "SL", P_RED)
        for i, leg in enumerate(pos.get("tp_legs") or [], start=1):
            _level_line(leg.get("level"), f"TP{i}", P_GREEN)
    elif pend and pend.get("entry_price") is not None:
        _level_line(pend["entry_price"], "ENTRY", P_YELLOW)

    border_row = plot_bottom + 1
    db.puts(border_row, x0, "─" * cols, P_DIM)
    o_row, h_row, l_row, c_row = border_row + 1, border_row + 2, border_row + 3, border_row + 4
    delta_row, vah_row, val_row, poc_row = border_row + 5, border_row + 6, border_row + 7, border_row + 8
    time_row = border_row + 9
    # Row labels in the axis gutter — same convention as footprint.py's own
    # table (right-justified into CHART_AXIS_W - 1, one label per row).
    for label, row_y in (("O", o_row), ("H", h_row), ("L", l_row), ("C", c_row), ("Δ", delta_row),
                          ("VAH", vah_row), ("VAL", val_row), ("POC", poc_row)):
        db.puts(row_y, x0, label.rjust(CHART_AXIS_W - 1), P_DIM)
    for i, bar in enumerate(visible):
        cx = col_x[i]
        w = CHART_COL_W - 1
        db.puts(o_row, cx, fmt_price(bar["o"]).center(w), P_CYAN, curses.A_BOLD)
        db.puts(h_row, cx, fmt_price(bar["h"]).center(w), P_GREEN, curses.A_BOLD)
        db.puts(l_row, cx, fmt_price(bar["l"]).center(w), P_RED, curses.A_BOLD)
        c_pair = P_GREEN if bar["c"] >= bar["o"] else P_RED
        db.puts(c_row, cx, fmt_price(bar["c"]).center(w), c_pair, curses.A_BOLD)

        delta = bar.get("delta", 0.0)
        prev_delta = visible[i - 1].get("delta") if i > 0 else None
        if delta > 0: d_pair = P_GREEN
        elif delta < 0: d_pair = P_RED
        else: d_pair = P_DEFAULT
        # (+)/(-) shift marker vs. the PREVIOUS bar's own delta — a change
        # indicator, NOT a restatement of this bar's own sign (which
        # d_pair/the base value's color already shows). Drawn in its own
        # fixed green(+)/red(-) color, independent of d_pair, same as
        # footprint.py's own delta_color/shift_color split — it must always
        # read as "rose"/"fell" even when d_pair is P_DEFAULT (delta==0) or
        # the opposite sign of the shift itself.
        shift = ""
        if prev_delta is not None and delta > prev_delta: shift = " (+)"
        elif prev_delta is not None and delta < prev_delta: shift = " (-)"
        delta_text = fmt_delta(delta)
        full_text = delta_text + shift
        centered = full_text.center(w)
        db.puts(delta_row, cx, centered, d_pair, curses.A_BOLD)
        if shift:
            shift_pair = P_GREEN if shift.strip() == "(+)" else P_RED
            shift_col = cx + centered.find(full_text) + len(delta_text)
            db.puts(delta_row, shift_col, shift, shift_pair, curses.A_BOLD)

        poc_price, vah_price, val_price = bar_stats[i]
        db.puts(vah_row, cx, fmt_price(vah_price).center(w), P_DIM)
        db.puts(val_row, cx, fmt_price(val_price).center(w), P_DIM)
        prev_poc = bar_stats[i - 1][0] if i > 0 else None
        if poc_price is not None and prev_poc is not None and poc_price > prev_poc: p_pair = P_GREEN
        elif poc_price is not None and prev_poc is not None and poc_price < prev_poc: p_pair = P_RED
        else: p_pair = P_DEFAULT
        db.puts(poc_row, cx, fmt_price(poc_price).center(w), p_pair, curses.A_BOLD)

        # Time axis — HH:MM:SS, same "fine" cadence footprint.py's own
        # fmt_time uses for any interval under 5 minutes (90s bars qualify).
        time_txt = "LIVE" if bar.get("live") else datetime.fromtimestamp(bar["ts"]).strftime("%H:%M:%S")
        db.puts(time_row, cx, time_txt.center(w), P_YELLOW if bar.get("live") else P_DIM)

# ── Bridging the async trading engine into the (sync) curses render loop ─────
# The entire trading engine (AthenaInstrument state machine, SimAccount,
# Phemex calls) stays asyncio-based and untouched — it runs on a background
# thread; the curses main loop (main thread) never awaits anything, it just
# reads AppState under a lock and draws. Same "background thread(s) mutate
# shared state under a lock, the render loop just reads it" shape
# charthacker.py already uses for its own WS feed/alert/trade-monitor
# threads (its state/state.lock), just fed by an asyncio loop instead of a
# plain sync one.
class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.instruments = {}
        self.snapshot_age = "no snapshot found yet"
        self.balance = None
        self.available = None
        self.open_pnl = None
        self.closed_pnl = None
        self.event_log = []
        self.closed_trades = []
        self.trade_pairs = []
        self.footprint_bars = {}
        self.live_bars = {}
        self.qqq_market_closed = True

    def publish_live_fast(self, live_bars):
        """Fast path (see _fast_publish_loop) — refreshes ONLY the live/
        forming-bar data and its derived display price, independent of the
        slow REST-heavy trading-engine cycle. Patches self.instruments'
        already-published dicts in place rather than rebuilding them, so
        this never regresses lights/state/position/etc — those still only
        change when the real publish() below runs."""
        with self.lock:
            for asset, bar in live_bars.items():
                if bar is None:
                    continue
                self.live_bars[asset] = bar
                inst = self.instruments.get(asset)
                if inst is not None:
                    inst["live_price"] = bar["c"]

    def publish(self, instruments, age, acc, live_prices, qqq_market_closed):
        symbols = {i.cfg["phemex_symbol"] for i in instruments}
        balance, available = account_balance_fields(acc)
        open_pnl_total = sum((instrument_open_pnl(i, live_prices.get(i.asset)) or 0.0) for i in instruments)
        has_open = any(i.position for i in instruments)
        closed_pnl = (get_sim_account().realized_pnl_today if DRY_RUN
                      else account_realized_pnl_today(acc, symbols))
        inst_snap = {}
        for i in instruments:
            inst_snap[i.asset] = {
                "lights": dict(i.lights), "regime": i.regime, "price": i.price, "state": i.state,
                "pending": dict(i.pending) if i.pending else None,
                "position": dict(i.position) if i.position else None,
                "live_price": live_prices.get(i.asset),
                "market_closed": i.market_closed,
            }
        # Enough history to actually scroll back through, not just the
        # live tail — CHART_HISTORY_BARS, not the handful the visible pane
        # itself can show at once (draw_footprint_panel slices whatever
        # window it needs out of this via hscroll_bars).
        bars = {i.asset: read_last_n_footprint_bars(i.asset, CHART_HISTORY_BARS) for i in instruments}
        with self.lock:
            self.instruments = inst_snap
            self.snapshot_age = age
            self.balance = balance
            self.available = available
            self.open_pnl = open_pnl_total if has_open else None
            self.closed_pnl = closed_pnl
            self.event_log = list(_event_log)
            self.closed_trades = recent_closed_trades(6)
            self.trade_pairs = recent_trade_pairs(8)
            self.footprint_bars = bars
            self.qqq_market_closed = qqq_market_closed

    def snapshot(self):
        with self.lock:
            return {
                "instruments": self.instruments, "snapshot_age": self.snapshot_age,
                "balance": self.balance, "available": self.available,
                "open_pnl": self.open_pnl, "closed_pnl": self.closed_pnl,
                "event_log": self.event_log, "closed_trades": self.closed_trades,
                "trade_pairs": self.trade_pairs,
                "footprint_bars": self.footprint_bars, "live_bars": self.live_bars,
                "qqq_market_closed": self.qqq_market_closed,
            }

APP_STATE = AppState()
_quit_evt = threading.Event()
_reset_sim_evt = threading.Event()   # [R] (armed+confirmed) sets this; engine_loop
                                      # is the only thing that ever mutates SimAccount,
                                      # so the actual reset() call happens there, not
                                      # on the curses thread — no new locking needed
_reset_sim_balance = [SIM_BALANCE_ARG]   # curses thread writes the user's typed-in
                                          # amount here before setting _reset_sim_evt
_flatten_all_evt = threading.Event()   # [F] (armed+confirmed) sets this; engine_loop
                                        # is the only thing that ever calls the real
                                        # Phemex/SimAccount close functions, so the
                                        # actual flatten happens there, not on the
                                        # curses thread — same convention as _reset_sim_evt
_profile_mode_idx = [0]   # mutable single-item box so the curses thread's [V] key can cycle it in place

async def engine_loop():
    if not DRY_RUN and (not PHEMEX_API_KEY or not PHEMEX_API_SECRET):
        console_log(f"{RED}PHEMEX_API_KEY/PHEMEX_API_SECRET not set in .env — real trading needs both{RST}")
        log_event("SYSTEM", "startup_error", {"error": "missing Phemex API keys"})
        return

    console_log(f"{BLD}Athena starting{RST} — interval={INTERVAL}s pct={PCT}% dry_run={DRY_RUN}")
    log_event("SYSTEM", "startup", {"interval": INTERVAL, "pct": PCT, "dry_run": DRY_RUN, "no_session": NO_SESSION})

    if DRY_RUN:
        sim = get_sim_account()
        if RESET_SIM:
            sim.reset(SIM_BALANCE_ARG)
            console_log(f"{YLW}Paper account reset to ${SIM_BALANCE_ARG:,.2f}{RST}")
        else:
            console_log(f"{DIM}Paper account balance: ${sim.balance:,.2f}{RST}")
    elif RESET_SIM:
        console_log(f"{YLW}--reset-sim ignored — not running with --dry-run{RST}")

    instruments = [AthenaInstrument(a) for a in ASSETS]
    for inst in instruments:
        await inst.reconcile_startup()

    while not _quit_evt.is_set():
        snapshot = read_last_status_snapshot()
        if snapshot:
            try:
                age = f"{time.time() - datetime.fromisoformat(snapshot['ts']).timestamp():.0f}s ago"
            except Exception:
                age = "unknown"
        else:
            age = "no snapshot found yet"

        if _reset_sim_evt.is_set():
            _reset_sim_evt.clear()
            if DRY_RUN:
                new_balance = _reset_sim_balance[0]
                get_sim_account().reset(new_balance)
                # A wiped sim ledger has no positions/orders left for these
                # to match against — force them back to WATCHING too, or
                # _check_fill/_manage_position would just poll forever
                # against a fill/position that no longer exists.
                for inst in instruments:
                    inst.pending = None
                    inst.position = None
                    inst.state = "WATCHING"
                    inst.lights["Order Flow"] = False
                console_log(f"{YLW}Paper account reset to ${new_balance:,.2f}{RST} (via [R])")
                log_event("SYSTEM", "reset_sim_inapp", {"balance": new_balance})

        if _flatten_all_evt.is_set():
            _flatten_all_evt.clear()
            console_log(f"{RED}{BLD}Flatten All triggered — closing every position, cancelling every order{RST}")
            log_event("SYSTEM", "flatten_all_triggered", {})
            for inst in instruments:
                try:
                    await inst._flatten_now("manual")
                except Exception as e:
                    console_log(f"{inst.asset}: Flatten All FAILED — {e}")
                    log_event(inst.asset, "manual_flatten_failed", {"error": str(e)})

        for inst in instruments:
            try:
                await inst.process_cycle(snapshot)
            except Exception as e:
                console_log(f"{inst.asset}: cycle error — {e}")
                log_event(inst.asset, "cycle_error", {"error": str(e)})

        try:
            acc = await fetch_account()
        except Exception as e:
            acc = None
            console_log(f"account fetch failed for dashboard — {e}")

        # Fetched unconditionally now (not just for positioned instruments)
        # — open_pnl and the dashboard's per-instrument price fallback need
        # it regardless of whether Athena itself currently holds anything.
        # Prefers LIVE_TAPE's own real-time WS price (no network round trip,
        # already fresher than a REST poll) — fetch_last_price is now only
        # a fallback for the brief window before the WS connects. The
        # still-forming CHART bar itself is no longer built here at all —
        # see _fast_publish_loop, which reads LIVE_TAPE far more often than
        # this REST-heavy cycle runs, so the chart isn't bottlenecked on
        # process_cycle/fetch_account/fetch_last_price latency.
        live_prices = {}
        for inst in instruments:
            tape_bar = LIVE_TAPE.snapshot(inst.asset)
            live_prices[inst.asset] = tape_bar["c"] if tape_bar is not None \
                else await fetch_last_price(inst.cfg["phemex_symbol"])

        qqq_market_closed = bool((snapshot.get("qqq") or {}).get("market_closed")) if snapshot else True
        APP_STATE.publish(instruments, age, acc, live_prices, qqq_market_closed)
        await asyncio.sleep(INTERVAL)

    log_event("SYSTEM", "shutdown", {})

def _run_engine():
    asyncio.run(engine_loop())

def _fast_publish_loop(stop_evt):
    """Refreshes ONLY the live/forming-bar chart data at a fast, fixed
    cadence — independent of engine_loop's slower --interval cycle (which
    does REST-heavy work: process_cycle, fetch_account, fetch_last_price
    per instrument). LIVE_TAPE itself updates in real time off the Phemex
    WS feed on every trade print; without this loop, AppState only ever
    saw a snapshot of it once per multi-second engine cycle, which is what
    made the chart visibly lag behind footprint.py's own live view. This
    loop is the fix — it does no network I/O of its own (LIVE_TAPE.snapshot
    is a fast in-memory lock+copy), so it can run every 0.25s cheaply."""
    while not stop_evt.is_set():
        live_bars = {a: LIVE_TAPE.snapshot(a) for a in ASSETS}
        APP_STATE.publish_live_fast(live_bars)
        stop_evt.wait(0.25)

# ── Dashboard panel (fixed row budget — see DASHBOARD_H) ──────────────────────
# Compacted 2026-07-23 (down from 29 to DASHBOARD_H rows) — the chart's own
# vertical resolution is directly bounded by however many rows are left
# after this budget (see draw_footprint_panel's plot_h/group_size math), and
# a fixed 29-row dashboard on anything but a very tall terminal was leaving
# the chart with roughly 1/3 the price-row resolution footprint.py's own
# full-screen view gets — visibly much coarser/blockier profile bars.
# Nothing here was REMOVED, just merged onto fewer lines (single-line
# instrument summary+price, single-line TP legs) and Recent Closed Trades/
# Recent Activity trimmed from 4 rows each to 2 (still fixed-height, still
# no jitter — see [H] for a full-width toggle when even this isn't enough).
def draw_dashboard(db, snap, cols):
    y = 0
    mode_tag = (('  ' + RED + BLD + '[PAUSED — A to resume]' + RST + DIM) if not ATHENA_ENABLED else '') + \
               ('  [DRY RUN]' if DRY_RUN else '') + ('  [24H MODE]' if NO_SESSION else '')
    now_et = datetime.now(TZ_ET).strftime("%I:%M:%S %p ET") if TZ_ET else "ET n/a"
    now_ct = datetime.now(TZ_CT).strftime("%I:%M:%S %p CT") if TZ_CT else "CT n/a"
    db.puts_ansi(y, 0, f"{BLD}{CYN}ATHENA{RST}{DIM} — {now_et} | {now_ct} | interval {INTERVAL}s | "
                       f"snapshot age: {snap['snapshot_age']}{mode_tag}{RST}")
    y += 1
    db.puts(y, 0, "─" * min(cols, BOX_W), P_DIM)
    y += 1

    op, cpnl = snap["open_pnl"], snap["closed_pnl"]
    bal_tag = "SIM" if DRY_RUN else "Phemex"
    db.puts_ansi(y, 0, f"{BLD}ACCOUNT{RST} ({bal_tag})   Balance {fmt_money(snap['balance'])}   "
                       f"Available {fmt_money(snap['available'])}   "
                       f"Open PnL {pnl_color(op) if op is not None else DIM}"
                       f"{fmt_money(op) if op is not None else 'n/a'}{RST}   "
                       f"Closed PnL Today {pnl_color(cpnl)}{fmt_money(cpnl)}{RST}")
    y += 2   # blank row separates ACCOUNT from the first instrument block

    for asset in ASSETS:
        inst = snap["instruments"].get(asset) or {}
        lights = inst.get("lights", {})
        names = gated_light_names()
        segs = "".join((GRN if lights.get(n) else RED) + "█" + RST for n in names)
        count = sum(1 for n in names if lights.get(n))
        title = f"── {asset} "
        db.puts(y, 0, (title + "─" * max(2, min(cols, BOX_W) - len(title)))[:cols], P_DIM)
        y += 1
        if inst.get("market_closed"):
            regime_txt, state_txt = "n/a", "CLOSED"
        else:
            regime_txt, state_txt = (inst.get("regime") or "none").upper(), inst.get("state", "?")
        price_txt = f"   {DIM}price {inst['price']:.2f}{RST}" if inst.get("price") is not None else ""
        db.puts_ansi(y, 0, f"  [{segs}] {count}/{len(names)}   regime: {regime_txt}   state: {state_txt}{price_txt}")
        y += 1
        detail = "  " + "  ".join(
            (GRN if lights.get(n) else RED) + n + RST + (f"{DIM}(bypassed){RST}" if n == "Session" and NO_SESSION else "")
            for n in LIGHT_ORDER)
        db.puts_ansi(y, 0, detail)
        y += 1

        line3 = ""
        if inst.get("state") == "PENDING_FILL" and inst.get("pending"):
            pd = inst["pending"]
            line3 = (f"  {YLW}Pending: {pd['pos_side']} {pd['order_type']} "
                     f"{fmt_num(pd['qty'], 2)} @ {fmt_num(pd.get('entry_price'))}{RST}")
        elif inst.get("position"):
            p = inst["position"]
            pnl = None
            live = inst.get("live_price")
            if live is not None:
                pnl = (live - p["fill_price"]) * p["qty"] if p["pos_side"] == "Long" else (p["fill_price"] - live) * p["qty"]
            line3 = (f"  {YLW}Position: {p['pos_side']} {fmt_num(p['qty'], 2)} @ {fmt_num(p['fill_price'])}   "
                     f"SL {fmt_num(p.get('sl_price'))}   uPnL {pnl_color(pnl)}{fmt_money(pnl)}{RST}")
        if line3:
            db.puts_ansi(y, 0, line3)
        y += 1

        # TP legs — one fixed row (blank if none), both legs on the same
        # line, each showing which target (BT/ST/GEX Flip/Cluster) it's
        # tied to, per explicit user request to see "the HPL they are
        # associated with".
        tp_legs = (inst.get("position") or {}).get("tp_legs") or []
        if tp_legs:
            parts = []
            for i, leg in enumerate(tp_legs[:2], start=1):
                gex_tag = f"{DIM}(GEX){RST}" if leg.get("tracks_gex_flip") else ""
                parts.append(f"{DIM}TP{i} ({leg.get('type', '?')}){RST} "
                             f"{fmt_num(leg['qty'], 2)} @ {fmt_num(leg['level'])} {gex_tag}")
            db.puts_ansi(y, 0, "       " + "    ".join(parts))
        y += 2   # blank row separates this instrument block from the next one

    title = "── Recent Closed Trades "
    db.puts(y, 0, (title + "─" * max(2, min(cols, BOX_W) - len(title)))[:cols], P_DIM)
    y += 1
    trades = (snap.get("closed_trades") or [])[-2:]
    for i in range(2):
        if i < len(trades):
            t = trades[i]
            ts = (t.get("ts") or "")[11:19]
            if DRY_RUN:
                sym, side, qty = t.get("symbol", "?"), t.get("pos_side", "?"), t.get("qty")
                entry, exitp, pnl, reason = t.get("entry"), t.get("price"), t.get("pnl"), t.get("reason", "")
            else:
                d = t.get("detail") or {}
                sym, side, qty = t.get("asset", "?"), d.get("pos_side", "?"), d.get("qty")
                entry, exitp, pnl, reason = d.get("entry"), d.get("exit_approx"), d.get("pnl_approx"), "~approx"
            db.puts_ansi(y, 0, f"  {DIM}{ts}{RST}  {sym:<8} {side:<5} {fmt_num(qty, 2)} @ {fmt_num(entry)} -> "
                               f"{fmt_num(exitp)}   PnL {pnl_color(pnl)}{fmt_money(pnl)}{RST}  {DIM}({reason}){RST}")
        y += 1
    y += 1   # blank row separates Recent Closed Trades from Recent Activity

    title = "── Recent Activity "
    db.puts(y, 0, (title + "─" * max(2, min(cols, BOX_W) - len(title)))[:cols], P_DIM)
    y += 1
    events = (snap.get("event_log") or [])[-2:]
    for i in range(2):
        if i < len(events):
            db.puts_ansi(y, 0, f"  {events[i]}")
        y += 1
    y += 1   # blank row separates Recent Activity from the chart below it

    return DASHBOARD_H

def _prompt_number(stdscr, label, default=None):
    """Blocking single-line numeric input on the footer row — same pattern
    footprint.py/charthacker.py's own text-entry dialogs use (switch to
    blocking input, get_wch() loop building a buffer, Enter confirms, Esc
    cancels). Uses `except Exception: continue`, never `break`, on a failed
    get_wch() call — a documented gotcha on this Windows/windows-curses
    setup: the very first get_wch() right after nodelay(False) can
    transiently raise once before it actually blocks, and `break` there
    would make the dialog flash and vanish instantly instead of accepting
    input. Returns the entered float, or `default` if left blank/cancelled."""
    rows, cols = stdscr.getmaxyx()
    row = rows - 1
    buf = ""
    stdscr.nodelay(False)
    curses.curs_set(1)
    try:
        while True:
            stdscr.move(row, 0)
            stdscr.clrtoeol()
            text = f"{label}{buf}"
            try:
                stdscr.addstr(row, 0, text[:max(0, cols - 1)])
            except curses.error:
                pass
            stdscr.refresh()
            try:
                ch = stdscr.get_wch()
            except Exception:
                continue
            if ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                break
            if ch == "\x1b":   # Esc — cancel
                buf = None
                break
            if ch in ("\x08", "\x7f") or ch == curses.KEY_BACKSPACE:
                buf = buf[:-1]
            elif isinstance(ch, str) and (ch.isdigit() or ch == "."):
                buf += ch
    finally:
        curses.curs_set(0)
        stdscr.nodelay(True)
    if not buf:
        return default
    try:
        return max(0.0, float(buf))
    except ValueError:
        return default

# ── [L] Full Recent Activity log popup ────────────────────────────────────────
def draw_activity_log_popup(db, rows, cols, event_log, scroll):
    """Centered box overlay showing the FULL in-memory activity buffer
    (EVENT_LOG_MAXLEN entries, not just the dashboard's own last-2-lines
    preview) — drawn on top of whatever the dashboard/chart already put in
    db this frame, since this is an overlay, not a separate full-screen
    view like [D]ata. event_log is oldest-first; scroll is "N lines back
    from the newest," same convention as the chart's own hscroll_bars, so
    0 always means "showing the latest activity."""
    box_w = min(cols - 4, 110)
    box_h = min(rows - 4, len(event_log) + 4) if event_log else 6
    box_h = max(6, box_h)
    x0 = max(0, (cols - box_w) // 2)
    y0 = max(0, (rows - box_h) // 2)

    title = " Recent Activity — full log "
    db.puts(y0, x0, ("┌" + title.center(box_w - 2, "─") + "┐"), P_CYAN, curses.A_BOLD)
    for r in range(1, box_h - 1):
        db.puts(y0 + r, x0, "│" + " " * (box_w - 2) + "│", P_CYAN)
    db.puts(y0 + box_h - 1, x0, "└" + "─" * (box_w - 2) + "┘", P_CYAN)

    visible_rows = box_h - 3
    total = len(event_log)
    scroll = max(0, min(scroll, max(0, total - visible_rows)))
    end = total - scroll
    start = max(0, end - visible_rows)
    for i, line in enumerate(event_log[start:end]):
        db.puts_ansi(y0 + 1 + i, x0 + 2, line[:box_w - 4])

    scroll_tag = f"  [{start + 1}-{end} of {total}]" if total else "  (empty)"
    footer = f" [↑/↓] scroll{scroll_tag}   [L]/Esc close "
    db.puts(y0 + box_h - 2, x0 + 1, footer[:box_w - 2].center(box_w - 2), P_DIM)

# ── Main curses loop ───────────────────────────────────────────────────────────
def curses_main(stdscr):
    global PCT, NO_SESSION, ATHENA_ENABLED
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.mousemask(0)
    init_curses_colors()
    stdscr.timeout(100)

    threading.Thread(target=_run_engine, daemon=True).start()
    for asset in ASSETS:
        # QQQ's live tape prefers Athena's own dedicated Alpaca account
        # (real QQQ shares, matching footprint.py's own closed-bar source
        # for QQQ) over Phemex's thinner QQQUSDT perp — per explicit user
        # request. Falls back to the Phemex tape if those credentials
        # aren't set, so a missing .env entry degrades gracefully instead
        # of leaving QQQ's live chart with no feed at all.
        if asset == "QQQ" and ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY:
            threading.Thread(target=_alpaca_trade_ws, args=(asset, _quit_evt), daemon=True).start()
        else:
            if asset == "QQQ":
                console_log(f"{YLW}ALPACA_API_ATHENA_ID/ALPACA_API_SECRET_KEY_ATHENA not set — "
                            f"QQQ live tape falling back to Phemex QQQUSDT{RST}")
            threading.Thread(target=_phemex_trade_ws, args=(asset, _quit_evt), daemon=True).start()
        # ETH's live tape aggregates Phemex + Kraken + Coinbase — matching
        # footprint.py's own multi-exchange source for ETH's closed bars
        # exactly, so a live bar isn't blind to a real print that happened
        # to land on Kraken/Coinbase first (see the module-level comment
        # above KRAKEN_WS_URL for the confirmed live discrepancy this fixes).
        if asset == "ETH":
            threading.Thread(target=_kraken_trade_ws, args=(asset, _quit_evt), daemon=True).start()
            threading.Thread(target=_coinbase_trade_ws, args=(asset, _quit_evt), daemon=True).start()
    threading.Thread(target=_fast_publish_loop, args=(_quit_evt,), daemon=True).start()

    rows, cols = stdscr.getmaxyx()
    db = DoubleBuffer(rows, cols)

    # Chart scroll/focus is purely a UI concern (which historical window
    # each pane shows) — lives only on this thread, never touches AppState
    # or the trading engine. hscroll_bars follows footprint.py's own "N
    # bars back from the newest" convention; 0 is always the live tail.
    chart_scroll = {"ETH": 0, "QQQ": 0}
    chart_focus = "ETH"
    dashboard_hidden = False   # [H] — gives the chart the ENTIRE terminal,
                               # same row budget footprint.py's own
                               # full-screen view uses, for when even the
                               # compacted DASHBOARD_H isn't enough room
    reset_armed = False
    flatten_armed = False   # [F] — arm-then-confirm, same convention as [R],
                             # for an action that closes real positions/cancels
                             # real orders when not in --dry-run
    data_view = False
    data_source = "sim" if DRY_RUN else "real"
    data_cache = {"source": None, "ts": 0.0, "events": [], "stats": None}
    trades_cache = {"source": None, "ts": 0.0, "trades": []}
    table_scroll = 0
    data_table_info = (0, 0)   # (visible_rows, total) — set from draw_data_view's
                               # own return each frame, so the footer's scroll
                               # hint never drifts from the table's real layout
    activity_log_open = False   # [L] — popup overlay over the FULL
                                 # EVENT_LOG_MAXLEN buffer, not just the
                                 # dashboard's own last-2-lines preview
    activity_log_scroll = 0

    while not _quit_evt.is_set():
        new_rows, new_cols = stdscr.getmaxyx()
        if (new_rows, new_cols) != (rows, cols):
            # windows-curses/PDCurses doesn't reliably resync the Windows
            # Console buffer via curses.resizeterm() alone — the working
            # pattern (per charthacker.py's own resize-bug history) is a
            # full endwin()+refresh() teardown/reinit, not the naive one.
            try:
                curses.endwin()
                stdscr.refresh()
            except curses.error:
                pass
            rows, cols = new_rows, new_cols
            db = DoubleBuffer(rows, cols)
            stdscr.clearok(True)

        snap = APP_STATE.snapshot()
        footer_row = rows - 1

        profile_mode = PROFILE_MODES[_profile_mode_idx[0]]
        both_panes = False

        if data_view:
            now_mono = time.time()
            if data_cache["source"] != data_source or now_mono - data_cache["ts"] > 2.0:
                events = scan_all_trade_events(data_source)
                data_cache = {"source": data_source, "ts": now_mono, "events": events,
                              "stats": compute_trade_stats(events)}
            if trades_cache.get("source") != data_source or now_mono - trades_cache["ts"] > 2.0:
                trades = scan_all_trades_detailed() if data_source == "sim" else []
                trades_cache = {"source": data_source, "ts": now_mono, "trades": trades}
            data_table_info = draw_data_view(db, rows, cols, data_source, data_cache["events"],
                                              data_cache["stats"], trades_cache["trades"], table_scroll)
        else:
            chart_top = 0 if dashboard_hidden else (DASHBOARD_H if rows - 1 > DASHBOARD_H + 10 else 0)
            if chart_top:
                draw_dashboard(db, snap, cols)
            chart_bottom = footer_row

            both_panes = chart_bottom - chart_top > 10 and not snap["qqq_market_closed"]
            if chart_bottom - chart_top > 10:
                eth_inst = snap["instruments"].get("ETH") or {}
                qqq_inst = snap["instruments"].get("QQQ") or {}
                eth_bars = snap["footprint_bars"].get("ETH") or []
                qqq_bars = snap["footprint_bars"].get("QQQ") or []
                eth_live = eth_inst.get("live_price") if eth_inst.get("live_price") is not None else eth_inst.get("price")
                qqq_live = qqq_inst.get("live_price") if qqq_inst.get("live_price") is not None else qqq_inst.get("price")
                eth_live_bar = snap["live_bars"].get("ETH")
                qqq_live_bar = snap["live_bars"].get("QQQ")
                if snap["qqq_market_closed"]:
                    draw_footprint_panel(db, "ETH", eth_bars, eth_inst, chart_top, chart_bottom, 0, cols,
                                          profile_mode, build_trade_markers("ETH", eth_bars, eth_inst, snap["trade_pairs"]),
                                          hscroll_bars=chart_scroll["ETH"], live_price=eth_live, live_bar=eth_live_bar)
                    db.puts(chart_top, max(0, cols - 12), "QQQ closed", P_DIM)
                else:
                    mid = cols // 2
                    draw_footprint_panel(db, "ETH", eth_bars, eth_inst, chart_top, chart_bottom, 0, mid,
                                          profile_mode, build_trade_markers("ETH", eth_bars, eth_inst, snap["trade_pairs"]),
                                          hscroll_bars=chart_scroll["ETH"], live_price=eth_live, live_bar=eth_live_bar,
                                          focused=(chart_focus == "ETH"))
                    draw_footprint_panel(db, "QQQ", qqq_bars, qqq_inst, chart_top, chart_bottom, mid, cols,
                                          profile_mode, build_trade_markers("QQQ", qqq_bars, qqq_inst, snap["trade_pairs"]),
                                          hscroll_bars=chart_scroll["QQQ"], live_price=qqq_live, live_bar=qqq_live_bar,
                                          focused=(chart_focus == "QQQ"))

        if activity_log_open:
            draw_activity_log_popup(db, rows, cols, snap["event_log"], activity_log_scroll)

        ts = datetime.now().strftime("%H:%M:%S")
        if flatten_armed:
            footer = (f"{ts}   {RED}{BLD}FLATTEN ALL — closes every position, cancels every order{RST}"
                      f"{YLW} — [F] again to CONFIRM, any other key to cancel{RST}")
            db.puts_ansi(footer_row, 0, footer.ljust(cols)[:cols])
        elif reset_armed:
            footer = f"{ts}   {YLW}Reset paper account — [R] again to enter a new balance, any other key to cancel{RST}"
            db.puts_ansi(footer_row, 0, footer.ljust(cols)[:cols])
        elif data_view:
            visible_rows, total = data_table_info
            scroll_tag = ""
            if total:
                shown = min(table_scroll + 1, total)
                shown_end = min(table_scroll + visible_rows, total)
                scroll_tag = f"  [{shown}-{shown_end} of {total}, ↑/↓ scroll table]"
            footer = f"{ts}   [D]ashboard   [S]witch source ({data_source}){scroll_tag}   [Q]uit"
            db.puts(footer_row, 0, footer.ljust(cols)[:cols], P_DIM)
        else:
            # [Tab] hint placed right after the scroll hint it's directly
            # related to (not appended at the very end) — the footer is
            # already long enough on a modest terminal that anything tacked
            # on last just gets silently truncated by the ljust/[:cols]
            # below, which is exactly what was hiding this hint before.
            tab_hint = f" [Tab]:{chart_focus}" if both_panes else ""
            footer = (f"{ts}  [Q]uit [A]:{'OFF' if not ATHENA_ENABLED else 'on'} [V]iew:{profile_mode}"
                      f" [←→]scroll{tab_hint} [Home]live"
                      f" [P]ct:{PCT:g}% [N]:{'24H' if NO_SESSION else 'sess'} [D]ata [L]og"
                      + ("  [R]eset" if DRY_RUN else "")
                      + "  [F]latten"
                      + ("  [H]:dash" if dashboard_hidden else "  [H]:hide"))
            db.puts(footer_row, 0, footer.ljust(cols)[:cols], P_DIM)

        db.flush(stdscr)
        stdscr.noutrefresh()
        curses.doupdate()

        key = stdscr.getch()
        if key == -1:
            continue

        if flatten_armed:
            flatten_armed = False
            if key in (ord("f"), ord("F")):
                _flatten_all_evt.set()
            continue

        if reset_armed:
            reset_armed = False
            if key in (ord("r"), ord("R")):
                current = get_sim_account().balance
                new_balance = _prompt_number(stdscr, f"New paper balance (current ${current:,.2f}, Enter keeps it): $",
                                              default=current)
                db.prev = None   # dialog wrote straight to stdscr, bypassing the DoubleBuffer — force a full repaint
                _reset_sim_balance[0] = new_balance
                _reset_sim_evt.set()
            continue

        if activity_log_open:
            if key in (ord("l"), ord("L"), 27, ord("q"), ord("Q")):
                activity_log_open = False
                if key in (ord("q"), ord("Q")):
                    _quit_evt.set()
            elif key == curses.KEY_UP:
                activity_log_scroll += 1
            elif key == curses.KEY_DOWN:
                activity_log_scroll = max(0, activity_log_scroll - 1)
            elif key == curses.KEY_PPAGE:
                activity_log_scroll += 10
            elif key == curses.KEY_NPAGE:
                activity_log_scroll = max(0, activity_log_scroll - 10)
            continue

        if data_view:
            if key in (ord("q"), ord("Q")):
                _quit_evt.set()
            elif key in (ord("d"), ord("D"), 27):   # [D] again or Esc — back to the dashboard
                data_view = False
            elif key in (ord("s"), ord("S")):
                data_source = "real" if data_source == "sim" else "sim"
                table_scroll = 0
            elif key == curses.KEY_UP:
                table_scroll += 1
            elif key == curses.KEY_DOWN:
                table_scroll = max(0, table_scroll - 1)
            elif key == curses.KEY_PPAGE:
                table_scroll += 10
            elif key == curses.KEY_NPAGE:
                table_scroll = max(0, table_scroll - 10)
            continue

        if key in (ord("q"), ord("Q")):
            _quit_evt.set()
        elif key in (ord("v"), ord("V")):
            _profile_mode_idx[0] = (_profile_mode_idx[0] + 1) % len(PROFILE_MODES)
        elif key in (ord("r"), ord("R")) and DRY_RUN:
            reset_armed = True
        elif key in (ord("f"), ord("F")):
            flatten_armed = True
        elif key in (ord("d"), ord("D")):
            data_view = True
        elif key in (ord("l"), ord("L")):
            activity_log_open = True
            activity_log_scroll = 0
        elif key in (ord("h"), ord("H")):
            dashboard_hidden = not dashboard_hidden
        elif key in (ord("n"), ord("N")):
            NO_SESSION = not NO_SESSION
            console_log(f"{YLW}Session requirement {'BYPASSED (24H mode)' if NO_SESSION else 'RE-ENABLED'} (via [N]){RST}")
            log_event("SYSTEM", "no_session_toggled", {"no_session": NO_SESSION})
        elif key in (ord("a"), ord("A")):
            ATHENA_ENABLED = not ATHENA_ENABLED
            # OFF blocks WATCHING->ARMED->confirm (no NEW entries) —
            # PENDING_FILL/IN_POSITION management keeps running regardless,
            # see process_cycle's own gating, so this is a genuine "stop
            # taking new signals" pause, not "abandon what's already open."
            console_log((f"{RED}{BLD}Athena PAUSED (via [A]) — no new entries; "
                          f"open positions still managed normally{RST}") if not ATHENA_ENABLED
                         else f"{GRN}{BLD}Athena RESUMED (via [A]){RST}")
            log_event("SYSTEM", "athena_enabled_toggled", {"enabled": ATHENA_ENABLED})
        elif key in (ord("p"), ord("P")):
            new_pct = _prompt_number(stdscr, f"New risk %% per trade (current {PCT:g}%, Enter keeps it): ", default=PCT)
            db.prev = None
            if new_pct is not None and new_pct != PCT:
                console_log(f"{YLW}Risk changed: {PCT:g}% -> {new_pct:g}% (via [P]){RST}")
                log_event("SYSTEM", "pct_changed", {"old": PCT, "new": new_pct})
                PCT = new_pct
        elif key == 9 and both_panes:   # Tab
            chart_focus = "QQQ" if chart_focus == "ETH" else "ETH"
        elif key == curses.KEY_LEFT:
            asset = chart_focus if both_panes else "ETH"
            max_back = max(0, len(snap["footprint_bars"].get(asset) or []) - 1)
            chart_scroll[asset] = min(max_back, chart_scroll[asset] + 1)
        elif key == curses.KEY_RIGHT:
            asset = chart_focus if both_panes else "ETH"
            chart_scroll[asset] = max(0, chart_scroll[asset] - 1)
        elif key in (curses.KEY_HOME, 27):   # Esc — same reset-to-live convention as footprint.py
            if both_panes:
                chart_scroll["ETH"] = chart_scroll["QQQ"] = 0
            else:
                chart_scroll["ETH"] = 0

if __name__ == "__main__":
    curses.wrapper(curses_main)
