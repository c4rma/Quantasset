#!/usr/bin/env python3
"""
status.py — ETH / QQQ Red-Light/Green-Light Rules Dashboard

Live terminal dashboard that evaluates a fixed list of discretionary trading
rules against current ETH and QQQ price/options data, so they don't have to
be checked by hand across multiple other tools in this repo.

  1. Session          — in a tradable kill zone right now? (reuses
                         opt_dashboard.py's exact KILL_ZONES/exclusion logic)
  2. Volatility       — ETH's Deribit 30d IV index (DVOL), plus the Layer 1
                         position sizing / Layer 2 cap it maps to (fixed
                         lookup table), and QQQ's volatility source, VXN
                         (Cboe Nasdaq-100 Volatility Index, via Yahoo)
  3. PCVR             — put/call volume ratio of the nearest-expiry chain for
                         TLT (08:45-15:00 CT) or BTC (all other times) — this
                         single ratio is the shared ">1.00"/"<1.00" regime
                         signal every conditional HPL rule below reads.
  4. HPLs             — High-Probability Levels, evaluated separately for ETH
                         (Deribit + Phemex) and QQQ (CBOE + Yahoo), 15 rules
                         each, grouped for display into Volume (VAH, VAL,
                         POC, +2/2.5sd, -2/2.5sd, 0.5sd band, VWAP), Expected
                         Range (40-80% band, 100%, 150%), Options (BT, ST,
                         GEX Flip, Medium/Large Gamma Clusters — shows the 2
                         clusters closest to live price, not every qualifying
                         strike), and Miscellaneous (Previous EOD Close).
                         QQQ's rows show CLOSED (not ACTIVE/INACTIVE) from
                         15:00 CT to 08:44 CT the next morning.
  5. Targets          — BT + every Large gamma cluster above price when
                         PCVR < 0.98, or ST + every Large gamma cluster below
                         price when PCVR > 1.02, in green. QQQ only during its
                         own 08:45-15:00 CT hours; ETH always.

Every rule renders ACTIVE (green) when its condition is met right now,
INACTIVE (red) otherwise. A centered "EXECUTE WHEN READY" (green) / "HOLD"
(red) line per instrument sits above the footer — READY only when there's an
active session, PCVR is in an extreme zone (<=0.98 or >=1.02), and that
instrument has at least one genuinely ACTIVE HPL row this cycle.

BT/ST ("Buy Territory"/"Sell Territory"), per the user's spec: scan each
instrument's OWN nearest-expiry chain, strikes ascending (lowest first).
  PCVR > 1.00: ST = the first strike of the first run of 3 consecutive
               strikes where put volume > call volume; BT = the very next
               strike above ST. Only "price >= BT" is scored this regime.
  PCVR < 1.00: BT = the first strike of the first run of 3 consecutive
               strikes where call volume > put volume; ST = the very next
               strike below BT. Only "price <= ST" is scored this regime.
This produces a single ascending-strike split point (put-dominant strikes
below it / call-dominant strikes above it) — matches the two annotated
screenshots the rules were specified from. ASSUMPTION, not yet confirmed
against a hand-checked example: scan direction is ascending from the lowest
listed strike. If this doesn't match your own read of a chain, say so and
the scan direction/starting point is a one-line fix.

Every HPL rule needs price within a tolerance of its level to count as green
(a "the level is being tested right now" reading, not "price is anywhere
beyond it"): ETH $2.00, QQQ $0.25. Gamma Clusters is the one exception with
its own 3-state light instead of plain green/red — green at/under that same
tolerance, yellow out to $5.00 (ETH) / $0.35 (QQQ), red beyond that.

Usage:
  python status.py [--interval SEC]
    --interval SEC   refresh interval for the FULL data fetch — chains,
                      candles, DVOL, PCVR (default 30). Live price (and
                      everything derived purely from it — distances,
                      near()/directional checks, Targets) updates on its own
                      independent 2s cadence regardless of this setting, via
                      tiny/cheap fetches (Deribit index price, Yahoo meta),
                      so the dashboard shows real-time price movement between
                      full refreshes instead of a price frozen at the last one.

Backtest log: every 60s (SNAPSHOT_INTERVAL, independent of --interval), a
full plain-data snapshot of everything currently shown (session, DVOL/VXN,
PCVR, every HPL value+status for both instruments, Targets, Final Status —
same values as the display, just ANSI-stripped and typed) is appended to
  status_logs/YYYY/MM/DD/status_MM_DD_YYYY.jsonl
one JSON object per line, folders created on demand. See
compute_dashboard_snapshot/append_snapshot/snapshot_log_path.

Data sources (all free, snapshot/REST — no websockets):
  ETH price/DVOL/chain  — Deribit REST
  ETH session candles   — Phemex kline/list, full current session in one call
  ETH previous close    — current session's own open (crypto trades 24/7, so
                           "previous close" == this session's open — same
                           value as charthacker.py's "EOpen" line)
  QQQ price/chain       — CBOE delayed-quotes (~15m delay) + Yahoo (live spot)
  QQQ volatility        — VXN via Yahoo (^VXN), not CBOE's iv30
  QQQ session candles   — Yahoo 1m/5d, includePrePost=true
  QQQ previous close    — close of the 15:59 CT candle the prior day (a real
                           market close, genuinely different from today's open)
  TLT/BTC PCVR chain    — CBOE (TLT) / Deribit (BTC), same nearest-expiry
                           pattern as chain.py/gex.py in this repo
"""

import sys
import os
import re
import time
import math
import json
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
except ModuleNotFoundError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"], stdout=subprocess.DEVNULL)
    import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── Windows console: enable VT100/ANSI escape processing ─────────────────────
# Without this, raw ANSI codes (colors, cursor-home, clear-to-end) print as
# literal "←[92m..." text instead of being interpreted — same fix
# opt_dashboard.py already applies for the same reason.
if sys.platform == "win32":
    import ctypes
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

# ── ARGS ─────────────────────────────────────────────────────────────────────
args = sys.argv[1:]
REFRESH_SEC = 30
if "--interval" in args:
    i = args.index("--interval")
    try:
        REFRESH_SEC = max(5, int(args[i + 1]))
    except (IndexError, ValueError):
        pass

# ── ANSI ─────────────────────────────────────────────────────────────────────
RED = '\033[91m'; GRN = '\033[92m'; YLW = '\033[93m'; CYN = '\033[96m'
MAG = '\033[95m'; BLD = '\033[1m';  DIM = '\033[2m';  RST = '\033[0m'

