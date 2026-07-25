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
#
# data/footprint/<Y>/<M>/<D>/footprint_<SYM>_90s_*.jsonl — this ONE used to be
# footprint.py's output too, but as of 2026-07-24 Athena's own LiveTape writes
# it directly (see LiveTape.ingest/_persist_closed_footprint_bar) in the exact
# same on-disk shape, so footprint.py no longer needs to run for Athena's
# Order Flow confirmation to work — it's now Athena's OWN file, just still
# read back the same way. Running footprint.py alongside Athena is harmless
# (same format) but no longer necessary. A REST backfill (_run_footprint_
# backfill, ported from footprint.py's own initialize_today/backfill_trades)
# runs once per asset at startup, on its own thread before that asset's live
# feed begins, so there's no meaningful cold-start gap either — same as
# footprint.py itself, not an approximation of it.
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
import shutil
import math
import bisect
import time
import hmac
import hashlib
import asyncio
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ModuleNotFoundError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"], stdout=subprocess.DEVNULL)
    import requests

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

def _in_entry_blackout():
    """No new entries 19:00-19:30 CT (explicit user request). Only gates
    the WATCHING->ARMED transition in process_cycle — PENDING_FILL/
    IN_POSITION handling is completely unaffected, same as ATHENA_ENABLED.
    If CT can't be resolved (no zoneinfo/tzdata), fail open rather than
    blocking entries all day over a missing tz database."""
    if TZ_CT is None:
        return False
    now = datetime.now(TZ_CT)
    return (now.hour, now.minute) >= (19, 0) and (now.hour, now.minute) < (19, 30)

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
MAG = '\033[95m'   # matches status.py's own MAG exactly — added for Phase 3c's
                    # Status screen (status.py's Volatility "Layer 2" tag is the
                    # only place this project needed magenta before now)

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

# REST endpoints for the startup backfill (distinct from the WS URLs above —
# ported from footprint.py's own REST historical-trades fetchers, see
# fetch_kraken_trades_range/fetch_coinbase_trades_range/fetch_alpaca_trades_
# range near LiveTape below). Kraken's REST pair format has no slash, unlike
# its WS pair (KRAKEN_ETH_PAIR above) — a separate constant, not reused.
KRAKEN_TRADES_URL     = 'https://api.kraken.com/0/public/Trades'
KRAKEN_ETH_TRADES_PAIR = 'ETHUSD'
COINBASE_REST_URL    = 'https://api.exchange.coinbase.com'
ALPACA_REST_URL      = 'https://data.alpaca.markets/v2'

SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
STATUS_LOG_DIR_BASE = os.path.join(SCRIPT_DIR, "status_logs")
FOOTPRINT_DATA_DIR  = os.path.join(SCRIPT_DIR, "data", "footprint")
ATHENA_LOG_DIR_BASE = os.path.join(SCRIPT_DIR, "athena_logs")
SIM_STATE_PATH      = os.path.join(SCRIPT_DIR, "sim_account.json")
SIM_LOG_DIR_BASE    = os.path.join(SCRIPT_DIR, "sim_logs")
SIM_DEFAULT_BALANCE = 10000.0
BLACKJACK_STATE_PATH = os.path.join(SCRIPT_DIR, "blackjack_state.json")

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
ASSET_BY_SYMBOL = {cfg["phemex_symbol"]: name for name, cfg in ASSETS.items()}

# ── "Blackjack" position-sizing mode ([B] toggle) ─────────────────────────────
# Loss progression 1R,1R,2R,3R,5R (R = balance*PCT/100, the SAME "1 unit of
# risk" the flat-PCT sizing already uses — Blackjack mode only changes HOW
# MANY R's are risked per trade, not what "1R" means). A loss advances one
# step forward through the sequence; a loss already at the max step (5R)
# wraps back to 1R. A win starts a 2-trade "win progression": the very next
# trade risks (the R-multiple ONE STEP BACK from wherever the sequence was) +
# (that win's own dollar profit) — e.g. at 3R with a $100 win, the next trade
# risks 2R + $100. If THAT trade also wins, two wins in a row, and the whole
# sequence resets to 1R. If it loses instead, the win progression ends and
# the loss ladder simply resumes at the level it was frozen at when the win
# started (3R in the example — losing the win-progression trade is not
# itself counted as an extra forward step; the next ordinary loss is what
# advances it further). Full spec confirmed with the user 2026-07-24 via
# AskUserQuestion — every branch above is a direct quote/paraphrase of their
# own worked example, not a guess.
BLACKJACK_STEPS = [1.0, 1.0, 2.0, 3.0, 5.0]
BLACKJACK_MODE = False

def _default_blackjack_state():
    return {a: {"loss_step": 0, "in_win_progression": False,
                "win_step_back": None, "win_profit_dollars": None} for a in ASSETS}

def _load_blackjack_state():
    try:
        with open(BLACKJACK_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        state = _default_blackjack_state()
        for a in ASSETS:
            if a in data:
                state[a].update(data[a])
        return state
    except Exception:
        return _default_blackjack_state()

def _save_blackjack_state():
    try:
        with open(BLACKJACK_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(BLACKJACK_STATE, f)
    except Exception:
        pass

BLACKJACK_STATE = _load_blackjack_state()

def _reset_blackjack_state():
    """CRITICAL bug fixed 2026-07-25, user-reported (QQQ still showing
    'win progression (2R + $39.81)' right after a paper-account reset,
    when both assets should start fresh at 1R): BLACKJACK_STATE is its
    own separate global, persisted to blackjack_state.json — neither
    SimAccount.reset() nor the [R]/--reset-sim flows ever touched it, so
    a wiped paper ledger still carried forward whatever loss/win
    progression each asset happened to be at. Called from both reset
    entry points (in-app [R] and the --reset-sim startup flag) right
    alongside SimAccount.reset()."""
    global BLACKJACK_STATE
    BLACKJACK_STATE = _default_blackjack_state()
    _save_blackjack_state()

# ── Daily Loss Limit (explicit user request, 2026-07-25) ─────────────────────
# 5 CONSECUTIVE losses on one asset blocks new entries on THAT asset until
# either PCVR's regime switches away from whatever it was when the limit was
# hit, or 19:30 CT passes — whichever comes first. Independent of
# BLACKJACK_MODE (tracked always, not just when Blackjack sizing is on), and
# hitting the limit ALSO resets that asset's own Blackjack loss progression
# back to 1R regardless of whether Blackjack mode is currently enabled — per
# the user's own explicit spec: "5 losses also means the loss progression
# automatically resets to 1R." Already-open positions are completely
# unaffected — this only gates the WATCHING->ARMED transition, same scope as
# ATHENA_ENABLED/_in_entry_blackout.
DAILY_LOSS_STATE_PATH = os.path.join(SCRIPT_DIR, "daily_loss_state.json")
DAILY_LOSS_LIMIT = 5

def _default_daily_loss_state():
    return {a: {"consecutive_losses": 0, "blocked": False, "blocked_regime": None, "blocked_at": None} for a in ASSETS}

def _load_daily_loss_state():
    try:
        with open(DAILY_LOSS_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        state = _default_daily_loss_state()
        for a in ASSETS:
            if a in data:
                state[a].update(data[a])
        return state
    except Exception:
        return _default_daily_loss_state()

def _save_daily_loss_state():
    try:
        with open(DAILY_LOSS_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(DAILY_LOSS_STATE, f)
    except Exception:
        pass

DAILY_LOSS_STATE = _load_daily_loss_state()

def _reset_daily_loss_state():
    """Same reasoning as _reset_blackjack_state — a fresh paper account
    via [R]/--reset-sim should start the daily risk-limit tracking fresh
    too, not carry forward a block (or a partial loss streak) from
    whatever the account was doing before the reset."""
    global DAILY_LOSS_STATE
    DAILY_LOSS_STATE = _default_daily_loss_state()
    _save_daily_loss_state()

def _next_1930_ct_after(dt_ct):
    """The next 19:30 CT instant strictly after dt_ct (a tz-aware CT
    datetime) — the "19:30 CT comes around" unblock condition. Reuses the
    real DST-aware TZ_CT zoneinfo already loaded at module import, not a
    fixed offset."""
    candidate = dt_ct.replace(hour=19, minute=30, second=0, microsecond=0)
    if candidate <= dt_ct:
        candidate += timedelta(days=1)
    return candidate

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

# ── Footprint startup backfill (--backfill-hours / --backfill-budget-secs) ──
# Ported from footprint.py's own CLI flags/defaults of the exact same name —
# see _footprint_session_start_ts/_run_footprint_backfill near LiveTape below.
def _footprint_session_start_ts(now=None):
    """The PREVIOUS calendar day's 00:00 — ported verbatim from
    footprint.py's own _session_start_ts (its docstring calls this '00:00
    CT' but the actual code just uses naive local system time, not an
    explicit CT conversion — mirrored exactly as written, not as
    labeled). Anchoring a full day back, not just today's own midnight,
    guarantees at least a full day of backfilled context on every
    startup, same reasoning footprint.py's own docstring gives."""
    now = now or datetime.now()
    yesterday = now - timedelta(days=1)
    return datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0).timestamp()

BACKFILL_HOURS = None
if "--backfill-hours" in args:
    i = args.index("--backfill-hours")
    try:
        BACKFILL_HOURS = max(0.0, float(args[i + 1]))
    except (IndexError, ValueError):
        pass
if BACKFILL_HOURS is None:
    BACKFILL_HOURS = max(0.0, (time.time() - _footprint_session_start_ts()) / 3600.0)

INITIAL_FOOTPRINT_BACKFILL_BUDGET_SECS = 30
if "--backfill-budget-secs" in args:
    i = args.index("--backfill-budget-secs")
    try:
        INITIAL_FOOTPRINT_BACKFILL_BUDGET_SECS = max(1.0, float(args[i + 1]))
    except (IndexError, ValueError):
        pass

DRY_RUN = "--dry-run" in args
NO_SESSION = "--no-session" in args
# [A] per-asset on/off toggle (acts on whichever pane is currently [Tab]-
# focused — explicit user request 2026-07-24 to control ETH/QQQ
# independently rather than one shared switch) — see process_cycle's own
# gating: OFF blocks WATCHING->ARMED->confirm (no NEW entries), but
# PENDING_FILL/IN_POSITION management (SL/TP fills, PCVR-flip-close,
# EOD-flatten) keeps running unconditionally either way — pausing must
# never mean abandoning an already-open position's own risk management.
#
# SAFETY REQUIREMENT (explicit user request 2026-07-25): starts False for
# EVERY asset whenever live (non-DRY_RUN) trading is what Athena is
# ACTUALLY about to do — whether that's launching `python athena.py`
# without --dry-run in the first place, or switching to live mid-session
# from the UI (see _go_live below, which re-applies this same rule at
# switch time). Athena must never place a single real-money trade until
# the user has manually reviewed the situation and pressed [A] for each
# asset they actually want trading — a paper-mode launch is unaffected,
# still starts enabled as before.
ATHENA_ENABLED = {a: DRY_RUN for a in ASSETS}
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
    os.makedirs(day_dir, exist_ok=True)   # now written by Athena itself
                                            # (Phase 3), not just read — was
                                            # a no-op-safe read-only path
                                            # before, when status.py alone
                                            # was responsible for creating it
    return os.path.join(day_dir, f"status_{dt.strftime('%m_%d_%Y')}.jsonl")

def read_last_status_snapshot():
    """Prefers Athena's own in-process status engine (STATUS_SNAPSHOT,
    populated by _status_snapshot_loop — see Phase 3 of the standalone-
    merge plan) over the on-disk file — no file round trip needed once
    that engine is running, same process. Falls back to the file when
    the in-memory value isn't populated yet (briefly at startup, before
    the first full refresh + snapshot cycle completes) or an external
    status.py process is also writing it."""
    with STATUS_STATE_LOCK:
        snap = STATUS_SNAPSHOT
    if snap is not None:
        return snap
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
    """Prefers Athena's own in-process GEX engine (GEX_EXPORT, populated
    by gex_export_status_snapshot — see _gex_engine_loop, Phase 2 of the
    standalone-merge plan) over the on-disk file — no file round trip
    needed once that engine is running, since it's the same process.
    Falls back to reading the file when the in-memory value isn't
    populated/fresh yet (e.g. briefly at startup before this asset's
    first GEX fetch completes) or an external gex.py/status.py process is
    also writing it — same GEX_EXPORT_MAX_AGE staleness rule either way,
    so callers never need to know which source actually served them."""
    with GEX_STATE_LOCK:
        exp = GEX_EXPORT.get(asset)
    if exp is not None and time.time() - exp.get("updated_at", 0) <= GEX_EXPORT_MAX_AGE:
        return exp
    path = os.path.join(SCRIPT_DIR, f"status_{asset}_gex.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("updated_at", 0) <= GEX_EXPORT_MAX_AGE:
            return data
    except Exception:
        pass
    return None

def magnitude_tier(net, scale_max):
    """Ported from status.py's own magnitude_tier (Medium 0.35-0.65
    fraction of session scale_max, Large >=0.65) — Athena reads gex.py's
    raw export directly (read_gex_export), not through status.py's
    periodic snapshot log, so it needs this to classify clusters itself,
    live, every cycle (see clusters_from_gex_export) instead of only ever
    seeing whatever cluster set status.py's own snapshot cadence happened
    to have baked in — that lag was why a cluster which had already
    crossed into Medium in gex.py's own live export could still be
    missing from Athena's target list for a cycle or more."""
    if scale_max <= 0:
        return None
    frac = min(1.0, abs(net) / scale_max)
    if frac < 0.35:
        return None
    elif frac < 0.65:
        return "Medium"
    else:
        return "Large"

def clusters_from_gex_export(gex_export, price, direction):
    """[(strike, tier), ...] for every Medium/Large cluster on `direction`
    ("above" or "below") of `price`, nearest-to-price first. Ported from
    status.py's all_clusters_from_gex_export/gamma_cluster_targets_
    directional, reading the same raw gex.py export Athena already pulls
    for GEX Flip (gex_by_strike + scale_max) — see magnitude_tier."""
    if not gex_export or price is None:
        return []
    scale_max = gex_export.get("scale_max") or 0.0
    by_strike = gex_export.get("gex_by_strike") or {}
    out = []
    for k_str, net in by_strike.items():
        strike = float(k_str)
        if direction == "above" and strike <= price:
            continue
        if direction == "below" and strike >= price:
            continue
        tier = magnitude_tier(float(net), scale_max)
        if tier:
            out.append((strike, tier))
    out.sort(key=lambda kt: abs(kt[0] - price))
    return out

def reconstruct_targets(log_targets, gex_export, price, regime):
    """[{'type','level'}, ...]: BT/ST, then every qualifying gamma
    Cluster (Large before Medium — explicit user request 2026-07-23),
    then GEX Flip last.

    Clusters are computed LIVE from this cycle's own gex_export via
    clusters_from_gex_export rather than read back out of status.py's
    log_targets (status.py's own snapshot log only refreshes every few
    seconds — computing them here, from the exact same raw export Athena
    already reads for GEX Flip, means a cluster that just crossed into
    Medium/Large this cycle is picked up immediately, and Medium ones are
    never silently dropped just because a couple were missing from
    whatever status.py had last written). log_targets is still the
    source for BT/ST (status.py fetches the live options chain for
    those; Athena doesn't replicate that call).

    GEX Flip is placed LAST, after every cluster (explicit user request
    2026-07-24, following from 'medium clusters are not ignored' — a real
    gamma cluster is an actual option-driven wall, GEX Flip a continuously
    recomputed synthetic level; with only 2 TP legs ever placed
    (_check_fill takes candidates[0]/[1]), GEX Flip sitting in the fixed
    2nd slot ahead of every cluster meant a cluster could get entered as
    the position's own primary target and then STILL never receive a TP
    leg. GEX Flip is the fallback target now, not the priority one — it
    already gets refreshed every cycle regardless (_sync_moving_tps), so
    losing a guaranteed slot doesn't make it any less trackable, just
    lower priority when a real cluster is also available)."""
    log_targets = log_targets or []
    full = []
    if log_targets and log_targets[0].get("type") in ("BT", "ST"):
        full.append({"type": log_targets[0]["type"], "level": float(log_targets[0]["level"])})

    if price is not None and regime in ("long", "short"):
        direction = "above" if regime == "long" else "below"
        clusters = clusters_from_gex_export(gex_export, price, direction)
        clusters.sort(key=lambda kt: 0 if kt[1] == "Large" else 1)
        for strike, _tier in clusters:
            full.append({"type": "Cluster", "level": strike})

    gex_flip = gex_export.get("gex_flip") if gex_export else None
    if gex_flip is not None:
        full.append({"type": "GEX Flip", "level": float(gex_flip)})
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
    targets_full = reconstruct_targets(log_targets, gex_export, price, regime)
    return lights, regime, price, targets_full, False

# ── footprint.py bar log — tail last CLOSED bar ───────────────────────────────
def footprint_log_paths(asset):
    """ALL matching footprint bar-log files across every day-folder,
    oldest-to-newest (the Y/M/D folder structure sorts correctly as plain
    strings, no mtime needed). Search the WHOLE data/footprint tree
    recursively rather than guessing how many days back to look — same
    reasoning footprint_log_glob's own history already established, just
    keeping every match instead of narrowing to one.

    CRITICAL bug fixed 2026-07-25, user-reported ("the footprint chart
    resets and I'm unable to see historical data" right at a local
    day-folder rollover): footprint_log_glob's old "single most-recently-
    modified file" model meant the instant the first bar of a new local
    day closed and _persist_closed_footprint_bar started writing a
    brand-new (near-empty) file, EVERY reader of it — the chart AND
    read_last_two_footprint_bars' own confirmation-logic comparison —
    switched over to that one new file and lost access to the entire
    previous day's bars in a single moment, even though they're still
    sitting right there on disk in the prior day-folder. Both callers
    below now walk across as many of these files as needed instead of
    ever trusting exactly one."""
    pattern = os.path.join(FOOTPRINT_DATA_DIR, "**",
                            f"footprint_{ASSETS[asset]['footprint_symbol']}_{FOOTPRINT_INTERVAL_LABEL}_*.jsonl")
    matches = glob.glob(pattern, recursive=True)
    matches.sort()
    return matches

def footprint_log_glob(asset):
    """The single newest matching file — kept for compat/simple callers;
    anything that needs bar HISTORY should use footprint_log_paths
    instead, since a lone file can't represent it correctly across a
    day-folder rollover (see footprint_log_paths' own docstring)."""
    paths = footprint_log_paths(asset)
    return paths[-1] if paths else None

def _footprint_log_path_for_bar(asset, bar_ts):
    """Same folder/filename convention footprint.py itself writes
    (data/footprint/<Y>/<M>/<D>/footprint_<SYM>_<INTERVAL>_<TICK>.jsonl,
    matching footprint_log_glob's own read pattern above) — but the
    day-folder is computed from the BAR's own timestamp, not wall-clock-
    at-call-time, so a bar that closes right at a local-midnight boundary
    always lands in the correct day's folder. Deliberately NOT
    footprint.py's own TODAY_STR-frozen-at-launch behavior — see
    footprint_log_glob's own docstring just above for why that's a bug
    worth avoiding here, not copying. Local time, same convention every
    other day-folder helper in this file already uses (sim_log_path/
    athena_log_path/status_log_path all key off datetime.now())."""
    dt = datetime.fromtimestamp(bar_ts)
    day_dir = os.path.join(FOOTPRINT_DATA_DIR, f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}")
    os.makedirs(day_dir, exist_ok=True)
    cfg = ASSETS[asset]
    return os.path.join(day_dir, f"footprint_{cfg['footprint_symbol']}_{FOOTPRINT_INTERVAL_LABEL}_{cfg['tick']}.jsonl")

def _persist_closed_footprint_bar(asset, bar):
    """Writes ONE closed bar in footprint.py's own persisted shape (see
    its _serialize_bar: ts/o/h/l/c/buy_vol/sell_vol/delta/tick/levels) so
    read_last_two_footprint_bars/footprint_log_glob keep working
    completely unchanged — they just end up reading files Athena itself
    now writes, not footprint.py. Deliberately excludes the "live" key —
    that's an Athena-only in-memory marker for the still-forming bar,
    footprint.py's own on-disk format never had it. Called from
    LiveTape.ingest() the moment a NEW bucket's first trade arrives,
    exactly mirroring footprint.py's own close trigger (a bar only closes
    when the next bucket's first trade lands, not a wall-clock timer —
    see LiveTape.ingest's own docstring)."""
    try:
        path = _footprint_log_path_for_bar(asset, bar["ts"])
        row = {"ts": bar["ts"], "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"],
               "buy_vol": bar.get("buy_vol", 0.0), "sell_vol": bar.get("sell_vol", 0.0),
               "delta": bar["delta"], "tick": bar["tick"],
               "levels": {str(k): list(v) for k, v in bar["levels"].items()}}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass

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
        footprint.py's own values for the same bar.

        A bucket rollover (the current bar's bucket differs from this
        trade's) closes and PERSISTS the outgoing bar to the same on-disk
        format/path footprint.py itself writes to (see
        _persist_closed_footprint_bar) — this is what lets Athena be its
        own source for read_last_two_footprint_bars/footprint_log_glob,
        no footprint.py process required. A trade for an OLDER bucket
        than the current live bar (a late/out-of-order print) is dropped
        outright rather than reopening/mutating a bar — footprint.py's
        own ingest_trade() does the exact same thing (see its
        `elif bucket_ts < live["ts"]: return`); this matters a lot more
        now that a rollover persists to disk than it did when a stray
        late trade could only ever corrupt one in-memory tick harmlessly."""
        bucket_ts = int(ts // FOOTPRINT_BAR_SECS) * FOOTPRINT_BAR_SECS
        with self.lock:
            bar = self.bars[asset]
            if bar is not None and bucket_ts < bar["ts"]:
                return
            if bar is None or bar["ts"] != bucket_ts:
                if bar is not None:
                    _persist_closed_footprint_bar(asset, bar)
                bar = {"ts": bucket_ts, "o": price, "h": price, "l": price, "c": price,
                       "delta": 0.0, "buy_vol": 0.0, "sell_vol": 0.0, "tick": tick, "levels": {}, "live": True}
            bar["h"] = max(bar["h"], price)
            bar["l"] = min(bar["l"], price)
            bar["c"] = price
            bar["tick"] = tick
            lvl = round(price / tick)
            cell = bar["levels"].setdefault(lvl, [0.0, 0.0])
            if is_buy:
                cell[1] += qty
                bar["buy_vol"] += qty
            else:
                cell[0] += qty
                bar["sell_vol"] += qty
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

# ── Footprint startup backfill (mirrors footprint.py's initialize_today) ────
# Ported from footprint.py's own REST historical-trades fetchers (its own
# docstrings/reasoning kept verbatim below) so LiveTape.ingest can be REPLAYED
# with real historical trades at startup, the exact same trick footprint.py's
# own backfill_trades()/initialize_today() use: replaying trades through the
# SAME ingest function a live print would go through means the bucket-close/
# persist-on-rollover logic already built for live trading reconstructs and
# writes every historical closed bar as a side effect, with zero separate
# "bar builder" needed. This is what closes the "no REST backfill" gap noted
# as an accepted tradeoff in Phase 1 of the standalone-merge plan — per
# explicit user request 2026-07-24 that every ported piece mirror its source
# script, not just approximate its behavior.
_coinbase_backfill_session = requests.Session()

def fetch_kraken_trades_range(pair, since_ts, until_ts, deadline=None):
    """Ported verbatim from footprint.py's fetch_kraken_trades_range —
    pages forward through Kraken's public Trades endpoint. deadline
    (time.time() cutoff) stops paging early, returning whatever's
    gathered so far rather than blocking indefinitely."""
    since = int(since_ts * 1e9)
    out = []
    last_id = None
    for _ in range(4000):
        if deadline and time.time() >= deadline:
            break
        try:
            r = requests.get(KRAKEN_TRADES_URL, params={"pair": pair, "since": since}, timeout=15)
            d = r.json()
        except Exception:
            break
        if d.get("error"):
            break
        keys = [k for k in d.get("result", {}) if k != "last"]
        if not keys:
            break
        trades = d["result"][keys[0]]
        if not trades:
            break
        hit_end = False
        for t in trades:
            tid = t[6]
            if last_id is not None and tid <= last_id:
                continue
            last_id = tid
            ts = float(t[2])
            if ts > until_ts:
                hit_end = True
                break
            out.append((ts, float(t[0]), float(t[1]), t[3] == "b"))
        if hit_end or trades[-1][2] >= until_ts:
            break
        since = int(d["result"]["last"])
        time.sleep(0.25)
    return out

def fetch_coinbase_trades_range(product_id, since_ts, until_ts, deadline=None):
    """Ported verbatim from footprint.py's fetch_coinbase_trades_range —
    pages backward through Coinbase Exchange's public trades endpoint (no
    key needed). Coinbase's `side` is the MAKER's side — flipped here so
    is_buy means "aggressive buy", matching Kraken/Phemex's convention."""
    out = []
    after_cursor = None
    for _ in range(8000):
        if deadline and time.time() >= deadline:
            break
        params = {"limit": 1000}
        if after_cursor is not None:
            params["after"] = after_cursor
        try:
            r = _coinbase_backfill_session.get(f"{COINBASE_REST_URL}/products/{product_id}/trades",
                                                params=params, timeout=15)
            page = r.json()
        except Exception:
            break
        if not isinstance(page, list) or not page:
            break
        hit_old_end = False
        for t in page:
            try:
                ts = _parse_rfc3339(t["time"])
                price = float(t["price"])
                qty = float(t["size"])
            except Exception:
                continue
            if ts > until_ts:
                continue
            if ts < since_ts:
                hit_old_end = True
                continue
            out.append((ts, price, qty, t["side"] == "sell"))
        after_cursor = r.headers.get("cb-after")
        if hit_old_end or not after_cursor:
            break
        time.sleep(0.2)
    out.reverse()
    return out

def _fetch_alpaca_trades_raw(symbol, since_ts, until_ts, deadline=None):
    """Ported verbatim from footprint.py's _fetch_alpaca_trades_raw —
    historical trade prints (IEX feed) — (ts, price, size), no side yet."""
    out = []
    page_token = None
    start_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(until_ts, tz=timezone.utc).isoformat()
    for _ in range(2000):
        if deadline and time.time() >= deadline:
            break
        params = {"symbols": symbol, "start": start_iso, "end": end_iso, "limit": 10000, "feed": "iex"}
        if page_token:
            params["page_token"] = page_token
        try:
            r = requests.get(f"{ALPACA_REST_URL}/stocks/trades", params=params,
                              headers={"APCA-API-KEY-ID": ALPACA_API_KEY_ID,
                                       "APCA-API-SECRET-KEY": ALPACA_API_SECRET_KEY}, timeout=15)
            d = r.json()
        except Exception:
            break
        page_trades = (d.get("trades") or {}).get(symbol) or []
        for t in page_trades:
            out.append((_parse_rfc3339(t["t"]), t["p"], t["s"]))
        page_token = d.get("next_page_token")
        if not page_token:
            break
        time.sleep(0.2)
    return out

def fetch_alpaca_trades_range(symbol, since_ts, until_ts, deadline=None):
    """Ported from footprint.py's fetch_alpaca_trades_range — historical/
    backfilled bars use TICK-RULE classification only (no quotes fetched
    here), deliberately, not an oversight: a single liquid symbol can
    generate hundreds of thousands of NBBO quote updates per hour, and
    paging through that just to classify a backfill window doesn't scale
    the way trade counts do. Live bars still get the more accurate
    quote-rule classification via _alpaca_trade_ws's own continuously-
    updated running quote, unaffected by this."""
    raw_trades = _fetch_alpaca_trades_raw(symbol, since_ts, until_ts, deadline=deadline)
    return classify_trades_quote_rule(raw_trades, [])

def _fetch_footprint_trades_range(asset, since_ts, until_ts, deadline=None):
    """Dispatch to whichever data source this asset actually uses —
    ported from footprint.py's fetch_trades_range (its own
    include_coinbase=False branch, used only by extend_history_backward,
    is out of scope here — see the standalone-merge plan for why that
    function isn't being ported). ETH: Kraken+Coinbase concurrently
    (Phemex has no historical trades API at all, matching footprint.py's
    own documented limitation), merge-sorted chronologically — NOT a
    plain concatenation, since LiveTape.ingest drops late/out-of-order
    prints for an already-closed bucket, so an unsorted replay would
    silently corrupt bars. QQQ: Alpaca."""
    if asset == "ETH":
        results = {}
        def _fetch_kraken():
            results["kraken"] = fetch_kraken_trades_range(KRAKEN_ETH_TRADES_PAIR, since_ts, until_ts, deadline=deadline)
        def _fetch_coinbase():
            results["coinbase"] = fetch_coinbase_trades_range(COINBASE_ETH_PRODUCT, since_ts, until_ts, deadline=deadline)
        t1 = threading.Thread(target=_fetch_kraken, daemon=True)
        t2 = threading.Thread(target=_fetch_coinbase, daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        kraken_trades = results.get("kraken", [])
        coinbase_trades = results.get("coinbase", [])
        starts = [since_ts]
        if kraken_trades:
            starts.append(kraken_trades[0][0])
        if coinbase_trades:
            starts.append(coinbase_trades[0][0])
        effective_since = max(starts)
        combined = [t for t in (kraken_trades + coinbase_trades) if t[0] >= effective_since]
        combined.sort(key=lambda t: t[0])
        return combined
    return fetch_alpaca_trades_range(ASSETS[asset]["footprint_symbol"], since_ts, until_ts, deadline=deadline)

def _run_footprint_backfill(asset):
    """Athena's own version of footprint.py's initialize_today(): prefer
    resuming from today's already-persisted bars (fetch only the GAP
    since the last one) over a full fresh backfill; only do a full
    BACKFILL_HOURS-back backfill when there's no usable log yet for
    today. Trades are replayed through LIVE_TAPE.ingest() itself, in
    chronological order — the exact same closed-bar persist-on-rollover
    mechanism a live trade triggers reconstructs and writes every
    historical closed bar, so no separate bar-building step is needed
    here (footprint.py's own backfill replays through ingest_trade() for
    the identical reason).

    Deliberately NOT ported: footprint.py's extend_history_backward()/
    fill_equity_gap() (scroll-triggered incremental backfill for its own
    interactive deep-history browsing) — Athena's confirmation logic only
    ever needs the last two closed bars, it has no analogous "user is
    scrolling near the loaded edge" trigger to hang an incremental fetch
    off of. See the standalone-merge plan for this scope boundary."""
    if asset != "ETH" and not (ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY):
        # QQQ's backfill has no viable data source without Alpaca creds —
        # footprint.py itself has no Phemex-backfill fallback either (its
        # historical-trades gap is Alpaca-only for equities); the
        # Phemex-WS fallback below is a live-feed-only accommodation,
        # doesn't extend to backfill.
        LIVE_TAPE.set_status(asset, "no backfill — Alpaca credentials not set")
        return
    cfg = ASSETS[asset]
    tick = cfg["tick"]
    deadline = time.time() + INITIAL_FOOTPRINT_BACKFILL_BUDGET_SECS
    today_path = _footprint_log_path_for_bar(asset, time.time())
    existing = []
    if os.path.isfile(today_path):
        try:
            with open(today_path, encoding="utf-8") as f:
                existing = [json.loads(l) for l in f if l.strip()]
        except Exception:
            existing = []

    now = time.time()
    until_ts = now if asset == "ETH" else now - 900   # equities: 15-min REST delay, matches footprint.py

    if existing:
        since_ts = existing[-1]["ts"]
        if until_ts <= since_ts:
            LIVE_TAPE.set_status(asset, "resumed from today's log")
            return
        trades = _fetch_footprint_trades_range(asset, since_ts, until_ts, deadline=deadline)
        for ts, price, qty, is_buy in trades:
            LIVE_TAPE.ingest(asset, ts, price, qty, is_buy, tick)
        LIVE_TAPE.set_status(asset, f"resumed, caught up {len(trades)} trades")
        console_log(f"{asset}: resumed {len(existing)} bars from today's log, caught up {len(trades)} newer trades")
        return

    if BACKFILL_HOURS <= 0:
        LIVE_TAPE.set_status(asset, "starting fresh — no backfill")
        return
    since_ts = now - BACKFILL_HOURS * 3600.0
    trades = _fetch_footprint_trades_range(asset, since_ts, until_ts, deadline=deadline)
    if trades:
        for ts, price, qty, is_buy in trades:
            LIVE_TAPE.ingest(asset, ts, price, qty, is_buy, tick)
        src = "Kraken+Coinbase" if asset == "ETH" else "Alpaca (IEX)"
        since_str = datetime.fromtimestamp(since_ts).strftime('%H:%M:%S')
        LIVE_TAPE.set_status(asset, f"backfilled {len(trades)} trades")
        console_log(f"{asset}: backfilled {len(trades)} {src} trades since {since_str}")
    else:
        LIVE_TAPE.set_status(asset, "starting fresh — backfill returned nothing")

def _backfill_then_feeds(asset, feed_starters):
    """Runs this asset's REST backfill to completion on its OWN dedicated
    thread — mirrors footprint.py's own sequencing (initialize_today()
    blocks, THEN start_feeds() begins — see its call site) so live trades
    for this asset never start being ingested until the backfill has
    finished catching up, avoiding a race where a live trade for a much
    newer bucket arrives first and causes the late-trade guard to drop
    older backfilled trades that hadn't been replayed yet. Deliberately
    NOT run on the curses main thread the way footprint.py blocks ITS
    single curses screen during backfill — Athena runs two assets at
    once and its curses UI must stay responsive regardless, so each
    asset gets its own thread instead of blocking startup for
    everything. feed_starters is the list of (target, args) to start as
    their own additional threads once the backfill completes."""
    try:
        _run_footprint_backfill(asset)
    except Exception as e:
        console_log(f"{asset}: backfill failed ({e}) — starting live feed anyway")
    for target, fargs in feed_starters:
        threading.Thread(target=target, args=fargs, daemon=True).start()

# ── GEX engine (mirrors gex.py — Phase 2 of the standalone-merge plan) ──────
# Ported from gex.py's own fetch/GEX-math/persistence/export layer
# (gex.py:139-733, confirmed curses-free) so Athena no longer needs a
# separate `gex.py` process running per asset. gex.py's SYMBOL/IS_CRYPTO/
# MULT/BAND_PCT are mutable MODULE GLOBALS reassigned by its own in-app
# symbol switch — Athena instead runs TWO independent instances of this
# engine at once (one per asset, in the SAME process), so every function
# below takes asset/is_crypto/mult/band_pct as explicit parameters instead
# of reading module globals — that's the one structural adaptation this
# port needs; the actual fetch/math/persistence logic itself is ported
# close to verbatim.
GEX_DERIBIT_BASE_URL = "https://www.deribit.com/api/v2"
GEX_CBOE_URL         = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
GEX_YAHOO_CHART_URL  = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
_GEX_YAHOO_HEADERS   = {"User-Agent": "Mozilla/5.0"}

GEX_BS_SWEEP_PCT    = 0.20   # Black-Scholes hypothetical-spot sweep for the
GEX_BS_SWEEP_POINTS = 61     # GEX Flip — how far/how many points, see build_bs_gex_curve

GEX_REFRESH_SEC    = 60    # matches gex.py's own default --interval
GEX_SMOOTH_N       = 5     # matches gex.py's own default --smooth-n
GEX_DIAG_THRESHOLD = 30.0  # matches gex.py's own default --diag-threshold
GEX_HISTORY_MAXLEN = 1500  # matches gex.py's own HISTORY_MAXLEN (~25h of 1-min columns)

GEX_LOG_DIR_BASE = os.path.join(SCRIPT_DIR, "logs")   # SAME "logs/" folder
                    # name gex.py itself uses (not ATHENA_LOG_DIR_BASE/
                    # SIM_LOG_DIR_BASE's own naming) — so a real gex.py
                    # instance run standalone against the same folder later
                    # shares/continues the exact same history files.

def _gex_deribit_api(path, **params):
    """Ported verbatim from gex.py's own api()."""
    r = requests.get(GEX_DERIBIT_BASE_URL + path, params=params, timeout=12)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"]["message"])
    return j["result"]

def _gex_fetch_ticker(name):
    return name, _gex_deribit_api("/public/ticker", instrument_name=name)

def _gex_countdown(ts_ms):
    """Ported verbatim from gex.py's own countdown()."""
    ms = ts_ms - int(time.time() * 1000)
    if ms <= 0:
        return "EXPIRED"
    h, rem = divmod(ms // 1000, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def bs_gamma(S, K, T, sigma):
    """Ported verbatim from gex.py's own bs_gamma — Black-Scholes gamma
    (identical formula for calls and puts) at spot S, strike K, time-to-
    expiry T (years), annualized vol sigma (decimal). r=0, a standard
    simplification for short-dated options where the rate barely moves d1."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * S * sigma * math.sqrt(T))

def build_bs_gex_curve(contracts, spot, mult, n_points=GEX_BS_SWEEP_POINTS, sweep_pct=GEX_BS_SWEEP_PCT):
    """Ported verbatim from gex.py's own build_bs_gex_curve — the real
    Zero Gamma/GEX Flip calc: re-prices every contract's gamma at a sweep
    of hypothetical spot levels via Black-Scholes and returns the
    resulting [(hyp_spot, total_net_gex), ...] curve. contracts: [(strike,
    "call"|"put", oi, iv_decimal, T_years), ...]."""
    lo, hi = spot * (1 - sweep_pct), spot * (1 + sweep_pct)
    step = (hi - lo) / (n_points - 1) if n_points > 1 else 0.0
    curve = []
    for i in range(n_points):
        S = lo + step * i
        total = 0.0
        for strike, otype, oi, iv, T in contracts:
            gamma = bs_gamma(S, strike, T, iv)
            gex = gamma * oi * mult * S * S * 0.01
            total += -gex if otype == "put" else gex
        curve.append((S, total))
    return curve

def _gex_find_nearest_zero_crossing(points, ref):
    """Ported verbatim from gex.py's own _find_nearest_zero_crossing.
    points: [(x, y), ...] sorted ascending by x. Returns (x_lo, x_hi,
    interpolated_x) for the y-crossing nearest ref, or None."""
    if len(points) < 2:
        return None
    crossings = []
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        if y0 == 0:
            crossings.append((x0, x0, x0))
        elif (y0 < 0) != (y1 < 0):
            level = x0 + (x1 - x0) * (-y0 / (y1 - y0))
            crossings.append((x0, x1, level))
    if not crossings:
        return None
    return min(crossings, key=lambda c: abs(c[2] - ref))

def compute_max_pain(oi_by_type):
    """Ported verbatim from gex.py's own compute_max_pain — strike where
    total intrinsic payout to option holders is minimized at expiry."""
    if not oi_by_type:
        return None
    items = list(oi_by_type.items())
    best_strike, best_payout = None, None
    for k in oi_by_type:
        payout = 0.0
        for s, oi in items:
            c_oi, p_oi = oi.get("call", 0.0), oi.get("put", 0.0)
            if k > s:
                payout += (k - s) * c_oi
            elif k < s:
                payout += (s - k) * p_oi
        if best_payout is None or payout < best_payout:
            best_payout, best_strike = payout, k
    return best_strike

def smoothed_max_pain_and_flip(history, end_idx, n):
    """Ported verbatim from gex.py's own smoothed_max_pain_and_flip —
    averages Max Pain and the GEX-flip level over the last n raw columns
    ending at end_idx, to absorb single-snapshot OI/IV artifacts."""
    window = history[max(0, end_idx - n):end_idx]
    if not window:
        return None, None
    mps, levels = [], []
    for c in window:
        mp = compute_max_pain(c.get("oi_by_type"))
        if mp is not None:
            mps.append(mp)
        fl = _gex_find_nearest_zero_crossing(c.get("bs_gex_curve") or [], c["spot"])
        if fl:
            levels.append(fl[2])
    smoothed_mp = sum(mps) / len(mps) if mps else None
    smoothed_level = sum(levels) / len(levels) if levels else None
    return smoothed_mp, smoothed_level

def gex_bounding_strikes(sorted_strikes, level):
    """Ported verbatim from gex.py's own bounding_strikes."""
    i = bisect.bisect_left(sorted_strikes, level)
    if i <= 0:
        return sorted_strikes[0], sorted_strikes[0]
    if i >= len(sorted_strikes):
        return sorted_strikes[-1], sorted_strikes[-1]
    if sorted_strikes[i] == level:
        return sorted_strikes[i], sorted_strikes[i]
    return sorted_strikes[i - 1], sorted_strikes[i]

def gex_resolve_marker_row(lo_s, hi_s, row_of):
    """Ported verbatim from gex.py's own resolve_marker_row."""
    if lo_s == hi_s:
        return row_of.get(lo_s)
    if hi_s in row_of and lo_s in row_of:
        return row_of[hi_s] + 1
    return None

def gex_resolve_marker_col(lo_s, hi_s, col_of):
    """Ported verbatim from gex.py's own resolve_marker_col."""
    if lo_s == hi_s:
        return col_of.get(lo_s)
    if lo_s in col_of and hi_s in col_of:
        return (col_of[lo_s] + col_of[hi_s]) // 2
    return None

def gex_ingest_column(col, grid, band_pct):
    """Ported from gex.py's own ingest_column, parameterized on band_pct
    instead of a global. Grows `grid` (the strike universe) in place and
    tags col with its nearest-to-spot strike. Returns (col,
    local_max_abs_gex) — caller folds local_max into scale_max."""
    spot = col["spot"]
    band = spot * band_pct
    grid.update(s for s in col["gex"] if abs(s - spot) <= band)
    col["nearest"] = min(grid, key=lambda s: abs(s - spot)) if grid else None
    local_max = max((abs(v) for v in col["gex"].values()), default=0.0)
    return col, local_max

def fstrike(strike, is_crypto):
    """Ported verbatim from gex.py's own fstrike, parameterized on
    is_crypto instead of a global."""
    if is_crypto:
        return f"${strike:,.0f}"
    return f"${strike:,.2f}"

def fdollars_compact(v):
    """Ported verbatim from gex.py's own fdollars_compact — compact
    signed dollar format for totals that can run into the billions."""
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e9:
        return f"{sign}${v / 1e9:,.2f}B"
    if v >= 1e6:
        return f"{sign}${v / 1e6:,.2f}M"
    if v >= 1e3:
        return f"{sign}${v / 1e3:,.2f}K"
    return f"{sign}${v:,.2f}"

def magnitude_char(net, scale_max, floor_char=" "):
    """Ported verbatim from gex.py's own magnitude_char — size-tier
    character for |net| as a fraction of scale_max."""
    if scale_max <= 0:
        return floor_char, 0.0
    frac = min(1.0, abs(net) / scale_max)
    if frac < 0.04:
        return floor_char, frac
    elif frac < 0.15:
        return ".", frac
    elif frac < 0.35:
        return "o", frac
    elif frac < 0.65:
        return "O", frac
    else:
        return "●", frac

def _gex_fetch_live_price(symbol):
    """Ported verbatim from gex.py's own fetch_live_price — best-effort
    near-real-time quote via Yahoo's v8 chart 'meta' block, used only to
    display/position price for an equity whose options feed (CBOE) is
    itself ~15m delayed. Returns None on any failure — caller falls back
    to the options feed's own reference price."""
    try:
        r = requests.get(GEX_YAHOO_CHART_URL.format(symbol), headers=_GEX_YAHOO_HEADERS,
                          params={"interval": "1m", "range": "1d"}, timeout=6)
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        return float(price) if price else None
    except Exception:
        return None

def _gex_cboe_time_to_expiry_years(exp_str):
    """Ported verbatim from gex.py's own _cboe_time_to_expiry_years.
    exp_str: 'YYMMDD'. Treats expiry as market close (15:00 local)."""
    exp_date = datetime.strptime(exp_str, "%y%m%d").date()
    close_dt = datetime(exp_date.year, exp_date.month, exp_date.day, 15, 0, 0)
    seconds = (close_dt - datetime.now()).total_seconds()
    return max(seconds, 60.0) / 86400.0 / 365.0

def fetch_gex_crypto(currency, mult, all_exp=False):
    """Ported from gex.py's own fetch_eth, parameterized on mult instead
    of a global MULT (Deribit — real-time gamma+OI read directly off each
    ticker)."""
    instruments = _gex_deribit_api("/public/get_instruments", currency=currency, kind="option", expired="false")
    now_ms = int(time.time() * 1000)
    by_exp = {}
    for ins in instruments:
        by_exp.setdefault(ins["expiration_timestamp"], []).append(ins)

    if all_exp:
        chain_ins = instruments
        target_exp = None
    else:
        target_exp = min((e for e in by_exp if e > now_ms), default=None)
        if not target_exp:
            raise RuntimeError("No active expiry found")
        chain_ins = by_exp[target_exp]

    with ThreadPoolExecutor(max_workers=40) as ex:
        fut_index = ex.submit(_gex_deribit_api, "/public/get_index_price", index_name=f"{currency.lower()}_usd")
        ticker_futs = {ex.submit(_gex_fetch_ticker, ins["instrument_name"]): ins for ins in chain_ins}
        spot = fut_index.result()["index_price"]
        tickers = {}
        for fut in as_completed(ticker_futs):
            try:
                name, t = fut.result()
                tickers[name] = t
            except Exception:
                pass

    strikes = {}
    oi_by_type = {}
    gex_by_type = {}
    contracts = []
    for ins in chain_ins:
        t = tickers.get(ins["instrument_name"])
        if not t:
            continue
        greeks = t.get("greeks") or {}
        gamma = greeks.get("gamma") or 0.0
        oi = t.get("open_interest") or 0.0
        gex = gamma * oi * mult * spot * spot * 0.01
        otype = ins["option_type"]
        if otype == "put":
            gex = -gex
        strikes[ins["strike"]] = strikes.get(ins["strike"], 0.0) + gex
        oi_by_type.setdefault(ins["strike"], {"call": 0.0, "put": 0.0})[otype] += oi
        gex_by_type.setdefault(ins["strike"], {"call": 0.0, "put": 0.0})[otype] += gex

        iv_pct = t.get("mark_iv") or 0.0
        exp_ms = ins["expiration_timestamp"]
        T = max(exp_ms - now_ms, 60_000) / 1000.0 / 86400.0 / 365.0
        if oi > 0 and iv_pct > 0:
            contracts.append((ins["strike"], otype, oi, iv_pct / 100.0, T))

    bs_gex_curve = build_bs_gex_curve(contracts, spot, mult)

    if target_exp:
        exp_label = datetime.fromtimestamp(target_exp / 1000, tz=timezone.utc).strftime("%d%b%y").upper()
        ttl = _gex_countdown(target_exp)
    else:
        exp_label = f"ALL({len(by_exp)})"
        ttl = None

    return {"spot": spot, "strikes": strikes, "oi_by_type": oi_by_type, "gex_by_type": gex_by_type,
            "bs_gex_curve": bs_gex_curve, "expiry_label": exp_label, "ttl": ttl,
            "fetched_at": datetime.now().strftime("%H:%M:%S")}

def fetch_gex_equity(symbol, mult, all_exp=False):
    """Ported from gex.py's own fetch_equity, parameterized on mult
    instead of a global MULT (CBOE delayed-quotes feed, gamma+OI read
    directly off each contract — no Black-Scholes needed for the raw
    per-strike GEX, only for the Flip sweep)."""
    r = requests.get(GEX_CBOE_URL.format(symbol), timeout=15)
    r.raise_for_status()
    data = r.json().get("data") or {}
    price = float(data.get("current_price") or 0)
    if price <= 0:
        raise RuntimeError("no spot price")

    today = datetime.now().strftime("%y%m%d")
    by_exp = {}
    for o in data.get("options") or []:
        name = o.get("option") or ""
        if len(name) < 15:
            continue
        cp_flag = name[-9]
        exp = name[-15:-9]
        try:
            strike = int(name[-8:]) / 1000.0
        except ValueError:
            continue
        by_exp.setdefault(exp, []).append((cp_flag, strike, o))
    if not by_exp:
        raise RuntimeError("empty chain")

    if all_exp:
        target_exps = sorted(by_exp)
    elif today in by_exp:
        target_exps = [today]
    else:
        future = [e for e in by_exp if e >= today]
        target_exps = [min(future)] if future else [min(by_exp)]

    strikes = {}
    oi_by_type = {}
    gex_by_type = {}
    contracts = []
    for exp in target_exps:
        T_exp = _gex_cboe_time_to_expiry_years(exp)
        for cp_flag, strike, o in by_exp[exp]:
            gamma = float(o.get("gamma") or 0)
            oi = float(o.get("open_interest") or 0)
            gex = gamma * oi * mult * price * price * 0.01
            if cp_flag == "P":
                gex = -gex
            strikes[strike] = strikes.get(strike, 0.0) + gex
            otype = "call" if cp_flag == "C" else "put"
            oi_by_type.setdefault(strike, {"call": 0.0, "put": 0.0})[otype] += oi
            gex_by_type.setdefault(strike, {"call": 0.0, "put": 0.0})[otype] += gex

            iv = float(o.get("iv") or 0)
            if oi > 0 and iv > 0:
                contracts.append((strike, otype, oi, iv, T_exp))

    bs_gex_curve = build_bs_gex_curve(contracts, price, mult)

    is_0dte = (not all_exp) and target_exps[0] == today
    exp_label = target_exps[0] if len(target_exps) == 1 else f"ALL({len(target_exps)})"

    live_price = _gex_fetch_live_price(symbol)
    spot = live_price if live_price else price

    return {"spot": spot, "spot_is_live": live_price is not None, "cboe_ref_price": price,
            "strikes": strikes, "oi_by_type": oi_by_type, "gex_by_type": gex_by_type,
            "bs_gex_curve": bs_gex_curve, "expiry_label": exp_label,
            "ttl": None, "is_0dte": is_0dte, "fetched_at": datetime.now().strftime("%H:%M:%S")}

def fetch_gex_snapshot(asset, is_crypto, mult):
    """Ported from gex.py's own fetch_snapshot — dispatches to whichever
    data source this asset actually uses. `asset` doubles as both the
    Deribit currency ("ETH") and the CBOE ticker ("QQQ") since Athena's
    own ASSETS keys already match both conventions directly."""
    if is_crypto:
        return fetch_gex_crypto(asset, mult)
    return fetch_gex_equity(asset, mult)

# ── GEX persistence — logs/<Y>/<M>/<D>/gex_<ASSET>_<date>.jsonl ─────────────
# SAME "logs/" folder gex.py itself writes to (GEX_LOG_DIR_BASE, defined
# above) — a standalone gex.py instance pointed at this same directory later
# shares/continues the exact same history, same as footprint.py's log
# convention from Phase 1.
def _gex_date_folder(*roots, date_str):
    """Ported verbatim from gex.py's own _date_folder."""
    mm, dd, yyyy = date_str.split("_")
    folder = os.path.join(GEX_LOG_DIR_BASE, *roots, yyyy, mm, dd)
    os.makedirs(folder, exist_ok=True)
    return folder

def gex_log_path(asset, date_str):
    folder = _gex_date_folder(date_str=date_str)
    return os.path.join(folder, f"gex_{asset}_{date_str}.jsonl")

def gex_append_log(asset, col):
    """Ported from gex.py's own append_log. Returns (ok, err_str_or_None)."""
    try:
        with open(gex_log_path(asset, datetime.now().strftime("%m_%d_%Y")), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": col["ts"].isoformat(), "spot": col["spot"], "gex": col["gex"],
                "oi_by_type": col.get("oi_by_type") or {},
                "gex_by_type": col.get("gex_by_type") or {},
                "bs_gex_curve": col.get("bs_gex_curve") or [],
                "expiry_label": col.get("expiry_label"), "is_0dte": col.get("is_0dte"),
            }) + "\n")
        return True, None
    except Exception as e:
        return False, str(e)

def gex_diag_log_path(asset, date_str):
    folder = _gex_date_folder(date_str=date_str)
    return os.path.join(folder, f"gex_diag_{asset}_{date_str}.jsonl")

def gex_append_diag(asset, prev_col, prev_level, new_col, new_level):
    """Ported from gex.py's own append_diag — logs a raw-flip jump
    exceeding GEX_DIAG_THRESHOLD with full before/after OI+gamma."""
    try:
        with open(gex_diag_log_path(asset, datetime.now().strftime("%m_%d_%Y")), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": new_col["ts"].isoformat(), "prev_ts": prev_col["ts"].isoformat(),
                "delta": new_level - prev_level,
                "prev_flip_level": prev_level, "new_flip_level": new_level,
                "prev_spot": prev_col["spot"], "new_spot": new_col["spot"],
                "prev_gex": prev_col["gex"], "new_gex": new_col["gex"],
                "prev_oi_by_type": prev_col.get("oi_by_type"), "new_oi_by_type": new_col.get("oi_by_type"),
            }) + "\n")
        return True, None
    except Exception as e:
        return False, str(e)

def gex_check_flip_jump(asset, prev_col, new_col):
    """Ported from gex.py's own check_flip_jump — compares raw
    (unsmoothed) flip levels between two consecutive columns; if the jump
    exceeds GEX_DIAG_THRESHOLD, logs it and returns a short status
    message, else None."""
    if GEX_DIAG_THRESHOLD <= 0:
        return None
    prev_flip = _gex_find_nearest_zero_crossing(prev_col.get("bs_gex_curve") or [], prev_col["spot"])
    new_flip = _gex_find_nearest_zero_crossing(new_col.get("bs_gex_curve") or [], new_col["spot"])
    if not prev_flip or not new_flip:
        return None
    delta = new_flip[2] - prev_flip[2]
    if abs(delta) < GEX_DIAG_THRESHOLD:
        return None
    gex_append_diag(asset, prev_col, prev_flip[2], new_col, new_flip[2])
    sign = "+" if delta > 0 else "-"
    is_crypto = (asset == "ETH")
    return f"flip jumped {sign}{fstrike(abs(delta), is_crypto)} @ {new_col['ts'].strftime('%H:%M:%S')} (logged)"

def gex_load_log(asset, date_str):
    """Ported from gex.py's own load_log — reads a day's log back into
    column dicts (no 'nearest' yet — gex_ingest_column adds it)."""
    path = gex_log_path(asset, date_str)
    cols = []
    if not os.path.exists(path):
        return cols
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                cols.append({
                    "ts": datetime.fromisoformat(d["ts"]), "spot": d["spot"],
                    "gex": {float(k): v for k, v in d["gex"].items()},
                    "oi_by_type": {float(k): v for k, v in (d.get("oi_by_type") or {}).items()},
                    "gex_by_type": {float(k): v for k, v in (d.get("gex_by_type") or {}).items()},
                    "bs_gex_curve": d.get("bs_gex_curve") or [],
                    "expiry_label": d.get("expiry_label"), "is_0dte": d.get("is_0dte"),
                })
            except Exception:
                continue
    return cols

# ── GEX in-process state (replaces the file round-trip a separate gex.py
# process needed — read_gex_export now prefers this directly; see below) ──
GEX_HISTORY = {a: deque(maxlen=GEX_HISTORY_MAXLEN) for a in ASSETS}
GEX_GRID    = {a: set() for a in ASSETS}
GEX_SCALE_MAX = {a: 0.0 for a in ASSETS}
GEX_META    = {a: {} for a in ASSETS}
GEX_EXPORT  = {a: None for a in ASSETS}   # export_status_snapshot's own payload shape, kept in memory
GEX_STATUS  = {a: "connecting…" for a in ASSETS}   # same convention as LIVE_TAPE.status
GEX_LOG_ROWS = {a: 0 for a in ASSETS}
GEX_DIAG_COUNT = {a: 0 for a in ASSETS}
GEX_STATE_LOCK = threading.Lock()

def _gex_snapshot_state(asset):
    """A fully independent copy of this asset's GEX state, for the
    render thread — same convention as LiveTape.snapshot() (the WS/fetch
    thread keeps mutating the live originals after this returns, so the
    consumer must never hold a reference into them)."""
    with GEX_STATE_LOCK:
        return (list(GEX_HISTORY[asset]), set(GEX_GRID[asset]), GEX_SCALE_MAX[asset], dict(GEX_META[asset]))

def _gex_atm_idx(asset):
    """Ported from gex.py's own _atm_idx — index of the strike nearest
    the latest spot, used to seed vert_center_idx the moment the user
    starts scrolling away from auto-center."""
    with GEX_STATE_LOCK:
        grid, history = GEX_GRID[asset], GEX_HISTORY[asset]
        if not grid or not history:
            return 0
        gs = sorted(grid)
        spot_now = history[-1]["spot"]
    return min(range(len(gs)), key=lambda i: abs(gs[i] - spot_now))

def _gex_vert_step(asset, delta, vert_follow, vert_center_idx):
    """Ported from gex.py's own _vert_step — moves vert_center_idx[asset]
    by delta strikes, seeding it from the currently auto-centered strike
    the first time (so scrolling starts from wherever you're already
    looking, not index 0). Shared by the interval map's ↑/↓/[/] (vertical)
    and the by-strike chart's ←/→/PgUp/PgDn (horizontal) — same "which
    strike is centered" state either way, just rendered differently.
    vert_follow/vert_center_idx are the per-asset dicts from curses_main
    (mutated in place — gex.py's own version closes over plain locals,
    Athena needs the dict-per-asset indirection instead)."""
    if vert_follow[asset]:
        vert_center_idx[asset] = _gex_atm_idx(asset)
    vert_follow[asset] = False
    with GEX_STATE_LOCK:
        grid_len = len(GEX_GRID[asset])
    vert_center_idx[asset] = max(0, min(max(0, grid_len - 1), vert_center_idx[asset] + delta))

def gex_export_status_snapshot(asset, col, scale_max, flip_level):
    """Ported from gex.py's own export_status_snapshot — SAME payload
    shape (`{asset, updated_at, spot, scale_max, gex_by_strike,
    gex_flip}`), but updates Athena's own in-memory GEX_EXPORT[asset]
    directly (what read_gex_export now actually reads — no file round
    trip needed for Athena's own consumption) AND still writes the file,
    for compat with any external process (status.py, or gex.py itself run
    standalone) that might also want to read it. Best-effort — a write
    failure here must never interrupt the fetch loop."""
    payload = {"asset": asset, "updated_at": time.time(), "spot": col["spot"],
               "scale_max": scale_max, "gex_by_strike": col.get("gex") or {},
               "gex_flip": flip_level}
    with GEX_STATE_LOCK:
        GEX_EXPORT[asset] = payload
    try:
        path = os.path.join(SCRIPT_DIR, f"status_{asset}_gex.json")
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_path, path)
    except Exception:
        pass