# ── Alert sound (PCVR crossing into an extreme zone) ──────────────────────────
def play_alert():
    """Play alert.wav non-blocking from the same folder as this script — same
    convention opt_dashboard.py already uses for its own sentiment alert."""
    wav = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert.wav")
    if not os.path.exists(wav):
        return
    try:
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            import subprocess
            player = "afplay" if sys.platform == "darwin" else "aplay"
            subprocess.Popen([player, wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

_pcvr_alert_zone = None   # None/"extreme"/"neutral" — tracks the LAST cycle's
                          # zone so the alert only fires on entry into the
                          # extreme zone, not on every refresh while sitting in it

def check_pcvr_alert(data):
    """Plays alert.wav once when PCVR crosses into <=0.98 or >=1.02 from the
    neutral zone — edge-triggered, not level-triggered, so it doesn't replay
    every single refresh cycle while the ratio just sits in that zone."""
    global _pcvr_alert_zone
    pcvr = data.get("pcvr")
    if not pcvr:
        return
    ratio = pcvr["ratio"]
    zone = "extreme" if (ratio <= 0.98 or ratio >= 1.02) else "neutral"
    if zone == "extreme" and _pcvr_alert_zone != "extreme":
        play_alert()
    _pcvr_alert_zone = zone

# inst_name -> {"above_bt": bool_or_None, "below_st": bool_or_None} — tracks
# whether price was already past the level on the LAST check, so the alert
# only fires on the actual crossing (False->True), never while price just
# sits past it, and never on first entry into the PCVR zone even if price
# already happens to be past the level at that moment.
_bt_st_cross_state = {}

def check_bt_st_cross_alert(display_data, pcvr):
    """Plays alert.wav when live price crosses ABOVE BT while PCVR >= 1.02,
    or crosses BELOW ST while PCVR <= 0.98. Reads whatever live price is
    currently in `display_data` (the live-price-overlaid dict, not the
    slow-cadence fetch), so it reacts as fast as the live-price poller does,
    same as the Targets section's own distance display."""
    if not pcvr:
        return
    ratio = pcvr["ratio"]
    for inst_name, price_key, chain_key, is_crypto in (
        ("ETH", "eth_price", "eth_chain", True),
        ("QQQ", "qqq_price", "qqq_chain", False),
    ):
        chain = display_data.get(chain_key)
        price = display_data.get(price_key)
        state = _bt_st_cross_state.setdefault(inst_name, {"above_bt": None, "below_st": None})
        if not chain or price is None:
            continue
        bt, st, _active = compute_bt_st(chain["strikes"], is_crypto, ratio > 1.00, price)

        if ratio >= 1.02 and bt is not None:
            now_above = price > bt
            if now_above and state["above_bt"] is False:
                play_alert()
            state["above_bt"] = now_above
        else:
            state["above_bt"] = None   # out of the watched regime — re-arm for next entry

        if ratio <= 0.98 and st is not None:
            now_below = price < st
            if now_below and state["below_st"] is False:
                play_alert()
            state["below_st"] = now_below
        else:
            state["below_st"] = None

def move(row, col=1): sys.stdout.write(f'\033[{row};{col}H')
def erase_line(): sys.stdout.write('\033[K')
def hide_cursor(): sys.stdout.write('\033[?25l')
def show_cursor(): sys.stdout.write('\033[?25h')

def clr_inplace():
    """Cursor home + clear-to-end-of-screen, written as part of the same frame
    buffer as everything else — unlike os.system('cls'), this never tears down
    and rebuilds the console buffer, so there's no visible flicker between the
    old and new frame."""
    sys.stdout.write('\033[H\033[J')

LIGHT_W = 10   # visible width of light(): "● " + 8-char word ("ACTIVE  "/"INACTIVE")

def light(ok):
    word = "ACTIVE  " if ok else "INACTIVE"
    col = GRN if ok else RED
    return f"{col}{BLD}●{RST} {col}{BLD}{word}{RST}"

def light_closed():
    """Distinct from light(False)/INACTIVE — this means the market itself is
    closed, not that the rule failed its condition. Same LIGHT_W width."""
    return f"{DIM}●{RST} {DIM}{BLD}CLOSED  {RST}"

LIGHT_BLANK = " " * LIGHT_W   # placeholder for rows with no green/red status

# ── SESSION (verbatim from opt_dashboard.py's kill-zone logic) ───────────────
CT_OFFSET = timedelta(hours=-5)   # CT = UTC-5 (CDT); use -6 for CST
KILL_ZONES = [
    ('NDO',         0,    210,  'CYN'),
    ('Morning',     510,  630,  'YLW'),
    ('Lunchtime',   690,  810,  'YLW'),
    ('Power Hour',  840,  900,  'YLW'),
    ('EOD',         960,  1080, 'YLW'),
    ('EEOD',        1110, 1440, 'YLW'),
]
EXCL_DAYS_09    = {2, 3}   # Wed=2, Thu=3
EXCL_START      = 540      # 09:00
EXCL_END        = 600      # 10:00
EXCL_SUN        = 6
EXCL_EEOD_START = 1110     # 18:30 CT

TLT_WINDOW_START = 8 * 60 + 45   # 08:45 CT
TLT_WINDOW_END   = 15 * 60       # 15:00 CT

def now_ct():
    return datetime.now(timezone.utc) + CT_OFFSET

def get_session_status():
    """(name_or_None, excl_reason_or_None) — mirrors opt_dashboard.py exactly."""
    n = now_ct()
    t_mins = n.hour * 60 + n.minute
    dow = n.weekday()
    excl_reason = None
    if dow == EXCL_SUN:
        excl_reason = 'Sunday — no trading'
    elif dow in EXCL_DAYS_09 and EXCL_START <= t_mins < EXCL_END:
        excl_reason = 'Excluded (09:00-10:00)'
    elif t_mins >= EXCL_EEOD_START:
        excl_reason = 'EEOD — no trading'
    for name, start, end, _col in KILL_ZONES:
        if start <= t_mins < end:
            return name, excl_reason
    return None, excl_reason

def in_tlt_window():
    n = now_ct()
    t_mins = n.hour * 60 + n.minute
    return TLT_WINDOW_START <= t_mins < TLT_WINDOW_END

def session_open_ts():
    """Unix ts of the current session's 19:00 CT open (same anchor charthacker.py
    uses for VP/VWAP), plus the previous session's open (for prev-close bucketing)."""
    n = datetime.now()
    today_open = datetime(n.year, n.month, n.day, 19, 0, 0)
    if n < today_open:
        curr = today_open - timedelta(days=1)
    else:
        curr = today_open
    prev = curr - timedelta(days=1)
    return prev.timestamp(), curr.timestamp()

# ── DVOL + Layer sizing table ─────────────────────────────────────────────────
DVOL_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"

def fetch_dvol(ccy="ETH"):
    try:
        now_ms = int(time.time() * 1000)
        r = requests.get(DVOL_URL, params={
            "currency": ccy, "start_timestamp": now_ms - 7200000,
            "end_timestamp": now_ms, "resolution": "3600",
        }, timeout=8)
        data = (r.json().get("result") or {}).get("data") or []
        if data:
            return float(data[-1][4])
    except Exception:
        pass
    return None

def dvol_layers(dvol):
    """(layer1_label, layer2_label) from the fixed DVOL lookup table."""
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

# ── Deribit chain (crypto: ETH, BTC) — nearest expiry only ───────────────────
DERIBIT_BASE = "https://www.deribit.com/api/v2"

def _deribit_api(path, **params):
    r = requests.get(DERIBIT_BASE + path, params=params, timeout=12)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"]["message"])
    return j["result"]

def fetch_deribit_chain(currency):
    """Nearest-expiry option chain. Returns
    {"spot": float, "strikes": {strike: {"call": ticker, "put": ticker}}}
    where ticker is the raw Deribit ticker dict (stats.volume, greeks.gamma,
    open_interest all present on it)."""
    instruments = _deribit_api("/public/get_instruments", currency=currency, kind="option", expired="false")
    now_ms = int(time.time() * 1000)
    by_exp = {}
    for ins in instruments:
        by_exp.setdefault(ins["expiration_timestamp"], []).append(ins)
    target_exp = min((e for e in by_exp if e > now_ms), default=None)
    if not target_exp:
        raise RuntimeError(f"No active {currency} expiry found")
    chain_ins = by_exp[target_exp]

    with ThreadPoolExecutor(max_workers=40) as ex:
        fut_index = ex.submit(_deribit_api, "/public/get_index_price", index_name=f"{currency.lower()}_usd")
        ticker_futs = {ex.submit(_deribit_api, "/public/ticker", instrument_name=ins["instrument_name"]): ins
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

# ── CBOE chain (equities: TLT, QQQ) — nearest expiry only ────────────────────
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

def fetch_yahoo_meta(symbol):
    """Live spot + previous close from Yahoo's v8 chart 'meta' block."""
    r = requests.get(YAHOO_CHART_URL.format(symbol), headers=_YAHOO_HEADERS,
                      params={"interval": "1m", "range": "1d"}, timeout=8)
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
    return (float(price) if price else None,
            float(prev_close) if prev_close else None)

def fetch_vxn():
    """VXN (Cboe Nasdaq-100 Volatility Index) via the same Yahoo chart meta
    block as fetch_yahoo_meta — QQQ's volatility source (replaces CBOE's
    iv30 from the options chain). VXN is already quoted as an annualized %,
    same shape as DVOL/iv30, so it drops straight into compute_er()."""
    try:
        price, _prev = fetch_yahoo_meta("^VXN")
        return price
    except Exception:
        return None

PHEMEX_TICKER_URL = "https://api.phemex.com/md/v3/ticker/24hr"

def fetch_eth_live_price():
    """Fast, tiny fetch — Phemex's ETHUSDT perp last-traded price (lastRp),
    same live-price source the rest of this repo's tools use (opt_dashboard.py,
    gex.py's price marker), not Deribit's index — used by the independent
    live-price poller (see live_price_loop)."""
    try:
        r = requests.get(PHEMEX_TICKER_URL, params={"symbol": "ETHUSDT"}, timeout=6)
        r.raise_for_status()
        result = r.json().get("result") or {}
        price = result.get("lastRp") or result.get("markRp")
        return float(price) if price is not None else None
    except Exception:
        return None

def fetch_cboe_chain(symbol):
    """Nearest-expiry option chain. Returns
    {"spot": float, "iv30": float, "strikes": {strike: {"call": opt, "put": opt}}}
    where opt is the raw CBOE option dict (volume, gamma, open_interest, iv all
    present on it directly)."""
    r = requests.get(CBOE_URL.format(symbol), timeout=15)
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
        live_price, _ = fetch_yahoo_meta(symbol)
    except Exception:
        pass
    spot = live_price if live_price else ref_price

    return {"spot": spot, "cboe_ref_price": ref_price, "iv30": iv30, "strikes": strikes,
            "expiry_label": target_exp}

# ── PCVR (shared regime signal: TLT 08:45-15:00 CT, BTC all other times) ─────
# This is a DIFFERENT sum than the nearest-expiry chains used for BT/ST/gamma
# clusters below — PCVR matches opt_dashboard.py's own established number
# exactly, which sums put/call VOLUME (not OI) across the ENTIRE chain, every
# expiry, not just the nearest one.
DERIBIT_BOOK_SUMMARY_URL = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"

def fetch_deribit_pcvr(currency):
    """Sum 24h volume by put/call across ALL expiries — same REST call
    opt_dashboard.py's WS method (public/get_book_summary_by_currency) mirrors."""
    r = requests.get(DERIBIT_BOOK_SUMMARY_URL, params={"currency": currency, "kind": "option"}, timeout=12)
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

def fetch_cboe_pcvr(symbol):
    """Sum volume by put/call across the ENTIRE CBOE chain (every expiry) —
    same loop as opt_dashboard.py's _cboe_symbol_volume."""
    r = requests.get(CBOE_URL.format(symbol), timeout=15)
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

def fetch_pcvr():
    """Returns dict: underlying, put_vol, call_vol, ratio (put/call)."""
    if in_tlt_window():
        underlying = "TLT"
        put_vol, call_vol = fetch_cboe_pcvr("TLT")
    else:
        underlying = "BTC"
        put_vol, call_vol = fetch_deribit_pcvr("BTC")
    ratio = (put_vol / call_vol) if call_vol > 0 else 0.0
    return {"underlying": underlying, "put_vol": put_vol, "call_vol": call_vol, "ratio": ratio}

# ── BT / ST ────────────────────────────────────────────────────────────────
BT_ST_BAND_PCT = {"crypto": 0.20, "equity": 0.12}   # same convention as gex.py's BAND_PCT

def compute_bt_st(strikes, is_crypto, pcvr_gt1, spot):
    """Scan ASCENDING FROM THE BOTTOM of a spot-centered band (±20% crypto,
    ±12% equity — same band gex.py already uses around ATM) for the first
    3-consecutive-strike run where one side's volume dominates. The band
    restriction matters because a raw options chain lists strikes far beyond
    any reasonable trading range (CBOE especially — QQQ's chain runs into
    strikes 30%+ from spot); those deep-OTM strikes have thin, noisy volume
    that can spuriously satisfy "3 in a row" long before reaching the real,
    liquid run near the money. Scanning the whole unrestricted chain from its
    edge picks up that noise; the band keeps the scan confined to strikes that
    are actually tradeable/relevant.
    Returns (bt, st, active) where `active` is "BT" or "ST" — whichever one
    this PCVR regime actually scores (see module docstring)."""
    band_pct = BT_ST_BAND_PCT["crypto" if is_crypto else "equity"]
    lo_bound, hi_bound = spot * (1 - band_pct), spot * (1 + band_pct)
    sorted_strikes = sorted(k for k in strikes.keys() if lo_bound <= k <= hi_bound)
    n = len(sorted_strikes)
    if n < 3:
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

    def window_ok(i, want_put):
        """True if strikes[i..i+2] all have put_vol>call_vol (want_put) or
        call_vol>put_vol (not want_put)."""
        for k in (sorted_strikes[i], sorted_strikes[i + 1], sorted_strikes[i + 2]):
            cv, pv = vols(k)
            if want_put and not (pv > cv):
                return False
            if not want_put and not (cv > pv):
                return False
        return True

    if pcvr_gt1:
        # ST = first strike (ascending from the bottom of the band) of the
        # first run where put_vol > call_vol at 3 in a row; BT = the very
        # next strike above ST.
        for i in range(n - 2):
            if window_ok(i, want_put=True):
                st = sorted_strikes[i]
                bt = sorted_strikes[i + 1] if i + 1 < n else None
                return bt, st, "BT"
        return None, None, "BT"
    else:
        # BT = first strike (ascending from the bottom of the band) of the
        # first run where call_vol > put_vol at 3 in a row; ST = the strike
        # just below BT.
        for i in range(n - 2):
            if window_ok(i, want_put=False):
                bt = sorted_strikes[i]
                st = sorted_strikes[i - 1] if i - 1 >= 0 else None
                return bt, st, "ST"
        return None, None, "ST"

# ── Gamma clusters (Medium/Large tier, reusing gex.py's magnitude_char tiers) ─
MULT_CRYPTO = 1
MULT_EQUITY = 100

def compute_gamma_by_strike(strikes, spot, is_crypto):
    """{strike: net_gex} — calls positive / puts negative, same formula as gex.py."""
    mult = MULT_CRYPTO if is_crypto else MULT_EQUITY
    out = {}
    for strike, legs in strikes.items():
        net = 0.0
        for otype, opt in legs.items():
            if is_crypto:
                greeks = opt.get("greeks") or {}
                gamma = greeks.get("gamma") or 0.0
                oi = opt.get("open_interest") or 0.0
            else:
                gamma = opt.get("gamma") or 0.0
                oi = opt.get("open_interest") or 0.0
            gex = gamma * oi * mult * spot * spot * 0.01
            if otype == "put":
                gex = -gex
            net += gex
        out[strike] = net
    return out

def magnitude_tier(net, scale_max):
    if scale_max <= 0:
        return None
    frac = min(1.0, abs(net) / scale_max)
    if frac < 0.35:
        return None
    elif frac < 0.65:
        return "Medium"
    else:
        return "Large"

def gamma_clusters(strikes, spot, is_crypto):
    """List of (strike, tier) for every strike tagged Medium or Large."""
    by_strike = compute_gamma_by_strike(strikes, spot, is_crypto)
    if not by_strike:
        return []
    scale_max = max(abs(v) for v in by_strike.values())
    clusters = []
    for strike, net in by_strike.items():
        tier = magnitude_tier(net, scale_max)
        if tier:
            clusters.append((strike, tier))
    return sorted(clusters)

def nearest_gamma_clusters(strikes, spot, is_crypto, price, n=2):
    """The `n` Medium/Large gamma clusters closest to live price, as
    [(strike, tier, distance), ...] sorted by distance ascending."""
    clusters = gamma_clusters(strikes, spot, is_crypto)
    with_dist = [(k, t, abs(price - k)) for k, t in clusters]
    with_dist.sort(key=lambda x: x[2])
    return with_dist[:n]

# ── GEX Flip (Zero Gamma) — same Black-Scholes re-pricing sweep as gex.py ────
# A raw "sum today's already-priced gamma by strike" proxy only describes
# today's gamma shape, not where the market would actually flip sign as spot
# moves — professional GEX tools re-price every contract's gamma at a sweep
# of hypothetical spot levels via Black-Scholes and find where THAT crosses
# zero. Ported verbatim from gex.py, which already validated this against
# live data (QQQ flip landed within ~$12 of Barchart's own figure).
BS_SWEEP_PCT    = 0.20
BS_SWEEP_POINTS = 61

def bs_gamma(S, K, T, sigma):
    """Black-Scholes gamma (identical formula for calls and puts). r=0 — a
    standard simplification for short-dated options."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * S * sigma * math.sqrt(T))

def build_bs_gex_curve(contracts, spot, mult, n_points=BS_SWEEP_POINTS, sweep_pct=BS_SWEEP_PCT):
    """contracts: [(strike, "call"|"put", oi, iv_decimal, T_years), ...].
    Returns [(hyp_spot, total_net_gex), ...] swept across ±sweep_pct of spot."""
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

def _find_nearest_zero_crossing(points, ref):
    """points: [(x, y), ...] sorted ascending by x. Returns the interpolated
    x of the y-crossing nearest `ref`, or None if y never crosses zero."""
    if len(points) < 2:
        return None
    crossings = []
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        if y0 == 0:
            crossings.append(x0)
        elif (y0 < 0) != (y1 < 0):
            crossings.append(x0 + (x1 - x0) * (-y0 / (y1 - y0)))
    if not crossings:
        return None
    return min(crossings, key=lambda x: abs(x - ref))

def _cboe_time_to_expiry_years(exp_str):
    """exp_str: 'YYMMDD'. Time to expiry in years, treating expiry as market
    close (15:00 local) on that date."""
    exp_date = datetime.strptime(exp_str, "%y%m%d").date()
    close_dt = datetime(exp_date.year, exp_date.month, exp_date.day, 15, 0, 0)
    seconds = (close_dt - datetime.now()).total_seconds()
    return max(seconds, 60.0) / 86400.0 / 365.0

def compute_gex_flip(chain, is_crypto):
    """GEX Flip level (Zero Gamma) for an already-fetched chain, or None if
    it never crosses zero within the ±20% sweep."""
    spot = chain["spot"]
    mult = MULT_CRYPTO if is_crypto else MULT_EQUITY
    now_ms = int(time.time() * 1000)
    contracts = []
    for strike, legs in chain["strikes"].items():
        for otype, opt in legs.items():
            if is_crypto:
                oi = opt.get("open_interest") or 0.0
                iv_pct = opt.get("mark_iv") or 0.0
                iv = iv_pct / 100.0
                T = max(chain["expiry_ts"] - now_ms, 60_000) / 1000.0 / 86400.0 / 365.0
            else:
                oi = opt.get("open_interest") or 0.0
                iv = opt.get("iv") or 0.0
                T = _cboe_time_to_expiry_years(chain["expiry_label"])
            if oi > 0 and iv > 0:
                contracts.append((strike, otype, oi, iv, T))
    if not contracts:
        return None
    curve = build_bs_gex_curve(contracts, spot, mult)
    return _find_nearest_zero_crossing(curve, spot)

# ── Candles: Phemex (ETH) / Yahoo (QQQ) ───────────────────────────────────────
# /kline/list is Phemex's historical-range endpoint (public, no auth needed —
# verified live) and, unlike /kline/last, accepts an explicit from/to range
# and limits up into the thousands, so the whole current session (since its
# 19:00 CT open) can be fetched in one call — matching charthacker.py's own
# session-anchored VP/VWAP window instead of an arbitrary trailing lookback.
PHEMEX_KLINE_LIST_URL = "https://api.phemex.com/exchange/public/md/v2/kline/list"

def fetch_phemex_session_candles(symbol="ETHUSDT", resolution=60):
    """Oldest-first [(ts,o,h,l,c,v), ...] spanning the current session's
    19:00 CT open through now."""
    _prev_open_ts, curr_open_ts = session_open_ts()
    now_ts = int(time.time())
    minutes = max(1, int((now_ts - curr_open_ts) / resolution) + 5)
    limit = min(2000, minutes)   # Phemex caps /kline/list around ~2000-3000
    r = requests.get(PHEMEX_KLINE_LIST_URL, params={
        "symbol": symbol, "resolution": resolution,
        "from": int(curr_open_ts), "to": now_ts, "limit": limit,
    }, timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(d.get("msg") or "phemex kline error")
    rows = (d.get("data") or {}).get("rows") or []   # already oldest-first
    out = []
    for row in rows:
        ts, _interval, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
        out.append((int(ts), float(o), float(h), float(l), float(c), float(v)))
    return out

def session_prev_eod_close(candles, curr_open_ts):
    """Close of the 15:59 CT candle the day before curr_open_ts (the last
    candle of the regular trading day, right before 16:00 CT) — QQQ-specific
    fallback for when no charthacker.py export is available (a real market
    close is a genuinely different price from today's open, unlike crypto's
    24/7 session where "previous close" == current session's own open — see
    evaluate_hpls's is_crypto handling). Prefer reading this from a live
    charthacker.py export when one exists — see evaluate_hpls's export
    handling — since that's computed from the same live feed the chart
    itself shows, not a separate REST snapshot."""
    target_ts = curr_open_ts - 3 * 3600 - 60   # 15:59 CT, not 16:00 CT
    before = [c for c in candles if c[0] <= target_ts]
    return before[-1][4] if before else None

def fetch_yahoo_candles(symbol, rng="5d", interval="1m"):
    """Oldest-first [(ts,o,h,l,c,v), ...]."""
    r = requests.get(YAHOO_CHART_URL.format(symbol), headers=_YAHOO_HEADERS,
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

# ── VP (VAH/VAL/POC) — same bucketing as charthacker.py's compute_vp ─────────
VP_BUCKETS = 200

def compute_vp(candles):
    """candles: [(ts,o,h,l,c,v), ...]. Returns (poc, vah, val) or None."""
    if not candles:
        return None
    s_lo = min(c[3] for c in candles)
    s_hi = max(c[2] for c in candles)
    s_range = s_hi - s_lo
    if s_range <= 0:
        return None
    vp = [0.0] * VP_BUCKETS

    def ptb(p):
        return max(0, min(VP_BUCKETS - 1, int((p - s_lo) / s_range * (VP_BUCKETS - 1))))

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

# ── VWAP + SD — same cumulative Welford formula as charthacker.py ────────────
def compute_vwap_sd(candles):
    """candles: [(ts,o,h,l,c,v), ...] oldest-first. Returns (vwap, sd) as of the
    LAST candle, or (None, None) if no volume yet."""
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

# ── Expected Range — same formula as charthacker.py's compute_er ─────────────
def compute_er(open_price, iv):
    if not open_price or not iv or iv <= 0:
        return None
    daily_move = iv / math.sqrt(365) / 100.0
    dist = open_price * daily_move
    return {
        "upper": {p: open_price + dist * (p / 100.0) for p in (40, 80, 100, 150)},
        "lower": {p: open_price + dist * (-p / 100.0) for p in (40, 80, 100, 150)},
    }

# ── Live export from a running charthacker.py instance ───────────────────────
STATUS_EXPORT_MAX_AGE = 30   # charthacker.py exports every 5s — 30s gives 6x margin
# gex.py's own refresh cadence defaults to 60s (user-configurable via
# --interval) and isn't on a fixed fast loop like charthacker.py's export —
# using the same 30s window here made the export look "stale" for most of
# every interval, flickering the gex.py-sourced data in and out even though
# the file was perfectly current. 120s comfortably covers the 60s default
# plus real fetch latency; a much longer --interval would need this raised.
GEX_STATUS_EXPORT_MAX_AGE = 120

def read_charthacker_export(asset, max_age_sec=STATUS_EXPORT_MAX_AGE):
    """Read status_<asset>.json (written by charthacker.py's status_export_loop,
    same directory as this script) if present and fresh. Returns the dict, or
    None if missing/stale/unreadable — callers fall back to their own REST
    computation, so status.py works standalone whether or not charthacker.py
    happens to be running on that asset right now."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"status_{asset}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("updated_at", 0) <= max_age_sec:
            return data
    except Exception:
        pass
    return None

def read_gex_export(asset, max_age_sec=GEX_STATUS_EXPORT_MAX_AGE):
    """Read status_<asset>_gex.json (written by gex.py's export_status_snapshot)
    if present and fresh. SEPARATE file from charthacker.py's own
    status_<asset>.json — the two apps could both be running for the same
    asset at once, and each independently overwriting the exact same
    filename would race/clobber the other's fields. Returns the dict, or
    None if missing/stale/unreadable."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"status_{asset}_gex.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("updated_at", 0) <= max_age_sec:
            return data
    except Exception:
        pass
    return None

# HPL display grouping — evaluate_hpls() still returns rows in its own fixed
# order; render() reorders/labels them into these categories purely for
# display, so the underlying computation and this presentation grouping stay
# decoupled (adding/renaming a category never touches evaluate_hpls).
HPL_CATEGORIES = (
    ("Volume", ("VAH", "VAL", "POC", "+2sd/2.5sd", "-2sd/2.5sd", "0.5sd band", "VWAP")),
    ("Expected Range", ("ER 40-80% band", "ER 100%", "ER 150%")),
    ("Options", ("BT", "ST", "GEX Flip", "Med/Large Gamma Clusters")),
    ("Miscellaneous", ("Prev EOD Close",)),
)

def all_clusters_from_gex_export(gex_export):
    """[(strike, tier), ...] sorted by strike — every Medium/Large cluster in
    a gex.py export, classified against ITS stable, session-accumulated
    scale_max (not a scale recomputed fresh every cycle from just the current
    snapshot, which let a strike sitting right at the Medium/Large boundary
    flicker in and out between refreshes even though its real GEX barely
    moved — gex.py's scale_max only grows across the session, frozen per-
    column at ingestion, so a cluster it's already showing stays classified
    the same way here too)."""
    scale_max = gex_export.get("scale_max") or 0.0
    by_strike = gex_export.get("gex_by_strike") or {}
    clusters = []
    for k_str, net in by_strike.items():
        tier = magnitude_tier(float(net), scale_max)
        if tier:
            clusters.append((float(k_str), tier))
    return sorted(clusters)

def clusters_from_gex_export(gex_export, price, n=2):
    """Same shape as nearest_gamma_clusters() — the `n` closest Medium/Large
    clusters to price, from a gex.py export instead of a fresh computation."""
    clusters = all_clusters_from_gex_export(gex_export)
    with_dist = [(k, t, abs(price - k)) for k, t in clusters]
    with_dist.sort(key=lambda x: x[2])
    return with_dist[:n]

def gamma_cluster_targets_directional(chain, is_crypto, gex_export, price):
    """(above, below) — sorted [(strike, tier), ...] for EVERY Medium or
    Large gamma cluster above/below current price, from whichever source
    (gex.py export if fresh, else a live computation) evaluate_hpls would
    also use for this instrument. Used by the Targets section — unlike the
    HPL row's "2 closest" display, targets want every qualifying cluster
    (both tiers) in the relevant direction, not just the nearest Large ones."""
    if gex_export and gex_export.get("gex_by_strike"):
        clusters = all_clusters_from_gex_export(gex_export)
    else:
        clusters = gamma_clusters(chain["strikes"], chain["spot"], is_crypto)
    above = sorted((k, t) for k, t in clusters if k > price)
    below = sorted((k, t) for k, t in clusters if k < price)
    return above, below

QQQ_OPEN_CT_MIN  = 8 * 60 + 45   # 08:45 CT — regular market open
QQQ_CLOSE_CT_MIN = 15 * 60       # 15:00 CT — regular market close

def qqq_market_closed():
    """True from 15:00 CT through 08:29 CT the next day — QQQ's HPLs render
    as CLOSED/inactive outside regular hours rather than showing numbers
    computed against a market that isn't actively trading."""
    n = now_ct()
    minutes = n.hour * 60 + n.minute
    return not (QQQ_OPEN_CT_MIN <= minutes < QQQ_CLOSE_CT_MIN)

# ── HPL evaluation for one instrument (ETH or QQQ) ────────────────────────────
def evaluate_hpls(name, price, chain, session_candles, prev_close, iv, tol, gamma_tol, pcvr_gt1, is_crypto,
                   export=None, gamma_yellow_tol=None, gex_export=None):
    """Returns list of (label, level_str, is_green) rows, in the fixed order.
    If `export` is given (a fresh status_<ASSET>.json written by a live
    charthacker.py instance), VAH/VAL/POC/VWAP/SD bands/session-open/prev-close
    come from THAT instead of being recomputed from session_candles — this is
    what actually gets charthacker.py-exact values (its own REST snapshot vs.
    charthacker's live WebSocket-accumulated candles are never bit-for-bit
    identical). If `gex_export` is given (status_<ASSET>_gex.json from a live
    gex.py instance), Gamma Clusters and GEX Flip come from THAT instead of
    being recomputed here, for the same reason — gex.py's own stable,
    session-accumulated scale_max doesn't flicker cluster tiers the way
    status.py's own per-cycle recompute could. BT/ST/PCVR always come from
    status.py's own options-chain fetches regardless — neither other app
    fetches those."""
    rows = []

    def near(level):
        return level is not None and abs(price - level) <= tol

    # Shared 3-tier distance coloring (green <= tol, yellow tol < d <=
    # yellow_ceiling, red beyond) — used by both GEX Flip and gamma clusters'
    # distance text below.
    yellow_ceiling = gamma_yellow_tol if gamma_yellow_tol is not None else gamma_tol

    def dist_color(dist):
        if dist <= tol:
            return GRN
        elif dist <= yellow_ceiling:
            return YLW
        else:
            return RED

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
        vp = compute_vp(session_candles)
        poc = vah = val = None
        if vp:
            poc, vah, val = vp
        vwap, sd = compute_vwap_sd(session_candles)
        if vwap is not None and sd is not None:
            sd_p05, sd_m05 = vwap + 0.5 * sd, vwap - 0.5 * sd
            sd_p2,  sd_m2  = vwap + 2.0 * sd, vwap - 2.0 * sd
            sd_p25, sd_m25 = vwap + 2.5 * sd, vwap - 2.5 * sd
        else:
            sd_p05 = sd_m05 = sd_p2 = sd_m2 = sd_p25 = sd_m25 = None
        _prev_ts, curr_open_ts = session_open_ts()
        open_candidates = [c for c in session_candles if c[0] >= curr_open_ts]
        session_open = open_candidates[0][1] if open_candidates else (session_candles[0][1] if session_candles else None)

    # For crypto (24/7, no real market close), "Previous EOD Close" IS the
    # current session's own open — this is exactly charthacker.py's "EOpen"
    # line (_er_sess_candles[0].o), which the user confirmed is the value to
    # use. Simpler and more reliable than fetching a specific boundary candle
    # by timestamp (which had real off-by-one/definition bugs).
    if is_crypto and session_open is not None:
        prev_close = session_open
    # Equities (QQQ): prefer a live charthacker.py export's own prev_eod_close
    # (computed from the same live feed the chart shows) over status.py's own
    # REST-snapshot fallback (session_prev_eod_close, already applied by the
    # caller into the prev_close argument) — a real market close is a
    # genuinely different price from today's open, so this does NOT fold
    # into the is_crypto branch above.
    elif export and export.get("prev_eod_close") is not None:
        prev_close = export["prev_eod_close"]

    rows.append(("VAH", f"${vah:,.2f}" if vah is not None else "n/a",
                 vah is not None and price > vah and near(vah) and pcvr_gt1))
    rows.append(("VAL", f"${val:,.2f}" if val is not None else "n/a",
                 val is not None and price < val and near(val) and not pcvr_gt1))
    rows.append(("POC", f"${poc:,.2f}" if poc is not None else "n/a", near(poc)))

    if vwap is not None and sd_p2 is not None:
        rows.append(("+2sd/2.5sd", f"${sd_p2:,.2f} / ${sd_p25:,.2f}",
                     price >= sd_p2 and near(sd_p2) and pcvr_gt1))
        rows.append(("-2sd/2.5sd", f"${sd_m2:,.2f} / ${sd_m25:,.2f}",
                     price <= sd_m2 and near(sd_m2) and not pcvr_gt1))
        rows.append(("0.5sd band", f"${sd_m05:,.2f} - ${sd_p05:,.2f}",
                     sd_m05 <= price <= sd_p05))
        rows.append(("VWAP", f"${vwap:,.2f}", near(vwap)))
    else:
        rows.append(("+2sd/2.5sd", "n/a", False))
        rows.append(("-2sd/2.5sd", "n/a", False))
        rows.append(("0.5sd band", "n/a", False))
        rows.append(("VWAP", "n/a", False))

    er = compute_er(session_open, iv)
    if er:
        u40, u80, u100, u150 = er["upper"][40], er["upper"][80], er["upper"][100], er["upper"][150]
        l40, l80, l100, l150 = er["lower"][40], er["lower"][80], er["lower"][100], er["lower"][150]
        in_band = (u40 <= price <= u80) or (l80 <= price <= l40)
        rows.append(("ER 40-80% band", f"${u40:,.2f}-${u80:,.2f} / ${l80:,.2f}-${l40:,.2f}", in_band))
        rows.append(("ER 100%", f"${u100:,.2f} / ${l100:,.2f}", near(u100) or near(l100)))
        rows.append(("ER 150%", f"${u150:,.2f} / ${l150:,.2f}", near(u150) or near(l150)))
    else:
        rows.append(("ER 40-80% band", "n/a", False))
        rows.append(("ER 100%", "n/a", False))
        rows.append(("ER 150%", "n/a", False))

    bt, st, active = compute_bt_st(chain["strikes"], is_crypto, pcvr_gt1, price)
    bt_green = active == "BT" and bt is not None and price >= bt and near(bt)
    st_green = active == "ST" and st is not None and price <= st and near(st)
    # BT/ST values are always colored green/red respectively (buy/sell
    # territory), independent of the row's own light/condition.
    rows.append(("BT", f"{GRN}{BLD}${bt:,.2f}{RST}" if bt is not None else "n/a", bt_green))
    rows.append(("ST", f"{RED}{BLD}${st:,.2f}{RST}" if st is not None else "n/a", st_green))

    rows.append(("Prev EOD Close", f"${prev_close:,.2f}" if prev_close is not None else "n/a", near(prev_close)))

    if gex_export and gex_export.get("gex_flip") is not None:
        gex_flip = gex_export["gex_flip"]
    else:
        gex_flip = compute_gex_flip(chain, is_crypto)
    if gex_flip is not None:
        gflip_dist = abs(price - gex_flip)
        gflip_col = dist_color(gflip_dist)
        gex_flip_str = f"${gex_flip:,.2f} ({gflip_col}{BLD}${gflip_dist:,.2f} away{RST})"
    else:
        gex_flip_str = "n/a"
    rows.append(("GEX Flip", gex_flip_str, near(gex_flip)))

    # Row's own light follows the ORIGINAL rule (unchanged): green if ANY of
    # the shown clusters is within gamma_tol. The per-cluster DISTANCE TEXT
    # gets its own separate green/yellow/red coloring (green <= tol, yellow
    # tol < d <= gamma_yellow_tol, red beyond) — a distinct, purely cosmetic
    # scheme layered on top, not a replacement for the row's light.
    if gex_export and gex_export.get("gex_by_strike"):
        nearest = clusters_from_gex_export(gex_export, price, n=2)
    else:
        nearest = nearest_gamma_clusters(chain["strikes"], chain["spot"], is_crypto, price, n=2)
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

# ── Backtest snapshot (plain data, no ANSI — for the minute-interval log) ────
def compute_dashboard_snapshot(data):
    """Plain-data (JSON-serializable) snapshot of everything the dashboard
    currently shows. Mirrors render()'s own computation (same evaluate_hpls/
    compute_bt_st/gamma_cluster_targets_directional calls, same rule
    thresholds) but returns raw values instead of colored display strings,
    for the minute-interval backtest log."""
    snap = {"ts": datetime.now().isoformat()}

    sess_name, excl_reason = data.get("session", (None, None))
    in_session = sess_name is not None and excl_reason is None
    snap["session"] = {"name": sess_name, "excl_reason": excl_reason, "in_session": in_session}

    dvol = data.get("dvol")
    l1, l2 = dvol_layers(dvol)
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

        export = read_charthacker_export(inst_name)
        gex_export = read_gex_export(inst_name)
        rows = evaluate_hpls(inst_name, price, chain, candles, prev_close, iv, tol, gtol, pcvr_gt1, is_crypto,
                              export=export, gamma_yellow_tol=gyellow, gex_export=gex_export)
        closed = inst_name == "QQQ" and qqq_market_closed()
        inst_snap["market_closed"] = closed

        hpl = {}
        for label_, level_str, ok in rows:
            hpl[label_] = {
                "value": _ANSI_RE.sub("", level_str),
                "status": "closed" if closed else ("active" if ok else "inactive"),
            }
        inst_snap["hpl"] = hpl
        any_active = (not closed) and any(ok for _l, _v, ok in rows)
        inst_snap["any_active"] = any_active

        ratio = pcvr["ratio"] if pcvr else None
        targets = []
        if ratio is not None and not closed:
            if ratio < 0.98:
                bt, _st, _active = compute_bt_st(chain["strikes"], is_crypto, ratio > 1.00, price)
                if bt is not None:
                    targets.append({"type": "BT", "level": bt, "tier": None})
                above, _below = gamma_cluster_targets_directional(chain, is_crypto, gex_export, price)
                targets += [{"type": "Cluster", "level": k, "tier": t} for k, t in above]
            elif ratio > 1.02:
                _bt, st, _active = compute_bt_st(chain["strikes"], is_crypto, ratio > 1.00, price)
                if st is not None:
                    targets.append({"type": "ST", "level": st, "tier": None})
                _above, below = gamma_cluster_targets_directional(chain, is_crypto, gex_export, price)
                targets += [{"type": "Cluster", "level": k, "tier": t} for k, t in below]
        inst_snap["targets"] = targets
        has_targets = bool(targets)

        ready = in_session and pcvr_extreme and any_active and has_targets
        inst_snap["final_status"] = "EXECUTE WHEN READY" if ready else "HOLD"

        instruments[inst_name] = inst_snap

    snap["eth"] = instruments.get("ETH")
    snap["qqq"] = instruments.get("QQQ")
    return snap

# ── Backtest log: year/month/day folder structure ─────────────────────────────
SNAPSHOT_DIR_BASE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status_logs")
SNAPSHOT_INTERVAL  = 60   # seconds — independent of --interval and the live-price poller

def snapshot_log_path(dt):
    """status_logs/YYYY/MM/DD/status_MM_DD_YYYY.jsonl — creates the
    year/month/day folders on demand. One JSONL file per day (not one file
    per snapshot — matches this repo's existing gex.py/cvd.py/footprint.py
    per-day-log convention) so a day's worth of minute snapshots (~1440
    rows) stays a single, appendable file instead of thousands of tiny ones."""
    year_dir  = os.path.join(SNAPSHOT_DIR_BASE, f"{dt.year:04d}")
    month_dir = os.path.join(year_dir, f"{dt.month:02d}")
    day_dir   = os.path.join(month_dir, f"{dt.day:02d}")
    os.makedirs(day_dir, exist_ok=True)
    return os.path.join(day_dir, f"status_{dt.strftime('%m_%d_%Y')}.jsonl")

def append_snapshot(snap):
    try:
        path = snapshot_log_path(datetime.now())
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap) + "\n")
        return True, None
    except Exception as e:
        return False, str(e)

# inst/display_data shared between main()'s fetch loop and the snapshot
# logger thread, so the logger doesn't have to run its own independent
# fetch cycle (which would double the network load) — it just reads
# whatever the main loop most recently built (data + live-overlaid prices).
_latest_data_lock = threading.Lock()
_latest_display_data = None

def snapshot_logger_loop():
    """Background: every SNAPSHOT_INTERVAL seconds, write a full backtest
    snapshot of the current dashboard state to today's dated log file."""
    while not _quit_evt.is_set():
        with _latest_data_lock:
            display_data = _latest_display_data
        if display_data:
            try:
                snap = compute_dashboard_snapshot(display_data)
                append_snapshot(snap)
            except Exception:
                pass
        for _ in range(int(SNAPSHOT_INTERVAL / 0.5)):
            if _quit_evt.is_set():
                break
            time.sleep(0.5)

# ── Full refresh cycle ────────────────────────────────────────────────────────
def run_cycle():
    result = {"errors": {}}

    def safe(key, fn):
        try:
            result[key] = fn()
        except Exception as e:
            result["errors"][key] = str(e)
            result[key] = None

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {
            "dvol": ex.submit(fetch_dvol, "ETH"),
            "pcvr": ex.submit(fetch_pcvr),
            "eth_chain": ex.submit(fetch_deribit_chain, "ETH"),
            "eth_candles": ex.submit(fetch_phemex_session_candles, "ETHUSDT", 60),
            "qqq_chain": ex.submit(fetch_cboe_chain, "QQQ"),
            "qqq_candles": ex.submit(fetch_yahoo_candles, "QQQ", "5d", "1m"),
            "qqq_meta": ex.submit(fetch_yahoo_meta, "QQQ"),
            "vxn": ex.submit(fetch_vxn),
        }
        for key, fut in futs.items():
            try:
                result[key] = fut.result()
            except Exception as e:
                result["errors"][key] = str(e)
                result[key] = None

    result["session"] = get_session_status()
    return result

# ── Render ─────────────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SEP_MARKER    = "\0SEP\0"      # placeholder — replaced with a full-width separator
_TITLE_MARKER  = "\0TITLE\0"    # placeholder — replaced with the centered title
_FOOTER_MARKER = "\0FOOTER\0"   # placeholder — replaced with the centered footer
_STATUS_MARKER = "\0STATUS\0"   # placeholder — replaced with the centered final status
                                # once the actual content width for this frame is known
TITLE_TEXT = "BLACKJACK FRAMEWORK DASHBOARD"

def _visible_len(s):
    return len(_ANSI_RE.sub("", s))

def render(data, remaining=None):
    """`remaining`: seconds left until the next full refresh (for the
    countdown in the footer), or None to show the static refresh interval —
    the render() call right after a real fetch doesn't have this loop state."""
    lines = []
    def p(s=""): lines.append(s)
    deferred = {}   # marker -> already-colored content, centered at substitution time

    p(_SEP_MARKER)
    p(_TITLE_MARKER)
    p(_SEP_MARKER)

    # 1. Session — light lives in a fixed-width left column so every row
    # (Session/PCVR/HPL alike) lines its dot up at the same screen column.
    sess_name, excl_reason = data["session"]
    in_session = sess_name is not None and excl_reason is None
    p(f"  {BLD}1. Session{RST}")
    label = excl_reason or sess_name or "No active session"
    p(f"     {light(in_session)}  {DIM}{label}{RST}")
    p()

    # 2. Volatility — no green/red rule defined for either row; blank-pad the
    # light column so the text still lines up under the rows around it.
    dvol = data.get("dvol")
    l1, l2 = dvol_layers(dvol)
    p(f"  {BLD}2. Volatility{RST}")
    if dvol is not None:
        p(f"     {LIGHT_BLANK}  {DIM}{'DVOL (ETH)':<12}{RST}{CYN}{BLD}{dvol:>6.2f}{RST}   "
          f"Layer 1: {YLW}{l1}{RST}   Layer 2: {MAG}{l2}{RST}")
    else:
        p(f"     {LIGHT_BLANK}  {DIM}DVOL (ETH) unavailable{RST}")
    qqq_iv = data.get("qqq_iv")
    if qqq_iv is not None:
        p(f"     {LIGHT_BLANK}  {DIM}{'VXN (QQQ)':<12}{RST}{CYN}{BLD}{qqq_iv:>6.2f}{RST}")
    else:
        p(f"     {LIGHT_BLANK}  {DIM}VXN (QQQ) unavailable{RST}")
    p()

    # 3. PCVR — no light row (just the text): green when ratio <= 0.98, red
    # when ratio >= 1.02, yellow for the 0.98-1.02 neutral gap.
    pcvr = data.get("pcvr")
    p(f"  {BLD}3. PCVR{RST}")
    if pcvr:
        ratio = pcvr["ratio"]
        col = RED if ratio >= 1.02 else (GRN if ratio <= 0.98 else YLW)
        p(f"     {LIGHT_BLANK}  {col}{BLD}{ratio:>6.2f}{RST}   ({pcvr['underlying']})   "
          f"{DIM}put {pcvr['put_vol']:,.0f} / call {pcvr['call_vol']:,.0f}{RST}")
    else:
        p(f"     {LIGHT_BLANK}  {DIM}unavailable{RST}")
    p()

    pcvr_gt1 = bool(pcvr and pcvr["ratio"] > 1.00)
    pcvr_lt1 = bool(pcvr and pcvr["ratio"] < 1.00)

    # 4. HPLs
    p(f"  {BLD}4. High-Probability Levels{RST}")
    # gtol = the ORIGINAL gamma-cluster row-light threshold (unchanged from
    # before this feature existed): $2.00 ETH / $0.35 QQQ. gyellow is only
    # for the per-cluster distance-text coloring's yellow ceiling. ETH has no
    # prev_key — evaluate_hpls derives its prev-close from session_open
    # (crypto's "previous close" == current session's own open).
    active_status = {}   # inst_name -> "has >=1 real ACTIVE HPL row this cycle" (used by Final Status below)
    for inst_name, price_key, chain_key, candles_key, prev_key, iv_key, tol, gtol, gyellow, is_crypto in (
        ("ETH", "eth_price", "eth_chain", "eth_candles", None, "eth_iv", 2.00, 2.00, 5.00, True),
        ("QQQ", "qqq_price", "qqq_chain", "qqq_candles", "qqq_prev_close", "qqq_iv", 0.25, 0.35, 0.35, False),
    ):
        chain = data.get(chain_key)
        candles = data.get(candles_key) or []
        price = data.get(price_key)
        prev_close = data.get(prev_key)
        iv = data.get(iv_key)
        export = read_charthacker_export(inst_name)
        gex_export = read_gex_export(inst_name)
        tags = []
        if export:
            tags.append(f"{GRN}VP/VWAP: charthacker.py{RST}{DIM}")
        if gex_export:
            tags.append(f"{GRN}clusters/flip: gex.py{RST}{DIM}")
        src_tag = ", ".join(tags) if tags else "status.py REST snapshot"
        p(f"     {BLD}{YLW}── {inst_name} {RST}{DIM}({src_tag}){RST}")
        if not chain or price is None:
            p(f"        {LIGHT_BLANK}  {DIM}unavailable{RST}")
            p()
            active_status[inst_name] = False
            continue
        p(f"        {LIGHT_BLANK}  {'Live Price':<26}{CYN}{BLD}${price:,.2f}{RST}")
        rows = evaluate_hpls(inst_name, price, chain, candles, prev_close, iv, tol, gtol, pcvr_gt1, is_crypto,
                              export=export, gamma_yellow_tol=gyellow, gex_export=gex_export)
        closed = inst_name == "QQQ" and qqq_market_closed()
        rows_by_label = {label_: (level_str, ok) for label_, level_str, ok in rows}
        any_active = (not closed) and any(ok for _l, _v, ok in rows)
        active_status[inst_name] = any_active
        for cat_i, (cat_name, cat_labels) in enumerate(HPL_CATEGORIES):
            if cat_i > 0:
                p()
            p(f"        {LIGHT_BLANK}  {DIM}{BLD}{cat_name}{RST}")
            for label_ in cat_labels:
                level_str, ok = rows_by_label.get(label_, ("n/a", False))
                dot = light_closed() if closed else light(ok)
                p(f"        {dot}  {label_:<26}{level_str}")
        p()

    # 5. Targets — BT + Large clusters above price when PCVR < 0.98, or
    # ST + Large clusters below price when PCVR > 1.02. QQQ only during its
    # own 08:45-15:00 CT hours; ETH always (24/7 asset, no such gate).
    p(f"  {BLD}5. Targets{RST}")
    has_targets = {}   # inst_name -> "has >=1 real target this cycle" (used by Final Status below)
    ratio = pcvr["ratio"] if pcvr else None
    for inst_name, price_key, chain_key, is_crypto, gated in (
        ("ETH", "eth_price", "eth_chain", True, False),
        ("QQQ", "qqq_price", "qqq_chain", False, True),
    ):
        chain = data.get(chain_key)
        price = data.get(price_key)
        if gated and qqq_market_closed():
            p(f"     {LIGHT_BLANK}  {DIM}{inst_name:<6}CLOSED{RST}")
            has_targets[inst_name] = False
            continue
        if not chain or price is None or ratio is None:
            p(f"     {LIGHT_BLANK}  {DIM}{inst_name:<6}unavailable{RST}")
            has_targets[inst_name] = False
            continue
        gex_export = read_gex_export(inst_name)
        # Only the $ price itself and the above/below/distance tag are
        # colored — labels ("BT"/"ST"/"Cluster") stay plain/white. Tag
        # describes where the TARGET sits relative to live price (not the
        # other way), plus the $ distance between them — recomputed fresh
        # every render call against whatever the live-price poller currently
        # has, so it updates in step with the live price, not just on full
        # refreshes. Color: PCVR>=1.02 wants price falling toward ST, so a
        # target BELOW price is green (still ahead) / above is red (passed);
        # PCVR<=0.98 is the mirror (target ABOVE price is green).
        def target_rel(level):
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
        targets = []
        if ratio < 0.98:
            bt, _st, _active = compute_bt_st(chain["strikes"], is_crypto, ratio > 1.00, price)
            if bt is not None:
                targets.append(f"BT {GRN}{BLD}${bt:,.2f}{RST} ({target_rel(bt)})")
            above, _below = gamma_cluster_targets_directional(chain, is_crypto, gex_export, price)
            targets += [f"Cluster ({t[0]}) {GRN}{BLD}${k:,.2f}{RST} ({target_rel(k)})" for k, t in above]
        elif ratio > 1.02:
            _bt, st, _active = compute_bt_st(chain["strikes"], is_crypto, ratio > 1.00, price)
            if st is not None:
                targets.append(f"ST {GRN}{BLD}${st:,.2f}{RST} ({target_rel(st)})")
            _above, below = gamma_cluster_targets_directional(chain, is_crypto, gex_export, price)
            targets += [f"Cluster ({t[0]}) {GRN}{BLD}${k:,.2f}{RST} ({target_rel(k)})" for k, t in below]
        has_targets[inst_name] = bool(targets)
        if targets:
            # Max 2 targets per line — more than that on one line ran wider
            # than the terminal and broke the whole dashboard's rendering.
            for i in range(0, len(targets), 2):
                chunk = targets[i:i + 2]
                label = f"{DIM}{inst_name:<6}{RST}" if i == 0 else " " * 6
                p(f"     {LIGHT_BLANK}  {label}{', '.join(chunk)}")
        else:
            p(f"     {LIGHT_BLANK}  {DIM}{inst_name:<6}no active targets{RST}")
    p()

    # Final Status — "EXECUTE WHEN READY" only when: an active session (1),
    # PCVR is in an extreme zone (<=0.98 or >=1.02), this instrument has at
    # least one genuinely ACTIVE HPL row this cycle (active_status, already
    # False whenever QQQ is showing CLOSED), AND it has at least one real
    # target above/below price for that same PCVR direction (has_targets,
    # from section 5 above). Otherwise HOLD.
    pcvr_extreme = bool(pcvr) and (pcvr["ratio"] <= 0.98 or pcvr["ratio"] >= 1.02)
    def final_status_text(inst):
        ready = in_session and pcvr_extreme and active_status.get(inst, False) and has_targets.get(inst, False)
        word = f"{GRN}{BLD}EXECUTE WHEN READY{RST}" if ready else f"{RED}{BLD}HOLD{RST}"
        return f"{BLD}{inst}:{RST} {word}"
    status_line = f"{final_status_text('ETH')}     {final_status_text('QQQ')}"
    p(_STATUS_MARKER)
    deferred[_STATUS_MARKER] = status_line
    p()

    p(_SEP_MARKER)
    ts = datetime.now().strftime("%H:%M:%S")
    if remaining is not None:
        refresh_txt = f"next refresh in {max(0, int(round(remaining)))}s"
    else:
        refresh_txt = f"refresh {REFRESH_SEC}s"
    footer = (f"{DIM}{ts}  {refresh_txt}{RST}   {BLD}[Q]{RST}{DIM}uit  {BLD}[R]{RST}{DIM}efresh{RST}")
    p(_FOOTER_MARKER)
    deferred[_FOOTER_MARKER] = footer
    errs = data.get("errors") or {}
    if errs:
        p(f"  {RED}errors: {', '.join(errs.keys())}{RST}")
    p(_SEP_MARKER)

    # Separators/title/footer stretch to (or center within) the widest actual
    # content line in this frame, not a fixed guess — so they always reach
    # the edge of the data instead of falling short (or over-running) as row
    # content varies cycle to cycle.
    width = max((_visible_len(l) for l in lines
                 if l not in (_SEP_MARKER, _TITLE_MARKER, _FOOTER_MARKER, _STATUS_MARKER)), default=78)
    def centered(s):
        return " " * max(0, (width - _visible_len(s)) // 2) + s
    sep = f"{BLD}{CYN}{'─' * width}{RST}"
    title = f"{BLD}{CYN}{TITLE_TEXT.center(width)}{RST}"
    footer_centered = centered(deferred[_FOOTER_MARKER])
    status_centered = centered(deferred[_STATUS_MARKER])
    lines = [sep if l == _SEP_MARKER else
             title if l == _TITLE_MARKER else
             footer_centered if l == _FOOTER_MARKER else
             status_centered if l == _STATUS_MARKER else l
             for l in lines]

    clr_inplace()
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()

# ── Data assembly wrapper (fills price/iv keys the render loop expects) ──────
def assemble(raw):
    out = dict(raw)
    eth_chain = raw.get("eth_chain")
    out["eth_price"] = eth_chain["spot"] if eth_chain else None
    out["eth_iv"] = raw.get("dvol")
    qqq_chain = raw.get("qqq_chain")
    qqq_meta = raw.get("qqq_meta")
    out["qqq_price"] = (qqq_meta[0] if qqq_meta and qqq_meta[0] else (qqq_chain["spot"] if qqq_chain else None))
    _prev_open_ts, curr_open_ts = session_open_ts()
    out["qqq_prev_close"] = session_prev_eod_close(raw.get("qqq_candles") or [], curr_open_ts)
    out["qqq_iv"] = raw.get("vxn")   # VXN, not CBOE's iv30 — see fetch_vxn
    return out

# ── Live price poller ──────────────────────────────────────────────────────
# --interval controls the FULL data refresh (chains/candles/DVOL/PCVR — the
# heavy fetches). Live price (and everything derived purely from price —
# distances, near()/directional checks, targets) updates on its own much
# faster, independent cadence, so the dashboard shows real-time price
# movement between full refreshes rather than a price frozen at the last
# full fetch. Deliberately its own tiny/cheap fetches (Deribit index price,
# Yahoo meta — not the heavy chain/candle endpoints) so this stays safe to
# run fast regardless of how long --interval is set to.
LIVE_PRICE_INTERVAL = 2   # seconds

_live_price_lock = threading.Lock()
_live_prices = {"ETH": None, "QQQ": None}

def live_price_loop():
    while not _quit_evt.is_set():
        eth_p = fetch_eth_live_price()
        qqq_p = None
        try:
            qqq_p, _prev = fetch_yahoo_meta("QQQ")
        except Exception:
            pass
        with _live_price_lock:
            if eth_p is not None:
                _live_prices["ETH"] = eth_p
            if qqq_p is not None:
                _live_prices["QQQ"] = qqq_p
        for _ in range(int(LIVE_PRICE_INTERVAL / 0.2)):
            if _quit_evt.is_set():
                break
            time.sleep(0.2)

def get_live_prices():
    with _live_price_lock:
        return dict(_live_prices)

# ── Keyboard control (non-blocking, matches opt_dashboard.py's convention) ───
_quit_evt = threading.Event()
_refresh_evt = threading.Event()

def _keyboard_thread():
    try:
        if sys.platform == "win32":
            import msvcrt
            while not _quit_evt.is_set():
                if msvcrt.kbhit():
                    ch = msvcrt.getch().decode(errors="ignore").lower()
                    if ch == "q":
                        _quit_evt.set()
                    elif ch == "r":
                        _refresh_evt.set()
                time.sleep(0.1)
        else:
            import termios, tty, select
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            try:
                while not _quit_evt.is_set():
                    r, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if r:
                        ch = sys.stdin.read(1).lower()
                        if ch == "q":
                            _quit_evt.set()
                        elif ch == "r":
                            _refresh_evt.set()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass

def main():
    global _latest_display_data
    hide_cursor()
    kb = threading.Thread(target=_keyboard_thread, daemon=True)
    kb.start()
    threading.Thread(target=live_price_loop, daemon=True).start()
    threading.Thread(target=snapshot_logger_loop, daemon=True).start()
    try:
        while not _quit_evt.is_set():
            raw = run_cycle()
            data = assemble(raw)
            check_pcvr_alert(data)
            _refresh_evt.clear()
            waited = 0.0
            last_shown = None
            while waited < REFRESH_SEC and not _quit_evt.is_set() and not _refresh_evt.is_set():
                remaining = REFRESH_SEC - waited
                shown = max(0, int(round(remaining)))
                if shown != last_shown:
                    # Overlay the independently-polled live price on top of
                    # this cycle's otherwise-static data — everything derived
                    # purely from price (distances, near()/directional checks,
                    # targets) recomputes fresh against it on every redraw,
                    # while chain/candle-derived levels stay from the last
                    # full fetch until the next one.
                    live = get_live_prices()
                    display_data = dict(data)
                    if live.get("ETH") is not None:
                        display_data["eth_price"] = live["ETH"]
                    if live.get("QQQ") is not None:
                        display_data["qqq_price"] = live["QQQ"]
                    check_bt_st_cross_alert(display_data, data.get("pcvr"))
                    with _latest_data_lock:
                        _latest_display_data = display_data
                    render(display_data, remaining=remaining)
                    last_shown = shown
                time.sleep(0.2)
                waited += 0.2
    except KeyboardInterrupt:
        pass
    finally:
        show_cursor()
        print(f"{DIM}Exited.{RST}")

if __name__ == "__main__":
    main()