def _gex_load_today(asset):
    """Ported from gex.py's own _load_into (called at curses_main
    startup there) — resumes today's already-logged columns into
    GEX_HISTORY/GEX_GRID/GEX_SCALE_MAX/GEX_META before the live fetch
    loop starts, same expiry-rollover scale_max reset and scale_at_ingest
    freezing gex.py's own version does. Best-effort: a corrupt/missing
    log just leaves state empty, the first live fetch fills it in."""
    grid = GEX_GRID[asset]
    history = GEX_HISTORY[asset]
    is_crypto = (asset == "ETH")
    band_pct = 0.20 if is_crypto else 0.12
    grid.clear()
    history.clear()
    scale_max = 0.0
    last_expiry = None
    for c in gex_load_log(asset, datetime.now().strftime("%m_%d_%Y")):
        c, local_max = gex_ingest_column(c, grid, band_pct)
        if last_expiry is not None and c.get("expiry_label") != last_expiry:
            scale_max = 0.0
        last_expiry = c.get("expiry_label")
        scale_max = max(scale_max, local_max)
        c["scale_at_ingest"] = scale_max
        history.append(c)
    GEX_SCALE_MAX[asset] = scale_max
    if history:
        last = history[-1]
        GEX_META[asset] = {"spot": last["spot"], "expiry_label": last.get("expiry_label") or "—",
                            "is_0dte": last.get("is_0dte"), "fetched_at": last["ts"].strftime("%H:%M:%S")}
    GEX_LOG_ROWS[asset] = len(history)

def _gex_engine_loop(asset, stop_evt):
    """Athena's per-asset background GEX engine — runs on its OWN plain
    thread (not asyncio), mirroring gex.py's own headless_main() loop
    shape AND execution model almost exactly: gex.py has zero asyncio
    anywhere in it, and even its live curses mode fetches on a plain
    thread, never asyncio (see its trigger_fetch/do_fetch) — so a plain
    thread here is the faithful mirror, not a wrapped asyncio task.
    fetch -> log -> diag-check -> scale_max update (with expiry-rollover
    reset, same as gex.py) -> smoothed export, on a GEX_REFRESH_SEC
    cadence, forever."""
    is_crypto = (asset == "ETH")
    mult = 1 if is_crypto else 100
    band_pct = 0.20 if is_crypto else 0.12
    _gex_load_today(asset)
    GEX_STATUS[asset] = "live" if GEX_HISTORY[asset] else "connecting…"
    prev_col = list(GEX_HISTORY[asset])[-1] if GEX_HISTORY[asset] else None
    while not stop_evt.is_set():
        t0 = time.time()
        ts = datetime.now()
        try:
            d = fetch_gex_snapshot(asset, is_crypto, mult)
            col = {"ts": ts, "spot": d["spot"], "gex": d["strikes"],
                   "oi_by_type": d.get("oi_by_type") or {},
                   "gex_by_type": d.get("gex_by_type") or {},
                   "bs_gex_curve": d.get("bs_gex_curve") or [],
                   "expiry_label": d.get("expiry_label"),
                   "is_0dte": d.get("is_0dte", True if is_crypto else False)}
            with GEX_STATE_LOCK:
                grid = GEX_GRID[asset]
                col, local_max = gex_ingest_column(col, grid, band_pct)
                if prev_col is not None and prev_col.get("expiry_label") != col.get("expiry_label"):
                    GEX_SCALE_MAX[asset] = 0.0
                GEX_SCALE_MAX[asset] = max(GEX_SCALE_MAX[asset], local_max)
                col["scale_at_ingest"] = GEX_SCALE_MAX[asset]
                GEX_HISTORY[asset].append(col)
                GEX_META[asset] = {k: v for k, v in d.items() if k != "strikes"}
                hist_list = list(GEX_HISTORY[asset])
                scale_max = GEX_SCALE_MAX[asset]
            ok, err = gex_append_log(asset, col)
            GEX_LOG_ROWS[asset] = GEX_LOG_ROWS[asset] + 1 if ok else GEX_LOG_ROWS[asset]
            if prev_col is not None:
                msg = gex_check_flip_jump(asset, prev_col, col)
                if msg:
                    GEX_DIAG_COUNT[asset] += 1
                    console_log(f"{asset}: GEX {msg}")
            try:
                _mp, _flip = smoothed_max_pain_and_flip(hist_list, len(hist_list), GEX_SMOOTH_N)
            except Exception:
                _flip = None
            gex_export_status_snapshot(asset, col, scale_max, _flip)
            GEX_STATUS[asset] = "live"
            prev_col = col
        except Exception as e:
            GEX_STATUS[asset] = f"error: {e}"[:60]
        elapsed = time.time() - t0
        stop_evt.wait(max(1.0, GEX_REFRESH_SEC - elapsed))

STATUS_REFRESH_SEC = 30          # matches status.py's own default --interval
STATUS_LIVE_PRICE_INTERVAL = 2   # matches status.py's own LIVE_PRICE_INTERVAL
STATUS_SNAPSHOT_INTERVAL = 3     # matches status.py's own SNAPSHOT_INTERVAL —
                                  # "feeds athena.py, so the cadence is a
                                  # functional dependency, not just a
                                  # backtesting convenience" is now literally
                                  # Athena itself, same statement as before

STATUS_CT_OFFSET = timedelta(hours=-5)

def _status_now_ct():
    return datetime.now(timezone.utc) + STATUS_CT_OFFSET

STATUS_KILL_ZONES = [
    ('NDO',         0,    210,  'CYN'),
    ('Morning',     510,  630,  'YLW'),
    ('Lunchtime',   690,  810,  'YLW'),
    ('Power Hour',  840,  900,  'YLW'),
    ('EOD',         960,  1080, 'YLW'),
    ('EEOD',        1110, 1440, 'YLW'),
]
STATUS_EXCL_DAYS_09    = {2, 3}
STATUS_EXCL_START      = 540
STATUS_EXCL_END        = 600
STATUS_EXCL_SUN        = 6
STATUS_EXCL_EEOD_START = 1110
STATUS_TLT_WINDOW_START = 8 * 60 + 45
STATUS_TLT_WINDOW_END   = 15 * 60
STATUS_QQQ_OPEN_CT_MIN  = 8 * 60 + 45
STATUS_QQQ_CLOSE_CT_MIN = 15 * 60

def status_get_session_status():
    n = _status_now_ct()
    t_mins = n.hour * 60 + n.minute
    dow = n.weekday()
    excl_reason = None
    if dow == STATUS_EXCL_SUN:
        excl_reason = 'Sunday — no trading'
    elif dow in STATUS_EXCL_DAYS_09 and STATUS_EXCL_START <= t_mins < STATUS_EXCL_END:
        excl_reason = 'Excluded (09:00-10:00)'
    elif t_mins >= STATUS_EXCL_EEOD_START:
        excl_reason = 'EEOD — no trading'
    for name, start, end, _col in STATUS_KILL_ZONES:
        if start <= t_mins < end:
            return name, excl_reason
    return None, excl_reason

def status_in_tlt_window():
    n = _status_now_ct()
    t_mins = n.hour * 60 + n.minute
    is_weekday = n.weekday() < 5
    return is_weekday and STATUS_TLT_WINDOW_START <= t_mins < STATUS_TLT_WINDOW_END

def status_session_open_ts():
    n = datetime.now()
    today_open = datetime(n.year, n.month, n.day, 19, 0, 0)
    if n < today_open:
        curr = today_open - timedelta(days=1)
    else:
        curr = today_open
    prev = curr - timedelta(days=1)
    return prev.timestamp(), curr.timestamp()

def status_qqq_market_closed():
    n = _status_now_ct()
    if n.weekday() >= 5:
        return True
    minutes = n.hour * 60 + n.minute
    return not (STATUS_QQQ_OPEN_CT_MIN <= minutes < STATUS_QQQ_CLOSE_CT_MIN)

STATUS_DVOL_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"

def fetch_status_dvol(ccy="ETH"):
    try:
        now_ms = int(time.time() * 1000)
        r = requests.get(STATUS_DVOL_URL, params={
            "currency": ccy, "start_timestamp": now_ms - 7200000,
            "end_timestamp": now_ms, "resolution": "3600",
        }, timeout=8)
        data = (r.json().get("result") or {}).get("data") or []
        if data:
            return float(data[-1][4])
    except Exception:
        pass
    return None

def status_dvol_layers(dvol):
    if dvol is None:
        return "n/a", "n/a"
    if dvol <= 60.00:
        return "Full base unit", "5R cap"
    elif dvol <= 75.00:
        return "75% base unit", "3R cap"
    elif dvol <= 90.00:
        return "50% base unit", "2R cap"
    else:
        return "25% base unit", "1R cap"

STATUS_DERIBIT_BASE = "https://www.deribit.com/api/v2"

def _status_deribit_api(path, **params):
    r = requests.get(STATUS_DERIBIT_BASE + path, params=params, timeout=12)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"]["message"])
    return j["result"]

def fetch_status_deribit_chain(currency):
    """Ported verbatim from status.py's own fetch_deribit_chain — nearest-
    expiry option chain: {"spot", "strikes": {strike: {"call": ticker,
    "put": ticker}}}, ticker being the raw Deribit ticker dict (stats.
    volume/greeks.gamma/open_interest all present) — distinct from Phase
    2's fetch_gex_crypto, which returns pre-aggregated net GEX per strike,
    not raw per-leg tickers; BT/ST here needs the per-leg call/put volume
    split gex.py's own shape doesn't carry."""
    instruments = _status_deribit_api("/public/get_instruments", currency=currency, kind="option", expired="false")
    now_ms = int(time.time() * 1000)
    by_exp = {}
    for ins in instruments:
        by_exp.setdefault(ins["expiration_timestamp"], []).append(ins)
    target_exp = min((e for e in by_exp if e > now_ms), default=None)
    if not target_exp:
        raise RuntimeError(f"No active {currency} expiry found")
    chain_ins = by_exp[target_exp]

    with ThreadPoolExecutor(max_workers=40) as ex:
        fut_index = ex.submit(_status_deribit_api, "/public/get_index_price", index_name=f"{currency.lower()}_usd")
        ticker_futs = {ex.submit(_status_deribit_api, "/public/ticker", instrument_name=ins["instrument_name"]): ins
                       for ins in chain_ins}
        spot = fut_index.result()["index_price"]
        tickers = {}
        for fut, ins in ticker_futs.items():
            try:
                tickers[ins["instrument_name"]] = fut.result()
            except Exception:
                pass

    strikes = {}
    for ins in chain_ins:
        t = tickers.get(ins["instrument_name"])
        if not t:
            continue
        strikes.setdefault(ins["strike"], {})[ins["option_type"]] = t

    return {"spot": spot, "strikes": strikes, "expiry_ts": target_exp}

STATUS_CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
STATUS_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
_STATUS_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

def fetch_status_yahoo_meta(symbol):
    r = requests.get(STATUS_YAHOO_CHART_URL.format(symbol), headers=_STATUS_YAHOO_HEADERS,
                      params={"interval": "1m", "range": "1d"}, timeout=8)
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
    return (float(price) if price else None,
            float(prev_close) if prev_close else None)

def fetch_status_vxn():
    try:
        price, _prev = fetch_status_yahoo_meta("^VXN")
        return price
    except Exception:
        return None

STATUS_PHEMEX_TICKER_URL = "https://api.phemex.com/md/v3/ticker/24hr"

def fetch_status_eth_live_price():
    try:
        r = requests.get(STATUS_PHEMEX_TICKER_URL, params={"symbol": "ETHUSDT"}, timeout=6)
        r.raise_for_status()
        result = r.json().get("result") or {}
        price = result.get("lastRp") or result.get("markRp")
        return float(price) if price is not None else None
    except Exception:
        return None

def fetch_status_cboe_chain(symbol):
    r = requests.get(STATUS_CBOE_URL.format(symbol), timeout=15)
    r.raise_for_status()
    data = r.json().get("data") or {}
    ref_price = float(data.get("current_price") or 0)
    if ref_price <= 0:
        raise RuntimeError(f"no spot price for {symbol}")
    try:
        iv30 = float(data.get("iv30"))
    except (TypeError, ValueError):
        iv30 = 0.0

    today = datetime.now().strftime("%y%m%d")
    by_exp = {}
    for o in data.get("options") or []:
        name = o.get("option") or ""
        if len(name) < 15:
            continue
        cp_flag = name[-9]
        exp = name[-15:-9]
        try:
            strike = int(name[-8:]) / 1000.0
        except ValueError:
            continue
        by_exp.setdefault(exp, []).append((cp_flag, strike, o))
    if not by_exp:
        raise RuntimeError(f"empty chain for {symbol}")

    if today in by_exp:
        target_exp = today
    else:
        future = sorted(e for e in by_exp if e >= today)
        target_exp = future[0] if future else min(by_exp)

    strikes = {}
    for cp_flag, strike, o in by_exp[target_exp]:
        otype = "call" if cp_flag == "C" else "put"
        strikes.setdefault(strike, {})[otype] = o

    live_price, _ = (None, None)
    try:
        live_price, _ = fetch_status_yahoo_meta(symbol)
    except Exception:
        pass
    spot = live_price if live_price else ref_price

    return {"spot": spot, "cboe_ref_price": ref_price, "iv30": iv30, "strikes": strikes,
            "expiry_label": target_exp}

STATUS_DERIBIT_BOOK_SUMMARY_URL = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"

def fetch_status_deribit_pcvr(currency):
    r = requests.get(STATUS_DERIBIT_BOOK_SUMMARY_URL, params={"currency": currency, "kind": "option"}, timeout=12)
    r.raise_for_status()
    instruments = r.json().get("result") or []
    put_vol = call_vol = 0.0
    for inst in instruments:
        name = inst.get("instrument_name", "")
        volume = float(inst.get("volume") or 0)
        if volume == 0:
            continue
        suffix = name.split("-")[-1]
        if suffix == "P":
            put_vol += volume
        elif suffix == "C":
            call_vol += volume
    return put_vol, call_vol

def fetch_status_cboe_pcvr(symbol):
    r = requests.get(STATUS_CBOE_URL.format(symbol), timeout=15)
    r.raise_for_status()
    options = (r.json().get("data") or {}).get("options") or []
    put_vol = call_vol = 0.0
    for o in options:
        name = o.get("option") or ""
        if len(name) < 15:
            continue
        cp = name[-9]
        try:
            vol = float(o.get("volume") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        if cp == "P":
            put_vol += vol
        elif cp == "C":
            call_vol += vol
    return put_vol, call_vol

def fetch_status_pcvr():
    if status_in_tlt_window():
        underlying = "TLT"
        put_vol, call_vol = fetch_status_cboe_pcvr("TLT")
    else:
        underlying = "BTC"
        put_vol, call_vol = fetch_status_deribit_pcvr("BTC")
    ratio = (put_vol / call_vol) if call_vol > 0 else 0.0
    return {"underlying": underlying, "put_vol": put_vol, "call_vol": call_vol, "ratio": ratio}

STATUS_BT_ST_BAND_PCT = {"crypto": 0.20, "equity": 0.12}
STATUS_BT_ST_MAX_STRIKES_FROM_ATM = 10
STATUS_BT_ST_RUN_LEN = {"crypto": 3, "equity": 5}

def compute_bt_st(strikes, is_crypto, pcvr_gt1, spot):
    """Ported verbatim from status.py's own compute_bt_st — see its own
    docstring there for the full spec. Scans outward from the strike
    nearest spot for the first consecutive-strike run where one side's
    option volume dominates; returns (bt, st, active)."""
    band_pct = STATUS_BT_ST_BAND_PCT["crypto" if is_crypto else "equity"]
    run_len = STATUS_BT_ST_RUN_LEN["crypto" if is_crypto else "equity"]
    lo_bound, hi_bound = spot * (1 - band_pct), spot * (1 + band_pct)
    sorted_strikes = sorted(k for k in strikes.keys() if lo_bound <= k <= hi_bound)
    n = len(sorted_strikes)
    if n < run_len:
        return None, None, ("BT" if pcvr_gt1 else "ST")

    def vols(k):
        legs = strikes.get(k, {})
        call = legs.get("call"); put = legs.get("put")
        if is_crypto:
            cv = float((call.get("stats") or {}).get("volume") or 0) if call else 0.0
            pv = float((put.get("stats") or {}).get("volume") or 0) if put else 0.0
        else:
            cv = float(call.get("volume") or 0) if call else 0.0
            pv = float(put.get("volume") or 0) if put else 0.0
        return cv, pv

    def run_ok(start, want_put):
        for idx in range(start, start + run_len):
            cv, pv = vols(sorted_strikes[idx])
            if want_put and not (pv > cv):
                return False
            if not want_put and not (cv > pv):
                return False
        return True

    atm_idx = min(range(n), key=lambda idx: abs(sorted_strikes[idx] - spot))
    max_start = n - run_len
    if max_start < 0:
        return None, None, ("BT" if pcvr_gt1 else "ST")

    lo_start = max(0, atm_idx - STATUS_BT_ST_MAX_STRIKES_FROM_ATM)
    hi_start = min(max_start, atm_idx + STATUS_BT_ST_MAX_STRIKES_FROM_ATM)

    candidates = []
    for s in range(lo_start, hi_start + 1):
        top = s + run_len - 1
        if top <= atm_idx:
            near_idx, dist = top, atm_idx - top
        elif s >= atm_idx:
            near_idx, dist = s, s - atm_idx
        else:
            near_idx, dist = atm_idx, 0
        candidates.append((dist, s, near_idx))
    candidates.sort(key=lambda c: c[0])

    want_put = bool(pcvr_gt1)
    for _dist, s, near_idx in candidates:
        if run_ok(s, want_put):
            anchor = sorted_strikes[near_idx]
            if want_put:
                st = anchor
                bt = sorted_strikes[near_idx + 1] if near_idx + 1 < n else None
                return bt, st, "BT"
            else:
                bt = anchor
                st = sorted_strikes[near_idx - 1] if near_idx - 1 >= 0 else None
                return bt, st, "ST"

    return None, None, ("BT" if pcvr_gt1 else "ST")

STATUS_PHEMEX_KLINE_LIST_URL = "https://api.phemex.com/exchange/public/md/v2/kline/list"

def fetch_status_phemex_session_candles(symbol="ETHUSDT", resolution=60):
    """Ported verbatim from status.py's own fetch_phemex_session_candles —
    oldest-first [(ts,o,h,l,c,v), ...] spanning the current session's
    19:00 CT open through now."""
    _prev_open_ts, curr_open_ts = status_session_open_ts()
    now_ts = int(time.time())
    minutes = max(1, int((now_ts - curr_open_ts) / resolution) + 5)
    limit = min(2000, minutes)
    r = requests.get(STATUS_PHEMEX_KLINE_LIST_URL, params={
        "symbol": symbol, "resolution": resolution,
        "from": int(curr_open_ts), "to": now_ts, "limit": limit,
    }, timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(d.get("msg") or "phemex kline error")
    rows = (d.get("data") or {}).get("rows") or []
    out = []
    for row in rows:
        ts, _interval, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
        out.append((int(ts), float(o), float(h), float(l), float(c), float(v)))
    return out

def status_session_prev_eod_close(candles, curr_open_ts):
    """Ported verbatim from status.py's own session_prev_eod_close —
    close of the 15:59 CT candle the day before curr_open_ts."""
    target_ts = curr_open_ts - 3 * 3600 - 60
    before = [c for c in candles if c[0] <= target_ts]
    return before[-1][4] if before else None

def fetch_status_yahoo_candles(symbol, rng="5d", interval="1m"):
    r = requests.get(STATUS_YAHOO_CHART_URL.format(symbol), headers=_STATUS_YAHOO_HEADERS,
                      params={"interval": interval, "range": rng, "includePrePost": "true"}, timeout=12)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts_list = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(ts_list):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, l, c):
            continue
        out.append((int(ts), float(o), float(h), float(l), float(c), float(v or 0)))
    return out

STATUS_VP_BUCKETS = 200

def status_compute_vp(candles):
    """Ported verbatim from status.py's own compute_vp (its own docstring
    notes: "same bucketing as charthacker.py's compute_vp") — REST-based
    fallback used until Phase 3b's charthacker WS layer is built (and
    still used as the fallback afterward, exactly mirroring status.py's
    own dual-path design — see evaluate_hpls)."""
    if not candles:
        return None
    s_lo = min(c[3] for c in candles)
    s_hi = max(c[2] for c in candles)
    s_range = s_hi - s_lo
    if s_range <= 0:
        return None
    vp = [0.0] * STATUS_VP_BUCKETS

    def ptb(p):
        return max(0, min(STATUS_VP_BUCKETS - 1, int((p - s_lo) / s_range * (STATUS_VP_BUCKETS - 1))))

    for _ts, o, h, l, c, v in candles:
        body_hi, body_lo = max(o, c), min(o, c)
        wv = v * 0.5
        bw, bh = ptb(l), ptb(h)
        sw = max(1, bh - bw + 1)
        for b in range(bw, bh + 1):
            vp[b] += wv / sw
        bv = v * 0.5
        bl, bb = ptb(body_lo), ptb(body_hi)
        sb = max(1, bb - bl + 1)
        if sb > 1:
            for b in range(bl, bb + 1):
                vp[b] += bv / sb
        else:
            vp[ptb(c)] += bv

    mx = max(vp)
    tv = sum(vp)
    pi = vp.index(mx)
    poc_p = s_lo + (pi / (STATUS_VP_BUCKETS - 1)) * s_range

    tgt = tv * 0.70
    acc = vp[pi]
    lo_b = hi_b = pi
    while acc < tgt:
        ab = vp[hi_b + 1] if hi_b < STATUS_VP_BUCKETS - 1 else 0.0
        bb2 = vp[lo_b - 1] if lo_b > 0 else 0.0
        if ab == 0 and bb2 == 0:
            break
        if ab >= bb2:
            hi_b += 1; acc += ab
        else:
            lo_b -= 1; acc += bb2

    vah_p = s_lo + (hi_b / (STATUS_VP_BUCKETS - 1)) * s_range
    val_p = s_lo + (lo_b / (STATUS_VP_BUCKETS - 1)) * s_range
    return poc_p, vah_p, val_p

def status_compute_vwap_sd(candles):
    """Ported verbatim from status.py's own compute_vwap_sd — same
    cumulative Welford formula as charthacker.py's own."""
    cum_tpv = cum_vol = cum_dev2 = 0.0
    vwap = sd = None
    for _ts, o, h, l, c, v in candles:
        tp = (h + l + c) / 3.0
        old_vol = cum_vol
        cum_tpv += tp * v
        cum_vol += v
        if cum_vol > 0:
            vw = cum_tpv / cum_vol
            if old_vol > 0:
                old_vw = (cum_tpv - tp * v) / old_vol
                cum_dev2 += v * (tp - old_vw) * (tp - vw)
            var = max(0.0, cum_dev2 / cum_vol)
            vwap, sd = vw, var ** 0.5
    return vwap, sd

def status_compute_er(open_price, iv):
    if not open_price or not iv or iv <= 0:
        return None
    daily_move = iv / math.sqrt(365) / 100.0
    dist = open_price * daily_move
    return {
        "upper": {p: open_price + dist * (p / 100.0) for p in (40, 80, 100, 150)},
        "lower": {p: open_price + dist * (-p / 100.0) for p in (40, 80, 100, 150)},
    }

STATUS_CHARTHACKER_EXPORT_MAX_AGE = 30

def read_status_charthacker_export(asset, max_age_sec=STATUS_CHARTHACKER_EXPORT_MAX_AGE):
    """Ported from status.py's own read_charthacker_export, adapted to the
    same "in-memory first, file fallback" pattern read_gex_export/
    read_last_status_snapshot already use (Phases 1-3a) — CH_EXPORT[asset]
    is populated by Athena's own CH engine (see _ch_export_loop below,
    Phase 3b), the exact thing this function used to always miss before
    that engine existed. The file read stays as a fallback for compat with
    any external process, same convention as every prior phase."""
    with CH_EXPORT_LOCK:
        data = CH_EXPORT.get(asset)
    if data and time.time() - data.get("updated_at", 0) <= max_age_sec:
        return data
    path = os.path.join(SCRIPT_DIR, f"status_{asset}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("updated_at", 0) <= max_age_sec:
            return data
    except Exception:
        pass
    return None

# ── CH engine (Phase 3b of the standalone-merge plan) — charthacker.py's
# own live-WS-fed VAH/VAL/POC/VWAP/SD-band tracking, ported to supersede
# the REST-based status_compute_vp/status_compute_vwap_sd fallback above
# (per the user's explicit preference, confirmed via AskUserQuestion before
# Phase 1 even started: "I want charthacker's live-WS accuracy"). One
# background engine per asset, mirroring charthacker.py's own "one instance
# handles one asset" model — ETH mirrors ws_phemex (charthacker.py:888-
# 1067), QQQ mirrors ws_yahoo (charthacker.py:685-769, itself just a 5s
# Yahoo REST poll — charthacker has no true WS for equities either, so this
# is a faithful mirror of charthacker's own "live" for QQQ, not a downgrade
# from the ETH path). compute_vp/build_vwap_map are ported near-verbatim
# from the closures charthacker.py nests inside draw() (charthacker.py:
# 2958-3020, 3190-3220) — see each function's own docstring for the exact,
# explicitly-reasoned adaptations made lifting them out of that scope.
#
# Deliberately NOT populated in the export here: "iv" and "prev_eod_close".
# In charthacker.py both are sourced from the SAME upstream endpoints
# (Deribit DVOL / CBOE IV for er_iv via bt_st_gex_loop; Yahoo's 15:59 CT
# candle for prev_eod_close) that Athena's OWN status engine (Phase 3a)
# already fetches independently every cycle and passes into evaluate_hpls
# directly. Re-fetching either here would be pure duplication of a value
# that's already fresh via a path Athena already owns — exactly the same
# reasoning Phase 3a used to skip status.py's redundant gamma-cluster
# fallback. evaluate_hpls's `export.get("iv")`/`export.get("prev_eod_close")`
# guards already fall through cleanly to the status-engine-supplied values
# whenever those keys are absent, so this is a genuine scope boundary, not
# a silently-broken field.
class CH_AssetState:
    def __init__(self):
        self.lock = threading.Lock()
        self.candles = deque(maxlen=1600)   # >24h of 1m bars —
                                                           # only ever need
                                                           # this session's
                                                           # worth (filtered
                                                           # at read time),
                                                           # older entries
                                                           # just age out
        self.last_price = 0.0
        self.indicator_levels = {}   # mirrors state.indicator_levels' own
                                       # persistent-merge semantics — see
                                       # _ch_export_loop

class CH_Candle:
    __slots__ = ("ts", "o", "h", "l", "c", "v", "closed")
    def __init__(self, ts, o, h, l, c, v, closed=False):
        self.ts, self.o, self.h, self.l, self.c, self.v, self.closed = \
            int(ts), float(o), float(h), float(l), float(c), float(v), closed

CH_STATE = {"ETH": CH_AssetState(), "QQQ": CH_AssetState()}
CH_EXPORT_LOCK = threading.Lock()
CH_EXPORT = {"ETH": None, "QQQ": None}
CH_EXPORT_INTERVAL = 5   # matches charthacker.py's own STATUS_EXPORT_INTERVAL

def _ch_apply_resampled(ch_state, closed, live):
    """Simplified from charthacker.py's own _apply_resampled (charthacker.py:
    230-265): charthacker keeps a separate state.live pointer for the
    still-forming bar purely so draw() can render it with different visual
    styling. compute_vp/build_vwap_map never read Candle.closed anywhere in
    either algorithm, so that distinction carries no information this
    engine needs — this collapses both into one deque, always overwriting
    the last entry in place when the ts matches (a forming bar updating)
    and appending only on a genuinely newer bucket. Still enforces the
    exact same strict-monotonicity guard charthacker.py's own version does
    (a real bug it fixed 2026-07-10: a WS reconnect racing a resampler
    reseed produced non-monotonic candles) — dropping anything that would
    move backward, regardless of the exact race that produced it.

    charthacker.py's own _Resampler (charthacker.py:149-206) is NOT ported
    at all: it exists solely to group multiple BASE-resolution candles into
    a wider DISPLAY resolution for charthacker's own [I]-selectable
    interval UI. Athena's CH engine has no such UI — it always runs at the
    fixed 60s resolution both Phemex/Yahoo natively stream — so base
    resolution == display resolution always, and the resampler is provably
    a no-op at that ratio (verified by hand before dropping it: its
    "closed" output for a superseded bucket is always a same-ts, same-OHLCV
    restatement of what was already the deque's last entry, and its "live"
    output is always the raw incoming candle itself). Callers below feed
    raw kline/poll rows straight through this function instead.

    Caller must already hold ch_state.lock."""
    for c in (closed, live):
        if c is None:
            continue
        if ch_state.candles and ch_state.candles[-1].ts == c.ts:
            ch_state.candles[-1] = c
        elif not ch_state.candles or c.ts > ch_state.candles[-1].ts:
            ch_state.candles.append(c)
        # else: c.ts older than the last stored candle — stale, drop it
        # rather than corrupt ordering.

def _ch_compute_vp(candles):
    """Ported near-verbatim from charthacker.py's own compute_vp closure
    (charthacker.py:2958-3020), lifted out of draw()'s local scope — the
    only change is dropping the (row_vol/rendering-only) return values
    draw() needed for its own bar-chart drawing; the VP_BUCKETS=200
    bucketing/70%-value-area-expansion math is identical. Returns
    (poc_price, vah_price, val_price) or None. status_compute_vp (Phase 3a)
    implements this exact same algorithm too, under a different name — that
    one is status.py's own REST-based fallback; this is charthacker's
    live-WS-fed version, a different-fidelity INPUT candle series, not a
    duplicate computation."""
    if not candles:
        return None
    s_lo = min(c.l for c in candles)
    s_hi = max(c.h for c in candles)
    s_range = s_hi - s_lo
    if s_range <= 0:
        return None
    VP_BUCKETS = 200
    vp = [0.0] * VP_BUCKETS

    def ptb(p):
        return max(0, min(VP_BUCKETS - 1, int((p - s_lo) / s_range * (VP_BUCKETS - 1))))

    for c in candles:
        body_hi, body_lo = max(c.o, c.c), min(c.o, c.c)
        wv = c.v * 0.5
        bw, bh = ptb(c.l), ptb(c.h)
        sw = max(1, bh - bw + 1)
        for b in range(bw, bh + 1):
            vp[b] += wv / sw
        bv = c.v * 0.5
        bl, bb = ptb(body_lo), ptb(body_hi)
        sb = max(1, bb - bl + 1)
        if sb > 1:
            for b in range(bl, bb + 1):
                vp[b] += bv / sb
        else:
            vp[ptb(c.c)] += bv

    mx = max(vp)
    tv = sum(vp)
    pi = vp.index(mx)
    poc_p = s_lo + (pi / (VP_BUCKETS - 1)) * s_range

    tgt = tv * 0.70
    acc = vp[pi]
    lo_b = hi_b = pi
    while acc < tgt:
        ab = vp[hi_b + 1] if hi_b < VP_BUCKETS - 1 else 0.0
        bb2 = vp[lo_b - 1] if lo_b > 0 else 0.0
        if ab == 0 and bb2 == 0:
            break
        if ab >= bb2:
            hi_b += 1; acc += ab
        else:
            lo_b -= 1; acc += bb2

    vah_p = s_lo + (hi_b / (VP_BUCKETS - 1)) * s_range
    val_p = s_lo + (lo_b / (VP_BUCKETS - 1)) * s_range
    return poc_p, vah_p, val_p

def _ch_build_vwap_map(candles):
    """Ported from charthacker.py's own build_vwap_map closure
    (charthacker.py:3190-3220) — same cumulative-typical-price VWAP +
    Welford-online-variance formula, verbatim. charthacker's own version
    returns a full {ts: (vwap, sd)} map because it also draws a line across
    every visible column; status_export_loop itself only ever reads the
    LAST entry (`_vw_last, _sd_last`) to build the export payload, so this
    returns that final (vwap, sd) tuple directly instead of the full map —
    mathematically identical, since the Welford recurrence only depends on
    iterating candles in order and the last step already holds the
    cumulative-to-date values. Returns (0.0, 0.0) if candles is empty or
    carries no volume."""
    cum_tpv = 0.0
    cum_vol = 0.0
    cum_dev2 = 0.0
    vw, sd = 0.0, 0.0
    for c in candles:
        tp = (c.h + c.l + c.c) / 3.0
        v = c.v
        old_vol = cum_vol
        cum_tpv += tp * v
        cum_vol += v
        if cum_vol > 0:
            vw = cum_tpv / cum_vol
            if old_vol > 0:
                old_vw = (cum_tpv - tp * v) / old_vol
                cum_dev2 += v * (tp - old_vw) * (tp - vw)
            var = max(0.0, cum_dev2 / cum_vol)
            sd = var ** 0.5
        else:
            vw, sd = 0.0, 0.0
    return vw, sd

def _ch_bootstrap(asset):
    """One-shot REST seed before the live feed starts — mirrors start_feed()'s
    own initial `candles = fetch_candles(...)` bootstrap, scoped down to just
    this session's candles (curr_open_ts to now) rather than charthacker's
    own REST_LIMIT=500/48h preload, since the CH engine only ever computes
    VP/VWAP over the current session — charthacker's own deeper scrollback
    history serves ITS interactive multi-session chart, a genuinely
    out-of-scope feature Athena's headless engine has no use for. Reuses
    Phase 3a's own fetch_status_phemex_session_candles/
    fetch_status_yahoo_candles rather than re-porting fetch_phemex/
    fetch_yahoo a second time — same REST endpoints, same session window,
    already-proven-working fetchers."""
    ch_state = CH_STATE[asset]
    try:
        if asset == "ETH":
            rows = fetch_status_phemex_session_candles(ASSETS["ETH"]["phemex_symbol"], 60)
        else:
            rows = fetch_status_yahoo_candles("QQQ", rng="1d", interval="1m")
            _prev_ts, curr_ts = status_session_open_ts()
            rows = [r for r in rows if r[0] >= curr_ts]
        with ch_state.lock:
            for (ts, o, h, l, c, v) in rows:
                if ts % 60 != 0:
                    continue   # drop QQQ's possible trailing unaligned tick
                ch_state.candles.append(CH_Candle(ts=ts, o=o, h=h, l=l, c=c, v=v, closed=True))
            if ch_state.candles:
                ch_state.last_price = ch_state.candles[-1].c
    except Exception:
        pass

def _ch_engine_eth(stop_evt):
    """Port of charthacker.py's ws_phemex (charthacker.py:888-1067) — public
    kline_p feed, no auth required. See _ch_apply_resampled's docstring for
    why _Resampler isn't ported: raw kline rows are applied directly."""
    ch_state = CH_STATE["ETH"]
    symbol = ASSETS["ETH"]["phemex_symbol"]

    def on_open(ws):
        ws.send(json.dumps({"id": 1, "method": "kline_p.subscribe", "params": [symbol, 60]}))

    def on_message(ws, message):
        if stop_evt.is_set():
            return
        try:
            msg = json.loads(message)
        except Exception:
            return
        if msg.get("result") in ({"status": "success"}, "pong"):
            return
        klines = msg.get("kline_p")
        if not klines:
            return
        sym = msg.get("symbol", "")
        if sym and sym != symbol:
            return
        msg_type = msg.get("type", "incremental")
        with ch_state.lock:
            rows = sorted(klines, key=lambda r: r[0]) if msg_type == "snapshot" else klines
            for row in rows:
                c = CH_Candle(ts=row[0], o=row[3], h=row[4], l=row[5], c=row[6], v=row[7],
                               closed=(msg_type == "snapshot"))
                _ch_apply_resampled(ch_state, None, c)
            if ch_state.candles:
                ch_state.last_price = ch_state.candles[-1].c

    def on_error(ws, err):
        pass

    def on_close(ws, code, msg):
        pass

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
            ws_app.run_forever(ping_interval=25, ping_timeout=10)
        except Exception:
            pass
        if stop_evt.is_set():
            break
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

def _ch_engine_qqq(stop_evt):
    """Port of charthacker.py's ws_yahoo (charthacker.py:685-769) — QQQ has
    no true WS anywhere in charthacker.py either; a 5s Yahoo REST poll IS
    charthacker's own idea of "live" for equities, so this is a faithful
    mirror, not a downgrade from the ETH path. Yahoo never populates the
    still-forming bar with real OHLCV — it appends a trailing real-time
    quote tick (o=h=l=c=last price, v=0) instead, snapped onto its base-bar
    boundary and aggregated the same way ws_yahoo does."""
    ch_state = CH_STATE["QQQ"]
    POLL_SECS = 5
    BAR_SECS = 60
    with ch_state.lock:
        last_aligned_ts = ch_state.candles[-1].ts if ch_state.candles else 0

    while not stop_evt.is_set():
        try:
            fresh = fetch_status_yahoo_candles("QQQ", rng="1d", interval="1m")
        except Exception:
            fresh = []

        if fresh:
            tick = None
            if fresh[-1][0] % BAR_SECS != 0:
                tick = fresh.pop()

            with ch_state.lock:
                for (ts, o, h, l, c, v) in fresh:
                    if ts <= last_aligned_ts:
                        continue
                    _ch_apply_resampled(ch_state, None, CH_Candle(ts=ts, o=o, h=h, l=l, c=c, v=v, closed=True))
                if fresh:
                    last_aligned_ts = max(last_aligned_ts, fresh[-1][0])
                if tick is not None:
                    t_ts, _to, _th, _tl, t_c, _tv = tick
                    bucket = t_ts - (t_ts % BAR_SECS)
                    if bucket > last_aligned_ts:
                        _ch_apply_resampled(ch_state, None,
                                             CH_Candle(ts=bucket, o=t_c, h=t_c, l=t_c, c=t_c, v=0, closed=False))
                        ch_state.last_price = t_c
                elif ch_state.candles:
                    ch_state.last_price = ch_state.candles[-1].c

        for _ in range(POLL_SECS):
            if stop_evt.is_set():
                return
            time.sleep(1)

def _ch_engine_thread_eth(stop_evt):
    _ch_bootstrap("ETH")
    _ch_engine_eth(stop_evt)

def _ch_engine_thread_qqq(stop_evt):
    _ch_bootstrap("QQQ")
    _ch_engine_qqq(stop_evt)

def _ch_export_loop(stop_evt):
    """Port of charthacker.py's status_export_loop (charthacker.py:2055-
    2104) — every CH_EXPORT_INTERVAL, computes this session's VP/VWAP from
    each asset's own CH_STATE candles and writes status_<ASSET>.json, same
    "in-memory first, file also written for compat" pattern as every prior
    phase. Runs both assets from one thread (charthacker.py itself only
    ever has ONE active `state.asset` per process — this loop is the one
    genuinely Athena-specific structural adaptation: iterating both assets'
    already-independent CH_STATE here, rather than needing a second
    charthacker.py instance per its own doc comment)."""
    while not stop_evt.is_set():
        for asset in ("ETH", "QQQ"):
            try:
                ch_state = CH_STATE[asset]
                with ch_state.lock:
                    candles_snap = list(ch_state.candles)
                    spot = ch_state.last_price

                _prev_ts, curr_open_ts = status_session_open_ts()
                session_candles = [c for c in candles_snap if c.ts >= curr_open_ts]
                session_open = session_candles[0].o if session_candles else None

                _ind = {}
                vp_result = _ch_compute_vp(session_candles)
                if vp_result:
                    poc_p, vah_p, val_p = vp_result
                    _ind.update({"vah": vah_p, "val": val_p, "poc": poc_p})
                if session_candles:
                    vw, sd = _ch_build_vwap_map(session_candles)
                    if vw > 0:
                        _ind.update({
                            "vwap": vw,
                            "sd_p05": vw + 0.5 * sd, "sd_m05": vw - 0.5 * sd,
                            "sd_p2":  vw + 2.0 * sd, "sd_m2":  vw - 2.0 * sd,
                            "sd_p25": vw + 2.5 * sd, "sd_m25": vw - 2.5 * sd,
                        })
                if _ind:
                    with ch_state.lock:
                        ch_state.indicator_levels.update(_ind)

                with ch_state.lock:
                    levels = dict(ch_state.indicator_levels)

                if session_open is None or not levels:
                    continue

                payload = {
                    "asset": asset,
                    "updated_at": time.time(),
                    "spot": spot,
                    "session_open": session_open,
                    **levels,
                }
                with CH_EXPORT_LOCK:
                    CH_EXPORT[asset] = payload
                path = os.path.join(SCRIPT_DIR, f"status_{asset}.json")
                tmp_path = f"{path}.{os.getpid()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                os.replace(tmp_path, path)   # atomic on both POSIX and Windows
            except Exception:
                pass
        for _ in range(CH_EXPORT_INTERVAL):
            if stop_evt.is_set():
                return
            time.sleep(1)

def status_nearest_gex_clusters(gex_export, price, n=2):
    """Ported from status.py's own clusters_from_gex_export (renamed here
    to avoid colliding with Athena's OWN clusters_from_gex_export from
    Phase 2, which has a different signature/purpose — that one returns
    every qualifying cluster in ONE direction for target-building; this
    one returns the N clusters closest to price in EITHER direction, for
    the HPL row's own "2 closest" display text). Reuses Athena's existing
    magnitude_tier directly against gex_export's own gex_by_strike/
    scale_max — same math status.py's own all_clusters_from_gex_export
    used, just inlined rather than a separate helper."""
    scale_max = gex_export.get("scale_max") or 0.0
    by_strike = gex_export.get("gex_by_strike") or {}
    clusters = []
    for k_str, net in by_strike.items():
        tier = magnitude_tier(float(net), scale_max)
        if tier:
            clusters.append((float(k_str), tier))
    with_dist = [(k, t, abs(price - k)) for k, t in clusters]
    with_dist.sort(key=lambda x: x[2])
    return with_dist[:n]

def status_gamma_cluster_targets_directional(gex_export, price):
    """Ported from status.py's own gamma_cluster_targets_directional,
    simplified to ALWAYS use the gex_export path (see this section's
    module-level comment for why the live options-chain fallback branch
    isn't reachable/ported here — Phase 2's GEX engine guarantees fresh
    gex_export always). Thin wrapper around Athena's own EXISTING
    clusters_from_gex_export (Phase 2), called once per direction. Note:
    this field is effectively display-only for Athena's own trading
    logic — reconstruct_targets computes its OWN Cluster targets directly
    from gex_export (see the "missing medium gamma cluster" fix earlier
    this session) rather than trusting this snapshot field, so this
    output's ordering doesn't affect gating/target decisions."""
    above = clusters_from_gex_export(gex_export, price, "above")
    below = clusters_from_gex_export(gex_export, price, "below")
    return above, below

STATUS_LIGHT_W = 10   # ported verbatim from status.py's own LIGHT_W —
                        # visible width of status_light(): "* " + 8-char word

def status_light(ok):
    """Ported verbatim from status.py's own light()."""
    word = "ACTIVE  " if ok else "INACTIVE"
    col = GRN if ok else RED
    return f"{col}{BLD}●{RST} {col}{BLD}{word}{RST}"

def status_light_closed():
    """Ported verbatim from status.py's own light_closed() — distinct from
    status_light(False)/INACTIVE: means the market itself is closed, not
    that the rule failed its condition."""
    return f"{DIM}●{RST} {DIM}{BLD}CLOSED  {RST}"

STATUS_LIGHT_BLANK = " " * STATUS_LIGHT_W

HPL_CATEGORIES = (
    ("Volume", ("VAH", "VAL", "POC", "+2sd/2.5sd", "-2sd/2.5sd", "0.5sd band", "VWAP")),
    ("Expected Range", ("ER 40-80% band", "ER 100%", "ER 150%")),
    ("Options", ("BT", "ST", "Med/Large Gamma Clusters")),
    ("Miscellaneous", ("Prev EOD Close",)),
)

def evaluate_hpls(name, price, chain, session_candles, prev_close, iv, tol, gamma_tol, pcvr_gt1, is_crypto,
                   export=None, gamma_yellow_tol=None, gex_export=None, ratio=None):
    """Ported from status.py's own evaluate_hpls. If `export` is given (a
    fresh status_<ASSET>.json — Phase 3b's not-yet-built charthacker WS
    layer), VAH/VAL/POC/VWAP/SD bands/session-open/prev-close come from
    THAT instead of being recomputed from session_candles. gex_export
    (Athena's OWN Phase 2 GEX engine output, via read_gex_export) is
    ALWAYS used for Gamma Clusters here — see this section's own
    module-level comment for why status.py's live-options-chain fallback
    for clusters isn't reachable/ported. BT/ST/PCVR always come from
    Athena's own options-chain fetches regardless, same as status.py."""
    rows = []

    def near(level):
        return level is not None and abs(price - level) <= tol

    yellow_ceiling = gamma_yellow_tol if gamma_yellow_tol is not None else gamma_tol

    def dist_color(dist):
        if dist <= tol:
            return GRN
        elif dist <= yellow_ceiling:
            return YLW
        else:
            return RED

    def dist_tag(level):
        if level is None:
            return ""
        dist = abs(price - level)
        return f" ({dist_color(dist)}{BLD}${dist:,.2f} away{RST})"

    def band_dist_tag(lo, hi):
        if lo is None or hi is None:
            return ""
        dist = 0.0 if lo <= price <= hi else min(abs(price - lo), abs(price - hi))
        return f" ({dist_color(dist)}{BLD}${dist:,.2f} away{RST})"

    if export:
        vah, val, poc = export.get("vah"), export.get("val"), export.get("poc")
        vwap = export.get("vwap")
        sd_p05, sd_m05 = export.get("sd_p05"), export.get("sd_m05")
        sd_p2,  sd_m2  = export.get("sd_p2"),  export.get("sd_m2")
        sd_p25, sd_m25 = export.get("sd_p25"), export.get("sd_m25")
        session_open = export.get("session_open")
        if export.get("iv") is not None:
            iv = export["iv"]
    else:
        vp = status_compute_vp(session_candles)
        poc = vah = val = None
        if vp:
            poc, vah, val = vp
        vwap, sd = status_compute_vwap_sd(session_candles)
        if vwap is not None and sd is not None:
            sd_p05, sd_m05 = vwap + 0.5 * sd, vwap - 0.5 * sd
            sd_p2,  sd_m2  = vwap + 2.0 * sd, vwap - 2.0 * sd
            sd_p25, sd_m25 = vwap + 2.5 * sd, vwap - 2.5 * sd
        else:
            sd_p05 = sd_m05 = sd_p2 = sd_m2 = sd_p25 = sd_m25 = None
        _prev_ts, curr_open_ts = status_session_open_ts()
        open_candidates = [c for c in session_candles if c[0] >= curr_open_ts]
        session_open = open_candidates[0][1] if open_candidates else (session_candles[0][1] if session_candles else None)

    if is_crypto and session_open is not None:
        prev_close = session_open
    elif export and export.get("prev_eod_close") is not None:
        prev_close = export["prev_eod_close"]

    pcvr_lt_098 = ratio is not None and ratio < 0.98
    pcvr_gt_102 = ratio is not None and ratio > 1.02
    rows.append(("VAH", f"${vah:,.2f}{dist_tag(vah)}" if vah is not None else "n/a",
                 vah is not None and pcvr_gt_102 and price >= vah))
    rows.append(("VAL", f"${val:,.2f}{dist_tag(val)}" if val is not None else "n/a",
                 val is not None and pcvr_lt_098 and price <= val))
    rows.append(("POC", f"${poc:,.2f}{dist_tag(poc)}" if poc is not None else "n/a",
                 near(poc) and (pcvr_lt_098 or pcvr_gt_102)))

    if vwap is not None and sd_p2 is not None:
        rows.append(("+2sd/2.5sd", f"${sd_p2:,.2f} / ${sd_p25:,.2f}{dist_tag(sd_p2)}",
                     pcvr_gt_102 and price >= sd_p2))
        rows.append(("-2sd/2.5sd", f"${sd_m2:,.2f} / ${sd_m25:,.2f}{dist_tag(sd_m2)}",
                     pcvr_lt_098 and price <= sd_m2))
        rows.append(("0.5sd band", f"${sd_m05:,.2f} - ${sd_p05:,.2f}{band_dist_tag(sd_m05, sd_p05)}",
                     sd_m05 <= price <= sd_p05))
        rows.append(("VWAP", f"${vwap:,.2f}{dist_tag(vwap)}", near(vwap)))
    else:
        rows.append(("+2sd/2.5sd", "n/a", False))
        rows.append(("-2sd/2.5sd", "n/a", False))
        rows.append(("0.5sd band", "n/a", False))
        rows.append(("VWAP", "n/a", False))

    er = status_compute_er(session_open, iv)
    if er:
        u40, u80, u100, u150 = er["upper"][40], er["upper"][80], er["upper"][100], er["upper"][150]
        l40, l80, l100, l150 = er["lower"][40], er["lower"][80], er["lower"][100], er["lower"][150]
        pcvr_ge_102 = ratio is not None and ratio >= 1.02
        pcvr_le_098 = ratio is not None and ratio <= 0.98
        in_upper_band = u40 <= price <= u80
        in_lower_band = l80 <= price <= l40
        band_active = (pcvr_ge_102 and in_upper_band) or (pcvr_le_098 and in_lower_band)
        er_band_dist = min(
            0.0 if in_upper_band else min(abs(price - u40), abs(price - u80)),
            0.0 if in_lower_band else min(abs(price - l40), abs(price - l80)),
        )
        er_band_tag = f" ({dist_color(er_band_dist)}{BLD}${er_band_dist:,.2f} away{RST})"
        rows.append(("ER 40-80% band", f"${u40:,.2f}-${u80:,.2f} / ${l80:,.2f}-${l40:,.2f}{er_band_tag}", band_active))
        def _nearest_dist_tag(*levels):
            valid = [l for l in levels if l is not None]
            if not valid:
                return ""
            dist = min(abs(price - l) for l in valid)
            return f" ({dist_color(dist)}{BLD}${dist:,.2f} away{RST})"
        rows.append(("ER 100%", f"${u100:,.2f} / ${l100:,.2f}{_nearest_dist_tag(u100, l100)}",
                     (pcvr_ge_102 and near(u100)) or (pcvr_le_098 and near(l100))))
        rows.append(("ER 150%", f"${u150:,.2f} / ${l150:,.2f}{_nearest_dist_tag(u150, l150)}",
                     (pcvr_ge_102 and near(u150)) or (pcvr_le_098 and near(l150))))
    else:
        rows.append(("ER 40-80% band", "n/a", False))
        rows.append(("ER 100%", "n/a", False))
        rows.append(("ER 150%", "n/a", False))

    bt, st, active = compute_bt_st(chain["strikes"], is_crypto, pcvr_gt1, price)
    bt_green = active == "BT" and bt is not None and price >= bt
    st_green = active == "ST" and st is not None and price <= st
    rows.append(("BT", f"{GRN}{BLD}${bt:,.2f}{RST}{dist_tag(bt)}" if bt is not None else "n/a", bt_green))
    rows.append(("ST", f"{RED}{BLD}${st:,.2f}{RST}{dist_tag(st)}" if st is not None else "n/a", st_green))

    rows.append(("Prev EOD Close", f"${prev_close:,.2f}{dist_tag(prev_close)}" if prev_close is not None else "n/a",
                 near(prev_close)))

    nearest = status_nearest_gex_clusters(gex_export, price, n=2) if gex_export else []
    if nearest:
        hit = any(dist <= gamma_tol for _k, _t, dist in nearest)
        segs = []
        for k, t, dist in nearest:
            dcol = dist_color(dist)
            segs.append(f"${k:,.2f} ({t[0]}, {dcol}{BLD}${dist:,.2f} away{RST})")
        cluster_str = ", ".join(segs)
    else:
        hit = False
        cluster_str = "none"
    rows.append(("Med/Large Gamma Clusters", cluster_str, hit))

    return rows

def hpl_any_active(rows, closed):
    """Ported verbatim from status.py's own hpl_any_active — True if at
    least one HPL row is green this cycle, EXCLUDING BT/ST (they're
    directional profit-take TARGETS, not a 'price is near a key level'
    condition that should itself gate a new entry)."""
    if closed:
        return False
    return any(ok for label_, _v, ok in rows if label_ not in ("BT", "ST"))

def compute_dashboard_snapshot(data):
    """Ported from status.py's own compute_dashboard_snapshot — the exact
    function whose output athena.py has been reading off disk this whole
    time (status_logs/.../status_MM_DD_YYYY.jsonl). Same shape, same
    field names, same thresholds — `data` is the SAME plain dict shape
    status.py's own run_cycle()+assemble() produce (see status_run_cycle/
    status_assemble below), so this function itself needed NO changes
    beyond routing gamma-cluster/GEX-Flip work through Athena's own
    Phase 2 engine (see status_gamma_cluster_targets_directional's own
    docstring)."""
    snap = {"ts": datetime.now().isoformat()}

    sess_name, excl_reason = data.get("session", (None, None))
    in_session = sess_name is not None and excl_reason is None
    snap["session"] = {"name": sess_name, "excl_reason": excl_reason, "in_session": in_session}

    dvol = data.get("dvol")
    l1, l2 = status_dvol_layers(dvol)
    snap["dvol"] = dvol
    snap["layer1"] = l1
    snap["layer2"] = l2
    snap["vxn"] = data.get("qqq_iv")

    pcvr = data.get("pcvr")
    snap["pcvr"] = pcvr
    pcvr_gt1 = bool(pcvr and pcvr["ratio"] > 1.00)
    pcvr_extreme = bool(pcvr) and (pcvr["ratio"] <= 0.98 or pcvr["ratio"] >= 1.02)

    instruments = {}
    for inst_name, price_key, chain_key, candles_key, prev_key, iv_key, tol, gtol, gyellow, is_crypto in (
        ("ETH", "eth_price", "eth_chain", "eth_candles", None, "eth_iv", 2.00, 2.00, 5.00, True),
        ("QQQ", "qqq_price", "qqq_chain", "qqq_candles", "qqq_prev_close", "qqq_iv", 0.25, 0.35, 0.35, False),
    ):
        chain = data.get(chain_key)
        candles = data.get(candles_key) or []
        price = data.get(price_key)
        prev_close = data.get(prev_key)
        iv = data.get(iv_key)
        inst_snap = {"price": price, "available": bool(chain and price is not None)}
        if not inst_snap["available"]:
            instruments[inst_name] = inst_snap
            continue

        export = read_status_charthacker_export(inst_name)
        gex_export = read_gex_export(inst_name)
        rows = evaluate_hpls(inst_name, price, chain, candles, prev_close, iv, tol, gtol, pcvr_gt1, is_crypto,
                              export=export, gamma_yellow_tol=gyellow, gex_export=gex_export,
                              ratio=(pcvr["ratio"] if pcvr else None))
        closed = inst_name == "QQQ" and status_qqq_market_closed()
        inst_snap["market_closed"] = closed

        hpl = {}
        for label_, level_str, ok in rows:
            hpl[label_] = {
                # _ANSI_RE is Athena's own existing ANSI-tag regex (used by
                # ansi_segments for curses rendering) — functionally the
                # same pattern status.py's own _ANSI_RE strips with here,
                # just written with a capturing group and \033 instead of
                # \x1b; re.sub() behaves identically either way for a
                # plain strip-to-empty-string call.
                "value": _ANSI_RE.sub("", level_str),
                "status": "closed" if closed else ("active" if ok else "inactive"),
            }
        inst_snap["hpl"] = hpl
        any_active = hpl_any_active(rows, closed)
        inst_snap["any_active"] = any_active

        ratio = pcvr["ratio"] if pcvr else None
        targets = []
        if ratio is not None and not closed:
            if ratio < 0.98:
                bt, _st, _active = compute_bt_st(chain["strikes"], is_crypto, ratio > 1.00, price)
                if bt is not None:
                    targets.append({"type": "BT", "level": bt, "tier": None})
                above, _below = status_gamma_cluster_targets_directional(gex_export, price)
                targets += [{"type": "Cluster", "level": k, "tier": t} for k, t in above]
            elif ratio > 1.02:
                _bt, st, _active = compute_bt_st(chain["strikes"], is_crypto, ratio > 1.00, price)
                if st is not None:
                    targets.append({"type": "ST", "level": st, "tier": None})
                _above, below = status_gamma_cluster_targets_directional(gex_export, price)
                targets += [{"type": "Cluster", "level": k, "tier": t} for k, t in below]
        inst_snap["targets"] = targets
        has_targets = bool(targets)

        ready = in_session and pcvr_extreme and any_active and has_targets
        inst_snap["final_status"] = "EXECUTE WHEN READY" if ready else "HOLD"

        instruments[inst_name] = inst_snap

    snap["eth"] = instruments.get("ETH")
    snap["qqq"] = instruments.get("QQQ")
    return snap

_STATUS_SEP_MARKER    = "\0SEP\0"
_STATUS_TITLE_MARKER  = "\0TITLE\0"
_STATUS_FOOTER_MARKER = "\0FOOTER\0"
_STATUS_STATUS_MARKER = "\0STATUS\0"
STATUS_TITLE_TEXT = "BLACKJACK FRAMEWORK DASHBOARD"

def _status_visible_len(s):
    return len(_ANSI_RE.sub("", s))

def _status_build_render_lines(display_data):
    """Ported from status.py's own render() (status.py:1402-1633) — same
    ANSI-tagged line construction, same 5 sections + Final Status, same
    two-pass marker/centering technique. Deliberately NOT ported: the
    actual terminal write (`clr_inplace()`/`sys.stdout.write`) — this
    returns the finished `lines` list instead, for draw_status_screen to
    render into a DoubleBuffer via the SAME ansi_segments() helper every
    other console_log()/log message already uses, so none of this
    construction logic needed rewriting into (text,pair,attrs) tuples by
    hand. Also NOT ported: the `remaining`-seconds refresh countdown and
    the `[Q]uit [R]efresh` footer keys — both are tied to status.py's own
    poll-and-reprint terminal loop, which Athena's continuously-redrawing
    curses UI has no equivalent of; the footer here shows Athena's own
    key hints instead (a reasoned adaptation, not a dropped feature — see
    draw_status_screen for how scrolling/exiting actually works).

    `display_data` must be the SAME live-price-overlaid dict `_status_
    snapshot_loop` already builds before calling compute_dashboard_
    snapshot — i.e. this reads price/chain/candles/iv/prev_close fresh
    from the raw data, not the already-ANSI-stripped STATUS_SNAPSHOT, so
    per-row distance coloring (evaluate_hpls's own dist_tag/band_dist_tag)
    survives into the Status screen exactly as status.py's own render()
    shows it."""
    lines = []
    def p(s=""):
        lines.append(s)

    p(_STATUS_SEP_MARKER)
    p(_STATUS_TITLE_MARKER)
    p(_STATUS_SEP_MARKER)

    sess_name, excl_reason = display_data.get("session", (None, None))
    in_session = sess_name is not None and excl_reason is None
    p(f"  {BLD}1. Session{RST}")
    label = excl_reason or sess_name or "No active session"
    p(f"     {status_light(in_session)}  {DIM}{label}{RST}")
    p()

    dvol = display_data.get("dvol")
    l1, l2 = status_dvol_layers(dvol)
    p(f"  {BLD}2. Volatility{RST}")
    if dvol is not None:
        p(f"     {STATUS_LIGHT_BLANK}  {DIM}{'DVOL (ETH)':<12}{RST}{CYN}{BLD}{dvol:>6.2f}{RST}   "
          f"Layer 1: {YLW}{l1}{RST}   Layer 2: {MAG}{l2}{RST}")
    else:
        p(f"     {STATUS_LIGHT_BLANK}  {DIM}DVOL (ETH) unavailable{RST}")
    qqq_iv = display_data.get("qqq_iv")
    if qqq_iv is not None:
        p(f"     {STATUS_LIGHT_BLANK}  {DIM}{'VXN (QQQ)':<12}{RST}{CYN}{BLD}{qqq_iv:>6.2f}{RST}")
    else:
        p(f"     {STATUS_LIGHT_BLANK}  {DIM}VXN (QQQ) unavailable{RST}")
    p()

    pcvr = display_data.get("pcvr")
    p(f"  {BLD}3. PCVR{RST}")
    if pcvr:
        ratio = pcvr["ratio"]
        col = RED if ratio >= 1.02 else (GRN if ratio <= 0.98 else YLW)
        p(f"     {STATUS_LIGHT_BLANK}  {col}{BLD}{ratio:>6.2f}{RST}   ({pcvr['underlying']})   "
          f"{DIM}put {pcvr['put_vol']:,.0f} / call {pcvr['call_vol']:,.0f}{RST}")
    else:
        p(f"     {STATUS_LIGHT_BLANK}  {DIM}unavailable{RST}")
    p()

    pcvr_gt1 = bool(pcvr and pcvr["ratio"] > 1.00)

    p(f"  {BLD}4. High-Probability Levels{RST}")
    active_status = {}
    for inst_name, price_key, chain_key, candles_key, prev_key, iv_key, tol, gtol, gyellow, is_crypto in (
        ("ETH", "eth_price", "eth_chain", "eth_candles", None, "eth_iv", 2.00, 2.00, 5.00, True),
        ("QQQ", "qqq_price", "qqq_chain", "qqq_candles", "qqq_prev_close", "qqq_iv", 0.25, 0.35, 0.35, False),
    ):
        chain = display_data.get(chain_key)
        candles = display_data.get(candles_key) or []
        price = display_data.get(price_key)
        prev_close = display_data.get(prev_key)
        iv = display_data.get(iv_key)
        export = read_status_charthacker_export(inst_name)
        gex_export = read_gex_export(inst_name)
        tags = []
        if export:
            tags.append(f"{GRN}VP/VWAP: charthacker.py{RST}{DIM}")
        if gex_export:
            tags.append(f"{GRN}clusters/flip: gex.py{RST}{DIM}")
        src_tag = ", ".join(tags) if tags else "status.py REST snapshot"
        p(f"     {BLD}{YLW}── {inst_name} {RST}{DIM}({src_tag}){RST}")
        if not chain or price is None:
            p(f"        {STATUS_LIGHT_BLANK}  {DIM}unavailable{RST}")
            p()
            active_status[inst_name] = False
            continue
        p(f"        {STATUS_LIGHT_BLANK}  {'Live Price':<26}{CYN}{BLD}${price:,.2f}{RST}")
        rows = evaluate_hpls(inst_name, price, chain, candles, prev_close, iv, tol, gtol, pcvr_gt1, is_crypto,
                              export=export, gamma_yellow_tol=gyellow, gex_export=gex_export,
                              ratio=(pcvr["ratio"] if pcvr else None))
        closed = inst_name == "QQQ" and status_qqq_market_closed()
        rows_by_label = {label_: (level_str, ok) for label_, level_str, ok in rows}
        any_active = hpl_any_active(rows, closed)
        active_status[inst_name] = any_active
        for cat_i, (cat_name, cat_labels) in enumerate(HPL_CATEGORIES):
            if cat_i > 0:
                p()
            p(f"        {STATUS_LIGHT_BLANK}  {DIM}{BLD}{cat_name}{RST}")
            for label_ in cat_labels:
                level_str, ok = rows_by_label.get(label_, ("n/a", False))
                dot = status_light_closed() if closed else status_light(ok)
                p(f"        {dot}  {label_:<26}{level_str}")
        p()

    p(f"  {BLD}5. Targets{RST}")
    has_targets = {}
    ratio = pcvr["ratio"] if pcvr else None
    for inst_name, price_key, chain_key, is_crypto, gated in (
        ("ETH", "eth_price", "eth_chain", True, False),
        ("QQQ", "qqq_price", "qqq_chain", False, True),
    ):
        chain = display_data.get(chain_key)
        price = display_data.get(price_key)
        if gated and status_qqq_market_closed():
            p(f"     {STATUS_LIGHT_BLANK}  {DIM}{inst_name:<6}CLOSED{RST}")
            has_targets[inst_name] = False
            continue
        if not chain or price is None or ratio is None:
            p(f"     {STATUS_LIGHT_BLANK}  {DIM}{inst_name:<6}unavailable{RST}")
            has_targets[inst_name] = False
            continue
        gex_export = read_gex_export(inst_name)

        def target_rel(level, price=price, ratio=ratio):
            dist = abs(level - price)
            if level > price:
                direction = "above"
            elif level < price:
                direction = "below"
            else:
                return f"{DIM}at price{RST}"
            if ratio >= 1.02:
                col = GRN if direction == "below" else RED
            elif ratio <= 0.98:
                col = GRN if direction == "above" else RED
            else:
                col = DIM
            return f"{col}{BLD}{direction}, ${dist:,.2f} away{RST}"

        # Phase 2's own GEX engine guarantees gex_export is always fresh
        # (same process, not a separate gex.py that might not be running)
        # — status.py's own live-options-chain compute_gex_flip fallback
        # for a missing/stale gex_export is therefore unreachable here and
        # wasn't ported, same documented simplification Phase 3a already
        # made for the gamma-cluster fallback.
        gex_flip = gex_export.get("gex_flip") if gex_export else None
        targets = []
        if ratio < 0.98:
            bt, _st, _active = compute_bt_st(chain["strikes"], is_crypto, ratio > 1.00, price)
            if bt is not None:
                targets.append(f"BT {GRN}{BLD}${bt:,.2f}{RST} ({target_rel(bt)})")
            if gex_flip is not None:
                targets.append(f"GEX Flip {GRN}{BLD}${gex_flip:,.2f}{RST} ({target_rel(gex_flip)})")
            above, _below = status_gamma_cluster_targets_directional(gex_export, price)
            targets += [f"Cluster ({t}) {GRN}{BLD}${k:,.2f}{RST} ({target_rel(k)})" for k, t in above]
        elif ratio > 1.02:
            _bt, st, _active = compute_bt_st(chain["strikes"], is_crypto, ratio > 1.00, price)
            if st is not None:
                targets.append(f"ST {GRN}{BLD}${st:,.2f}{RST} ({target_rel(st)})")
            if gex_flip is not None:
                targets.append(f"GEX Flip {GRN}{BLD}${gex_flip:,.2f}{RST} ({target_rel(gex_flip)})")
            _above, below = status_gamma_cluster_targets_directional(gex_export, price)
            targets += [f"Cluster ({t}) {GRN}{BLD}${k:,.2f}{RST} ({target_rel(k)})" for k, t in below]
        has_targets[inst_name] = bool(targets)
        if targets:
            for i in range(0, len(targets), 2):
                chunk = targets[i:i + 2]
                label = f"{DIM}{inst_name:<6}{RST}" if i == 0 else " " * 6
                p(f"     {STATUS_LIGHT_BLANK}  {label}{', '.join(chunk)}")
        else:
            p(f"     {STATUS_LIGHT_BLANK}  {DIM}{inst_name:<6}no active targets{RST}")
    p()

    pcvr_extreme = bool(pcvr) and (pcvr["ratio"] <= 0.98 or pcvr["ratio"] >= 1.02)
    def final_status_text(inst):
        ready = in_session and pcvr_extreme and active_status.get(inst, False) and has_targets.get(inst, False)
        word = f"{GRN}{BLD}EXECUTE WHEN READY{RST}" if ready else f"{RED}{BLD}HOLD{RST}"
        return f"{BLD}{inst}:{RST} {word}"
    status_line = f"{final_status_text('ETH')}     {final_status_text('QQQ')}"
    p(_STATUS_STATUS_MARKER)
    p()

    p(_STATUS_SEP_MARKER)
    ts = datetime.now().strftime("%H:%M:%S")
    footer = f"{DIM}{ts}  updates every ~{STATUS_SNAPSHOT_INTERVAL}s{RST}   {BLD}[Q]{RST}{DIM}uit  {BLD}[S]{RST}{DIM}/Esc back  {BLD}[↑/↓/PgUp/PgDn]{RST}{DIM} scroll{RST}"
    p(_STATUS_FOOTER_MARKER)
    errs = display_data.get("errors") or {}
    if errs:
        p(f"  {RED}errors: {', '.join(errs.keys())}{RST}")
    p(_STATUS_SEP_MARKER)

    width = max((_status_visible_len(l) for l in lines
                 if l not in (_STATUS_SEP_MARKER, _STATUS_TITLE_MARKER, _STATUS_FOOTER_MARKER, _STATUS_STATUS_MARKER)),
                default=78)
    def centered(s):
        return " " * max(0, (width - _status_visible_len(s)) // 2) + s
    sep = f"{BLD}{CYN}{'─' * width}{RST}"
    title = f"{BLD}{CYN}{STATUS_TITLE_TEXT.center(width)}{RST}"
    footer_centered = centered(footer)
    status_centered = centered(status_line)
    return [sep if l == _STATUS_SEP_MARKER else
            title if l == _STATUS_TITLE_MARKER else
            footer_centered if l == _STATUS_FOOTER_MARKER else
            status_centered if l == _STATUS_STATUS_MARKER else l
            for l in lines]

def append_status_snapshot(snap):
    """Ported from status.py's own append_snapshot, using Athena's own
    EXISTING status_log_path (already used to READ these files — now also
    the write side, same file/folder convention, no format change)."""
    try:
        path = status_log_path(datetime.now())
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap) + "\n")
        return True, None
    except Exception as e:
        return False, str(e)

def status_run_cycle():
    """Ported from status.py's own run_cycle — the FULL data refresh
    (chains/candles/DVOL/PCVR — the heavy fetches), parallelized the same
    way status.py's own does."""
    result = {"errors": {}}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {
            "dvol": ex.submit(fetch_status_dvol, "ETH"),
            "pcvr": ex.submit(fetch_status_pcvr),
            "eth_chain": ex.submit(fetch_status_deribit_chain, "ETH"),
            "eth_candles": ex.submit(fetch_status_phemex_session_candles, "ETHUSDT", 60),
            "qqq_chain": ex.submit(fetch_status_cboe_chain, "QQQ"),
            "qqq_candles": ex.submit(fetch_status_yahoo_candles, "QQQ", "5d", "1m"),
            "qqq_meta": ex.submit(fetch_status_yahoo_meta, "QQQ"),
            "vxn": ex.submit(fetch_status_vxn),
        }
        for key, fut in futs.items():
            try:
                result[key] = fut.result()
            except Exception as e:
                result["errors"][key] = str(e)
                result[key] = None
    result["session"] = status_get_session_status()
    return result

def status_assemble(raw):
    """Ported verbatim from status.py's own assemble."""
    out = dict(raw)
    eth_chain = raw.get("eth_chain")
    out["eth_price"] = eth_chain["spot"] if eth_chain else None
    out["eth_iv"] = raw.get("dvol")
    qqq_chain = raw.get("qqq_chain")
    qqq_meta = raw.get("qqq_meta")
    out["qqq_price"] = (qqq_meta[0] if qqq_meta and qqq_meta[0] else (qqq_chain["spot"] if qqq_chain else None))
    _prev_open_ts, curr_open_ts = status_session_open_ts()
    out["qqq_prev_close"] = status_session_prev_eod_close(raw.get("qqq_candles") or [], curr_open_ts)
    out["qqq_iv"] = raw.get("vxn")
    return out

# ── Status engine in-process state (replaces the file round trip a separate
# status.py process needed — read_last_status_snapshot now prefers this
# directly, same "no consumer-side changes needed" pattern as Phases 1-2) ──
STATUS_STATE_LOCK = threading.Lock()
STATUS_SNAPSHOT = None                          # latest compute_dashboard_snapshot() output
STATUS_LIVE_PRICES = {"ETH": None, "QQQ": None}
STATUS_ENGINE_STATE = ["connecting…"]           # mutable single-item box, same
                                                 # convention _profile_mode_idx uses
STATUS_RENDER_LINES = []   # latest _status_build_render_lines() output — Phase
                            # 3c's Status screen reads this directly every draw
                            # (same "engine computes on its own cadence, UI just
                            # renders the cache" split GEX mode's own history/
                            # grid state already uses), rather than recomputing
                            # evaluate_hpls et al. on every ~100ms curses redraw.
_status_last_full_lock = threading.Lock()
_status_last_full_data = None

# Deliberately NOT ported: status.py's own check_pcvr_alert/play_alert
# (plays alert.wav on a PCVR sentiment flip) — a terminal-audio notification
# for a human watching status.py's OWN screen, not data Athena's trading
# logic reads or gates on. A genuine scope boundary, not a fidelity cut.

def _status_full_refresh_loop(stop_evt):
    """The heavy 30s-cadence half of status.py's own main() loop
    (run_cycle()+assemble()), on its own dedicated thread — a plain
    thread, not asyncio, matching status.py's own execution model (it
    has no asyncio anywhere either)."""
    global _status_last_full_data
    while not stop_evt.is_set():
        try:
            raw = status_run_cycle()
            data = status_assemble(raw)
            with _status_last_full_lock:
                _status_last_full_data = data
            STATUS_ENGINE_STATE[0] = "live"
        except Exception as e:
            STATUS_ENGINE_STATE[0] = f"error: {e}"[:60]
        stop_evt.wait(STATUS_REFRESH_SEC)

def _status_live_price_loop(stop_evt):
    """Mirrors status.py's own live_price_loop — independent 2s cadence,
    tiny/cheap fetches only (Phemex ticker, Yahoo meta), not the heavy
    chain/candle endpoints, so this stays responsive regardless of how
    long the full refresh takes."""
    while not stop_evt.is_set():
        eth_p = fetch_status_eth_live_price()
        qqq_p = None
        try:
            qqq_p, _prev = fetch_status_yahoo_meta("QQQ")
        except Exception:
            pass
        with STATUS_STATE_LOCK:
            if eth_p is not None:
                STATUS_LIVE_PRICES["ETH"] = eth_p
            if qqq_p is not None:
                STATUS_LIVE_PRICES["QQQ"] = qqq_p
        stop_evt.wait(STATUS_LIVE_PRICE_INTERVAL)

def _status_snapshot_loop(stop_evt):
    """Mirrors status.py's own snapshot_logger_loop — builds+stores a
    fresh compute_dashboard_snapshot() every STATUS_SNAPSHOT_INTERVAL.
    ALSO does the "overlay the live price on top of the last full
    refresh" step itself (status.py's own version does that inline in
    main()'s 0.2s terminal-redraw tick, since it needs fresh numbers for
    the screen too — Athena has no redraw tick to piggyback on, so this
    thread does the overlay directly, at the same net cadence the
    snapshot content itself is built at — the SNAPSHOT's own values are
    identical either way, just not also re-rendered to a screen 15x more
    often for no reason)."""
    global STATUS_SNAPSHOT, STATUS_RENDER_LINES
    while not stop_evt.is_set():
        with _status_last_full_lock:
            data = _status_last_full_data
        if data:
            with STATUS_STATE_LOCK:
                live = dict(STATUS_LIVE_PRICES)
            display_data = dict(data)
            if live.get("ETH") is not None:
                display_data["eth_price"] = live["ETH"]
            if live.get("QQQ") is not None:
                display_data["qqq_price"] = live["QQQ"]
            try:
                snap = compute_dashboard_snapshot(display_data)
                with STATUS_STATE_LOCK:
                    STATUS_SNAPSHOT = snap
                append_status_snapshot(snap)
            except Exception:
                pass
            try:
                lines = _status_build_render_lines(display_data)
                with STATUS_STATE_LOCK:
                    STATUS_RENDER_LINES = lines
            except Exception:
                pass
        stop_evt.wait(STATUS_SNAPSHOT_INTERVAL)

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

def classify_trades_quote_rule(raw_trades, quotes):
    """Ported verbatim from footprint.py — turns (ts, price, size) trades
    + (ts, bid, ask) quotes into (ts, price, qty, is_buy), the same tuple
    shape the crypto fetchers produce. See classify_one_trade for the
    actual rule. quotes=[] (as used by the backfill, which fetches no
    historical quotes — see fetch_alpaca_trades_range's own docstring)
    just falls straight through to the tick-rule/assume-buy branches."""
    out = []
    qi = 0
    bid = ask = None
    prev_price, prev_side = None, True
    for ts, price, size in raw_trades:
        while qi < len(quotes) and quotes[qi][0] <= ts:
            bid, ask = quotes[qi][1], quotes[qi][2]
            qi += 1
        is_buy = classify_one_trade(price, bid, ask, prev_price, prev_side)
        out.append((ts, price, size, is_buy))
        prev_price, prev_side = price, is_buy
    return out

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
    empty; prev_bar is None if there's only one bar so far — with the
    startup REST backfill (_run_footprint_backfill, see the module
    docstring's footprint.py section) this is no longer the normal
    post-restart state, just the genuine "brand new symbol/day, nothing
    has traded yet" case.

    Spans across day-folders (footprint_log_paths, not a single
    footprint_log_glob file) — see that function's own 2026-07-25 fix
    note: right after a local day-folder rollover, the newest file may
    have only 0-1 bars in it, and without this the OTHER bar this
    function needs would incorrectly come back None (a real correctness
    bug for the confirmation check, not just a display gap) instead of
    the true previous bar sitting in yesterday's file."""
    paths = footprint_log_paths(asset)
    collected = []   # newest-first
    for path in reversed(paths):
        try:
            with open(path, encoding="utf-8") as f:
                lines = [l for l in f if l.strip()]
        except Exception:
            continue
        for line in reversed(lines):
            collected.append(line)
            if len(collected) >= 2:
                break
        if len(collected) >= 2:
            break
    if not collected:
        return None, None
    try:
        cur = json.loads(collected[0])
        prev = json.loads(collected[1]) if len(collected) >= 2 else None
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

def _archive_sim_logs():
    """Moves the ENTIRE sim_logs tree aside into a timestamped
    sim_logs_archive/ folder rather than deleting it — [R]eset is meant to
    give the Data view (its PnL chart + trade table are both reconstructed
    from sim_logs, via scan_all_trade_events/scan_all_trades_detailed) a
    genuinely clean slate; before this, a reset only touched
    sim_account.json's live balance/positions, leaving the OLD trade
    history sitting in sim_logs where the Data view would keep showing it
    forever. History is archived, not destroyed — same "move aside, don't
    delete" convention already established for this project's own data
    files. The very next sim_log_event() call (reset() logs one itself,
    right after this runs) recreates a fresh day-folder under
    SIM_LOG_DIR_BASE automatically, so nothing further needs to be
    (re)created here."""
    if not os.path.isdir(SIM_LOG_DIR_BASE):
        return
    try:
        archive_root = os.path.join(SCRIPT_DIR, "sim_logs_archive")
        os.makedirs(archive_root, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        shutil.move(SIM_LOG_DIR_BASE, os.path.join(archive_root, f"reset_{ts}"))
    except Exception:
        pass

async def live_price_for_symbol(symbol):
    """LIVE_TAPE's own real-time WS trade price (in-memory, no network
    round trip) when available, else fetch_last_price's REST poll as a
    fallback (e.g. right at startup before the WS thread connects). Same
    LIVE_TAPE-first pattern SimAccount.tick_matching already uses — pulled
    out into its own helper so every OTHER SimAccount method that needs a
    current price (to_account_snapshot, market_close, place_entry's market
    branch) gets the same speed instead of each making its own always-REST
    fetch_last_price call. That inconsistency was the actual root cause of
    "flatten takes 10+ seconds": [F]latten alone chains cancel_all ->
    fetch_account (-> to_account_snapshot, one REST call PER open
    position) -> market_close (another REST call) -> a THIRD REST call for
    the athena_logs exit-price note — each with up to a 6s httpx timeout,
    easily compounding into a double-digit-second wait on anything but a
    fast/lucky network, when every one of those prices was already sitting
    in memory via LIVE_TAPE the whole time."""
    asset = ASSET_BY_SYMBOL.get(symbol)
    tape_bar = LIVE_TAPE.snapshot(asset) if asset else None
    if tape_bar is not None:
        return tape_bar["c"]
    return await fetch_last_price(symbol)

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
        _archive_sim_logs()
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
            price = await live_price_for_symbol(symbol)
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
        price = await live_price_for_symbol(symbol)
        if price is None:
            return {"code": -1, "msg": "sim: no live price available"}
        self._close(symbol, pos_side, qty, price, reason)
        self._save()
        return {"code": 0}

    async def tick_matching(self):
        """Check every resting sim order against the live price and
        fill/close whatever now qualifies. Prefers LIVE_TAPE's own
        real-time WS trade price (in-memory, no network round trip,
        updates on every print) over fetch_last_price's REST ticker poll
        — a REST-only check here meant fills/SL/TP could only ever react
        to whatever price a poll happened to sample, which on a symbol
        that briefly touched a level between polls could silently miss
        the touch entirely and only catch it (at a worse price) on a
        LATER poll once price had moved further. LIVE_TAPE is cheap
        enough that this can now be called far more often than once per
        engine cycle (see engine_loop's fast matching wait) without
        adding any extra REST load. Falls back to fetch_last_price only
        for a symbol LIVE_TAPE has no data for yet (e.g. right at
        startup, before its WS thread connects)."""
        symbols = {o["symbol"] for o in self.orders.values()} | set(self.positions.keys())
        for symbol in symbols:
            asset = ASSET_BY_SYMBOL.get(symbol)
            tape_bar = LIVE_TAPE.snapshot(asset) if asset else None
            price = tape_bar["c"] if tape_bar is not None else await fetch_last_price(symbol)
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
                price = await live_price_for_symbol(symbol)
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
    """(balance, available, margin_used) for the dashboard's ACCOUNT line.
    `used` (totalUsedBalanceRv) is Phemex's own real margin-in-use figure
    across every open position/resting order for this account — SimAccount.
    to_account_snapshot() produces the identical field (per-symbol leverage-
    aware), so this works unchanged for DRY_RUN too. margin_used added
    2026-07-25 (user request: "include how much margin is actively being
    used across all positions")."""
    acc = (acc_data or {}).get('data', {}).get('account', {})
    bal = float(acc.get('accountBalanceRv') or 0)
    used = float(acc.get('totalUsedBalanceRv') or 0)
    return bal, max(0.0, bal - used), used

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

def _order_ok(result):
    """True only for a genuine Phemex success response (`{"code": 0, ...}`
    — SimAccount's own place_entry/place_sl/place_tp_leg/cancel_all/
    market_close all return this exact shape too, on purpose, so this one
    check works identically for both DRY_RUN and real). Added 2026-07-25
    while hardening the real-money order path for the FIRST time it's
    ever actually been exercised — place_entry already checked this
    correctly; place_sl/place_tp_leg/cancel_all/market_close never did,
    only catching outright exceptions (a network error), which misses the
    much more common case of Phemex responding 200 OK with an
    application-level rejection (bad price, insufficient margin, etc.) —
    that response never raises, so every one of those calls was silently
    treated as a success regardless of what Phemex actually did with it."""
    return isinstance(result, dict) and result.get("code") == 0

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

# target_type <-> a short code embedded in the TP leg's own clOrdID
# (athena_tp{suffix}_{code}_{ts}) — added 2026-07-25 alongside
# fetch_resting_orders/_parse_resting_orders below, so a REAL Phemex
# order can be reconciled back to "which HPL/target this leg came from"
# exactly the way DRY_RUN already can via SimAccount's own order dict
# (which just stores "type" directly, no encoding needed there — Athena
# owns that whole data structure; a real Phemex order has no such custom
# field, so the clOrdID itself is the only place to carry this).
_TP_TYPE_CODES = {"BT": "BT", "ST": "ST", "GEX Flip": "GF", "Cluster": "CL"}
_TP_TYPE_CODES_REV = {v: k for k, v in _TP_TYPE_CODES.items()}

def _tp_type_code(target_type):
    return _TP_TYPE_CODES.get(target_type, "UNK")

def _tp_type_from_code(code):
    return _TP_TYPE_CODES_REV.get(code, "?")

async def place_tp_leg(symbol, pos_side, qty_str, price, price_decimals, suffix, target_type="?"):
    if DRY_RUN:
        return await get_sim_account().place_tp_leg(symbol, pos_side, qty_str, price, suffix, target_type)
    close_side = 'Sell' if pos_side == 'Long' else 'Buy'
    params = {
        'symbol': symbol,
        'clOrdID': f'athena_tp{suffix}_{_tp_type_code(target_type)}_{int(time.time() * 1000)}',
        'side': close_side, 'posSide': pos_side, 'orderQtyRq': qty_str,
        'ordType': 'Limit', 'priceRp': f'{price:.{price_decimals}f}',
        'reduceOnly': 'true', 'timeInForce': 'GoodTillCancel',
    }
    return await phemex_request('PUT', '/g-orders/create', params=params)

async def fetch_resting_orders(symbol):
    """Real mode only: Phemex's own /g-orders/activeList, queried TWICE —
    once plain (regular resting orders — Athena's TP legs, ordType=Limit)
    and once with orderType='conditional' (trigger orders — Athena's SL,
    ordType=Stop, only shows up under the conditional filter) — same
    two-call pattern copycat.py's own already-proven order-listing code
    uses (cross-checked directly against that file before writing this,
    same discipline as the rest of this session's real-trading hardening
    work). Returns the combined raw row list, or None if either call
    outright failed (network/auth) — an empty list is a genuine "no
    resting orders" answer, distinct from "couldn't ask"."""
    try:
        active = await phemex_request('GET', '/g-orders/activeList',
                                       params={'symbol': symbol, 'currency': 'USDT'})
        cond = await phemex_request('GET', '/g-orders/activeList',
                                     params={'symbol': symbol, 'currency': 'USDT', 'orderType': 'conditional'})
    except Exception:
        return None
    rows = []
    for resp in (active, cond):
        if resp and resp.get('code') == 0:
            rows += (resp.get('data', {}).get('rows', []) or [])
    return rows

def _parse_resting_orders(rows, pos_side):
    """Reconstructs (sl_price, tp_legs) for ONE position side from
    Phemex's own /g-orders/activeList rows, using Athena's OWN clOrdID
    naming convention (athena_sl_..., athena_tp{suffix}_{code}_...) to
    classify each order precisely — far more reliable than guessing purely
    from stopDirection/side the way copycat.py's own parsing has to
    (it doesn't control clOrdID as tightly). tp_legs come back sorted by
    suffix (1, 2, ...), matching the SAME order _check_fill's own tp_legs
    list is always built in.

    posSide filtering: uses the order's own posSide field when Phemex
    echoes it back (should be, since every Athena order sets it
    explicitly); falls back to inferring it from stopDirection/side for
    conditional (SL) orders — copycat.py's own documented fallback,
    "Phemex doesn't return posSide on conditional orders" in some
    response shapes — and from side-vs-close_side for plain (TP) orders,
    which need no stopDirection at all.

    Pure function — no network I/O — same "testable without a real
    Phemex connection" discipline as _footprint_crosshair_clamp/
    _order_ok elsewhere in this file."""
    sl_price = None
    tp_legs = []
    close_side = 'Sell' if pos_side == 'Long' else 'Buy'
    for o in rows or []:
        cl_id = o.get('clOrdID') or ''
        if not cl_id.startswith('athena_'):
            continue   # not one of Athena's own orders (e.g. a manually-placed one)

        o_pos_side = o.get('posSide')
        if not o_pos_side:
            stop_dir, side_o = o.get('stopDirection', ''), o.get('side', '')
            if stop_dir == 'Falling' and side_o == 'Sell':
                o_pos_side = 'Long'
            elif stop_dir == 'Rising' and side_o == 'Buy':
                o_pos_side = 'Long'
            elif stop_dir == 'Rising' and side_o == 'Sell':
                o_pos_side = 'Short'
            elif stop_dir == 'Falling' and side_o == 'Buy':
                o_pos_side = 'Short'
            elif not stop_dir and side_o == close_side:
                o_pos_side = pos_side   # plain Limit TP leg, no stopDirection at all
        if o_pos_side != pos_side:
            continue

        if cl_id.startswith('athena_sl_'):
            spx = o.get('stopPxRp')
            if spx:
                sl_price = float(spx)
        elif cl_id.startswith('athena_tp'):
            # athena_tp{suffix}_{code}_{ts} -> ['athena', 'tp1', 'GF', '...']
            parts = cl_id.split('_')
            suffix = parts[1][2:] if len(parts) > 1 and len(parts[1]) > 2 else '?'
            code = parts[2] if len(parts) > 2 else 'UNK'
            price = o.get('priceRp')
            qty = o.get('leavesQtyRq') or o.get('orderQtyRq')
            if price and qty:
                leg_type = _tp_type_from_code(code)
                tp_legs.append({"level": float(price), "qty": float(qty),
                                 "type": leg_type, "tracks_gex_flip": leg_type == "GEX Flip",
                                 "_suffix": suffix})
    tp_legs.sort(key=lambda leg: leg["_suffix"])
    for leg in tp_legs:
        del leg["_suffix"]
    return sl_price, tp_legs

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
        self._entry_balance_baseline = None   # DRY_RUN only — see _check_fill/_update_blackjack
        self._tp_skip_warned = {}   # "gex"/"cluster" -> last-warned near level,
                                     # see _sync_moving_tps's log-dedup fix
        self._sl_missing = False   # True whenever a real position is known to
                                     # have NO resting stop-loss right now (an
                                     # SL placement failed and every retry so
                                     # far has too) — see _place_sl_with_retry/
                                     # _manage_position's own retry-every-cycle
                                     # safety net, added 2026-07-25 as part of
                                     # hardening the real-money order path,
                                     # which had never actually been exercised.

    async def _place_sl_with_retry(self, symbol, pos_side, sl_price, price_decimals, retries=3):
        """A resting stop-loss is the single most important order this app
        ever places for a real position — losing it silently is the worst
        failure mode here, worse than a missed TP or a slightly-late close.
        Retries with backoff (same 3-attempt/1s-backoff shape copycat.py's
        own proven phemex_amend_sl already uses for the exact same order —
        cross-checked directly against that file's real Phemex field names/
        values while hardening this path). Returns True/False and updates
        self._sl_missing so _manage_position's own per-cycle safety net
        (below) keeps retrying for as long as it takes if every attempt
        here fails, rather than the position ever being silently left
        unprotected with nothing watching for it."""
        last_result = None
        for attempt in range(retries):
            if attempt > 0:
                await asyncio.sleep(1.0)
            try:
                last_result = await place_sl(symbol, pos_side, sl_price, price_decimals)
            except Exception as e:
                last_result = {"code": -1, "msg": str(e)}
            if _order_ok(last_result):
                if self._sl_missing:
                    console_log(f"{self.asset}: {GRN}stop-loss re-established at {fmt_num(sl_price)}{RST}")
                self._sl_missing = False
                return True
        console_log(f"{self.asset}: {RED}{BLD}CRITICAL — stop-loss placement FAILED after {retries} attempts "
                    f"({last_result}) — position has NO resting stop-loss right now{RST}")
        log_event(self.asset, "sl_placement_failed", {"sl_price": sl_price, "result": last_result, "sim": DRY_RUN})
        self._sl_missing = True
        return False

    async def _place_tp_leg_checked(self, symbol, pos_side, qty_str, level, price_decimals, suffix, target_type):
        """A failed TP leg isn't the same kind of danger a failed SL is (no
        resting TP just means no automatic profit-taking, not open-ended
        risk) — so this logs loudly rather than retrying in a loop, same
        general "check the actual response, don't just catch exceptions"
        fix as _place_sl_with_retry, added while hardening the real-money
        order path for the first time it's ever been exercised."""
        try:
            result = await place_tp_leg(symbol, pos_side, qty_str, level, price_decimals, suffix, target_type)
        except Exception as e:
            result = {"code": -1, "msg": str(e)}
        if not _order_ok(result):
            console_log(f"{self.asset}: {RED}TP{suffix} ({target_type}) placement FAILED @ {fmt_num(level)} "
                        f"({result}) — that leg is NOT resting{RST}")
            log_event(self.asset, "tp_placement_failed",
                       {"suffix": suffix, "level": level, "type": target_type, "result": result, "sim": DRY_RUN})
            return False
        return True

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
            # authoritative order data (SimAccount.orders); real mode now
            # queries Phemex's own resting orders directly (fetch_resting_
            # orders/_parse_resting_orders, added 2026-07-25 — the exact
            # gap flagged, not fixed, in the earlier real-trading hardening
            # pass) instead of leaving sl_price/tp_legs empty and guessing.
            sl_price, tp_legs = None, []
            symbol = self.cfg["phemex_symbol"]
            if DRY_RUN:
                sim_orders = get_sim_account().orders
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
            else:
                try:
                    rows = await fetch_resting_orders(symbol)
                    if rows is not None:
                        sl_price, tp_legs = _parse_resting_orders(rows, pos_side)
                except Exception as e:
                    console_log(f"{self.asset}: resting-order lookup failed on restart ({e}) — SL/TP display will show n/a")

            # A resurrected position with NO resting SL order found is
            # EXACTLY what _place_sl_with_retry's own _sl_missing flag
            # exists for — this makes _manage_position's per-cycle retry
            # safety net kick in immediately on restart too, not just for
            # failures that happen while Athena is already running.
            self._sl_missing = (not DRY_RUN) and sl_price is None

            self.position = {"pos_side": pos_side, "qty": size, "fill_price": fill_price,
                              "sl_price": sl_price, "tp_legs": tp_legs,
                              "price_decimals": pd, "qty_decimals": qd}
            self.state = "IN_POSITION"
            self.lights["Order Flow"] = True
            sl_note = ""
            if not DRY_RUN:
                sl_note = f" {DIM}(no resting SL found — will retry placing one){RST}" if self._sl_missing \
                          else f" {DIM}(SL/TP reconciled from Phemex's own resting orders){RST}"
            console_log(f"{self.asset}: found existing {pos_side} position ({size}) on restart — resuming IN_POSITION{sl_note}")
            log_event(self.asset, "reconciled_existing_position", {"pos_side": pos_side, "qty": size,
                                                                     "fill_price": fill_price, "sl_price": sl_price,
                                                                     "tp_legs": tp_legs, "sl_missing": self._sl_missing})

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
        # Daily Loss Limit (2026-07-25) — same scope as ATHENA_ENABLED/
        # _in_entry_blackout: only gates WATCHING->ARMED, never touches an
        # already-open PENDING_FILL/IN_POSITION. Computed once per cycle
        # (it also performs its own unblock check/side effect — see the
        # method's own docstring) and reused in both branches below.
        daily_loss_blocked = self._daily_loss_limit_active(regime)

        if self.state == "WATCHING":
            self.lights["Order Flow"] = False
            if not gate_ok or not ATHENA_ENABLED[self.asset] or _in_entry_blackout() or daily_loss_blocked:
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
            if not gate_ok or not ATHENA_ENABLED[self.asset] or _in_entry_blackout() or daily_loss_blocked:
                self.state = "WATCHING"
                self.lights["Order Flow"] = False
                reason = ("19:00-19:30 CT entry blackout" if _in_entry_blackout()
                          else "Daily Loss Limit active" if daily_loss_blocked
                          else f"{self.asset} paused ([A])" if not ATHENA_ENABLED[self.asset] else "a required condition dropped")
                console_log(f"{self.asset}: {reason} — back to WATCHING")
                log_event(self.asset, "disarmed", {"lights": lights5, "no_session": NO_SESSION,
                                                     "athena_enabled": ATHENA_ENABLED[self.asset]})
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

    def _trade_net_pnl(self, pnl_approx):
        """Exact in DRY_RUN (diffs SimAccount.balance against the snapshot
        taken at fill — see _check_fill), otherwise falls back to the
        caller's own entry-vs-current-price approximation (real mode's
        existing, already-documented limitation)."""
        if DRY_RUN and self._entry_balance_baseline is not None:
            return get_sim_account().balance - self._entry_balance_baseline
        return pnl_approx

    def _update_blackjack(self, pnl):
        """Advances this asset's OWN Blackjack progression (BLACKJACK_STATE
        is per-asset, independent — explicit user answer 2026-07-24) off
        one trade's realized net pnl. No I/O of its own — "R" is always
        computed fresh, from CURRENT balance, at the next entry's sizing
        time (see _check_confirmation), not frozen at the moment a trade
        closes. See BLACKJACK_STEPS' own comment for the full spec this
        implements."""
        if not BLACKJACK_MODE or pnl is None:
            return
        bj = BLACKJACK_STATE[self.asset]
        if bj["in_win_progression"]:
            # This was the 2nd trade of a win progression.
            bj["in_win_progression"] = False
            bj["win_step_back"] = None
            bj["win_profit_dollars"] = None
            if pnl > 0:
                bj["loss_step"] = 0   # two wins in a row — full reset
            # else: loss progression simply resumes at the level it was
            # frozen at when the win progression started — NOT touched here.
        elif pnl > 0:
            step_back = max(0, bj["loss_step"] - 1)
            bj["win_step_back"] = BLACKJACK_STEPS[step_back]
            bj["win_profit_dollars"] = pnl
            bj["in_win_progression"] = True
            # loss_step stays frozen (used to resume from if trade 2 loses)
        else:
            bj["loss_step"] = 0 if bj["loss_step"] >= len(BLACKJACK_STEPS) - 1 else bj["loss_step"] + 1
        _save_blackjack_state()
        if bj["in_win_progression"]:
            status_txt = f"win progression started (next risks {bj['win_step_back']:g}R + ${bj['win_profit_dollars']:,.2f})"
        else:
            status_txt = f"now at {BLACKJACK_STEPS[bj['loss_step']]:g}R"
        console_log(f"{self.asset}: {YLW}Blackjack — {status_txt} (trade pnl {fmt_money(pnl)}){RST}")
        log_event(self.asset, "blackjack_progression_updated", {"pnl": pnl, "state": dict(bj)})

    def _update_daily_loss_limit(self, pnl):
        """Daily Loss Limit tracking (explicit user request, 2026-07-25) —
        see the DAILY_LOSS_STATE module-level comment for the full spec.
        Called alongside _update_blackjack from every close site,
        regardless of BLACKJACK_MODE (this tracks independently of it)."""
        if pnl is None:
            return
        dl = DAILY_LOSS_STATE[self.asset]
        if pnl > 0:
            dl["consecutive_losses"] = 0
        else:
            dl["consecutive_losses"] += 1
            if dl["consecutive_losses"] >= DAILY_LOSS_LIMIT:
                dl["consecutive_losses"] = 0
                dl["blocked"] = True
                dl["blocked_regime"] = self.regime
                dl["blocked_at"] = datetime.now(TZ_CT).isoformat() if TZ_CT else datetime.now().isoformat()
                bj = BLACKJACK_STATE[self.asset]
                bj["loss_step"] = 0
                bj["in_win_progression"] = False
                bj["win_step_back"] = None
                bj["win_profit_dollars"] = None
                _save_blackjack_state()
                console_log(f"{self.asset}: {RED}{BLD}Daily Loss Limit hit — {DAILY_LOSS_LIMIT} consecutive losses. "
                            f"No new entries until PCVR switches or 19:30 CT. Loss progression reset to 1R.{RST}")
                log_event(self.asset, "daily_loss_limit_hit", {"blocked_regime": dl["blocked_regime"]})
        _save_daily_loss_state()

    def _clear_daily_loss_block(self, reason):
        dl = DAILY_LOSS_STATE[self.asset]
        dl["blocked"] = False
        dl["blocked_regime"] = None
        dl["blocked_at"] = None
        _save_daily_loss_state()
        console_log(f"{self.asset}: {GRN}Daily Loss Limit cleared ({reason}) — new entries allowed again{RST}")
        log_event(self.asset, "daily_loss_limit_cleared", {"reason": reason})

    def _daily_loss_limit_active(self, regime):
        """True if this asset is currently blocked from NEW entries by the
        Daily Loss Limit. Also performs the unblock check itself (and
        clears the block as a side effect) — called every cycle from
        process_cycle's own gate, so there's no separate polling loop
        needed for the "PCVR switches or 19:30 CT" conditions."""
        dl = DAILY_LOSS_STATE[self.asset]
        if not dl["blocked"]:
            return False
        # Unblock condition 1: PCVR's regime switched away from whatever
        # it was when the limit was hit — must be a REAL regime (long/
        # short), not just "went neutral", same "flipped" concept
        # _manage_position's own PCVR-flip emergency-close already uses.
        if regime in ("long", "short") and regime != dl["blocked_regime"]:
            self._clear_daily_loss_block("PCVR switched")
            return False
        # Unblock condition 2: 19:30 CT has passed since the block was set.
        if TZ_CT and dl["blocked_at"]:
            try:
                blocked_at = datetime.fromisoformat(dl["blocked_at"])
                if blocked_at.tzinfo is None:
                    blocked_at = blocked_at.replace(tzinfo=TZ_CT)
                if datetime.now(TZ_CT) >= _next_1930_ct_after(blocked_at):
                    self._clear_daily_loss_block("19:30 CT reached")
                    return False
            except Exception:
                pass
        return True

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
        one_r_dollars = balance * (PCT / 100.0)
        if BLACKJACK_MODE:
            bj = BLACKJACK_STATE[self.asset]
            if bj["in_win_progression"]:
                trade_risk_dollars = bj["win_step_back"] * one_r_dollars + bj["win_profit_dollars"]
            else:
                trade_risk_dollars = BLACKJACK_STEPS[bj["loss_step"]] * one_r_dollars
        else:
            trade_risk_dollars = one_r_dollars
        raw_qty = trade_risk_dollars / R
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

        # Snapshot DRY_RUN's own exact running balance right at fill —
        # diffing sim.balance (not realized_pnl_today, which resets every
        # day and would corrupt this across a midnight rollover) against
        # this baseline once the position goes fully flat gives Blackjack
        # an EXACT trade net pnl in sim mode, vs. real mode's existing
        # entry-vs-current-price approximation (see _manage_position/
        # _flatten_now — same documented gap the rest of the app already
        # accepts for real-mode exit pricing).
        self._entry_balance_baseline = get_sim_account().balance if DRY_RUN else None

        R = self.cfg["sl"]
        sl_price = fill_price - R if p["pos_side"] == "Long" else fill_price + R
        symbol = self.cfg["phemex_symbol"]
        await self._place_sl_with_retry(symbol, p["pos_side"], sl_price, p["price_decimals"])

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
            await self._place_tp_leg_checked(symbol, p["pos_side"], f"{tp1_qty:.{qd}f}", tp1_level, p["price_decimals"], "1", tp1_t["type"])
            await self._place_tp_leg_checked(symbol, p["pos_side"], f"{tp2_qty:.{qd}f}", tp2_level, p["price_decimals"], "2", tp2_t["type"])
            tp_legs = [{"level": tp1_level, "qty": tp1_qty, "tracks_gex_flip": tp1_t["type"] == "GEX Flip", "type": tp1_t["type"]},
                       {"level": tp2_level, "qty": tp2_qty, "tracks_gex_flip": tp2_t["type"] == "GEX Flip", "type": tp2_t["type"]}]
        elif valid:
            tp1_level, tp1_t = valid[0]
            await self._place_tp_leg_checked(symbol, p["pos_side"], f"{qty:.{qd}f}", tp1_level, p["price_decimals"], "1", tp1_t["type"])
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
            exit_price = await live_price_for_symbol(symbol)
            entry = self.position.get("fill_price")
            qty = self.position.get("qty")
            pnl_approx = None
            if exit_price is not None and entry:
                pnl_approx = (exit_price - entry) * qty if pos_side == "Long" else (entry - exit_price) * qty
            log_event(self.asset, "position_closed", {"pos_side": pos_side, "qty": qty, "entry": entry,
                                                        "exit_approx": exit_price, "pnl_approx": pnl_approx,
                                                        "sim": DRY_RUN})
            net_pnl = self._trade_net_pnl(pnl_approx)
            self._update_blackjack(net_pnl)
            self._update_daily_loss_limit(net_pnl)
            self.position = None
            self.state = "WATCHING"
            self.lights["Order Flow"] = False
            return

        # Safety net, 2026-07-25: if a PRIOR cycle's SL placement (initial
        # fill or a TP refresh's re-place) ultimately failed after retries,
        # this position has been sitting with NO resting stop since then —
        # keep retrying every cycle for as long as it takes rather than
        # ever letting that be a one-shot attempt that's forgotten about.
        if self._sl_missing:
            sl_price = self.position.get("sl_price")
            pd = self.position.get("price_decimals", 2)
            if sl_price is not None:
                await self._place_sl_with_retry(symbol, pos_side, sl_price, pd, retries=1)

        # A partial TP fill reduces the exchange's own position without it
        # going fully flat — keep the displayed size (and the resting TP
        # legs) in sync, or the dashboard/open-PnL calc would keep showing
        # the original full size and both TP legs forever.
        self.position["qty"] = abs(float(pos.get("size") or 0))
        if DRY_RUN:
            # The sim order itself already carries its own "type" (set at
            # placement — see SimAccount.place_tp_leg/_check_fill/
            # _sync_moving_tps), so read it straight off the order instead
            # of round-tripping through the OLD tp_legs list matched by
            # rounded price — that match silently failed (falling back to
            # "?") whenever a price didn't line up exactly, e.g. right
            # after _sync_moving_tps had just moved it. tracks_gex_flip is
            # always exactly `type == "GEX Flip"` everywhere else it's set
            # (_check_fill, _sync_moving_tps) — same rule here, not a
            # separately round-tripped value.
            sim_orders = get_sim_account().orders
            self.position["tp_legs"] = [
                {"level": o["price"], "qty": o["qty"],
                 "tracks_gex_flip": o.get("type") == "GEX Flip",
                 "type": o.get("type", "?")}
                for o in sim_orders.values() if o["symbol"] == symbol and o["kind"] == "tp"]
        else:
            # Real mode — added 2026-07-25, the SAME resting-order
            # reconciliation reconcile_startup now uses on restart, run
            # here every cycle too so a partial TP fill's tp_legs display
            # doesn't go stale mid-session (the gap flagged, not fixed, in
            # the earlier real-trading hardening pass), AND so a real SL
            # that disappears for any reason OTHER than Athena's own
            # cancel_all (manually cancelled, rejected asynchronously by
            # Phemex's risk engine, etc.) gets caught here too — not just
            # the failure paths _place_sl_with_retry already covers.
            # Best-effort: a lookup failure here leaves tp_legs/sl_missing
            # exactly as they were rather than guessing either way.
            try:
                rows = await fetch_resting_orders(symbol)
                if rows is not None:
                    real_sl_price, real_tp_legs = _parse_resting_orders(rows, pos_side)
                    self.position["tp_legs"] = real_tp_legs
                    if real_sl_price is not None:
                        self.position["sl_price"] = real_sl_price
                        if self._sl_missing:
                            console_log(f"{self.asset}: {GRN}stop-loss confirmed resting at {fmt_num(real_sl_price)}{RST}")
                        self._sl_missing = False
                    else:
                        self._sl_missing = True
            except Exception:
                pass

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
                close_result = await market_close(symbol, pos_side, qty, reason="flip")
            except Exception as e:
                console_log(f"{self.asset}: emergency close FAILED — {e}")
                log_event(self.asset, "pcvr_flip_close_failed", {"error": str(e)})
                return
            if not _order_ok(close_result):
                # Hardening fix, 2026-07-25: Phemex responding 200 OK with an
                # application-level rejection (e.g. bad price, insufficient
                # margin) never raises an exception — the `except` above only
                # ever caught a genuine network/transport failure, so this
                # exact case previously fell straight through and got treated
                # as a successful close: self.position was cleared and
                # "closed" was logged while the REAL position was still open
                # on the exchange, with NOTHING watching it anymore. Now
                # leaves state as IN_POSITION (unchanged) so the very next
                # cycle notices the flip is still active and retries the
                # close, instead of Athena silently believing it's flat.
                console_log(f"{self.asset}: {RED}{BLD}CRITICAL — emergency close FAILED ({close_result}) — "
                            f"position is STILL OPEN, will retry next cycle{RST}")
                log_event(self.asset, "pcvr_flip_close_failed", {"result": close_result})
                return
            exit_price = await live_price_for_symbol(symbol)   # approximate, same caveat as position_closed
            pnl_approx = None
            if exit_price is not None and entry:
                pnl_approx = (exit_price - entry) * qty if pos_side == "Long" else (entry - exit_price) * qty
            log_event(self.asset, "pcvr_flip_close", {"pos_side": pos_side, "qty": qty, "entry": entry,
                                                        "exit_approx": exit_price, "pnl_approx": pnl_approx,
                                                        "sim": DRY_RUN})
            net_pnl = self._trade_net_pnl(pnl_approx)
            self._update_blackjack(net_pnl)
            self._update_daily_loss_limit(net_pnl)
            self.position = None
            self.state = "WATCHING"
            self.lights["Order Flow"] = False
            return

        await self._sync_moving_tps(symbol, pos_side)

    async def _sync_moving_tps(self, symbol, pos_side):
        """Refreshes any TP leg tracking a MOVING target — GEX Flip (a
        single level status.py/gex.py recompute every cycle) or a gamma
        Cluster (explicit user request 2026-07-24: the qualifying Medium/
        Large cluster set can itself change shape mid-trade as gamma
        builds/decays — a Large cluster appearing where only a Medium one
        existed before should retarget that TP leg to it, same
        Large-over-Medium precedence used when the position was first
        entered — see reconstruct_targets/clusters_from_gex_export). A TP
        leg's own `type` field (set at entry — "GEX Flip" or "Cluster",
        see _check_fill/tp_legs) says which rule applies to it; a leg
        whose target type never moves (BT/ST) is left untouched here.

        Full bracket refresh (cancel everything, re-place SL + all TP legs
        at current prices) rather than a selective single-order amend/
        cancel — Phemex's real API for editing just one resting order
        isn't verified/wired up here, and this is simpler and doesn't risk
        touching the wrong order; SL's own price is unchanged so
        re-placing it is a harmless no-op. Trade-off, worth knowing: the
        position briefly has no resting SL between the cancel and
        re-place calls."""
        tp_legs = self.position.get("tp_legs") or []
        if not any(leg.get("tracks_gex_flip") or leg.get("type") == "Cluster" for leg in tp_legs):
            return

        fill_price = self.position.get("fill_price")
        R = self.cfg["sl"]

        def _safe(level):
            return (level >= fill_price + R) if pos_side == "Long" else (level <= fill_price - R)

        gex_export = read_gex_export(self.asset)

        new_gex_level = None
        gex_flip = gex_export.get("gex_flip") if gex_export else None
        if gex_flip is not None:
            new_gex_level = gex_flip - GEX_FLIP_TP_BUFFER if pos_side == "Long" else gex_flip + GEX_FLIP_TP_BUFFER

        new_cluster_level = None
        if gex_export:
            direction = "above" if pos_side == "Long" else "below"
            clusters = clusters_from_gex_export(gex_export, fill_price, direction)
            clusters.sort(key=lambda kt: 0 if kt[1] == "Large" else 1)
            if clusters:
                new_cluster_level = clusters[0][0]

        new_legs = []
        changed = False
        for leg in tp_legs:
            level = leg["level"]
            # Same guard as the initial placement (_check_fill) — if the
            # refreshed target has drifted close enough to the fill price
            # that it would land within R of it (or past it), skip this
            # leg's refresh (leave the currently-resting order as-is) and
            # retry once it moves back to a safe distance, rather than
            # ever placing an unsafe level.
            if leg.get("tracks_gex_flip") and new_gex_level is not None:
                if _safe(new_gex_level):
                    self._tp_skip_warned.pop("gex", None)
                    if abs(level - new_gex_level) > 0.005:
                        level, changed = new_gex_level, True
                else:
                    # Log-spam fix, 2026-07-25 (user-reported: "TP refresh
                    # is getting skipped constantly") — this branch is
                    # re-evaluated every engine cycle (as fast as 2s) for
                    # as long as the target genuinely stays too close to
                    # the fill price, which is a real, possibly long-lived
                    # market condition, not a bug — but logging the IDENTICAL
                    # message every single cycle while it persists was pure
                    # noise. Now logs once when the skip starts (or the near
                    # level moves by more than a cent), then stays silent
                    # until it either clears or moves again.
                    if self._tp_skip_warned.get("gex") is None or abs(self._tp_skip_warned["gex"] - new_gex_level) > 0.01:
                        console_log(f"{self.asset}: {YLW}GEX Flip too close to fill (${fmt_num(new_gex_level)}) — TP refresh skipped this cycle{RST}")
                        self._tp_skip_warned["gex"] = new_gex_level
            elif leg.get("type") == "Cluster" and new_cluster_level is not None:
                if _safe(new_cluster_level):
                    self._tp_skip_warned.pop("cluster", None)
                    if abs(level - new_cluster_level) > 0.005:
                        level, changed = new_cluster_level, True
                else:
                    if self._tp_skip_warned.get("cluster") is None or abs(self._tp_skip_warned["cluster"] - new_cluster_level) > 0.01:
                        console_log(f"{self.asset}: {YLW}nearest Cluster too close to fill (${fmt_num(new_cluster_level)}) — TP refresh skipped this cycle{RST}")
                        self._tp_skip_warned["cluster"] = new_cluster_level
            new_legs.append({"level": level, "qty": leg["qty"],
                              "tracks_gex_flip": leg.get("tracks_gex_flip", False),
                              "type": leg.get("type", "?")})

        if not changed:
            return

        pd = self.position.get("price_decimals", 2)
        qd = self.position.get("qty_decimals", 2)
        try:
            cancel_result = await cancel_all(symbol)
        except Exception as e:
            console_log(f"{self.asset}: TP refresh — cancel failed ({e})")
            return
        if not _order_ok(cancel_result):
            # Hardening fix, 2026-07-25: same "check the response, not just
            # exceptions" gap as the flip-close path — a failed cancel here
            # must abort the refresh rather than proceeding to re-place SL/
            # TP on top of whatever's still resting (duplicate orders), and
            # since nothing was cancelled the ORIGINAL SL/TP are presumably
            # still in place, so this is a safe abort, not a naked position.
            console_log(f"{self.asset}: {RED}TP refresh — cancel failed ({cancel_result}), aborting this cycle's refresh{RST}")
            log_event(self.asset, "tp_refresh_cancel_failed", {"result": cancel_result})
            return
        # The cancel above just pulled the SL too — every attempt to
        # re-place it here is genuinely load-bearing (see _place_sl_with_
        # retry's own docstring: a naked real position is the worst
        # failure mode in this whole app), not just a formality.
        sl_price = self.position.get("sl_price")
        if sl_price is not None:
            await self._place_sl_with_retry(symbol, pos_side, sl_price, pd)
        for i, leg in enumerate(new_legs, start=1):
            await self._place_tp_leg_checked(symbol, pos_side, f"{leg['qty']:.{qd}f}", leg["level"], pd, str(i), leg.get("type", "?"))
        self.position["tp_legs"] = new_legs
        console_log(f"{self.asset}: {YLW}TP target(s) refreshed — {[fmt_num(l['level']) for l in new_legs]}{RST}")
        log_event(self.asset, "tp_targets_adjusted", {"legs": new_legs})

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
                    close_result = await market_close(symbol, pos_side, qty, reason=reason)
                except Exception as e:
                    console_log(f"{self.asset}: {label} close FAILED — {e}")
                    log_event(self.asset, f"{event_name}_failed", {"error": str(e)})
                    return
                if not _order_ok(close_result):
                    # Same hardening fix as the PCVR-flip-close path — a 200
                    # OK application-level rejection never raises, so this
                    # must be checked explicitly or a failed EOD/manual
                    # flatten gets silently logged and treated as closed
                    # while the real position stays open with no SL/TP
                    # (cancel_all already ran above) and nothing watching it.
                    console_log(f"{self.asset}: {RED}{BLD}CRITICAL — {label} close FAILED ({close_result}) — "
                                f"position is STILL OPEN{RST}")
                    log_event(self.asset, f"{event_name}_failed", {"result": close_result})
                    return
            exit_price = await live_price_for_symbol(symbol)   # approximate, same caveat as position_closed
            pnl_approx = None
            if exit_price is not None and entry and qty:
                pnl_approx = (exit_price - entry) * qty if pos_side == "Long" else (entry - exit_price) * qty
            log_event(self.asset, event_name, {"pos_side": pos_side, "qty": qty, "entry": entry,
                                                "exit_approx": exit_price, "pnl_approx": pnl_approx,
                                                "sim": DRY_RUN})
            net_pnl = self._trade_net_pnl(pnl_approx)
            self._update_blackjack(net_pnl)
            self._update_daily_loss_limit(net_pnl)
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
    caveat). Same detail.sim==False guard as scan_all_trade_events's own
    2026-07-25 fix — today's athena_logs file can still contain events
    from an EARLIER --dry-run run the same day (log_event writes there
    regardless of mode), which would otherwise show up in the real-mode
    blotter too if the process was later restarted without --dry-run."""
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
                elif (not DRY_RUN and d.get("event") in ("position_closed", "pcvr_flip_close", "eod_flatten", "manual_flatten")
                      and (d.get("detail") or {}).get("sim") is False):
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
            det = d.get("detail") or {}
            if det.get("sim") is not False:
                continue   # same guard as recent_closed_trades/scan_all_trade_events — skip
                            # any --dry-run-era event mixed into today's athena_logs file
            if ev == "filled":
                last_fill[asset] = (d.get("ts"), det.get("fill_price"))
            elif ev in ("position_closed", "pcvr_flip_close", "eod_flatten", "manual_flatten"):
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
    for the equity curve.

    CRITICAL bug fixed 2026-07-25, user-reported ("previously cleared sim
    trades are shown in the PnL chart"): log_event() (unlike sim_log_event)
    writes to athena_logs on EVERY close regardless of DRY_RUN — each
    close event's own detail dict already carries `"sim": DRY_RUN` for
    exactly this reason, but this function's 'real' branch never checked
    it, so every trade ever closed while running --dry-run permanently
    bled into the 'real' (LIVE Phemex) tab's all-time chart too — and
    survived [R]eset/--reset-sim, since those only ever archive sim_logs,
    never athena_logs. Now requires detail.sim to be explicitly False for
    the 'real' bucket — a close logged before this field existed (sim
    missing entirely) is excluded rather than guessed at, since silently
    including possibly-fake PnL in the REAL account's own chart is the
    worse failure mode of the two."""
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
                        detail = d.get("detail") or {}
                        if detail.get("sim") is not False:
                            continue
                        pnl = detail.get("pnl_approx")
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

    # DD ($ and %) — added 2026-07-25, user-requested "DD" column: how far
    # BELOW the peak account balance-to-date this trade's own close left
    # the account, walked oldest-to-newest using SimAccount's own real
    # running balance (exits[-1]["balance"] above) rather than a
    # PnL-reconstructed one — the exact figure the paper account actually
    # had, fees included, at that moment.
    peak_balance = None
    for t in out:
        bal = t.get("balance")
        if bal is None:
            t["dd"], t["dd_pct"] = None, None
            continue
        peak_balance = bal if peak_balance is None else max(peak_balance, bal)
        t["dd"] = peak_balance - bal
        t["dd_pct"] = (t["dd"] / peak_balance * 100.0) if peak_balance > 0 else None
    return out

def compute_trade_stats(events, source="sim"):
    pnls = [e["pnl"] for e in events]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    total_pnl = sum(pnls)

    # Max drawdown ($ and %) — added 2026-07-25. Walks the SAME cumulative-
    # PnL equity curve _draw_equity_curve already renders (oldest to
    # newest), tracking the running peak and the worst drop below it —
    # works identically for 'sim' and 'real' since both sources produce
    # the same {ts, pnl} shape. % needs a dollar reference to divide by;
    # 'sim' can back into the starting balance THIS all-time epoch began
    # at (current SimAccount.balance minus this epoch's own total PnL —
    # [R]eset/--reset-sim always archives sim_logs, so "all-time" here is
    # always exactly one continuous epoch from a single known starting
    # balance, never a mix of several). 'real' has no equivalent running-
    # balance tracking wired up (same already-documented approximate-PnL-
    # only gap real mode has everywhere else), so its % stays None —
    # shown as "n/a", not guessed at. Distinct from (but consistent with)
    # scan_all_trades_detailed's own PER-TRADE "dd"/"dd_pct" columns,
    # which use SimAccount's real running balance per trade instead of a
    # PnL-reconstructed one — that one's sim-only and more precise; this
    # one's the summary metric for the chart shared with real mode too.
    # NOTE: max $ drawdown and max % drawdown are tracked as two genuinely
    # independent running maximums, each against its OWN contemporaneous
    # peak — not the dollar figure divided by the FINAL/overall peak after
    # the fact. A later, larger peak can otherwise make an earlier, smaller
    # -peak drawdown look proportionally tinier than it really was at the
    # time it happened (caught by this function's own test suite: a
    # sequence peaking at $150 then dropping to $50 before later climbing
    # to $200 must still score that $100 drop as ~0.99% of a ~$10,150
    # peak-equity, not ~0.98% of the eventual ~$10,200).
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_pct = None
    if source == "sim" and n:
        starting_balance = get_sim_account().balance - total_pnl
        peak_equity = starting_balance
        max_dd_pct = 0.0
        for p in pnls:
            cum += p
            equity = starting_balance + cum
            peak = max(peak, cum)
            peak_equity = max(peak_equity, equity)
            max_dd = max(max_dd, peak - cum)
            if peak_equity > 0:
                max_dd_pct = max(max_dd_pct, (peak_equity - equity) / peak_equity * 100.0)
    else:
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

    # Sharpe ratio — added 2026-07-25. mean(trade net PnL) / stdev(trade
    # net PnL) across the whole trade sequence. This is a PER-TRADE
    # Sharpe, not an annualized one: trades don't land on a fixed time
    # interval (no consistent "daily return" to annualize from), so this
    # uses the standard simplification for discrete-trade systems —
    # reward-to-variability per trade, unitless, comparable across
    # different account sizes. Needs >=2 trades (a single data point has
    # no variance to divide by) and non-zero variance.
    sharpe = None
    if n >= 2:
        mean_pnl = total_pnl / n
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (n - 1)
        stdev = variance ** 0.5
        if stdev > 0:
            sharpe = mean_pnl / stdev

    return {
        "count": n, "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / n * 100.0) if n else 0.0,
        "total_pnl": total_pnl,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "largest_win": max(wins) if wins else 0.0,
        "largest_loss": min(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win <= 0 else float("inf")),
        "max_drawdown": max_dd, "max_drawdown_pct": max_dd_pct,
        "sharpe": sharpe,
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
    ("REASON", 10), ("GROSS", 10), ("FEES", 7), ("BALANCE", 11), ("DD", 10),
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
            (fmt_money(-t["dd"]) if t.get("dd") is not None else "—", P_RED if t.get("dd") else P_DIM),
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
    y += 1
    dd_pct_txt = f" ({stats['max_drawdown_pct']:.1f}%)" if stats.get("max_drawdown_pct") is not None else ""
    sharpe_txt = f"{stats['sharpe']:.2f}" if stats.get("sharpe") is not None else "n/a"
    db.puts_ansi(y, 0, f"{BLD}Risk{RST}      Max Drawdown {RED}{fmt_money(-stats['max_drawdown'])}{dd_pct_txt}{RST}   "
                       f"Sharpe {sharpe_txt}")
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

def take_screenshot(db):
    """[C] — plain-text dump of exactly what's currently on screen, same
    convention as footprint.py's own [P] screenshot (folder + filename
    style, txt not an image — curses has no pixel buffer to capture).
    Simpler here than footprint.py's version: footprint.py writes straight
    to curses via safe_add() and needs a SEPARATE shadow buffer just to
    reconstruct what was drawn; Athena's own DoubleBuffer.prev already IS
    that reconstruction (the last fully-flushed frame's cell grid), so
    this just reads it directly — no extra tracking needed. Read `prev`,
    not `buf`: by the time a keypress is handled, flush() has already
    copied buf->prev and wiped buf for the next frame, so buf itself is
    always blank at this point in the loop."""
    folder = os.path.join(SCRIPT_DIR, "screenshots")
    os.makedirs(folder, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fn = os.path.join(folder, f"athena_{ts}.txt")
    rows = db.prev if db.prev is not None else db.buf
    lines = ["".join(cell[0] for cell in row).rstrip() for row in rows]
    with open(fn, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return fn

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
        elif part == MAG: pair = P_MAGENTA
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
CROSSHAIR_LINE_CH = "│"   # [Z]/[X] crosshair vertical line — matches footprint.py's own
IMBALANCE_RATIO = 3.0
MIN_IMBALANCE_VOL = 0.0
STACK_COUNT = 3
BIG_TRADE_SIZE = 100.0
PROFILE_MODES = ("volume", "delta", "ohlc", "off")
DASHBOARD_H = 25   # fixed row budget so the chart region's top edge never
                   # jitters cycle to cycle regardless of how much optional
                   # content (pending order/position/TP-leg line) is present
                   # — 4 header/account rows + 1 Margin Used row (added
                   # 2026-07-25) + 2×6 per-instrument rows (rule/meter+
                   # price/lights/position/TP/spacer) + 4 closed-trades
                   # (incl. spacer) + 4 activity (incl. trailing spacer
                   # before the chart) = 25. A single blank row separates
                   # each section, INCLUDING between Recent Activity and
                   # the chart below it (2026-07-23, explicit user
                   # request) — was 19 with no inter-section spacing
                   # before that. This budget sits ABOVE the chart
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
    """Spans across day-folders (footprint_log_paths), not a single
    footprint_log_glob file — see that function's own 2026-07-25 fix
    note: without this, the chart's history silently truncated to
    whatever's in ONLY the newest day-folder's file the instant a local
    day rollover occurred, mid-session, with no warning."""
    paths = footprint_log_paths(asset)
    bars = []
    for path in reversed(paths):
        try:
            with open(path, encoding="utf-8") as f:
                lines = [l for l in f if l.strip()]
        except Exception:
            continue
        try:
            bars = [json.loads(l) for l in lines] + bars
        except Exception:
            continue
        if len(bars) >= n:
            break
    return bars[-n:]

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
                          hscroll_bars=0, live_price=None, live_bar=None, focused=False, market_closed=False,
                          crosshair_bar_idx=None):
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
    engine_loop's live_bar_state tracking for why.

    crosshair_bar_idx (2026-07-25, re-created per explicit user request —
    footprint.py's own [Z]/[X] crosshair was deliberately dropped during
    the original port as "always a live tail, read-only") is the SAME "N
    bars back from newest" addressing as hscroll_bars, identifying the one
    bar the crosshair currently has selected; None means inactive. Only
    ever addresses a real CLOSED bar (never the synthetic live_bar) —
    same restriction footprint.py's own crosshair has, since a forming
    bar has no persisted OHLC/levels to select in the first place."""
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
    # Folded into the header string itself, not a separate overlay drawn
    # on top after the fact — an earlier version wrote "QQQ closed" as a
    # standalone db.puts() call at this same row AFTER this header was
    # already drawn, which just overwrote characters in the middle of the
    # header text in place (DoubleBuffer.puts replaces cells, it doesn't
    # blend), producing exactly the garbled "QQQ FQQQ closedSED" text the
    # user reported. Building it into one single header string avoids any
    # possibility of two separate draws colliding on the same row.
    closed_tag = "  [MARKET CLOSED]" if market_closed else ""
    # Compact crosshair readout — folded into the SAME header string for
    # the same reason closed_tag is (see the comment block above): a
    # second db.puts() overlay on this row is exactly what caused the
    # "QQQ closed" corruption bug. Full O/H/L/C are already visible via
    # the reverse-video stat table below once the crosshair is active
    # (see crosshair_i, computed further down) — this is a short "which
    # bar, what time" confirmation, not a restatement of every value,
    # since a split pane rarely has room for footprint.py's own
    # full-width status-bar readout.
    ch_header_bar = None
    if crosshair_bar_idx is not None:
        ch_abs = len(bars) - 1 - crosshair_bar_idx
        if 0 <= ch_abs < len(bars):
            ch_header_bar = bars[ch_abs]
    ch_tag = (f"  ✛ {datetime.fromtimestamp(ch_header_bar['ts']).strftime('%H:%M:%S')} "
              f"C:{fmt_price(ch_header_bar['c'])} Δ:{fmt_delta(ch_header_bar.get('delta', 0.0))}") if ch_header_bar else ""
    header = f" {asset} FOOTPRINT — {profile_mode.upper()}{scroll_tag}{focus_tag}{closed_tag}{ch_tag} "
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

    crosshair_i = None
    crosshair_bar = None
    if crosshair_bar_idx is not None:
        abs_idx = len(bars) - 1 - crosshair_bar_idx
        start = max(0, end - hist_n)
        if start <= abs_idx < end:
            crosshair_i = abs_idx - start
            crosshair_bar = bars[abs_idx]

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

        # [Z]/[X] crosshair vertical line — runs down the visual CENTER of
        # the selected bar's column (ported from footprint.py's own
        # draw(), charthacker.py:2732+ note above). Non-destructive: only
        # drawn into a cell that's still blank, same principle the live
        # price line above already uses, checked directly against
        # db.buf (this IS the "shadow buffer" footprint.py's own version
        # needs a separate structure for, since DoubleBuffer already
        # tracks exactly this).
        if i == crosshair_i:
            center_x = cx + (CHART_COL_W - 1) // 2
            if x0 <= center_x < x1:
                for r_i in range(plot_h):
                    row_y = top + r_i
                    if db.buf[row_y][center_x][0] == " ":
                        db.put(row_y, center_x, CROSSHAIR_LINE_CH, P_CYAN)

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
            # TradingView-style price-axis badge — user request 2026-07-25:
            # "the live price line should have a label of the same color
            # with the live price on the price axis... the same way
            # TradingView does." Same P_YELLOW as the line itself, solid
            # reverse-video so it reads as a filled badge rather than plain
            # text (same badge convention _level_line's own Entry/SL/TP
            # tags already use elsewhere in this function) — overwrites
            # whatever regular dim gridline label might otherwise land on
            # this exact row, same priority TradingView's own current-price
            # badge always takes over a coincidentally-aligned gridline.
            db.puts(live_row_y, x0, fmt_price(live_price).rjust(CHART_AXIS_W - 1),
                    P_YELLOW, curses.A_BOLD | curses.A_REVERSE)

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
        # rev: the crosshair's selected column gets reverse-video across
        # every row of this stat table (plus the time-axis label below),
        # same as footprint.py's own convention — makes the selected bar
        # unambiguous without needing a separate status-bar readout to
        # spell out every value (the header's compact readout, added
        # below, is a supplement, not a replacement, for this).
        rev = curses.A_REVERSE if i == crosshair_i else 0
        db.puts(o_row, cx, fmt_price(bar["o"]).center(w), P_CYAN, curses.A_BOLD | rev)
        db.puts(h_row, cx, fmt_price(bar["h"]).center(w), P_GREEN, curses.A_BOLD | rev)
        db.puts(l_row, cx, fmt_price(bar["l"]).center(w), P_RED, curses.A_BOLD | rev)
        c_pair = P_GREEN if bar["c"] >= bar["o"] else P_RED
        db.puts(c_row, cx, fmt_price(bar["c"]).center(w), c_pair, curses.A_BOLD | rev)

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
        db.puts(delta_row, cx, centered, d_pair, curses.A_BOLD | rev)
        if shift:
            shift_pair = P_GREEN if shift.strip() == "(+)" else P_RED
            shift_col = cx + centered.find(full_text) + len(delta_text)
            db.puts(delta_row, shift_col, shift, shift_pair, curses.A_BOLD | rev)

        poc_price, vah_price, val_price = bar_stats[i]
        db.puts(vah_row, cx, fmt_price(vah_price).center(w), P_DIM, rev)
        db.puts(val_row, cx, fmt_price(val_price).center(w), P_DIM, rev)
        prev_poc = bar_stats[i - 1][0] if i > 0 else None
        if poc_price is not None and prev_poc is not None and poc_price > prev_poc: p_pair = P_GREEN
        elif poc_price is not None and prev_poc is not None and poc_price < prev_poc: p_pair = P_RED
        else: p_pair = P_DEFAULT
        db.puts(poc_row, cx, fmt_price(poc_price).center(w), p_pair, curses.A_BOLD | rev)

        # Time axis — HH:MM:SS, same "fine" cadence footprint.py's own
        # fmt_time uses for any interval under 5 minutes (90s bars qualify).
        time_txt = "LIVE" if bar.get("live") else datetime.fromtimestamp(bar["ts"]).strftime("%H:%M:%S")
        db.puts(time_row, cx, time_txt.center(w), P_YELLOW if bar.get("live") else P_DIM, rev)

def _footprint_crosshair_clamp(crosshair_bar_idx, hscroll_bars, total, n_est):
    """Ported verbatim (in spirit) from footprint.py's own _crosshair_clamp
    (footprint.py:2935-2950) — keeps the crosshair bar inside the current
    view, panning hscroll_bars just enough to follow it past either edge.
    footprint.py's own version has a `historical_mode` flag (its --date
    playback feature) that skips the final live-edge clamp; Athena has no
    historical/date-playback mode at all — always live — so that clamp
    always applies here unconditionally, the one adaptation this port
    needed. Pure function, directly testable without a real curses
    screen, same reasoning footprint.py's own docstring gives for pulling
    it out of the key-handling loop."""
    crosshair_bar_idx = max(0, min(crosshair_bar_idx, max(0, total - 1)))
    if crosshair_bar_idx < hscroll_bars:
        hscroll_bars = crosshair_bar_idx
    elif crosshair_bar_idx > hscroll_bars + n_est - 1:
        hscroll_bars = max(0, crosshair_bar_idx - (n_est - 1))
    hscroll_bars = max(0, min(hscroll_bars, max(0, total - n_est)))
    return crosshair_bar_idx, hscroll_bars

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
        self.margin_used = None
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
        balance, available, margin_used = account_balance_fields(acc)
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
                "sl_missing": i._sl_missing,
                "daily_loss_blocked": DAILY_LOSS_STATE[i.asset]["blocked"],
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
            self.margin_used = margin_used
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
                "balance": self.balance, "available": self.available, "margin_used": self.margin_used,
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

GEX_COL_W = 2   # matches gex.py's own COL_W (terminal columns per time interval)
GEX_ARROW_STEP = 5      # matches gex.py's own ARROW_STEP (time-axis columns per Left/Right)
GEX_PAGE_STEP = 30      # matches gex.py's own PAGE_STEP (time-axis columns per PgUp/PgDn)
GEX_VERT_STEP = 1       # matches gex.py's own VERT_STEP (strikes per Up/Down)
GEX_VERT_PAGE_STEP = 8  # matches gex.py's own VERT_PAGE_STEP (strikes per [/])

def _gex_dot_repr(net, scale_max):
    """Ported from gex.py's own dot_repr, returning (pair, attrs) split
    instead of one combined curses attr int (Athena's db.put/db.puts take
    them as two separate args) — None in place of gex.py's pair=0 for a
    blank cell, so the caller can skip the db.put() call entirely rather
    than paint a meaningless pair-0 space."""
    ch, frac = magnitude_char(net, scale_max)
    if ch == " ":
        return " ", None
    pair = P_GREEN if net > 0 else P_RED
    return ch, (pair, curses.A_BOLD if frac >= 0.35 else 0)

def _gex_price_marker_repr(net, scale_max):
    """Ported from gex.py's own price_marker_repr."""
    ch, frac = magnitude_char(net, scale_max, floor_char="·")
    return ch, (P_YELLOW, curses.A_BOLD if frac >= 0.35 else 0)

def _gex_draw_pieces(db, row, x0, pieces, max_x):
    """Draws a sequence of (text, pair, attrs) pieces left-to-right
    starting at x0 — Athena's equivalent of gex.py's own inline
    `x = 0; for text, attr in pieces: safe_add(win, row, x, text, attr);
    x += len(text)` pattern, repeated for the header/legend/info rows in
    both draw_gex_map and draw_gex_by_strike."""
    x = x0
    for text, pair, attrs in pieces:
        if x >= max_x:
            break
        db.puts(row, x, text[:max(0, max_x - x)], pair, attrs)
        x += len(text)
    return x

def _gex_build_header(history, meta, live_follow, end_idx, asset, is_crypto, title, log_rows, diag_count):
    """Ported from gex.py's own build_header, returning (text, pair,
    attrs) triples instead of (text, attr). Simplified from gex.py's own
    version in two ways, both deliberate scope boundaries (a different
    FEATURE than what Athena's GEX mode needs, not an approximation of
    one it does): no HISTORICAL_MODE/`--date` playback tag (Athena's GEX
    mode only ever shows live data, it has no standalone day-browsing
    mode), and no per-write log_err surfacing (GEX_LOG_ROWS already only
    increments on a successful write — see _gex_engine_loop)."""
    label = meta.get("expiry_label", "—")
    ttl = meta.get("ttl")
    is_0dte = meta.get("is_0dte", True if is_crypto else False)
    fetched = meta.get("fetched_at", "—")

    if live_follow:
        mode_piece = ("● LIVE", P_GREEN, curses.A_BOLD)
        spot = history[-1]["spot"] if history else meta.get("spot", 0.0)
    else:
        viewed_ts = history[end_idx - 1]["ts"].strftime("%H:%M:%S") if history else "—"
        mode_piece = (f"⏸ HISTORY @{viewed_ts}", P_YELLOW, curses.A_BOLD)
        spot = history[end_idx - 1]["spot"] if history else meta.get("spot", 0.0)

    pieces = [
        (title, P_CYAN, curses.A_BOLD),
        ("  │  ", P_DIM, 0),
        (asset, P_DEFAULT, curses.A_BOLD),
        ("  ", P_DIM, 0),
        mode_piece,
        ("  Spot ", P_DIM, 0),
        (f"${spot:,.2f}" if spot else "—", P_YELLOW, curses.A_BOLD),
        ("  Expiry ", P_DIM, 0),
        (f"{label}{'*' if is_0dte else ''}", P_DEFAULT, 0),
    ]
    if ttl and live_follow:
        pieces += [("  TTL ", P_DIM, 0), (ttl, P_YELLOW, curses.A_BOLD)]
    if is_crypto:
        src_tag = "  (live)"
    elif meta.get("spot_is_live"):
        src_tag = "  (spot live via Yahoo, GEX ~15m delay via CBOE)"
    else:
        src_tag = "  (spot+GEX ~15m delay — live quote fetch failed)"
    pieces += [("  Updated ", P_DIM, 0), (fetched, P_DEFAULT, 0), (src_tag, P_DIM, 0)]
    if diag_count:
        pieces += [(f"  Jumps {diag_count}", P_RED, curses.A_BOLD)]
    else:
        pieces += [(f"  Log {log_rows}", P_DIM, 0)]
    return spot, pieces

def draw_gex_map(db, asset, y0, y1, x0, x1, ui):
    """Ported from gex.py's own draw() — the time-interval GEX dot map.
    Y axis = strike, X axis = time (one column per fetch), dot size/color
    = net GEX at that strike at that moment, scaled against the largest
    magnitude seen up to that column (frozen at ingestion — see
    gex_ingest_column/scale_at_ingest, so a later bigger spike never
    retroactively shrinks/grows an already-drawn dot). ui carries this
    pane's own scroll state (live_follow/view_end_idx for time-axis
    panning, vert_follow/vert_center_idx for the strike axis) — same
    semantics as gex.py's own, ported unchanged."""
    h, w = y1 - y0, x1 - x0
    is_crypto = (asset == "ETH")
    history, grid, scale_max, meta = _gex_snapshot_state(asset)

    live_follow = ui["live_follow"]
    n_hist = len(history)
    end_idx = n_hist if live_follow else max(1, min(ui["view_end_idx"], n_hist))

    spot, pieces = _gex_build_header(history, meta, live_follow, end_idx, asset, is_crypto,
                                      "GEX MAP", GEX_LOG_ROWS[asset], GEX_DIAG_COUNT[asset])
    _gex_draw_pieces(db, y0, x0, pieces, x1)

    legend = [
        ("● ", P_GREEN, curses.A_BOLD), ("large  ", P_DIM, 0),
        ("O ", P_GREEN, 0), ("med  ", P_DIM, 0),
        ("o ", P_GREEN, 0), ("small  ", P_DIM, 0),
        (". ", P_GREEN, 0), ("tiny   ", P_DIM, 0),
        ("green", P_GREEN, curses.A_BOLD), ("=+gamma(pin/support)  ", P_DIM, 0),
        ("red", P_RED, curses.A_BOLD), ("=-gamma(accelerant)  ", P_DIM, 0),
        ("●", P_YELLOW, curses.A_BOLD), ("=price(sized)  ", P_DIM, 0),
        ("cyan", P_CYAN, curses.A_BOLD), ("=GEX flip strikes  ", P_DIM, 0),
        ("----", P_DEFAULT, curses.A_BOLD), ("=GEX flip level", P_DIM, 0),
    ]
    _gex_draw_pieces(db, y0 + 1, x0, legend, x1)

    if not history or not grid:
        msg = "Waiting for first snapshot…"
        db.puts(y0 + h // 2, x0 + 2, msg[:max(0, w - 2)], P_CYAN)
        bot = y1 - 1
        hint = f" q=quit  Esc=dashboard  M=next(Status)  [{asset}]  {GEX_STATUS[asset]}"
        db.puts(bot, x0, hint.ljust(w)[:w], P_STATUS)
        return

    axis_w = max(len(fstrike(s, is_crypto)) for s in grid) + 2
    top = y0 + 3
    bottom_reserved = 3
    row_h = 2
    avail_rows = max(1, (h - 3 - bottom_reserved) // row_h)

    grid_sorted = sorted(grid)
    if ui["vert_follow"]:
        center_idx = min(range(len(grid_sorted)), key=lambda i: abs(grid_sorted[i] - spot))
    else:
        center_idx = max(0, min(len(grid_sorted) - 1, ui["vert_center_idx"]))
    half = avail_rows // 2
    lo = max(0, center_idx - half)
    hi = min(len(grid_sorted), lo + avail_rows)
    lo = max(0, hi - avail_rows)
    visible = list(reversed(grid_sorted[lo:hi]))

    usable_w = max(0, w - axis_w - 1)
    n_cols = max(1, usable_w // GEX_COL_W)

    start_idx = max(0, end_idx - n_cols)
    cols = history[start_idx:end_idx]

    max_pain, flip_level = smoothed_max_pain_and_flip(history, end_idx, GEX_SMOOTH_N)
    flip = None
    if flip_level is not None:
        flip = gex_bounding_strikes(grid_sorted, flip_level) + (flip_level,)
    flip_strikes = (flip[0], flip[1]) if flip else ()

    row_of = {}
    for ri, strike in enumerate(visible):
        row = top + ri * row_h
        if row >= y1 - bottom_reserved:
            break
        row_of[strike] = row
        is_current = bool(cols) and cols[-1].get("nearest") == strike
        if is_current:
            lbl_pair, lbl_attrs = P_YELLOW, curses.A_BOLD
        elif strike in flip_strikes:
            lbl_pair, lbl_attrs = P_CYAN, curses.A_BOLD
        else:
            lbl_pair, lbl_attrs = P_DIM, 0
        db.puts(row, x0, fstrike(strike, is_crypto).rjust(axis_w - 1)[:max(0, axis_w - 1)], lbl_pair, lbl_attrs)

        cx = x0 + axis_w
        for col in cols:
            if cx >= x1 - 1:
                break
            net = col["gex"].get(strike, 0.0)
            ch, attr = _gex_dot_repr(net, col.get("scale_at_ingest") or scale_max)
            if attr is not None:
                pair, attrs = attr
                db.put(row, cx, ch, pair, attrs)
            cx += GEX_COL_W

    if flip:
        flip_row = gex_resolve_marker_row(flip[0], flip[1], row_of)
        if flip_row is not None and top <= flip_row < y1 - bottom_reserved:
            cx = x0 + axis_w
            for _ in cols:
                if cx >= x1 - 1:
                    break
                db.put(flip_row, cx, "-", P_DEFAULT, curses.A_BOLD)
                cx += GEX_COL_W

    for ci, col in enumerate(cols):
        cx = x0 + axis_w + ci * GEX_COL_W
        if cx >= x1 - 1:
            break
        spot_c = col["spot"]
        lo_s, hi_s = gex_bounding_strikes(grid_sorted, spot_c)
        target_row = gex_resolve_marker_row(lo_s, hi_s, row_of)
        net_src = lo_s if lo_s == hi_s or abs(spot_c - lo_s) <= abs(spot_c - hi_s) else hi_s
        if target_row is not None and top <= target_row < y1 - bottom_reserved:
            ch, attr = _gex_price_marker_repr(col["gex"].get(net_src, 0.0), col.get("scale_at_ingest") or scale_max)
            pair, attrs = attr
            db.put(target_row, cx, ch, pair, attrs)

    axis_row = y1 - bottom_reserved
    label_every_cols = max(1, 8 // GEX_COL_W)
    cx = x0 + axis_w
    for ci, col in enumerate(cols):
        if cx >= x1 - 6:
            break
        if ci % label_every_cols == 0 or ci == len(cols) - 1:
            ts_str = col["ts"].strftime("%H:%M")
            db.puts(axis_row, cx, ts_str, P_DIM, 0)
        cx += GEX_COL_W

    mp_str = fstrike(max_pain, is_crypto) if max_pain is not None else "N/A"
    net_gex = sum(cols[-1]["gex"].values()) if cols else None
    if net_gex is None:
        net_gex_str, net_gex_pair = "N/A", P_DIM
    else:
        net_gex_str = fdollars_compact(net_gex)
        net_gex_pair = P_GREEN if net_gex >= 0 else P_RED
    if flip:
        lo_f, hi_f, level = flip
        flip_str = (fstrike(level, is_crypto) if lo_f == hi_f else
                     f"{fstrike(level, is_crypto)}  (between {fstrike(lo_f, is_crypto)} & {fstrike(hi_f, is_crypto)})")
    else:
        flip_str = "N/A"
    info_pieces = [
        (" Max Pain ", P_DIM, 0), (mp_str, P_YELLOW, curses.A_BOLD),
        ("    Net GEX ", P_DIM, 0), (net_gex_str, net_gex_pair, curses.A_BOLD),
        ("    Zero Gamma/GEX Flip ", P_DIM, 0), (flip_str, P_CYAN, curses.A_BOLD),
        (f"    (Max Pain/Flip smoothed over last {GEX_SMOOTH_N})", P_DIM, 0),
    ]
    _gex_draw_pieces(db, y1 - bottom_reserved + 1, x0, info_pieces, x1)

    bot = y1 - 1
    other_asset = "QQQ" if asset == "ETH" else "ETH"
    vert_tag = "" if ui["vert_follow"] else "[↕scrolled]"
    hint = (f" q=quit  Esc=dashboard  M=next(Status)  time:←/→/PgUp/PgDn  strikes:↑/↓/[/]/{{/}}  z/End=reset  "
            f"g=by-strike  Tab={other_asset}  {vert_tag} [{asset}] {GEX_STATUS[asset]}")
    db.puts(bot, x0, hint.ljust(w)[:w], P_STATUS)

def draw_gex_by_strike(db, asset, y0, y1, x0, x1, ui):
    """Ported from gex.py's own draw_by_strike() — GEX BY STRIKE bar
    chart (strike on X, GEX $ on Y), matching the Barchart-style "Gamma
    Exposure by Strike" chart. ui['by_strike_net'] toggles one combined
    bar per strike (green/red, gex.py's 'n' key) vs two bars (call blue /
    put — gex.py's own P_ORANGE is just an alias for yellow, "no true
    orange in base 8-color curses" per its own init_colors comment, so
    this uses P_YELLOW directly for puts, same color gex.py itself
    actually renders, not an approximation of it)."""
    h, w = y1 - y0, x1 - x0
    is_crypto = (asset == "ETH")
    history, grid, scale_max, meta = _gex_snapshot_state(asset)
    net_mode = ui.get("by_strike_net", False)
    title = "GEX BY STRIKE (NET)" if net_mode else "GEX BY STRIKE"

    spot, pieces = _gex_build_header(history, meta, True, len(history), asset, is_crypto,
                                      title, GEX_LOG_ROWS[asset], GEX_DIAG_COUNT[asset])
    _gex_draw_pieces(db, y0, x0, pieces, x1)

    if net_mode:
        legend = [
            ("█ ", P_GREEN, curses.A_BOLD), ("net +gamma (call-dominated)  ", P_DIM, 0),
            ("█ ", P_RED, curses.A_BOLD), ("net -gamma (put-dominated)  ", P_DIM, 0),
            ("yellow", P_YELLOW, curses.A_BOLD), ("=current price (strike label + line)  ", P_DIM, 0),
            ("cyan", P_CYAN, curses.A_BOLD), ("=GEX flip strikes  ", P_DIM, 0),
            ("|", P_DEFAULT, curses.A_BOLD), ("=GEX flip level", P_DIM, 0),
        ]
    else:
        legend = [
            ("█ ", P_BLUE, curses.A_BOLD), ("call gamma  ", P_DIM, 0),
            ("█ ", P_YELLOW, curses.A_BOLD), ("put gamma  ", P_DIM, 0),
            ("yellow", P_YELLOW, curses.A_BOLD), ("=current price (strike label + line)  ", P_DIM, 0),
            ("cyan", P_CYAN, curses.A_BOLD), ("=GEX flip strikes  ", P_DIM, 0),
            ("|", P_DEFAULT, curses.A_BOLD), ("=GEX flip level", P_DIM, 0),
        ]
    _gex_draw_pieces(db, y0 + 1, x0, legend, x1)

    if not history or not grid:
        msg = "Waiting for first snapshot…"
        db.puts(y0 + h // 2, x0 + 2, msg[:max(0, w - 2)], P_CYAN)
        bot = y1 - 1
        hint = f" q=quit  Esc=dashboard  M=next(Status)  [{asset}]  {GEX_STATUS[asset]}"
        db.puts(bot, x0, hint.ljust(w)[:w], P_STATUS)
        return

    ref_col = history[-1]
    gex_by_type = ref_col.get("gex_by_type") or {}
    nearest_strike = ref_col.get("nearest")

    top = y0 + 3
    bottom_reserved = 3
    grid_sorted = sorted(grid)
    strike_col_w = 3
    y_axis_w = 10

    usable_w = max(0, w - y_axis_w - 1)
    n_strike_cols = max(1, usable_w // strike_col_w)

    if ui["vert_follow"]:
        center_idx = min(range(len(grid_sorted)), key=lambda i: abs(grid_sorted[i] - spot))
    else:
        center_idx = max(0, min(len(grid_sorted) - 1, ui["vert_center_idx"]))
    half = n_strike_cols // 2
    lo = max(0, center_idx - half)
    hi = min(len(grid_sorted), lo + n_strike_cols)
    lo = max(0, hi - n_strike_cols)
    visible_strikes = grid_sorted[lo:hi]

    scale = 0.0
    if net_mode:
        for strike in visible_strikes:
            scale = max(scale, abs(ref_col["gex"].get(strike, 0.0)))
    else:
        for strike in visible_strikes:
            entry = gex_by_type.get(strike, {})
            scale = max(scale, abs(entry.get("call", 0.0)), abs(entry.get("put", 0.0)))

    avail_v = max(2, h - 3 - bottom_reserved)
    zero_row = top + avail_v // 2
    avail_up = zero_row - top
    avail_down = (y1 - bottom_reserved - 1) - zero_row

    max_pain, flip_level = smoothed_max_pain_and_flip(history, len(history), GEX_SMOOTH_N)
    flip = None
    if flip_level is not None:
        flip = gex_bounding_strikes(grid_sorted, flip_level) + (flip_level,)
    flip_strikes = (flip[0], flip[1]) if flip else ()
    net_gex = sum(ref_col["gex"].values())

    col_of = {}
    for si, strike in enumerate(visible_strikes):
        cx = x0 + y_axis_w + si * strike_col_w
        if cx >= x1 - 1:
            break
        col_of[strike] = cx

    if flip:
        flip_col = gex_resolve_marker_col(flip[0], flip[1], col_of)
        if flip_col is not None:
            for ry in range(top, y1 - bottom_reserved):
                db.put(ry, flip_col, "|", P_DEFAULT, curses.A_BOLD)

    price_lo, price_hi = gex_bounding_strikes(grid_sorted, spot)
    price_col = gex_resolve_marker_col(price_lo, price_hi, col_of)
    if price_col is not None:
        for ry in range(top, y1 - bottom_reserved):
            db.put(ry, price_col, ":", P_YELLOW, curses.A_BOLD)

    for cx in range(x0 + y_axis_w, x1 - 1):
        db.put(zero_row, cx, "─", P_DIM, 0)

    db.puts(top, x0, fdollars_compact(scale).rjust(y_axis_w - 1)[:max(0, y_axis_w - 1)], P_DIM, 0)
    db.puts(zero_row, x0, "$0".rjust(y_axis_w - 1)[:max(0, y_axis_w - 1)], P_DIM, 0)
    db.puts(y1 - bottom_reserved - 1, x0, fdollars_compact(-scale).rjust(y_axis_w - 1)[:max(0, y_axis_w - 1)], P_DIM, 0)

    label_every = max(1, 12 // strike_col_w)
    for si, strike in enumerate(visible_strikes):
        cx = col_of.get(strike)
        if cx is None:
            break
        if net_mode:
            net_val = ref_col["gex"].get(strike, 0.0)
            rows = round(abs(net_val) / scale * (avail_up if net_val >= 0 else avail_down)) if scale > 0 else 0
            pair = P_GREEN if net_val >= 0 else P_RED
            step = -1 if net_val >= 0 else 1
            for r in range(1, rows + 1):
                db.put(zero_row + step * r, cx, "█", pair, curses.A_BOLD)
        else:
            entry = gex_by_type.get(strike, {})
            call_val, put_val = entry.get("call", 0.0), entry.get("put", 0.0)
            call_rows = round(abs(call_val) / scale * avail_up) if scale > 0 else 0
            put_rows = round(abs(put_val) / scale * avail_down) if scale > 0 else 0
            for r in range(1, call_rows + 1):
                db.put(zero_row - r, cx, "█", P_BLUE, curses.A_BOLD)
            for r in range(1, put_rows + 1):
                db.put(zero_row + r, cx, "█", P_YELLOW, curses.A_BOLD)

        if si % label_every == 0 or si == len(visible_strikes) - 1:
            if strike == nearest_strike:
                lbl_pair, lbl_attrs = P_YELLOW, curses.A_BOLD
            elif strike in flip_strikes:
                lbl_pair, lbl_attrs = P_CYAN, curses.A_BOLD
            else:
                lbl_pair, lbl_attrs = P_DIM, 0
            db.puts(y1 - bottom_reserved, max(x0, cx - 1), fstrike(strike, is_crypto), lbl_pair, lbl_attrs)

    mp_str = fstrike(max_pain, is_crypto) if max_pain is not None else "N/A"
    net_gex_str = fdollars_compact(net_gex)
    net_gex_pair = P_GREEN if net_gex >= 0 else P_RED
    if flip:
        lo_f, hi_f, level = flip
        flip_str = (fstrike(level, is_crypto) if lo_f == hi_f else
                     f"{fstrike(level, is_crypto)}  (between {fstrike(lo_f, is_crypto)} & {fstrike(hi_f, is_crypto)})")
    else:
        flip_str = "N/A"
    info_pieces = [
        (" Max Pain ", P_DIM, 0), (mp_str, P_YELLOW, curses.A_BOLD),
        ("    Net GEX ", P_DIM, 0), (net_gex_str, net_gex_pair, curses.A_BOLD),
        ("    Zero Gamma/GEX Flip ", P_DIM, 0), (flip_str, P_CYAN, curses.A_BOLD),
        (f"    (Max Pain/Flip smoothed over last {GEX_SMOOTH_N})", P_DIM, 0),
    ]
    _gex_draw_pieces(db, y1 - bottom_reserved + 1, x0, info_pieces, x1)

    bot = y1 - 1
    other_asset = "QQQ" if asset == "ETH" else "ETH"
    vert_tag = "" if ui["vert_follow"] else "[scrolled]"
    hint = (f" q=quit  Esc=dashboard  M=next(Status)  ←/→/PgUp/PgDn/↑/↓/[/]/{{/}}=pan strikes  z/End=reset  "
            f"g=interval map  n=net/separate  Tab={other_asset}  {vert_tag} [{asset}] {GEX_STATUS[asset]}")
    db.puts(bot, x0, hint.ljust(w)[:w], P_STATUS)

FAST_MATCH_STEP = 0.5   # DRY_RUN only — see _fast_match_wait

async def _fast_match_wait(seconds):
    """Sleeps out the gap between engine_loop cycles, same as a plain
    asyncio.sleep(seconds) — EXCEPT (a) in DRY_RUN, it also calls
    SimAccount.tick_matching() every FAST_MATCH_STEP instead of just once
    at the end, and (b) in EITHER mode, it wakes up early — after at most
    one FAST_MATCH_STEP — the moment _flatten_all_evt/_reset_sim_evt/
    _quit_evt gets set, instead of always sleeping out the FULL interval
    first. That early-wake is what actually fixes "[F]latten takes 10+
    seconds": engine_loop only ever checks those events once per outer
    while-loop iteration, right after this sleep returns — with the old
    plain asyncio.sleep(INTERVAL), confirming [F] mid-sleep meant waiting
    out however much of the up-to-3s INTERVAL was left before Athena even
    NOTICED the flatten had been requested, on top of whatever the
    flatten itself then took.

    tick_matching() itself is now LIVE_TAPE-driven (no REST calls for a
    symbol with an active WS feed — see tick_matching's own docstring), so
    the DRY_RUN branch costs nothing extra network-wise; it just means a
    simulated fill/SL/TP reacts within ~0.5s of the live tape crossing a
    resting order's price instead of waiting for the next full,
    REST-heavy INTERVAL cycle (1-3s, but also everything that cycle does
    ahead of the match check — status/footprint reads, process_cycle for
    every instrument, etc). Still exclusively the engine/asyncio thread
    touching SimAccount, same invariant as always — this is just a finer-
    grained loop on that same thread, not a new one."""
    sim = get_sim_account() if DRY_RUN else None
    remaining = seconds
    while remaining > 0:
        step = min(FAST_MATCH_STEP, remaining)
        await asyncio.sleep(step)
        remaining -= step
        if sim is not None:
            try:
                await sim.tick_matching()
            except Exception:
                pass
        if _flatten_all_evt.is_set() or _reset_sim_evt.is_set() or _quit_evt.is_set():
            return

async def engine_loop():
    if not DRY_RUN and (not PHEMEX_API_KEY or not PHEMEX_API_SECRET):
        console_log(f"{RED}PHEMEX_API_KEY/PHEMEX_API_SECRET not set in .env — real trading needs both{RST}")
        log_event("SYSTEM", "startup_error", {"error": "missing Phemex API keys"})
        return

    console_log(f"{BLD}Athena starting{RST} — interval={INTERVAL}s pct={PCT}% dry_run={DRY_RUN}")
    log_event("SYSTEM", "startup", {"interval": INTERVAL, "pct": PCT, "dry_run": DRY_RUN, "no_session": NO_SESSION})
    if not DRY_RUN:
        # Safety requirement (explicit user request 2026-07-25) — ATHENA_ENABLED
        # was already initialized to False for every asset at module load
        # (see its own comment) whenever DRY_RUN starts False; this just makes
        # that fact loud and visible the moment the real account comes up.
        console_log(f"{RED}{BLD}LIVE ACCOUNT{RST} — {YLW}every asset PAUSED, press [A] to enable trading{RST}")

    if DRY_RUN:
        sim = get_sim_account()
        if RESET_SIM:
            sim.reset(SIM_BALANCE_ARG)
            _reset_blackjack_state()
            _reset_daily_loss_state()
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
                _reset_blackjack_state()
                _reset_daily_loss_state()
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
        await _fast_match_wait(INTERVAL)

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
    # Per-asset PAUSED state now shown on each instrument's own title line
    # below (not here) — a single shared tag stopped making sense once [A]
    # could pause ETH and QQQ independently.
    mode_tag = ('  [DRY RUN]' if DRY_RUN else '') + ('  [24H MODE]' if NO_SESSION else '')
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
    y += 1
    # Margin actively in use across ALL open positions (Phemex's own
    # totalUsedBalanceRv, SimAccount.to_account_snapshot mirrors the same
    # field for DRY_RUN) — user request 2026-07-25. Own row, not crammed
    # onto the ACCOUNT line above, which was already at 4 fields.
    margin_used = snap.get("margin_used")
    margin_pct = (margin_used / snap["balance"] * 100.0) if margin_used is not None and snap.get("balance") else None
    pct_tag = f" ({margin_pct:.1f}% of balance)" if margin_pct is not None else ""
    db.puts_ansi(y, 0, f"{DIM}Margin Used{RST}   {fmt_money(margin_used) if margin_used is not None else 'n/a'}{DIM}{pct_tag}{RST}")
    y += 2   # blank row separates ACCOUNT from the first instrument block

    for asset in ASSETS:
        inst = snap["instruments"].get(asset) or {}
        lights = inst.get("lights", {})
        names = gated_light_names()
        segs = "".join((GRN if lights.get(n) else RED) + "█" + RST for n in names)
        count = sum(1 for n in names if lights.get(n))
        paused_tag = " [PAUSED]" if not ATHENA_ENABLED[asset] else ""
        loss_limit_tag = " [LOSS LIMIT]" if inst.get("daily_loss_blocked") else ""
        title = f"── {asset}{paused_tag}{loss_limit_tag} "
        db.puts(y, 0, (title + "─" * max(2, min(cols, BOX_W) - len(title)))[:cols], P_DIM)
        if paused_tag:
            # Overlay just the tag in red/bold — the dash-fill above is
            # already drawn plain-DIM the full width, so this only needs
            # to color the few characters the tag itself occupies.
            tag_x = len(f"── {asset}")
            db.puts(y, tag_x, paused_tag[:max(0, cols - tag_x)], P_RED, curses.A_BOLD)
        if loss_limit_tag:
            # Same overlay technique, positioned after paused_tag (both
            # can show at once — paused and loss-limited are independent).
            tag_x = len(f"── {asset}{paused_tag}")
            db.puts(y, tag_x, loss_limit_tag[:max(0, cols - tag_x)], P_RED, curses.A_BOLD)
        y += 1
        if inst.get("market_closed"):
            regime_txt, state_txt = "n/a", "CLOSED"
        else:
            regime_txt, state_txt = (inst.get("regime") or "none").upper(), inst.get("state", "?")
        price_txt = f"   {DIM}price {inst['price']:.2f}{RST}" if inst.get("price") is not None else ""
        bj_txt = ""
        if BLACKJACK_MODE:
            bj = BLACKJACK_STATE[asset]
            if bj["in_win_progression"]:
                bj_txt = f"   {DIM}BJ: win progression ({fmt_num(bj['win_step_back'], 0)}R + ${bj['win_profit_dollars']:,.2f}){RST}"
            else:
                bj_txt = f"   {DIM}BJ: {fmt_num(BLACKJACK_STEPS[bj['loss_step']], 0)}R{RST}"
        db.puts_ansi(y, 0, f"  [{segs}] {count}/{len(names)}   regime: {regime_txt}   state: {state_txt}{price_txt}{bj_txt}")
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
            # sl_missing (2026-07-25): a real position whose SL placement
            # failed (initial fill or a moving-TP refresh's re-place, after
            # every retry) has NO resting stop right now — _manage_position
            # keeps retrying it every cycle in the background, but that's
            # invisible unless it's ALSO surfaced here; a naked real
            # position is exactly the kind of thing that must never be
            # silent.
            sl_warn = f"  {RED}{BLD}⚠ NO STOP-LOSS RESTING{RST}" if inst.get("sl_missing") else ""
            # SL distance tag — user request 2026-07-25, same "[X.XX away]"
            # convention the TP legs line already uses, so the position
            # line reads consistently with it: how far live price
            # currently is from the resting stop, i.e. how much room is
            # left before it triggers.
            sl_dist_tag = f" {RED}[{fmt_num(abs(live - p['sl_price']))} away]{RST}" \
                          if live is not None and p.get("sl_price") is not None else ""
            line3 = (f"  {YLW}Position: {p['pos_side']} {fmt_num(p['qty'], 2)} @ {fmt_num(p['fill_price'])}   "
                     f"SL {fmt_num(p.get('sl_price'))}{sl_dist_tag}   uPnL {pnl_color(pnl)}{fmt_money(pnl)}{RST}{sl_warn}")
        if line3:
            db.puts_ansi(y, 0, line3)
        y += 1

        # TP legs — one fixed row (blank if none), both legs on the same
        # line, each showing which target (BT/ST/GEX Flip/Cluster) it's
        # tied to, per explicit user request to see "the HPL they are
        # associated with", plus how far the live price currently is from
        # each level (a second explicit user request — fetched fresh here
        # via inst.get("live_price") rather than reusing the `live` local
        # from the position/pending line above, since that branch may not
        # have run this cycle — e.g. PENDING_FILL has no "position" key —
        # and would leave it undefined).
        tp_legs = (inst.get("position") or {}).get("tp_legs") or []
        if tp_legs:
            live_for_tp = inst.get("live_price")
            parts = []
            for i, leg in enumerate(tp_legs[:2], start=1):
                gex_tag = f"{DIM}(GEX){RST}" if leg.get("tracks_gex_flip") else ""
                # User request 2026-07-25: the target label inside the "()"
                # and the distance-to-target tag should both be green — was
                # DIM (gray) for both, same as the surrounding "TP{i} (...)"
                # punctuation, which made the actually-useful bits (which
                # HPL this leg targets, how far price still has to go) no
                # more visually prominent than the label scaffolding around
                # them.
                dist_tag = f"{GRN}[{fmt_num(abs(live_for_tp - leg['level']))} away]{RST} " \
                           if live_for_tp is not None else ""
                parts.append(f"{DIM}TP{i} ({RST}{GRN}{leg.get('type', '?')}{RST}{DIM}){RST} "
                             f"{fmt_num(leg['qty'], 2)} @ {fmt_num(leg['level'])} {dist_tag}{gex_tag}")
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

# ── [G] Switch to live account ────────────────────────────────────────────────
def _prompt_confirm_text(stdscr, box_lines, confirm_word):
    """Blocking modal warning pop-up — a bordered box (box_lines, plain
    strings, centered) drawn in bold red, with a type-to-confirm text
    prompt underneath requiring the user to type `confirm_word` exactly
    (case-insensitive) before proceeding. Esc cancels at any point, same
    as every other blocking dialog in this file. Deliberately a stronger
    gate than the [R]eset/[F]latten "press the same key again" 2-step
    convention — those undo a PAPER balance or close positions Athena
    itself opened; this one switches to placing REAL-money orders, which
    warrants a real typed confirmation, not just a second keypress that
    could land accidentally. Writes directly to stdscr (bypassing
    DoubleBuffer), same established pattern _prompt_number/_prompt_text
    already use for blocking dialogs — caller must set db.prev = None
    afterward to force a full repaint. Returns True only if the user
    typed the exact confirm word and pressed Enter."""
    rows, cols = stdscr.getmaxyx()
    box_w = min(cols - 4, 74)
    prompt = f'Type "{confirm_word}" to confirm, Esc to cancel: '
    box_h = len(box_lines) + 4
    x0 = max(0, (cols - box_w) // 2)
    y0 = max(0, (rows - box_h) // 2)
    input_row = y0 + 1 + len(box_lines) + 1

    buf = ""
    result = False
    curses.curs_set(1)
    stdscr.nodelay(False)
    try:
        while True:
            attrs = curses.color_pair(P_RED) | curses.A_BOLD
            try:
                stdscr.addstr(y0, x0, ("┌" + "─" * (box_w - 2) + "┐")[:box_w], attrs)
                for i, line in enumerate(box_lines):
                    stdscr.addstr(y0 + 1 + i, x0, ("│" + line.center(box_w - 2)[:box_w - 2] + "│"), attrs)
                stdscr.addstr(y0 + 1 + len(box_lines), x0, ("│" + " " * (box_w - 2) + "│"), attrs)
                stdscr.addstr(input_row, x0, ("│" + (prompt + buf).ljust(box_w - 2)[:box_w - 2] + "│"), attrs)
                stdscr.addstr(input_row + 1, x0, ("└" + "─" * (box_w - 2) + "┘")[:box_w], attrs)
            except curses.error:
                pass
            try:
                stdscr.move(input_row, min(cols - 1, x0 + 1 + len(prompt) + len(buf)))
            except curses.error:
                pass
            stdscr.refresh()
            try:
                ch = stdscr.get_wch()
            except Exception:
                continue
            if ch == "\x1b":
                result = False
                break
            if ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                result = buf.strip().upper() == confirm_word.upper()
                break
            if ch in ("\x08", "\x7f") or ch == curses.KEY_BACKSPACE:
                buf = buf[:-1]
            elif isinstance(ch, str) and ch.isprintable():
                buf += ch
    finally:
        curses.curs_set(0)
        stdscr.nodelay(True)
    return result

def _go_live(stdscr):
    """[G] — switch from --dry-run paper trading to the REAL Phemex
    account, from within the running UI (explicit user request 2026-07-25).
    Gated by a blocking warning pop-up (_prompt_confirm_text) requiring
    the user to type LIVE, not just a second keypress — the stakes here
    are real money, not a paper balance, so this is deliberately a
    stronger confirmation than [R]eset/[F]latten's own arm-then-confirm
    convention.

    SAFETY REQUIREMENT: the instant live mode is entered — from here, or
    from launching `python athena.py` without --dry-run in the first
    place (see ATHENA_ENABLED's own module-level comment) — every
    asset's ATHENA_ENABLED goes False. Athena will not place a single new
    trade until the user manually reviews the situation and presses [A]
    for each asset they actually want trading; PENDING_FILL/IN_POSITION
    management is unaffected either way (same scope [A] already has).

    Refuses outright if PHEMEX_API_KEY/PHEMEX_API_SECRET aren't set —
    going live with no credentials would just fail on the very first real
    order anyway; better to catch it here with a clear message than let
    the user find out via a failed entry."""
    global DRY_RUN
    if not DRY_RUN:
        console_log(f"{YLW}Already on the live Phemex account{RST}")
        return
    if not PHEMEX_API_KEY or not PHEMEX_API_SECRET:
        console_log(f"{RED}{BLD}Cannot switch to live — PHEMEX_API_KEY/PHEMEX_API_SECRET not set in .env{RST}")
        log_event("SYSTEM", "go_live_refused_no_keys", {})
        return

    warning = [
        "",
        f"{BLD}⚠  SWITCHING TO YOUR LIVE PHEMEX ACCOUNT  ⚠{RST}",
        "",
        "All orders and trades placed from this point on will be",
        "executed with REAL FUNDS on your real Phemex account.",
        "",
        "Every asset's trading will start PAUSED — you must",
        "manually press [A] to enable each one before Athena",
        "places any new trade.",
        "",
    ]
    confirmed = _prompt_confirm_text(stdscr, warning, confirm_word="LIVE")
    if not confirmed:
        console_log(f"{DIM}Switch to live account cancelled{RST}")
        return

    DRY_RUN = False
    for a in ASSETS:
        ATHENA_ENABLED[a] = False
    console_log(f"{RED}{BLD}LIVE ACCOUNT ACTIVE{RST} — {YLW}every asset PAUSED, press [A] to enable trading{RST}")
    log_event("SYSTEM", "switched_to_live", {"assets_paused": list(ASSETS.keys())})

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

def draw_status_screen(db, y0, y1, x0, x1, scroll):
    """Phase 3c of the standalone-merge plan: a full-screen curses rendering
    of status.py's own render() output (see _status_build_render_lines),
    drawn from the pre-computed STATUS_RENDER_LINES cache (refreshed every
    STATUS_SNAPSHOT_INTERVAL by _status_snapshot_loop) via db.puts_ansi —
    the SAME ANSI-tag-parsing helper draw_activity_log_popup already uses,
    so every RED/GRN/YLW/CYN/MAG/BLD/DIM color status.py's own render()
    embeds in a line just works here unchanged. Unlike status.py's own
    fixed-height terminal (which just reprints the whole thing every
    cycle), this content can be taller than the available screen — plain
    top-to-bottom scroll (scroll=0 is the top), clamped here so callers
    don't need to know the content length in advance.

    Returns the clamped scroll value so the caller's own state stays in
    sync with what was actually drawn (same convention table_scroll/
    activity_log_scroll already use elsewhere in this file)."""
    h, w = y1 - y0, x1 - x0
    with STATUS_STATE_LOCK:
        lines = list(STATUS_RENDER_LINES)

    bottom_hint_rows = 1
    visible_rows = max(1, h - bottom_hint_rows)

    if not lines:
        msg = "Waiting for first status snapshot…"
        db.puts(y0 + h // 2, x0 + max(0, (w - len(msg)) // 2), msg[:w], P_CYAN)
        hint = " q=quit  Esc=dashboard  M=next(Trading) "
        db.puts(y1 - 1, x0, hint.ljust(w)[:w], P_STATUS)
        return 0

    total = len(lines)
    scroll = max(0, min(scroll, max(0, total - visible_rows)))
    for i, line in enumerate(lines[scroll:scroll + visible_rows]):
        db.puts_ansi(y0 + i, x0, line[:w])

    scroll_tag = f"  [{scroll + 1}-{min(total, scroll + visible_rows)} of {total}]" if total > visible_rows else ""
    hint = f" q=quit  Esc=dashboard  M=next(Trading)  ↑/↓/PgUp/PgDn=scroll{scroll_tag} "
    db.puts(y1 - 1, x0, hint.ljust(w)[:w], P_STATUS)
    return scroll

# ── Main curses loop ───────────────────────────────────────────────────────────
def curses_main(stdscr):
    global PCT, NO_SESSION, ATHENA_ENABLED, BLACKJACK_MODE
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.mousemask(0)
    init_curses_colors()
    stdscr.timeout(100)

    threading.Thread(target=_run_engine, daemon=True).start()
    for asset in ASSETS:
        feed_starters = []
        # QQQ's live tape prefers Athena's own dedicated Alpaca account
        # (real QQQ shares, matching footprint.py's own closed-bar source
        # for QQQ) over Phemex's thinner QQQUSDT perp — per explicit user
        # request. Falls back to the Phemex tape if those credentials
        # aren't set, so a missing .env entry degrades gracefully instead
        # of leaving QQQ's live chart with no feed at all.
        if asset == "QQQ" and ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY:
            feed_starters.append((_alpaca_trade_ws, (asset, _quit_evt)))
        else:
            if asset == "QQQ":
                console_log(f"{YLW}ALPACA_API_ATHENA_ID/ALPACA_API_SECRET_KEY_ATHENA not set — "
                            f"QQQ live tape falling back to Phemex QQQUSDT{RST}")
            feed_starters.append((_phemex_trade_ws, (asset, _quit_evt)))
        # ETH's live tape aggregates Phemex + Kraken + Coinbase — matching
        # footprint.py's own multi-exchange source for ETH's closed bars
        # exactly, so a live bar isn't blind to a real print that happened
        # to land on Kraken/Coinbase first (see the module-level comment
        # above KRAKEN_WS_URL for the confirmed live discrepancy this fixes).
        if asset == "ETH":
            feed_starters.append((_kraken_trade_ws, (asset, _quit_evt)))
            feed_starters.append((_coinbase_trade_ws, (asset, _quit_evt)))
        # Backfill runs to completion on its OWN thread BEFORE this asset's
        # live feed(s) start — see _backfill_then_feeds's own docstring for
        # why (mirrors footprint.py's initialize_today()-then-start_feeds()
        # sequencing without blocking curses startup for both assets).
        threading.Thread(target=_backfill_then_feeds, args=(asset, feed_starters), daemon=True).start()
        # GEX engine (Phase 2 of the standalone-merge plan) — one
        # background thread per asset, independent of the footprint
        # backfill/feed sequencing above (GEX has its own separate data
        # source entirely — options chains, not trade prints — so there's
        # no shared race to avoid the way footprint's late-trade guard
        # requires backfill-before-live-feed).
        threading.Thread(target=_gex_engine_loop, args=(asset, _quit_evt), daemon=True).start()
    threading.Thread(target=_fast_publish_loop, args=(_quit_evt,), daemon=True).start()
    # Status engine (Phase 3 of the standalone-merge plan) — 3 threads,
    # same cadences/shape as status.py's own main()+live_price_loop+
    # snapshot_logger_loop (one full-refresh cycle covers BOTH assets at
    # once, unlike the GEX engine above which is genuinely per-asset).
    threading.Thread(target=_status_full_refresh_loop, args=(_quit_evt,), daemon=True).start()
    threading.Thread(target=_status_live_price_loop, args=(_quit_evt,), daemon=True).start()
    threading.Thread(target=_status_snapshot_loop, args=(_quit_evt,), daemon=True).start()
    # CH engine (Phase 3b of the standalone-merge plan) — charthacker.py's
    # own live-WS-fed VAH/VWAP layer, one thread per asset (mirrors its own
    # ws_phemex/ws_yahoo feeds) plus one shared export loop (mirrors its
    # status_export_loop). Independent of the status engine threads above —
    # evaluate_hpls picks this up automatically via read_status_charthacker_
    # export the moment it starts producing fresh data, no other change
    # needed (same "zero consumer-side changes" pattern as every prior phase).
    threading.Thread(target=_ch_engine_thread_eth, args=(_quit_evt,), daemon=True).start()
    threading.Thread(target=_ch_engine_thread_qqq, args=(_quit_evt,), daemon=True).start()
    threading.Thread(target=_ch_export_loop, args=(_quit_evt,), daemon=True).start()

    rows, cols = stdscr.getmaxyx()
    db = DoubleBuffer(rows, cols)

    # Chart scroll/focus is purely a UI concern (which historical window
    # each pane shows) — lives only on this thread, never touches AppState
    # or the trading engine. hscroll_bars follows footprint.py's own "N
    # bars back from the newest" convention; 0 is always the live tail.
    chart_scroll = {"ETH": 0, "QQQ": 0}
    chart_focus = "ETH"
    chart_zoom = False   # [Z] — fullscreen whichever pane is [Tab]-focused
                          # instead of the normal ETH/QQQ side-by-side split
    # [X] crosshair — re-created 2026-07-25 per explicit user request
    # (footprint.py's own [Z]/[X] crosshair was deliberately dropped
    # during the original port as "always a live tail, read-only").
    # footprint.py's own Z/X keys aren't both available here — [Z] is
    # already Athena's own pane-zoom toggle — so this uses ONE key,
    # [X], to activate/deactivate, and repurposes the EXISTING [←]/[→]
    # keys to move the crosshair bar-by-bar (instead of panning the
    # whole view) while it's active, auto-panning the view to follow it
    # via _footprint_crosshair_clamp exactly like footprint.py's own
    # auto-follow does. Esc/[Home] deactivate it, same as every other
    # panning key already resets to live.
    chart_crosshair_active = {"ETH": False, "QQQ": False}
    chart_crosshair_idx = {"ETH": 0, "QQQ": 0}      # "N bars back from newest"
    gex_mode = False   # full-screen GEX mode (Phase 2 of the standalone-
                        # merge plan), replacing the dashboard+chart
                        # entirely while active, same as data_view. Phase 4:
                        # [M] cycles Trading -> GEX -> Status -> Trading —
                        # gex_mode/status_mode are never both True at once;
                        # see the [M]/Esc key dispatch for each mode below.
    gex_by_strike = False   # [G] within gex_mode — dot-map (False) vs
                             # GEX-by-strike bar chart (True)
    gex_by_strike_net = False   # [N] within gex_mode's by-strike view
    gex_live_follow = {"ETH": True, "QQQ": True}      # time-axis auto-follow (dot-map)
    gex_view_end_idx = {"ETH": 0, "QQQ": 0}           # frozen time-axis index when panned
    gex_vert_follow = {"ETH": True, "QQQ": True}      # strike-axis auto-center
    gex_vert_center_idx = {"ETH": 0, "QQQ": 0}        # frozen strike-axis index when scrolled
    status_mode = False   # full-screen Status mode (Phase 3c) — the [M]
                            # cycle's next stop after GEX; see gex_mode above
    status_scroll = 0
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
                              "stats": compute_trade_stats(events, source=data_source)}
            if trades_cache.get("source") != data_source or now_mono - trades_cache["ts"] > 2.0:
                trades = scan_all_trades_detailed() if data_source == "sim" else []
                trades_cache = {"source": data_source, "ts": now_mono, "trades": trades}
            data_table_info = draw_data_view(db, rows, cols, data_source, data_cache["events"],
                                              data_cache["stats"], trades_cache["trades"], table_scroll)
        elif gex_mode:
            # Full screen (0..rows, not 0..footer_row) — draw_gex_map/
            # draw_gex_by_strike own their entire own status/hint row at
            # the very last line, same as gex.py's own screen does, so
            # there's no separate Athena footer drawn underneath (see the
            # footer dispatch below).
            gex_asset = chart_focus if chart_focus in ASSETS else "ETH"
            gex_ui = {"live_follow": gex_live_follow[gex_asset], "view_end_idx": gex_view_end_idx[gex_asset],
                      "vert_follow": gex_vert_follow[gex_asset], "vert_center_idx": gex_vert_center_idx[gex_asset],
                      "by_strike_net": gex_by_strike_net}
            if gex_by_strike:
                draw_gex_by_strike(db, gex_asset, 0, rows, 0, cols, gex_ui)
            else:
                draw_gex_map(db, gex_asset, 0, rows, 0, cols, gex_ui)
        elif status_mode:
            # Full screen, same convention as gex_mode above — draw_status_
            # screen draws its own hint row at the very last line, so no
            # separate Athena footer is drawn underneath (see the footer
            # dispatch below).
            status_scroll = draw_status_screen(db, 0, rows, 0, cols, status_scroll)
        else:
            chart_top = 0 if dashboard_hidden else (DASHBOARD_H if rows - 1 > DASHBOARD_H + 10 else 0)
            if chart_top:
                draw_dashboard(db, snap, cols)
            chart_bottom = footer_row

            both_panes = chart_bottom - chart_top > 10
            if both_panes:
                eth_inst = snap["instruments"].get("ETH") or {}
                qqq_inst = snap["instruments"].get("QQQ") or {}
                eth_bars = snap["footprint_bars"].get("ETH") or []
                qqq_bars = snap["footprint_bars"].get("QQQ") or []
                eth_live = eth_inst.get("live_price") if eth_inst.get("live_price") is not None else eth_inst.get("price")
                qqq_live = qqq_inst.get("live_price") if qqq_inst.get("live_price") is not None else qqq_inst.get("price")
                eth_live_bar = snap["live_bars"].get("ETH")
                qqq_live_bar = snap["live_bars"].get("QQQ")
                if chart_zoom:
                    # [Z] fullscreen — whichever pane [Tab] currently has
                    # focused gets the ENTIRE chart width, same panel-
                    # drawing call as the split view just with x0=0/x1=cols
                    # instead of a half. [Tab] still works while zoomed, to
                    # flip which asset fills the screen without leaving
                    # zoom mode first.
                    zoom_asset = chart_focus
                    bars = eth_bars if zoom_asset == "ETH" else qqq_bars
                    inst = eth_inst if zoom_asset == "ETH" else qqq_inst
                    live = eth_live if zoom_asset == "ETH" else qqq_live
                    live_bar = eth_live_bar if zoom_asset == "ETH" else qqq_live_bar
                    draw_footprint_panel(db, zoom_asset, bars, inst, chart_top, chart_bottom, 0, cols,
                                          profile_mode, build_trade_markers(zoom_asset, bars, inst, snap["trade_pairs"]),
                                          hscroll_bars=chart_scroll[zoom_asset], live_price=live, live_bar=live_bar,
                                          focused=True, market_closed=(zoom_asset == "QQQ" and snap["qqq_market_closed"]),
                                          crosshair_bar_idx=(chart_crosshair_idx[zoom_asset] if chart_crosshair_active[zoom_asset] else None))
                else:
                    mid = cols // 2
                    draw_footprint_panel(db, "ETH", eth_bars, eth_inst, chart_top, chart_bottom, 0, mid,
                                          profile_mode, build_trade_markers("ETH", eth_bars, eth_inst, snap["trade_pairs"]),
                                          hscroll_bars=chart_scroll["ETH"], live_price=eth_live, live_bar=eth_live_bar,
                                          focused=(chart_focus == "ETH"),
                                          crosshair_bar_idx=(chart_crosshair_idx["ETH"] if chart_crosshair_active["ETH"] else None))
                    draw_footprint_panel(db, "QQQ", qqq_bars, qqq_inst, chart_top, chart_bottom, mid, cols,
                                          profile_mode, build_trade_markers("QQQ", qqq_bars, qqq_inst, snap["trade_pairs"]),
                                          hscroll_bars=chart_scroll["QQQ"], live_price=qqq_live, live_bar=qqq_live_bar,
                                          focused=(chart_focus == "QQQ"), market_closed=snap["qqq_market_closed"],
                                          crosshair_bar_idx=(chart_crosshair_idx["QQQ"] if chart_crosshair_active["QQQ"] else None))

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
        elif gex_mode:
            pass   # draw_gex_map/draw_gex_by_strike already drew their own
                   # full status/hint row at the screen's very last line —
                   # same as gex.py's own screen does — so there's nothing
                   # left for Athena's own generic footer to add here.
        elif status_mode:
            pass   # draw_status_screen already drew its own hint row —
                   # same reasoning as gex_mode above.
        else:
            # [Tab] hint placed right after the scroll hint it's directly
            # related to (not appended at the very end) — the footer is
            # already long enough on a modest terminal that anything tacked
            # on last just gets silently truncated by the ljust/[:cols]
            # below, which is exactly what was hiding this hint before.
            tab_hint = f" [Tab]:{chart_focus} [Z]:{'full' if chart_zoom else 'split'}" if both_panes else ""
            focus_asset = chart_focus if both_panes else "ETH"
            x_hint = " [X]:crosshair-ON" if chart_crosshair_active.get(focus_asset) else " [X]:crosshair"
            footer = (f"{ts}  [Q]uit [A]:{focus_asset} {'OFF' if not ATHENA_ENABLED[focus_asset] else 'on'} [V]iew:{profile_mode}"
                      f" [←→]scroll{x_hint}{tab_hint} [Home]live"
                      f" [P]ct:{PCT:g}% [B]:{'BJ' if BLACKJACK_MODE else 'flat'} [N]:{'24H' if NO_SESSION else 'sess'} [D]ata [L]og [C]apture [M]:GEX/Status"
                      + ("  [R]eset  [G]o live" if DRY_RUN else "")
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

        if status_mode:
            # Scrollable full-screen text, not a chart with pan state, so
            # the key set is smaller than gex_mode's. Phase 4: [M] and Esc
            # both land back on Trading here — Status is the LAST stop in
            # the Trading -> GEX -> Status -> Trading cycle, so "next" and
            # "exit" coincide at this one stop (see the gex_mode block
            # below for where they diverge).
            if key in (ord("q"), ord("Q")):
                _quit_evt.set()
            elif key in (ord("m"), ord("M"), 27):
                status_mode = False
            elif key == curses.KEY_UP:
                status_scroll = max(0, status_scroll - 1)
            elif key == curses.KEY_DOWN:
                status_scroll += 1
            elif key == curses.KEY_PPAGE:
                status_scroll = max(0, status_scroll - 10)
            elif key == curses.KEY_NPAGE:
                status_scroll += 10
            elif key == curses.KEY_HOME:
                status_scroll = 0
            elif key == curses.KEY_END:
                status_scroll = 10 ** 9   # clamped down to the real max inside draw_status_screen
            continue

        if gex_mode:
            # Ported from gex.py's own curses_main key dispatch. One
            # deliberate adaptation: gex.py uses Esc (alongside z/Z/End)
            # to reset pan/scroll to live — Athena instead uses Esc to
            # LEAVE gex_mode, matching the same cross-mode convention
            # data_view/activity_log_open already use ([mode-key] or Esc
            # = back out). z/Z/End alone still does gex.py's own reset.
            #
            # Phase 4: [M] no longer means "exit" here — it means "next
            # mode" in the Trading -> GEX -> Status -> Trading cycle, so
            # from GEX it moves on to Status instead of leaving to Trading.
            # Esc remains the fast "back to Trading" escape hatch from any
            # mode, unchanged.
            gex_asset = chart_focus if chart_focus in ASSETS else "ETH"
            if key in (ord("q"), ord("Q")):
                _quit_evt.set()
            elif key == 27:
                gex_mode = False
            elif key in (ord("m"), ord("M")):
                gex_mode = False
                status_mode = True
                status_scroll = 0
            elif key in (ord("g"), ord("G")):
                gex_by_strike = not gex_by_strike
            elif key in (ord("n"), ord("N")) and gex_by_strike:
                gex_by_strike_net = not gex_by_strike_net
            elif key == 9:   # Tab — switch asset, same convention as the footprint charts
                chart_focus = "QQQ" if chart_focus == "ETH" else "ETH"
            elif key in (ord("z"), ord("Z"), curses.KEY_END):
                gex_live_follow[gex_asset] = True
                gex_vert_follow[gex_asset] = True
            elif key == curses.KEY_LEFT:
                if gex_by_strike:
                    _gex_vert_step(gex_asset, -GEX_VERT_STEP, gex_vert_follow, gex_vert_center_idx)
                else:
                    n_hist = len(GEX_HISTORY[gex_asset])
                    if gex_live_follow[gex_asset]:
                        gex_view_end_idx[gex_asset] = n_hist
                    gex_live_follow[gex_asset] = False
                    gex_view_end_idx[gex_asset] = max(1, gex_view_end_idx[gex_asset] - GEX_ARROW_STEP)
            elif key == curses.KEY_RIGHT:
                if gex_by_strike:
                    _gex_vert_step(gex_asset, GEX_VERT_STEP, gex_vert_follow, gex_vert_center_idx)
                elif not gex_live_follow[gex_asset]:
                    n_hist = len(GEX_HISTORY[gex_asset])
                    gex_view_end_idx[gex_asset] = min(n_hist, gex_view_end_idx[gex_asset] + GEX_ARROW_STEP)
                    if gex_view_end_idx[gex_asset] >= n_hist:
                        gex_live_follow[gex_asset] = True
            elif key == curses.KEY_PPAGE:
                if gex_by_strike:
                    _gex_vert_step(gex_asset, -GEX_VERT_PAGE_STEP, gex_vert_follow, gex_vert_center_idx)
                else:
                    n_hist = len(GEX_HISTORY[gex_asset])
                    if gex_live_follow[gex_asset]:
                        gex_view_end_idx[gex_asset] = n_hist
                    gex_live_follow[gex_asset] = False
                    gex_view_end_idx[gex_asset] = max(1, gex_view_end_idx[gex_asset] - GEX_PAGE_STEP)
            elif key == curses.KEY_NPAGE:
                if gex_by_strike:
                    _gex_vert_step(gex_asset, GEX_VERT_PAGE_STEP, gex_vert_follow, gex_vert_center_idx)
                elif not gex_live_follow[gex_asset]:
                    n_hist = len(GEX_HISTORY[gex_asset])
                    gex_view_end_idx[gex_asset] = min(n_hist, gex_view_end_idx[gex_asset] + GEX_PAGE_STEP)
                    if gex_view_end_idx[gex_asset] >= n_hist:
                        gex_live_follow[gex_asset] = True
            elif key == curses.KEY_UP:
                _gex_vert_step(gex_asset, GEX_VERT_STEP, gex_vert_follow, gex_vert_center_idx)
            elif key == curses.KEY_DOWN:
                _gex_vert_step(gex_asset, -GEX_VERT_STEP, gex_vert_follow, gex_vert_center_idx)
            elif key == ord("["):
                _gex_vert_step(gex_asset, GEX_VERT_PAGE_STEP, gex_vert_follow, gex_vert_center_idx)
            elif key == ord("]"):
                _gex_vert_step(gex_asset, -GEX_VERT_PAGE_STEP, gex_vert_follow, gex_vert_center_idx)
            elif key == ord("{"):
                gex_vert_follow[gex_asset] = False
                with GEX_STATE_LOCK:
                    gex_vert_center_idx[gex_asset] = max(0, len(GEX_GRID[gex_asset]) - 1)
            elif key == ord("}"):
                gex_vert_follow[gex_asset] = False
                gex_vert_center_idx[gex_asset] = 0
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
            table_scroll = 0   # always reopen scrolled to the newest trades —
                                # without this, a scroll from a PRIOR visit
                                # (e.g. to check older trades) silently stayed
                                # in effect on every later reopen, pinning the
                                # view below newly-closed trades with no
                                # visible sign why the table "stopped updating"
        elif key in (ord("l"), ord("L")):
            activity_log_open = True
            activity_log_scroll = 0
        elif key in (ord("c"), ord("C")):
            fn = take_screenshot(db)
            console_log(f"{CYN}Screenshot saved: {os.path.basename(fn)}{RST}")
            log_event("SYSTEM", "screenshot", {"file": fn})
        elif key in (ord("h"), ord("H")):
            dashboard_hidden = not dashboard_hidden
        elif key in (ord("n"), ord("N")):
            NO_SESSION = not NO_SESSION
            console_log(f"{YLW}Session requirement {'BYPASSED (24H mode)' if NO_SESSION else 'RE-ENABLED'} (via [N]){RST}")
            log_event("SYSTEM", "no_session_toggled", {"no_session": NO_SESSION})
        elif key in (ord("a"), ord("A")):
            # Acts on whichever pane is currently [Tab]-focused — per-asset
            # toggle (explicit user request 2026-07-24), not one shared
            # switch, so ETH and QQQ can be paused/resumed independently.
            focus_asset = chart_focus if both_panes else "ETH"
            ATHENA_ENABLED[focus_asset] = not ATHENA_ENABLED[focus_asset]
            # OFF blocks WATCHING->ARMED->confirm (no NEW entries) for THIS
            # asset only — PENDING_FILL/IN_POSITION management keeps
            # running regardless, see process_cycle's own gating, so this
            # is a genuine "stop taking new signals" pause, not "abandon
            # what's already open."
            console_log((f"{RED}{BLD}{focus_asset} PAUSED (via [A]) — no new entries; "
                          f"open positions still managed normally{RST}") if not ATHENA_ENABLED[focus_asset]
                         else f"{GRN}{BLD}{focus_asset} RESUMED (via [A]){RST}")
            log_event(focus_asset, "athena_enabled_toggled", {"enabled": ATHENA_ENABLED[focus_asset]})
        elif key in (ord("b"), ord("B")):
            BLACKJACK_MODE = not BLACKJACK_MODE
            console_log((f"{YLW}{BLD}Blackjack sizing ON{RST} — 1R,1R,2R,3R,5R loss progression, "
                          f"2-trade win progression (via [B]){RST}") if BLACKJACK_MODE
                         else f"{GRN}{BLD}Blackjack sizing OFF — back to flat {PCT:g}% risk (via [B]){RST}")
            log_event("SYSTEM", "blackjack_mode_toggled", {"enabled": BLACKJACK_MODE})
        elif key in (ord("p"), ord("P")):
            new_pct = _prompt_number(stdscr, f"New risk %% per trade (current {PCT:g}%, Enter keeps it): ", default=PCT)
            db.prev = None
            if new_pct is not None and new_pct != PCT:
                console_log(f"{YLW}Risk changed: {PCT:g}% -> {new_pct:g}% (via [P]){RST}")
                log_event("SYSTEM", "pct_changed", {"old": PCT, "new": new_pct})
                PCT = new_pct
        elif key in (ord("g"), ord("G")) and DRY_RUN:
            _go_live(stdscr)
            db.prev = None   # the warning pop-up wrote straight to stdscr, bypassing
                              # the DoubleBuffer — force a full repaint next frame
        elif key == 9 and both_panes:   # Tab
            chart_focus = "QQQ" if chart_focus == "ETH" else "ETH"
        elif key in (ord("z"), ord("Z")) and both_panes:
            chart_zoom = not chart_zoom
            console_log(f"{YLW}Chart {'zoomed to ' + chart_focus if chart_zoom else 'split back to ETH/QQQ'} (via [Z]){RST}")
        elif key in (ord("m"), ord("M")):
            gex_mode = True
            console_log(f"{YLW}GEX mode ({chart_focus if chart_focus in ASSETS else 'ETH'}) — via [M]{RST}")
        elif key in (ord("x"), ord("X")):
            asset = chart_focus if both_panes else "ETH"
            chart_crosshair_active[asset] = not chart_crosshair_active[asset]
            if chart_crosshair_active[asset]:
                # Activates at the current right edge of the view, same
                # first-press behavior footprint.py's own Z/X has.
                chart_crosshair_idx[asset] = chart_scroll[asset]
        elif key == curses.KEY_LEFT:
            asset = chart_focus if both_panes else "ETH"
            if chart_crosshair_active[asset]:
                total = len(snap["footprint_bars"].get(asset) or [])
                pane_cols = cols if chart_zoom else (cols // 2 if both_panes else cols)
                n_est = max(1, (pane_cols - CHART_AXIS_W) // CHART_COL_W - (1 if chart_scroll[asset] == 0 else 0))
                chart_crosshair_idx[asset], chart_scroll[asset] = _footprint_crosshair_clamp(
                    chart_crosshair_idx[asset] + 1, chart_scroll[asset], total, n_est)
            else:
                max_back = max(0, len(snap["footprint_bars"].get(asset) or []) - 1)
                chart_scroll[asset] = min(max_back, chart_scroll[asset] + 1)
        elif key == curses.KEY_RIGHT:
            asset = chart_focus if both_panes else "ETH"
            if chart_crosshair_active[asset]:
                total = len(snap["footprint_bars"].get(asset) or [])
                pane_cols = cols if chart_zoom else (cols // 2 if both_panes else cols)
                n_est = max(1, (pane_cols - CHART_AXIS_W) // CHART_COL_W - (1 if chart_scroll[asset] == 0 else 0))
                chart_crosshair_idx[asset], chart_scroll[asset] = _footprint_crosshair_clamp(
                    max(0, chart_crosshair_idx[asset] - 1), chart_scroll[asset], total, n_est)
            else:
                chart_scroll[asset] = max(0, chart_scroll[asset] - 1)
        elif key in (curses.KEY_HOME, 27):   # Esc — same reset-to-live convention as footprint.py
            if both_panes:
                chart_scroll["ETH"] = chart_scroll["QQQ"] = 0
            else:
                chart_scroll["ETH"] = 0
            chart_crosshair_active["ETH"] = chart_crosshair_active["QQQ"] = False

if __name__ == "__main__":
    curses.wrapper(curses_main)
