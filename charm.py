#!/usr/bin/env python3
"""
charm.py — Charm (dealer delta-decay) exposure heatmap + OHLC price overlay
Curses terminal chart, styled after the Charm/OHLC combo charts public 0DTE
dashboards publish: a continuous orange/blue heatmap in the background (orange
= dealers' aggregate delta is building/growing more positive as time passes at
that price, blue = decaying/going more negative), OHLC candles drawn on top on
their own 5-min grid, and a dotted ± expected-move envelope narrowing toward
the tracked expiry's close. A vertical colorbar legend sits at the top-left
("Charm (δ / 5 min)", orange above 0, blue below), price axis on the right —
same layout convention as the reference charts this was modeled on.

One symbol at a time — [S] opens a prompt to type any symbol, same convention
as gex.py's own [S]/entering_symbol/_switch_symbol. ETH/BTC route to Deribit
REST (real-time); anything else is tried as a CBOE-listed equity/ETF ticker via
CBOE's delayed-quotes feed (~15m delay, same feed chain.py/gex.py use), with
spot displayed via Yahoo's near-real-time quote (same fallback as gex.py's
fetch_live_price). Switching resets history/scale/scroll state and (re)loads
whatever's already logged for the new symbol today (or at --date), same as
launching the app with that symbol from the start.

Why charm is hand-derived, not read off either feed:
Neither Deribit's /public/ticker "greeks" block nor CBOE's per-contract greeks
publish charm (∂delta/∂t) directly — both cap out at delta/gamma/theta(/vega/
rho for Deribit). Charm is computed here via closed-form Black-Scholes, r=0
and q=0 (same short-dated-rate simplification gex.py's bs_gamma already makes
for this repo). Under that simplification calls and puts have IDENTICAL charm
— Delta_put = Delta_call - e^(-qT), a q=0 constant offset, so both decay at
the same rate — unlike gamma this isn't a coincidence special to this repo,
it's a real property of the r=q=0 formula:
    d1 = (ln(S/K) + 0.5·σ²·T) / (σ√T),  d2 = d1 - σ√T
    charm(call) = charm(put) = φ(d1)·d2 / (2T)      [φ = standard normal pdf]
(Verified against a finite-difference re-pricing of BS delta at T vs T-1min —
matches within numerical-derivative error.) The per-strike CONTRACT charm is
annualized (T in years); it's rescaled to "per 5 minutes" to match the chart's
own legend before being multiplied out:
    CharmExposure(strike) = Σ [charm_per_5min × open_interest × contract_mult
                                × spot], calls positive, puts negative
— the same "dealer is long calls / short puts" convention gex.py's GEX uses,
applied to charm instead of gamma. Not observed truth, just the standard
public-tool assumption.

Expected-move envelope (the dotted lines):
width = spot_at_fetch × ATM_IV × √(T_remaining_to_tracked_expiry), where
ATM_IV/T come off whichever contract in the chain sits nearest the money at
that fetch. This is a from-scratch ±1σ Black-Scholes expected-move band, not
a reverse-engineered replica of any specific vendor's line — it narrows toward
zero as T_remaining shrinks over the session, which is the visual shape being
matched, not a claim about the exact source methodology.

Heatmap smoothing:
The options chain only gives discrete per-strike sums. To read as a smooth
continuous gradient (like the reference charts) rather than banded rows, each
screen row's price is linearly interpolated between the two nearest strikes'
charm exposure — a display-smoothing choice, not additional real granularity.

Data-history limitation (same as gex.py):
Neither feed exposes *historical* per-strike greeks/OI, only a live snapshot.
Bars for times before this tool started polling show candles (OHLC bars
themselves ARE available in bulk from Deribit/Yahoo) but no heatmap fill —
there's no options-chain snapshot to paint them with. Run --headless
continuously to build up real heatmap history to scroll back into (one
instance per symbol, same convention gex.py uses — headless no longer covers
multiple tickers in one process now that the symbol is switchable live). Use
--list-dates to see which symbols/days already have logged charm history, and
--date MM_DD_YYYY (one of those dates) to browse it — that also fetches THAT
day's own OHLC bars fresh from Deribit/Yahoo's historical range, so candles
line up with the charm data instead of always showing today's.

Bar granularity: [I] opens a one-line prompt — type e.g. "5m" (minutes), "30s"
(seconds), or "500v" (volume-bucketed), Enter to apply, Esc to cancel. Deribit's
own OHLC endpoint natively supports only a fixed ladder of minute resolutions
(NATIVE_DERIBIT_MINUTES); any other minute value you type, plus every seconds/
volume spec, is instead built by resampling Deribit's public trade tape — so
ETH/BTC accept ANY positive value in any of the three units. An equity/ETF
ticker has no free tick/trade feed, so it's limited to Yahoo's own pre-built
minute bars (1/2/5/15/30/60m) — no seconds, no volume mode; typing one of
those for an equity ticker is rejected with a status message rather than
silently doing something wrong.

Usage:
  python charm.py [SYMBOL] [--interval SEC] [--all-exp] [--date MM_DD_YYYY]
                   [--headless] [--smooth-n N] [--bar SPEC] [--list-dates]
    SYMBOL             which ticker to start on (default ETH) — ETH/BTC route
                        to Deribit, anything else is tried as a CBOE-listed
                        equity/ETF ticker (e.g. QQQ, SPY). Can be changed
                        in-app with [S]
    --interval SEC     refresh interval in seconds (default 60)
    --all-exp          sum charm across ALL expiries instead of just the
                        nearest (nearest/0DTE-style is the default); ATM
                        IV/T for the envelope still comes from whichever
                        single contract is nearest the money
    --date MM_DD_YYYY  browse a past day's logged charm history for SYMBOL
                        AND that day's own OHLC bars, instead of going live —
                        see --list-dates for which symbol/days are available
    --list-dates       print which symbols have logged charm history and on
                        which MM_DD_YYYY dates, then exit
    --headless         no UI — just fetch+log SYMBOL on schedule (run a
                        separate instance per symbol you want logged)
    --smooth-n N       raw fetches averaged into the displayed Net Charm
                        figure (default 5; 1 = show the raw instantaneous sum)
    --bar SPEC         starting bar size, e.g. "5m", "30s" (ETH/BTC-only),
                        "500v" (volume-bucketed, ETH/BTC-only). Falls back to
                        5m if invalid/unsupported for SYMBOL (default 5m)

In-app: [S] opens a prompt to switch to any symbol (see above) — resets all
per-symbol state and reloads/refetches for the new ticker. ←/→ pan time by a
few bars, PgUp/PgDn pan by a screenful. ↑/↓ scroll the price axis up/down
(panning only — the auto-fit zoom level from whatever bars are visible is
kept, just the vertical center moves). [C] re-centers the price axis on
whatever's currently in view WITHOUT resetting the time axis (distinct from
[Z]/[End]/[Esc], which reset both time AND price back to the live edge). Esc
does NOT quit — only [Q] does. [R] refreshes now (live mode) or reloads the
log + refetches that day's bars (history mode). [I] opens the bar-interval
prompt (see "Bar granularity" above). [T] opens a prompt to change
REFRESH_SEC — how often the options chain + OHLC bars are re-fetched (default
60s, floored at 5s) — without restarting (no effect in history mode, which
fetches nothing on a timer). [P] saves a plain-text screenshot to
screenshots/YYYY/MM/DD/charm_<SYMBOL>_*.txt, same convention as
gex.py/chart.py elsewhere in this repo.
"""

import sys
import time
import threading
import os
import json
import bisect
import math
import glob

def _pip_install(pkg):
    """Best-effort `pip install`. Plain install first; if that fails because the
    interpreter is a PEP 668 "externally managed environment" (Homebrew's Python
    on macOS enforces this by default since 3.12-ish) or any other non-zero exit,
    retry with --break-system-packages --user — installs into the user's own
    site-packages rather than system-wide, so it doesn't touch Homebrew's managed
    install the way a bare --break-system-packages would."""
    import subprocess
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    except subprocess.CalledProcessError:
        pass
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", pkg],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# Windows compatibility — install windows-curses if _curses is missing
try:
    import curses
except ModuleNotFoundError:
    try:
        _pip_install("windows-curses")
        import curses
    except Exception as e:
        print(f"Could not load curses: {e}")
        print("Run:  pip install --user --break-system-packages windows-curses")
        sys.exit(1)
try:
    import requests
except ModuleNotFoundError:
    try:
        _pip_install("requests")
        import requests
    except Exception as e:
        print(f"Could not install requests automatically: {e}")
        print("Run:  pip3 install --user --break-system-packages requests")
        sys.exit(1)

from collections import deque
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# Force UTF-8 stdout — the default Windows console codepage (cp1252) can't encode
# this file's unicode glyphs (δ, σ, √, ●, etc.), which would otherwise crash --help.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── ARG PARSING ────────────────────────────────────────────────────────────
args = sys.argv[1:]
if "-h" in args or "--help" in args:
    print(__doc__.strip())
    sys.exit(0)
ALL_EXP = "--all-exp" in args
args = [a for a in args if a != "--all-exp"]
HEADLESS = "--headless" in args
args = [a for a in args if a != "--headless"]
MIN_REFRESH_SEC = 5   # floor — also enforced by the in-app [T] refresh-interval prompt
REFRESH_SEC = 60
if "--interval" in args:
    i = args.index("--interval")
    try:
        REFRESH_SEC = max(MIN_REFRESH_SEC, int(args[i + 1]))
    except (IndexError, ValueError):
        pass
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]
SMOOTH_N = 5
if "--smooth-n" in args:
    i = args.index("--smooth-n")
    try:
        SMOOTH_N = max(1, int(args[i + 1]))
    except (IndexError, ValueError):
        pass
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]
LOAD_DATE = None
if "--date" in args:
    i = args.index("--date")
    try:
        LOAD_DATE = args[i + 1]
        datetime.strptime(LOAD_DATE, "%m_%d_%Y")   # validate format
    except (IndexError, ValueError):
        print("--date requires MM_DD_YYYY format, e.g. --date 07_01_2026")
        sys.exit(1)
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]
LIST_DATES = "--list-dates" in args
args = [a for a in args if a != "--list-dates"]
INITIAL_BAR_ARG = None
if "--bar" in args:
    i = args.index("--bar")
    try:
        INITIAL_BAR_ARG = args[i + 1]
    except IndexError:
        print("--bar requires a value, e.g. --bar 5m, --bar 30s, --bar 100vol")
        sys.exit(1)
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]

# Single active symbol, switchable at runtime with [S] — same convention as
# gex.py's own SYMBOL/IS_CRYPTO/MULT globals (reassigned in place by
# switch_symbol(), read fresh by every fetch call). ETH/BTC route to Deribit
# (mult=1, 1 coin/contract); anything else is tried as a CBOE-listed
# equity/ETF ticker (mult=100, 100 shares/contract) — chain.py/gex.py's own
# OCC-parsing generalizes to any ticker CBOE recognizes, so this isn't
# hardcoded to QQQ specifically.
CRYPTO_SYMBOLS = ("ETH", "BTC")

def is_crypto_symbol(symbol):
    return symbol in CRYPTO_SYMBOLS

def mult_for(symbol):
    return 1 if is_crypto_symbol(symbol) else 100

SYMBOL = args[0].upper() if args else "ETH"
if not SYMBOL:
    print("Symbol can't be empty — e.g. ETH, BTC, QQQ, SPY, or any CBOE-listed equity/ETF ticker")
    sys.exit(1)
IS_CRYPTO = is_crypto_symbol(SYMBOL)
MULT      = mult_for(SYMBOL)

TODAY_STR       = datetime.now().strftime("%m_%d_%Y")
VIEW_DATE       = LOAD_DATE or TODAY_STR
HISTORICAL_MODE = LOAD_DATE is not None and LOAD_DATE != TODAY_STR   # pure playback, no live fetch

BASE_URL        = "https://www.deribit.com/api/v2"
CBOE_URL        = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
_YAHOO_HEADERS  = {"User-Agent": "Mozilla/5.0"}

COL_W          = 1       # terminal columns per OHLC bar (heatmap fill shares the same cell)
# Charm-snapshot columns kept in memory per asset (also the cap load_into() reads
# a day's on-disk log into — a --date browse of a busy day is just as capped as
# live scroll-back). Sized for ETH's worst case: 24/7 trading at the minimum
# --interval (MIN_REFRESH_SEC=5s) is 24*3600/5 = 17,280 columns/day. The old
# value (1500) was sized for a ~60s cadence and silently evicted the earlier
# part of the day once a session ran with a shorter interval or just ran long
# enough — confirmed live: a QQQ session logged 9,364 columns in one day,
# 6x the old cap, which is why scrolling back stopped showing anything.
HISTORY_MAXLEN = 20_000
BAR_LOOKBACK_HOURS_ETH = 24   # crypto trades 24/7 — one rolling day of 5-min bars (native minute bars, live mode)
YAHOO_RANGE_QQQ = "1d"        # today's regular session, ~78 5-min bars (native minute bars, live mode)
TICK_LIVE_LOOKBACK_HOURS = 3  # rolling trade-tape window refetched each cycle in seconds/volume bar mode
TRADES_MAX_PAGES = 20          # cap on paginated trade fetches (20 * 1000 = 20,000 trades/window max)

# Bar granularity — set in-app via [I] (opens a type-in prompt, e.g. "5m"/"30s"/"500v")
# or at startup via --bar. Deribit's own get_tradingview_chart_data only natively
# supports a fixed set of minute resolutions (NATIVE_DERIBIT_MINUTES); any other
# minute value, or a seconds/volume spec, is instead built by resampling Deribit's
# public trade tape (see fetch_trades_deribit) — free-form and ETH-only, since QQQ
# has no free tick/trade feed, only Yahoo's own pre-built minute bars (YAHOO_MINUTES).
NATIVE_DERIBIT_MINUTES = {1, 3, 5, 10, 15, 30, 60, 120, 180, 360, 720}
YAHOO_MINUTES = {1, 2, 5, 15, 30, 60}
DEFAULT_BAR_SPEC = {"mode": "time", "unit": "m", "value": 5}
INTERVAL_PROMPT = " Interval — e.g. 5m, 30s, 500v (Enter=confirm, Esc=cancel): "
REFRESH_PROMPT  = " Refresh every N seconds — e.g. 30, 60 (Enter=confirm, Esc=cancel): "
SYMBOL_PROMPT   = " Enter symbol — ETH/BTC or any CBOE-listed ticker (Enter=confirm, Esc=cancel): "

def parse_bar_arg(spec):
    """Parse a typed/--bar value like '5m', '30s', '500v', '500vol' into a bar_spec
    dict {"mode": "time", "unit": "s"|"m", "value": int} or {"mode": "vol", "value":
    int}, or None if unparseable. Does NOT validate against an asset's actual
    capabilities (see validate_bar_spec) — this just parses the text."""
    if not spec:
        return None
    spec = spec.strip().lower()
    try:
        if spec.endswith("vol"):
            return {"mode": "vol", "value": int(spec[:-3])}
        if spec.endswith("v"):
            return {"mode": "vol", "value": int(spec[:-1])}
        if spec.endswith("s"):
            return {"mode": "time", "unit": "s", "value": int(spec[:-1])}
        if spec.endswith("m"):
            return {"mode": "time", "unit": "m", "value": int(spec[:-1])}
    except ValueError:
        return None
    return None

def validate_bar_spec(asset, spec):
    """(ok, error_or_None) — whether `spec` is actually usable for `asset`. Equity/
    ETF tickers (CBOE) have no free tick/trade feed, so they're limited to Yahoo's
    own fixed minute intervals; ETH/BTC (Deribit) can do any positive seconds/
    minutes/volume value (minutes outside Deribit's native set just fall back to
    trade-tape resampling — see fetch_ohlc)."""
    if not spec or spec.get("value", 0) <= 0:
        return False, "interval must be a positive number, e.g. 5m, 30s, 500v"
    crypto = is_crypto_symbol(asset)
    if spec["mode"] == "vol":
        if not crypto:
            return False, f"{asset} has no trade tape — volume bars are ETH/BTC-only"
        return True, None
    if spec["unit"] == "s" and not crypto:
        return False, f"{asset} has no sub-minute data — try e.g. 5m"
    if spec["unit"] == "m" and not crypto and spec["value"] not in YAHOO_MINUTES:
        return False, f"{asset} only supports {sorted(YAHOO_MINUTES)}-minute bars"
    return True, None

def bar_label(bar_spec):
    if bar_spec["mode"] == "vol":
        return f"{bar_spec['value']}v"
    return f"{bar_spec['value']}{bar_spec['unit']}"

LOG_DIR = os.path.dirname(os.path.abspath(__file__))
ARROW_STEP = 5    # bars per Left/Right press
PAGE_STEP  = 30   # bars per PgUp/PgDn press

def _all_logged_symbols_and_dates():
    """{symbol: [MM_DD_YYYY, ...]} for every charm_*.jsonl found on disk — i.e.
    every symbol/day --date can actually browse (charm data only ever exists for
    days this tool ran live/headless and logged a snapshot for that specific
    ticker; see module docstring's "Data-history limitation"). Not hardcoded to
    any fixed symbol list, since [S] now lets the live app switch to any ticker."""
    pattern = os.path.join(LOG_DIR, "logs", "*", "*", "*", "charm_*.jsonl")
    found = {}
    for p in sorted(glob.glob(pattern)):
        stem = os.path.basename(p)[len("charm_"):-len(".jsonl")]   # <SYM>[_allexp]_MM_DD_YYYY
        parts = stem.split("_")
        date_str = "_".join(parts[-3:])
        symbol = "_".join(parts[:-3])
        if symbol.endswith("_allexp"):
            symbol = symbol[:-len("_allexp")]
        found.setdefault(symbol, []).append(date_str)
    return found

if LIST_DATES:
    _found = _all_logged_symbols_and_dates()
    if not _found:
        print("no logged history yet — run live or --headless first")
    for _sym in sorted(_found):
        print(f"{_sym}: " + ", ".join(_found[_sym]))
    sys.exit(0)

CHARM_PERIOD_MIN = 5.0
MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0
PERIOD_SCALE     = CHARM_PERIOD_MIN / MINUTES_PER_YEAR   # annualized charm -> per-5-min charm

# Time-to-expiry floor used ONLY for the charm calc (both fetch_eth_charm and
# _cboe_time_to_expiry_years) — charm's closed-form has a 1/(2T) term, unlike
# gamma's 1/sqrt(T), so it blows up far more violently as T->0. A 60-second floor
# (what gex.py uses for its own gamma calc, where 1/sqrt(60s) is merely a ~700x
# multiplier) lets a single contract's charm hit the millions in the minutes
# around Deribit's DAILY expiry rollover — confirmed empirically: a live column
# at the 60s floor logged one strike's exposure at 1.7M against a normal day's
# few-hundred-to-few-thousand range. This isn't a rare edge case: ETH gets a new
# expiry every ~24h, so an ETH session run continuously WILL cross this every
# single day. 4 hours brings the same worst-case down to roughly the same order
# of magnitude as a normal chain's total (empirically ~14K vs. ~1.7M) — primarily
# a numerical-stability floor, not a claim that BS charm is somehow "wrong" only
# in the literal final minutes. See CHARM_SCALE_PCT below for the other half of
# the fix: even at this floor, a single strike could still be large enough to
# flatten the heatmap's dynamic range, so the color scale itself is also made
# robust to one outlier column rather than relying on the floor alone.
CHARM_MIN_T_SECONDS = 14400.0

# scale_max (the heatmap's color-intensity normalizer) is computed per-column via
# chain_scale() below — the SECOND-highest |charm| across that column's whole
# chain, NOT the raw max. gex.py's own gamma-based GEX map uses a plain running
# max safely, because gamma's gentler 1/sqrt(T) singularity keeps any single
# outlier column within a reasonable multiple of normal. Charm's steeper 1/(2T)
# blowup makes a plain max fragile even with CHARM_MIN_T_SECONDS in place: one
# freak high-OI strike at the exact rollover moment could still dominate
# scale_max for the rest of the session (scale_max only ever grows). Dropping
# just that single highest strike is deliberately NOT a percentile — ETH's
# whole chain is often only ~15-25 strikes (confirmed empirically: at a real
# rollover-adjacent moment, 24 of 25 strikes read exactly 0.0 and ONE read
# 1.7M), so any percentile close to 100% still lands on the max itself for a
# chain that small. A fixed "drop the top one" is size-invariant: it correctly
# zeroes out a column that's genuinely just noise-plus-one-freak-spike (that
# column then contributes ~nothing to the running max, which is correct — the
# running max still gets set by whichever OTHER column had a real, sustained
# peak), without requiring a percentile threshold tuned to a specific chain size.
def chain_scale(charm_dict):
    """Second-highest |charm exposure| across one column's whole chain — see the
    comment above for why this isn't a plain max or a percentile."""
    vals = sorted(abs(v) for v in charm_dict.values())
    if len(vals) < 2:
        return vals[0] if vals else 0.0
    return vals[-2]

# ── BLACK-SCHOLES CHARM (see module docstring for the r=q=0 derivation) ─────
_SQRT_2PI = math.sqrt(2.0 * math.pi)

def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / _SQRT_2PI

def bs_charm(S, K, T, sigma):
    """∂delta/∂t at spot S, strike K, time-to-expiry T (years), annualized vol sigma
    (decimal). r=0, q=0 — under that assumption calls and puts have identical charm
    (see module docstring). Units: 1/year (annualized rate of delta change)."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return _norm_pdf(d1) * d2 / (2.0 * T)

def envelope_band(atm_iv, spot_ref, T_years):
    """± expected-move width (see module docstring) — 0 if IV/T unavailable."""
    if atm_iv <= 0 or T_years <= 0 or spot_ref <= 0:
        return 0.0
    return spot_ref * atm_iv * math.sqrt(T_years)

def bounding_strikes(sorted_strikes, level):
    """The two adjacent strikes bracketing `level` (equal if it lands exactly on one,
    or is beyond either end of the grid) — shared by interpolation and Net Charm math."""
    if not sorted_strikes:
        return None, None
    i = bisect.bisect_left(sorted_strikes, level)
    if i <= 0:
        return sorted_strikes[0], sorted_strikes[0]
    if i >= len(sorted_strikes):
        return sorted_strikes[-1], sorted_strikes[-1]
    if sorted_strikes[i] == level:
        return sorted_strikes[i], sorted_strikes[i]
    return sorted_strikes[i - 1], sorted_strikes[i]

def interp_charm_at_price(strikes_dict, sorted_strikes, price):
    """Piecewise-linear interpolation of net charm exposure at an arbitrary price
    level — see module docstring's "Heatmap smoothing" note. Returns 0.0 off either
    end of the grid (no data to extrapolate against, same as gex.py's dot map showing
    nothing outside its strike grid)."""
    lo, hi = bounding_strikes(sorted_strikes, price)
    if lo is None:
        return 0.0
    if lo == hi:
        return strikes_dict.get(lo, 0.0)
    v_lo, v_hi = strikes_dict.get(lo, 0.0), strikes_dict.get(hi, 0.0)
    frac = (price - lo) / (hi - lo)
    return v_lo + (v_hi - v_lo) * frac

# ── DERIBIT HELPERS (ETH) ───────────────────────────────────────────────────
API_RETRIES = 3
API_BACKOFF_SEC = 0.4

def api(path, **params):
    """Deribit's public API rate-limits under the concurrent per-instrument ticker
    burst fetch_eth_charm does (confirmed live: a 429 on the 3rd call in a tight
    loop). Retrying with a short backoff instead of failing once matters a lot
    here — fetch_eth_charm's ThreadPoolExecutor loop silently drops any
    instrument whose ticker call raises, so an unretried 429 on even one or two
    of ~22 strikes shrinks that column's chain (confirmed: strike count swinging
    5-22 minute to minute), which then shifts which two strikes
    interp_charm_at_price interpolates between — the actual cause of the
    "gaps"/inconsistent coloring, not a math or scale problem."""
    last_exc = None
    for attempt in range(API_RETRIES + 1):
        try:
            r = requests.get(BASE_URL + path, params=params, timeout=12)
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                raise RuntimeError(j["error"]["message"])
            return j["result"]
        except Exception as e:
            last_exc = e
            if attempt < API_RETRIES:
                time.sleep(API_BACKOFF_SEC * (attempt + 1))
    raise last_exc

def fetch_ticker(name):
    return name, api("/public/ticker", instrument_name=name)

def countdown(ts_ms):
    ms = ts_ms - int(time.time() * 1000)
    if ms <= 0:
        return "EXPIRED"
    h, rem = divmod(ms // 1000, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def fetch_eth_charm(currency, all_exp, mult):
    instruments = api("/public/get_instruments", currency=currency, kind="option", expired="false")
    now_ms = int(time.time() * 1000)
    by_exp = {}
    for ins in instruments:
        by_exp.setdefault(ins["expiration_timestamp"], []).append(ins)

    if all_exp:
        chain_ins  = instruments
        target_exp = None
    else:
        target_exp = min((e for e in by_exp if e > now_ms), default=None)
        if not target_exp:
            raise RuntimeError("No active expiry found")
        chain_ins = by_exp[target_exp]

    # 10, not 40 — ETH's near-dated chain is only ~20-45 instruments, so 40
    # concurrent requests was firing nearly the whole chain at once, which is
    # very likely what was tripping Deribit's public-API rate limit in the first
    # place (see api()'s retry logic, added after confirming a live 429).
    # Spreading the same requests over fewer workers reduces how often the
    # retry path even needs to fire.
    with ThreadPoolExecutor(max_workers=10) as ex:
        fut_index   = ex.submit(api, "/public/get_index_price", index_name=f"{currency.lower()}_usd")
        ticker_futs = {ex.submit(fetch_ticker, ins["instrument_name"]): ins for ins in chain_ins}
        spot        = fut_index.result()["index_price"]
        tickers = {}
        for fut in as_completed(ticker_futs):
            try:
                name, t = fut.result()
                tickers[name] = t
            except Exception:
                pass

    strikes        = {}   # strike -> net charm exposure ($delta decay per 5min)
    oi_by_type     = {}
    charm_by_type  = {}
    atm_candidates = []    # (abs(strike-spot), iv_decimal, T_years) — for envelope
    for ins in chain_ins:
        t = tickers.get(ins["instrument_name"])
        if not t:
            continue
        oi     = t.get("open_interest") or 0.0
        iv_pct = t.get("mark_iv") or 0.0   # Deribit reports this in percentage points
        sigma  = iv_pct / 100.0
        exp_ms = ins["expiration_timestamp"]
        T      = max(exp_ms - now_ms, CHARM_MIN_T_SECONDS * 1000) / 1000.0 / 86400.0 / 365.0
        otype  = ins["option_type"]

        charm_annual = bs_charm(spot, ins["strike"], T, sigma)
        exposure = charm_annual * PERIOD_SCALE * oi * mult * spot
        if otype == "put":
            exposure = -exposure
        strikes[ins["strike"]] = strikes.get(ins["strike"], 0.0) + exposure
        oi_by_type.setdefault(ins["strike"], {"call": 0.0, "put": 0.0})[otype] += oi
        charm_by_type.setdefault(ins["strike"], {"call": 0.0, "put": 0.0})[otype] += exposure

        if oi > 0 and sigma > 0:
            atm_candidates.append((abs(ins["strike"] - spot), sigma, T))

    if atm_candidates:
        _, atm_iv, atm_T = min(atm_candidates, key=lambda x: x[0])
    else:
        atm_iv, atm_T = 0.0, 0.0

    if target_exp:
        exp_label = datetime.fromtimestamp(target_exp / 1000, tz=timezone.utc).strftime("%d%b%y").upper()
        ttl       = countdown(target_exp)
    else:
        exp_label = f"ALL({len(by_exp)})"
        ttl       = None

    return {"spot": spot, "strikes": strikes, "oi_by_type": oi_by_type,
            "charm_by_type": charm_by_type, "atm_iv": atm_iv, "T_years": atm_T,
            "expiry_label": exp_label, "ttl": ttl, "fetched_at": datetime.now().strftime("%H:%M:%S")}

def _day_range_ms(date_str):
    """MM_DD_YYYY -> (start_ms, end_ms) spanning that whole local calendar day —
    shared by every historical-playback OHLC fetch below."""
    mm, dd, yyyy = date_str.split("_")
    start = datetime(int(yyyy), int(mm), int(dd), 0, 0, 0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

def fetch_ohlc_deribit(currency, resolution=5, view_date=None):
    """view_date=None: rolling live window ending now. view_date='MM_DD_YYYY': that
    whole calendar day (so historical --date playback shows candles that actually
    line up with that day's logged charm columns, instead of always showing
    whatever's live right now)."""
    if view_date:
        start_ms, end_ms = _day_range_ms(view_date)
        end_ms = min(end_ms, int(time.time() * 1000))
    else:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - BAR_LOOKBACK_HOURS_ETH * 3600 * 1000
    d = api("/public/get_tradingview_chart_data", instrument_name=f"{currency}-PERPETUAL",
            start_timestamp=start_ms, end_timestamp=end_ms, resolution=str(resolution))
    if d.get("status") != "ok" or not d.get("ticks"):
        return []
    bars = []
    for i, ts in enumerate(d["ticks"]):
        try:
            bars.append({"ts": datetime.fromtimestamp(ts / 1000.0), "o": d["open"][i],
                         "h": d["high"][i], "l": d["low"][i], "c": d["close"][i]})
        except (IndexError, TypeError):
            continue
    return bars

def fetch_trades_deribit(currency, start_ms, end_ms, max_pages=TRADES_MAX_PAGES):
    """Paginated raw trade tape for the perpetual, ascending by time — the only free
    source of sub-minute granularity either feed offers (Deribit publishes no charm,
    but does publish its own trade prints for free; CBOE/Yahoo don't). Capped at
    max_pages*1000 trades so a very active window can't balloon an unbounded fetch —
    on a day busy enough to hit the cap, the tail of the window is simply missing
    rather than the request hanging or erroring."""
    instrument = f"{currency}-PERPETUAL"
    trades = []
    cursor = start_ms
    for _ in range(max_pages):
        d = api("/public/get_last_trades_by_instrument", instrument_name=instrument,
                start_timestamp=cursor, end_timestamp=end_ms, count=1000,
                sorting="asc", include_old="true")
        batch = d.get("trades") or []
        if not batch:
            break
        trades.extend(batch)
        if not d.get("has_more"):
            break
        cursor = batch[-1]["timestamp"] + 1
        if cursor >= end_ms:
            break
    return trades

def _resample_seconds(trades, seconds):
    """Bucket a raw trade tape into fixed-length time bars — used for the "seconds"
    granularities Deribit's own OHLC endpoint doesn't support (its resolution floor
    is 1 minute)."""
    bars = []
    bucket_start, o = None, None
    h = l = c = None
    for t in trades:
        ts = t["timestamp"] / 1000.0
        bucket_ts = int(ts // seconds) * seconds
        price = t["price"]
        if bucket_start is None or bucket_ts != bucket_start:
            if bucket_start is not None:
                bars.append({"ts": datetime.fromtimestamp(bucket_start), "o": o, "h": h, "l": l, "c": c})
            bucket_start, o, h, l, c = bucket_ts, price, price, price, price
        else:
            c = price
            h, l = max(h, price), min(l, price)
    if bucket_start is not None:
        bars.append({"ts": datetime.fromtimestamp(bucket_start), "o": o, "h": h, "l": l, "c": c})
    return bars

def _resample_volume(trades, vol_size):
    """Bucket a raw trade tape by cumulative traded volume ("amount", Deribit's own
    contract-size unit for the perpetual — not literally coins) instead of time, so
    each bar represents equal trading activity rather than equal elapsed time. The
    trailing partial bucket (not yet full) is still emitted, same as a live time bar's
    still-forming latest candle, so the chart doesn't lag one whole bucket behind."""
    bars = []
    cum, bucket_start, o = 0.0, None, None
    h = l = c = None
    for t in trades:
        price, amt = t["price"], t.get("amount") or 0.0
        if o is None:
            bucket_start, o, h, l, c = t["timestamp"] / 1000.0, price, price, price, price
        else:
            c = price
            h, l = max(h, price), min(l, price)
        cum += amt
        if cum >= vol_size:
            bars.append({"ts": datetime.fromtimestamp(bucket_start), "o": o, "h": h, "l": l, "c": c})
            cum, bucket_start, o, h, l, c = 0.0, None, None, None, None, None
    if o is not None:
        bars.append({"ts": datetime.fromtimestamp(bucket_start), "o": o, "h": h, "l": l, "c": c})
    return bars

def _tick_window_ms(view_date):
    """Time window for seconds/volume-bar trade fetches: the whole logged calendar
    day for historical playback, or a rolling lookback for live mode (refetching a
    FULL day of tick-by-tick trades every refresh cycle would be needlessly heavy —
    see TICK_LIVE_LOOKBACK_HOURS)."""
    if view_date:
        start_ms, end_ms = _day_range_ms(view_date)
        return start_ms, min(end_ms, int(time.time() * 1000))
    end_ms = int(time.time() * 1000)
    return end_ms - TICK_LIVE_LOOKBACK_HOURS * 3600 * 1000, end_ms

def fetch_second_bars_deribit(currency, seconds, view_date=None):
    start_ms, end_ms = _tick_window_ms(view_date)
    return _resample_seconds(fetch_trades_deribit(currency, start_ms, end_ms), seconds)

def fetch_volume_bars_deribit(currency, vol_size, view_date=None):
    start_ms, end_ms = _tick_window_ms(view_date)
    return _resample_volume(fetch_trades_deribit(currency, start_ms, end_ms), vol_size)

# ── CBOE / YAHOO HELPERS (QQQ) ───────────────────────────────────────────────
def _cboe_seconds_to_close(exp_str):
    """exp_str: 'YYMMDD'. RAW (unfloored) seconds until market close (15:00 local)
    on that date — negative once close has actually passed. Kept separate from
    _cboe_time_to_expiry_years so callers can tell "genuinely approaching expiry"
    apart from "already expired" (see fetch_equity_charm's per-expiry skip)."""
    exp_date = datetime.strptime(exp_str, "%y%m%d").date()
    close_dt = datetime(exp_date.year, exp_date.month, exp_date.day, 15, 0, 0)
    return (close_dt - datetime.now()).total_seconds()

def _cboe_time_to_expiry_years(exp_str):
    """Time to expiry in years, floored at CHARM_MIN_T_SECONDS (see its comment)
    rather than gex.py's own 60s, since charm's 1/(2T) term is far more
    blowup-prone than gamma's 1/sqrt(T). Callers must check
    _cboe_seconds_to_close(exp_str) > 0 first — this floors negative input the
    same as near-zero input, which is WRONG for an already-past-close expiry (see
    fetch_equity_charm's per-expiry skip: CBOE's feed keeps serving a same-day
    0DTE chain for hours after its own close, and flooring ALL its strikes to the
    same T at once — not just one outlier — produced a whole-chain blowup: a live
    after-hours snapshot hit scale_max=49M with a dozen+ strikes each over
    100K, versus a normal day's low-thousands)."""
    return max(_cboe_seconds_to_close(exp_str), CHARM_MIN_T_SECONDS) / 86400.0 / 365.0

def fetch_live_price(symbol):
    """Best-effort near-real-time quote via Yahoo's v8 chart 'meta' block, same as
    gex.py's fetch_live_price — used only to display/position price, since QQQ's
    options feed (CBOE) is itself ~15m delayed. Returns None on any failure."""
    try:
        r = requests.get(YAHOO_CHART_URL.format(symbol), headers=_YAHOO_HEADERS,
                          params={"interval": "1m", "range": "1d"}, timeout=6)
        r.raise_for_status()
        meta  = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        return float(price) if price else None
    except Exception:
        return None

def fetch_equity_charm(symbol, all_exp, mult):
    """Same CBOE feed + OCC-symbol-parsing approach as gex.py's fetch_equity /
    chain.py's fetch_all_equity, but computing charm via Black-Scholes off CBOE's
    published IV instead of reading gamma straight off the feed (CBOE doesn't
    publish charm — see module docstring)."""
    r = requests.get(CBOE_URL.format(symbol), timeout=15)
    r.raise_for_status()
    data  = r.json().get("data") or {}
    price = float(data.get("current_price") or 0)
    if price <= 0:
        raise RuntimeError("no spot price")

    today  = datetime.now().strftime("%y%m%d")
    by_exp = {}
    for o in data.get("options") or []:
        name = o.get("option") or ""
        if len(name) < 15:
            continue
        cp_flag = name[-9]
        exp     = name[-15:-9]
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

    strikes        = {}
    oi_by_type     = {}
    charm_by_type  = {}
    atm_candidates = []   # (abs(strike-price), iv_decimal, T_years)
    for exp in target_exps:
        if _cboe_seconds_to_close(exp) <= 0:
            # This expiry's own close has already passed (common for a couple
            # hours after a same-day/0DTE expiry — CBOE's delayed-quotes feed
            # keeps serving that stale chain until the next session). There's no
            # live dealer positioning left to compute charm against once an
            # expiry has actually closed, so skip its contracts entirely rather
            # than flooring T as if it were still approaching (see
            # _cboe_time_to_expiry_years's docstring for the blowup this caused).
            continue
        T_exp = _cboe_time_to_expiry_years(exp)
        for cp_flag, strike, o in by_exp[exp]:
            oi = float(o.get("open_interest") or 0)
            iv = float(o.get("iv") or 0)   # CBOE reports this as a decimal already

            charm_annual = bs_charm(price, strike, T_exp, iv)
            exposure = charm_annual * PERIOD_SCALE * oi * mult * price
            otype = "call" if cp_flag == "C" else "put"
            if cp_flag == "P":
                exposure = -exposure
            strikes[strike] = strikes.get(strike, 0.0) + exposure
            oi_by_type.setdefault(strike, {"call": 0.0, "put": 0.0})[otype] += oi
            charm_by_type.setdefault(strike, {"call": 0.0, "put": 0.0})[otype] += exposure

            if oi > 0 and iv > 0:
                atm_candidates.append((abs(strike - price), iv, T_exp))

    if atm_candidates:
        _, atm_iv, atm_T = min(atm_candidates, key=lambda x: x[0])
    else:
        atm_iv, atm_T = 0.0, 0.0

    is_0dte   = (not all_exp) and target_exps[0] == today
    exp_label = target_exps[0] if len(target_exps) == 1 else f"ALL({len(target_exps)})"

    live_price = fetch_live_price(symbol)
    spot = live_price if live_price else price

    return {"spot": spot, "spot_is_live": live_price is not None, "cboe_ref_price": price,
            "strikes": strikes, "oi_by_type": oi_by_type, "charm_by_type": charm_by_type,
            "atm_iv": atm_iv, "T_years": atm_T, "expiry_label": exp_label,
            "ttl": None, "is_0dte": is_0dte, "fetched_at": datetime.now().strftime("%H:%M:%S")}

def fetch_ohlc_yahoo(symbol, interval="5m", view_date=None):
    """view_date=None: today's regular session (range=1d). view_date='MM_DD_YYYY':
    that specific calendar day via explicit period1/period2 — Yahoo keeps several
    weeks of intraday history, so this actually works for recent past dates, not
    just today (unlike the old always-live behavior)."""
    params = {"interval": interval}
    if view_date:
        start_ms, end_ms = _day_range_ms(view_date)
        params["period1"] = start_ms // 1000
        params["period2"] = min(end_ms, int(time.time() * 1000)) // 1000
    else:
        params["range"] = YAHOO_RANGE_QQQ
    r = requests.get(YAHOO_CHART_URL.format(symbol), headers=_YAHOO_HEADERS, params=params, timeout=10)
    r.raise_for_status()
    result = r.json()["chart"]["result"]
    if not result:
        return []
    result   = result[0]
    ts_list  = result.get("timestamp") or []
    q        = result["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(ts_list):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        except IndexError:
            continue
        if None in (o, h, l, c):
            continue
        bars.append({"ts": datetime.fromtimestamp(ts), "o": o, "h": h, "l": l, "c": c})
    return bars

def fetch_snapshot(asset):
    mult = mult_for(asset)
    if is_crypto_symbol(asset):
        return fetch_eth_charm(asset, ALL_EXP, mult)
    return fetch_equity_charm(asset, ALL_EXP, mult)

def fetch_ohlc(asset, bar_spec, view_date=None):
    """Dispatches on bar_spec's shape (see parse_bar_arg). Callers are expected to
    have already run validate_bar_spec — this just executes whatever it's given,
    raising if something's structurally impossible (e.g. an equity ticker in
    volume mode) as a last-resort guard rather than the primary check."""
    crypto = is_crypto_symbol(asset)
    if bar_spec["mode"] == "vol":
        if not crypto:
            raise RuntimeError(f"volume bars need a trade tape — {asset} has no free one, only ETH/BTC (Deribit) do")
        return fetch_volume_bars_deribit(asset, bar_spec["value"], view_date)
    unit, val = bar_spec["unit"], bar_spec["value"]
    if crypto:
        if unit == "s":
            return fetch_second_bars_deribit(asset, val, view_date)
        if val in NATIVE_DERIBIT_MINUTES:
            return fetch_ohlc_deribit(asset, resolution=val, view_date=view_date)
        # Not one of Deribit's native resolutions — resample the trade tape instead,
        # so ETH/BTC can do ANY minute value, not just Deribit's fixed ladder.
        return fetch_second_bars_deribit(asset, val * 60, view_date)
    if unit == "s":
        raise RuntimeError(f"{asset} has no sub-minute data — pick a minute-or-larger interval")
    return fetch_ohlc_yahoo(asset, interval=f"{val}m", view_date=view_date)

# ── PERSISTENCE — one JSONL file per asset/day, filed under logs/YYYY/MM/DD/ ─
def _date_folder(*roots, date_str):
    mm, dd, yyyy = date_str.split("_")
    folder = os.path.join(LOG_DIR, *roots, yyyy, mm, dd)
    os.makedirs(folder, exist_ok=True)
    return folder

def log_path(asset, date_str):
    suffix = "_allexp" if ALL_EXP else ""
    folder = _date_folder("logs", date_str=date_str)
    return os.path.join(folder, f"charm_{asset}{suffix}_{date_str}.jsonl")

def append_log(asset, col):
    try:
        with open(log_path(asset, datetime.now().strftime("%m_%d_%Y")), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": col["ts"].isoformat(), "spot": col["spot"], "charm": col["charm"],
                "oi_by_type": col.get("oi_by_type") or {},
                "charm_by_type": col.get("charm_by_type") or {},
                "atm_iv": col.get("atm_iv"), "T_years": col.get("T_years"),
                "expiry_label": col.get("expiry_label"), "is_0dte": col.get("is_0dte"),
            }) + "\n")
        return True, None
    except Exception as e:
        return False, str(e)

def load_log(asset, date_str):
    path = log_path(asset, date_str)
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
                    "charm": {float(k): v for k, v in d["charm"].items()},
                    "oi_by_type": {float(k): v for k, v in (d.get("oi_by_type") or {}).items()},
                    "charm_by_type": {float(k): v for k, v in (d.get("charm_by_type") or {}).items()},
                    "atm_iv": d.get("atm_iv") or 0.0, "T_years": d.get("T_years") or 0.0,
                    "expiry_label": d.get("expiry_label"), "is_0dte": d.get("is_0dte"),
                })
            except Exception:
                continue
    return cols

STATUS_EXPORT_DIR = os.path.dirname(os.path.abspath(__file__))

def export_status_snapshot(asset, col, net_charm):
    """Best-effort, non-blocking — mirrors gex.py's export_status_snapshot/naming
    convention (status_<SYMBOL>_charm.json — a distinct suffix so this can run
    alongside gex.py/athena.py for the same symbol without clobbering their own
    status_<SYMBOL>.json / status_<SYMBOL>_gex.json exports)."""
    try:
        payload = {
            "asset": asset, "updated_at": time.time(), "spot": col["spot"],
            "net_charm": net_charm, "charm_by_strike": col.get("charm") or {},
            "atm_iv": col.get("atm_iv"),
        }
        path = os.path.join(STATUS_EXPORT_DIR, f"status_{asset}_charm.json")
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_path, path)
    except Exception:
        pass

def smoothed_net_charm(history, end_idx, n):
    """Net Charm averaged over the last n raw columns ending at end_idx — absorbs a
    single stale-OI-snapshot artifact, same rationale as gex.py's Max Pain/Flip
    smoothing (see check_flip_jump there); charm has no single "flip point" the way
    a strike-indexed GEX curve does, so this just smooths the aggregate sum."""
    window = history[max(0, end_idx - n):end_idx]
    if not window:
        return None
    vals = [sum(c["charm"].values()) for c in window]
    return sum(vals) / len(vals)

def nearest_charm_column(history_list, hist_ts_list, bar_ts):
    """Latest charm column at or before bar_ts (carried forward) — see module
    docstring's "Data-history limitation" note. None if bar_ts predates every
    column this tool has ever fetched."""
    i = bisect.bisect_right(hist_ts_list, bar_ts) - 1
    if i < 0:
        return None
    return history_list[i]

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def fdollars_compact(v):
    """Compact signed dollar format — $1.23B / $45.6M / $789.1K / $12.34."""
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e9:
        return f"{sign}${v / 1e9:,.2f}B"
    if v >= 1e6:
        return f"{sign}${v / 1e6:,.2f}M"
    if v >= 1e3:
        return f"{sign}${v / 1e3:,.2f}K"
    return f"{sign}${v:,.2f}"

# ── COLOUR PAIRS ─────────────────────────────────────────────────────────────
P_DEFAULT, P_DIM, P_CYAN, P_YELLOW, P_GREEN, P_RED, P_STATUS, P_ATM = range(1, 9)

N_HEAT_TIERS = 7   # per side (positive/orange, negative/blue)
HEAT_PAIR_BASE = 20
HEAT_PAIRS_POS = []
HEAT_PAIRS_NEG = []
CANDLE_BULL = {}
CANDLE_BEAR = {}
CANDLE_WICK = {}
ENV_DOT = {}

def init_colors():
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
    curses.init_pair(P_ATM,     curses.COLOR_YELLOW, BG)

def _try_init_pair(pid, fg, bg):
    try:
        curses.init_pair(pid, fg, bg)
    except curses.error:
        try:
            curses.init_pair(pid, curses.COLOR_WHITE, -1)
        except curses.error:
            pass

def _xterm256_brightness(code):
    """Approximate perceived brightness (0-255) of an xterm-256 color index, used
    to pick black-vs-white overlay text so candle wicks/envelope dots stay visible
    against whatever heat-tile color they're drawn on top of (a flat white wick
    disappears against the palest orange/blue tiers — see CANDLE_WICK below)."""
    if code < 16:
        return 60   # base-8 fallback colors (yellow/blue) — treat as "dark enough" for white text
    if code >= 232:
        return int((code - 232) / 23.0 * 255)   # grayscale ramp
    idx = code - 16
    r, g, b = idx // 36, (idx // 6) % 6, idx % 6
    ramp = (0, 95, 135, 175, 215, 255)
    return int(0.299 * ramp[r] + 0.587 * ramp[g] + 0.114 * ramp[b])

def _contrast_fg(bg_code):
    """Black text on pale tiles, white on dark tiles/the neutral default background
    (bg_code=-1) — this repo's tools all assume a dark terminal theme for their own
    plain white-on-default text (P_DEFAULT etc.), so -1 defaults to white here too."""
    if bg_code is None or bg_code < 0:
        return curses.COLOR_WHITE
    return curses.COLOR_BLACK if _xterm256_brightness(bg_code) > 140 else curses.COLOR_WHITE

def init_heat_pairs():
    """Diverging orange(+)/blue(-) heatmap palette. Uses xterm-256 color numbers when
    the terminal supports >=256 colors for a real gradient (matching the reference
    charts' smooth look); falls back to a flat yellow/blue base-8-color fill
    otherwise (base curses has no true orange either — same caveat gex.py documents
    for its own P_ORANGE) — every cell still shows *something* directional, just
    without the fine-grained tiering.
    Also builds combined "overlay-on-heat" pairs (candle bull/bear body, dim wick,
    envelope dot) for every heat tier PLUS a neutral (no-fill) background, since
    curses pairs are fixed (fg,bg) combos — a candle glyph drawn on top of a colored
    heatmap cell needs its own pair carrying that same background color, or it would
    blank the heatmap color out from under it."""
    global HEAT_PAIRS_POS, HEAT_PAIRS_NEG, CANDLE_BULL, CANDLE_BEAR, CANDLE_WICK, ENV_DOT
    has256 = curses.COLORS >= 256
    if has256:
        pos_colors = [230, 223, 216, 209, 202, 166, 130]   # pale -> deep orange
        neg_colors = [195, 159, 123, 81,  45,  33,  27]    # pale -> deep blue
    else:
        pos_colors = [curses.COLOR_YELLOW] * N_HEAT_TIERS
        neg_colors = [curses.COLOR_BLUE] * N_HEAT_TIERS

    pid = HEAT_PAIR_BASE
    HEAT_PAIRS_POS = []
    for c in pos_colors:
        _try_init_pair(pid, c, c)
        HEAT_PAIRS_POS.append(pid)
        pid += 1
    HEAT_PAIRS_NEG = []
    for c in neg_colors:
        _try_init_pair(pid, c, c)
        HEAT_PAIRS_NEG.append(pid)
        pid += 1

    bg_for_key = {("neu", None): -1}
    for i, c in enumerate(pos_colors):
        bg_for_key[("pos", i)] = c
    for i, c in enumerate(neg_colors):
        bg_for_key[("neg", i)] = c

    CANDLE_BULL, CANDLE_BEAR, CANDLE_WICK, ENV_DOT = {}, {}, {}, {}
    for key, bg in bg_for_key.items():
        _try_init_pair(pid, curses.COLOR_GREEN, bg); CANDLE_BULL[key] = pid; pid += 1
        _try_init_pair(pid, curses.COLOR_RED,   bg); CANDLE_BEAR[key] = pid; pid += 1
        overlay_fg = _contrast_fg(bg)   # black on pale tiles, white on dark tiles/default bg
        _try_init_pair(pid, overlay_fg, bg); CANDLE_WICK[key] = pid; pid += 1
        _try_init_pair(pid, overlay_fg, bg); ENV_DOT[key]     = pid; pid += 1

def cp(pair, bold=False, dim=False):
    a = curses.color_pair(pair)
    if bold: a |= curses.A_BOLD
    if dim:  a |= curses.A_DIM
    return a

def heat_tier(net, scale_max):
    """(sign, tier_idx) for |net| as a fraction of scale_max, or (0, -1) if too small
    to color (leaves the cell as terminal-default background, like gex.py's blank
    magnitude tier)."""
    if scale_max <= 0:
        return 0, -1
    frac = min(1.0, abs(net) / scale_max)
    if frac < 0.03:
        return 0, -1
    idx = min(N_HEAT_TIERS - 1, int(frac * N_HEAT_TIERS))
    return (1 if net > 0 else -1), idx

def heat_key(sign, idx):
    if idx < 0:
        return ("neu", None)
    return ("pos", idx) if sign > 0 else ("neg", idx)

_screen_mirror = []

def _mirror_reset(h, w):
    global _screen_mirror
    _screen_mirror = [[" "] * w for _ in range(h)]

def safe_add(win, y, x, s, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0:
        return
    avail = w - x - 1
    if avail <= 0:
        return
    s = s[:avail]
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass
    if _screen_mirror and y < len(_screen_mirror):
        row = _screen_mirror[y]
        row_w = len(row)
        for i, ch in enumerate(s):
            xi = x + i
            if 0 <= xi < row_w:
                row[xi] = ch

def take_screenshot(asset):
    """Dump the current frame to screenshots/YYYY/MM/DD/charm_<SYMBOL>_*.txt — same
    plain-text-mirror convention gex.py/chart.py use elsewhere in this repo (reads
    _screen_mirror rather than curses' own buffer since this file's glyphs — █, │,
    ·, etc. — are multi-byte and don't reliably round-trip through win.instr())."""
    now = datetime.now()
    folder = _date_folder("screenshots", date_str=now.strftime("%m_%d_%Y"))
    ts = now.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(folder, f"charm_{asset}_{ts}.txt")
    lines = ["".join(row).rstrip() for row in _screen_mirror]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path

# ── HEADER / LEGEND / COLORBAR ───────────────────────────────────────────────
def build_header(asset, bars, meta, live_follow, end_idx, bar_spec):
    crypto  = is_crypto_symbol(asset)
    label   = meta.get("expiry_label", "—")
    ttl     = meta.get("ttl")
    is_0dte = meta.get("is_0dte", True if crypto else False)
    fetched = meta.get("fetched_at", "—")

    if live_follow:
        mode_piece = ("● LIVE", cp(P_GREEN, bold=True))
        spot = bars[-1]["c"] if bars else meta.get("spot", 0.0)
    else:
        viewed_ts = bars[end_idx - 1]["ts"].strftime("%H:%M:%S") if bars else "—"
        mode_piece = (f"⏸ HISTORY @{viewed_ts}", cp(P_YELLOW, bold=True))
        spot = bars[end_idx - 1]["c"] if bars else meta.get("spot", 0.0)

    pieces = [
        ("CHARM MAP", cp(P_CYAN, bold=True)),
        ("  │  ", cp(P_DIM, dim=True)),
        (asset, cp(P_DEFAULT, bold=True)),
        ("  [S]ymbol  ", cp(P_DIM, dim=True)),
        mode_piece,
        ("  Bar ", cp(P_DIM, dim=True)),
        (bar_label(bar_spec), cp(P_CYAN, bold=True)),
        ("  Spot ", cp(P_DIM, dim=True)),
        (f"${spot:,.2f}" if spot else "—", cp(P_YELLOW, bold=True)),
        ("  Expiry ", cp(P_DIM, dim=True)),
        (f"{label}{'*' if is_0dte else ''}", cp(P_DEFAULT)),
    ]
    if ttl and live_follow:
        pieces += [("  TTL ", cp(P_DIM, dim=True)), (ttl, cp(P_YELLOW, bold=True))]
    if HISTORICAL_MODE:
        src_tag = f"  (historical playback — {VIEW_DATE})"
    elif crypto:
        src_tag = "  (live)"
    elif meta.get("spot_is_live"):
        src_tag = "  (spot live via Yahoo, charm ~15m delay via CBOE)"
    else:
        src_tag = "  (spot+charm ~15m delay — live quote fetch failed)"
    pieces += [("  Updated ", cp(P_DIM, dim=True)), (fetched, cp(P_DEFAULT)),
               (src_tag, cp(P_DIM, dim=True))]
    return spot, pieces

def draw_colorbar(win, top_row, col0=0):
    """Vertical colorbar legend at top-left: 'Charm (δ/5 min)', orange tiers above
    0, blue tiers below — mirrors the reference charts' left-side legend box."""
    safe_add(win, top_row, col0, "Charm", cp(P_DEFAULT, bold=True))
    safe_add(win, top_row + 1, col0, "(δ/5 min)", cp(P_DIM, dim=True))
    bar_top = top_row + 2
    n = N_HEAT_TIERS
    safe_add(win, bar_top, col0, "100", cp(P_DEFAULT, bold=True))
    for i in range(n):
        pair_id = HEAT_PAIRS_POS[n - 1 - i]   # deepest orange nearest "100"
        safe_add(win, bar_top + 1 + i, col0, "  ", curses.color_pair(pair_id))
    zero_row = bar_top + 1 + n
    safe_add(win, zero_row, col0, "0", cp(P_DIM, dim=True))
    for i in range(n):
        pair_id = HEAT_PAIRS_NEG[i]   # palest blue nearest "0", deepest nearest "-100"
        safe_add(win, zero_row + 1 + i, col0, "  ", curses.color_pair(pair_id))
    safe_add(win, zero_row + 1 + n, col0, "-100", cp(P_DEFAULT, bold=True))
    return zero_row + 2 + n   # first unused row, for callers that want to know

# ── DRAW: CHARM HEATMAP + OHLC OVERLAY ───────────────────────────────────────
def draw(win, asset, history, bars, meta, status, ui):
    h, w = win.getmaxyx()
    win.erase()
    _mirror_reset(h, w)

    live_follow = ui["live_follow"]
    n_bars      = len(bars)
    end_idx     = n_bars if live_follow else max(1, min(ui["view_end_idx"], n_bars))

    spot, pieces = build_header(asset, bars, meta, live_follow, end_idx, ui["bar"])
    x = 0
    for text, attr in pieces:
        safe_add(win, 0, x, text, attr)
        x += len(text)

    legend = [
        ("█ ", cp(P_GREEN, bold=True)), ("bull  ", cp(P_DIM, dim=True)),
        ("█ ", cp(P_RED, bold=True)), ("bear  ", cp(P_DIM, dim=True)),
        ("orange", cp(P_YELLOW, bold=True)), ("=+charm(Δ building)  ", cp(P_DIM, dim=True)),
        ("blue", cp(P_CYAN, bold=True)), ("=-charm(Δ decaying)  ", cp(P_DIM, dim=True)),
        (": ", cp(P_DEFAULT, bold=True)), ("=±1σ expected-move envelope", cp(P_DIM, dim=True)),
    ]
    x = 0
    for text, attr in legend:
        safe_add(win, 1, x, text, attr)
        x += len(text)

    colorbar_w = 9
    bottom_reserved = 3   # time axis + info row + status bar
    axis_w = 9             # right-side price label width

    # The chart canvas starts right after the header/legend rows and fills
    # everything down to bottom_reserved, regardless of terminal height. The
    # colorbar is a small fixed-size decoration overlaid in its own narrow left
    # column starting at the same row — it does NOT push the chart down (it used
    # to: chart_top was derived from the colorbar's own ~20-row height, wasting a
    # large blank gap on any terminal taller than that). safe_add's own bounds
    # check just clips the colorbar's bottom few rows if the terminal is shorter
    # than its ~20-row content.
    chart_top = 2
    draw_colorbar(win, chart_top, col0=1)

    if not bars:
        msg = (f"No OHLC bars for {asset} yet." if HISTORICAL_MODE else "Waiting for first fetch…")
        safe_add(win, h // 2, colorbar_w + 2, msg, cp(P_CYAN))
        bot = h - 1
        hint = f" q=quit  Tab=switch  [{asset}]  {status}"
        safe_add(win, bot, 0, hint.ljust(w - 1)[:w - 1], cp(P_STATUS))
        win.noutrefresh()
        return None

    chart_bot   = h - bottom_reserved
    chart_left  = colorbar_w
    chart_right = w - axis_w - 1
    if chart_bot <= chart_top + 1 or chart_right <= chart_left + 1:
        win.noutrefresh()
        return None

    n_cols = max(1, (chart_right - chart_left) // COL_W)
    start_idx = max(0, end_idx - n_cols)
    visible_bars = bars[start_idx:end_idx]
    if not visible_bars:
        win.noutrefresh()
        return None

    hist_ts = [c["ts"] for c in history]

    price_hi = max(b["h"] for b in visible_bars)
    price_lo = min(b["l"] for b in visible_bars)
    if price_hi <= price_lo:
        price_hi += 1.0
        price_lo -= 1.0
    # Auto-fit tracks the visible CANDLES only — a prior version also stretched
    # this to reach distant high-charm strikes, which kept candles readable but
    # squished them into a thin band whenever a concentration sat far from
    # price (confirmed: this is worse, not better). Use ↑/↓ (_scroll_vert) to
    # pan down/up to a distant strike manually instead of auto-widening to it.
    pad = (price_hi - price_lo) * 0.08
    price_hi += pad
    price_lo -= pad
    # ↑/↓ (see _scroll_vert) only override the CENTER, not this auto-fit span/zoom —
    # so scrolling pans the window rather than rescaling it.
    if not ui["vert_follow"] and ui.get("vert_center") is not None:
        span = price_hi - price_lo
        price_hi = ui["vert_center"] + span / 2
        price_lo = ui["vert_center"] - span / 2
    span_rows  = max(1, chart_bot - chart_top - 1)
    span_price = max(1e-9, price_hi - price_lo)

    def row_of(price):
        frac = (price_hi - price) / span_price
        return chart_top + frac * span_rows

    def price_of(row):
        frac = (row - chart_top) / span_rows
        return price_hi - frac * span_price

    for i, bar in enumerate(visible_bars):
        cx = chart_left + i * COL_W
        if cx >= chart_right:
            break
        col_data = nearest_charm_column(history, hist_ts, bar["ts"])
        if col_data:
            strikes_dict   = col_data["charm"]
            sorted_strikes = sorted(strikes_dict)
            scale          = col_data.get("scale_at_ingest") or 0.0
            atm_iv         = col_data.get("atm_iv") or 0.0
            T_years        = col_data.get("T_years") or 0.0
            col_spot       = col_data.get("spot")
        else:
            strikes_dict, sorted_strikes, scale, atm_iv, T_years, col_spot = None, [], 0.0, 0.0, 0.0, None

        env_upper_row = env_lower_row = None
        if col_data and col_spot:
            width = envelope_band(atm_iv, col_spot, T_years)
            if width > 0:
                env_upper_row = round(row_of(col_spot + width))
                env_lower_row = round(row_of(col_spot - width))

        r_hi  = clamp(round(row_of(bar["h"])), chart_top, chart_bot - 1)
        r_lo  = clamp(round(row_of(bar["l"])), chart_top, chart_bot - 1)
        r_top = clamp(round(row_of(max(bar["o"], bar["c"]))), chart_top, chart_bot - 1)
        r_bot = clamp(round(row_of(min(bar["o"], bar["c"]))), chart_top, chart_bot - 1)
        bull  = bar["c"] >= bar["o"]

        for ry in range(chart_top, chart_bot):
            if strikes_dict:
                price = price_of(ry)
                val = interp_charm_at_price(strikes_dict, sorted_strikes, price)
                sign, idx = heat_tier(val, scale)
            else:
                sign, idx = 0, -1
            key = heat_key(sign, idx)

            is_body = r_top <= ry <= r_bot
            is_wick = r_hi <= ry <= r_lo
            is_env  = ry == env_upper_row or ry == env_lower_row

            if is_body:
                ch = "─" if r_top == r_bot else "█"
                pair = CANDLE_BULL[key] if bull else CANDLE_BEAR[key]
                safe_add(win, ry, cx, ch, curses.color_pair(pair) | curses.A_BOLD)
            elif is_wick:
                safe_add(win, ry, cx, "│", curses.color_pair(CANDLE_WICK[key]))
            elif is_env:
                safe_add(win, ry, cx, ":", curses.color_pair(ENV_DOT[key]) | curses.A_BOLD)
            elif idx >= 0:
                fillpair = HEAT_PAIRS_POS[idx] if sign > 0 else HEAT_PAIRS_NEG[idx]
                safe_add(win, ry, cx, " ", curses.color_pair(fillpair))

    # ── Right-side price axis (a handful of evenly-spaced gridline labels) ──
    n_ticks = max(2, min(6, span_rows // 4))
    for t in range(n_ticks + 1):
        ry = chart_top + round(t * span_rows / n_ticks)
        if chart_top <= ry < chart_bot:
            p = price_of(ry)
            safe_add(win, ry, chart_right + 1, f"{p:,.2f}", cp(P_DIM, dim=True))

    # ── Time axis ────────────────────────────────────────────────────────
    axis_row = chart_bot
    label_every = max(1, 8 // COL_W)
    cx = chart_left
    for i, bar in enumerate(visible_bars):
        if cx >= chart_right - 5:
            break
        if i % label_every == 0 or i == len(visible_bars) - 1:
            safe_add(win, axis_row, cx, bar["ts"].strftime("%H:%M"), cp(P_DIM, dim=True))
        cx += COL_W

    # ── Info row: Net Charm / ATM IV ──────────────────────────────────────
    net_charm = smoothed_net_charm(history, len(history), SMOOTH_N)
    if net_charm is None:
        net_str, net_attr = "N/A", cp(P_DIM, dim=True)
    else:
        net_str = fdollars_compact(net_charm)
        net_attr = cp(P_GREEN if net_charm >= 0 else P_RED, bold=True)
    latest = history[-1] if history else None
    iv_str = f"{latest['atm_iv'] * 100:.1f}%" if latest and latest.get("atm_iv") else "N/A"
    info_pieces = [
        (" Net Charm(5m) ", cp(P_DIM, dim=True)), (net_str, net_attr),
        ("    ATM IV ", cp(P_DIM, dim=True)), (iv_str, cp(P_CYAN, bold=True)),
        (f"    (smoothed over last {SMOOTH_N})", cp(P_DIM, dim=True)),
    ]
    info_row = chart_bot + 1
    ix = 0
    for text, attr in info_pieces:
        safe_add(win, info_row, ix, text, attr)
        ix += len(text)

    # ── Status bar ───────────────────────────────────────────────────────
    bot = h - 1
    if ui.get("entering_interval"):
        prompt = INTERVAL_PROMPT + ui.get("interval_input_buf", "")
        safe_add(win, bot, 0, prompt.ljust(w - 1)[:w - 1], cp(P_STATUS))
    elif ui.get("entering_refresh"):
        prompt = REFRESH_PROMPT + ui.get("refresh_input_buf", "")
        safe_add(win, bot, 0, prompt.ljust(w - 1)[:w - 1], cp(P_STATUS))
    elif ui.get("entering_symbol"):
        prompt = SYMBOL_PROMPT + ui.get("symbol_input_buf", "")
        safe_add(win, bot, 0, prompt.ljust(w - 1)[:w - 1], cp(P_STATUS))
    else:
        reload_hint = "r=reload" if HISTORICAL_MODE else "r=refresh"
        hint = (f" q=quit  s=symbol  {reload_hint}  "
                f"←/→/PgUp/PgDn=pan  ↑/↓=scroll  c=center  z/End/Esc=live-edge  i=interval  t=timer  "
                f"p=screenshot  [{asset}] {status}")
        safe_add(win, bot, 0, hint.ljust(w - 1)[:w - 1], cp(P_STATUS))

    win.noutrefresh()
    return span_price / span_rows   # price-per-row — see _scroll_vert

# ── PER-ASSET LIVE STATE ─────────────────────────────────────────────────────
def _initial_bar_state(asset):
    """Apply --bar (if given and valid for this asset) else the 5-minute default —
    silently falls back to the default rather than erroring, since --bar with no
    asset context can't know in advance whether it'll accept it (e.g. a
    seconds/volume spec only makes sense for a crypto symbol)."""
    parsed = parse_bar_arg(INITIAL_BAR_ARG)
    if not parsed:
        return dict(DEFAULT_BAR_SPEC)
    ok, _ = validate_bar_spec(asset, parsed)
    return parsed if ok else dict(DEFAULT_BAR_SPEC)

def _fresh_state():
    return {
        "history": deque(maxlen=HISTORY_MAXLEN), "bars": [], "scale_max": 0.0,
        "meta": {}, "error_msg": "", "last_fetch": 0.0, "fetch_started": 0.0,
        "fetch_dur": 5.0, "fetching": False, "log_rows": 0, "log_err": None,
        "live_follow": True, "view_end_idx": 0, "bar": dict(DEFAULT_BAR_SPEC),
        "vert_follow": True, "vert_center": None, "row_price": None,
    }

# Single active symbol's live state — SYMBOL/IS_CRYPTO/MULT (module globals,
# reassigned by switch_symbol) say WHICH ticker this is; STATE holds its data.
# fetch_epoch is bumped on every switch so a fetch already in flight for the
# OLD symbol discards its result on completion rather than merging stale data
# into the new symbol's just-reset STATE — same convention as gex.py's own
# _switch_symbol/fetch_epoch.
STATE = _fresh_state()
STATE["bar"] = _initial_bar_state(SYMBOL)
LOCK = threading.Lock()
fetch_epoch = 0

def switch_symbol(new_symbol):
    """Retarget the whole app at a different symbol without restarting: resets
    every piece of per-symbol state, then (re)loads whatever's already logged
    for the new symbol at VIEW_DATE — mirrors what happens at startup. Caller
    must hold LOCK; does not itself trigger a fetch (same convention as gex.py's
    own _switch_symbol — call trigger_fetch after releasing the lock)."""
    global SYMBOL, IS_CRYPTO, MULT, fetch_epoch
    SYMBOL    = new_symbol
    IS_CRYPTO = is_crypto_symbol(SYMBOL)
    MULT      = mult_for(SYMBOL)
    fetch_epoch += 1
    STATE.update(_fresh_state())
    if HISTORICAL_MODE:
        load_historical(SYMBOL)
    else:
        load_into(SYMBOL, VIEW_DATE)   # resume today's log if present

def load_into(asset, date_str):
    """(Re)populate STATE's history/scale_max/meta from asset's day-log on disk."""
    STATE["history"].clear()
    STATE["scale_max"] = 0.0
    for c in load_log(asset, date_str):
        if not c["charm"]:
            # Skip empty (no live chain) columns — a log written before the
            # do_fetch fix (see its comment) could have these; loading them
            # would make nearest_charm_column's carry-forward land on an empty
            # column instead of skipping back to the last real one.
            continue
        local_max = chain_scale(c["charm"])
        STATE["scale_max"] = max(STATE["scale_max"], local_max)
        # Frozen at ingestion — draw() sizes THIS column's heat tier against the
        # scale that existed when it was gathered, never retroactively rescaled by
        # a later, bigger spike (same convention as gex.py's scale_at_ingest).
        c["scale_at_ingest"] = STATE["scale_max"]
        STATE["history"].append(c)
    if STATE["history"]:
        last = STATE["history"][-1]
        STATE["meta"] = {"spot": last["spot"], "expiry_label": last.get("expiry_label") or "—",
                          "is_0dte": last.get("is_0dte"), "fetched_at": last["ts"].strftime("%H:%M:%S"),
                          "atm_iv": last.get("atm_iv"), "T_years": last.get("T_years")}
    STATE["log_rows"] = len(STATE["history"])

def load_historical(asset):
    """HISTORICAL_MODE only: (re)load asset's logged charm history for VIEW_DATE
    AND fetch that SAME calendar day's OHLC bars. Bars are never logged (only charm
    snapshots are), so they have to be fetched fresh from Deribit/Yahoo's own
    historical range each time — this is what makes --date playback actually show
    candles that line up with that day's charm data, instead of always showing
    whatever's live right now."""
    load_into(asset, VIEW_DATE)
    try:
        STATE["bars"] = fetch_ohlc(asset, STATE["bar"], VIEW_DATE)
        STATE["error_msg"] = ""
    except Exception as e:
        STATE["bars"] = []
        STATE["error_msg"] = str(e)

def refetch_bars_only(asset):
    """Re-fetch just the OHLC bars (not the options chain) after an [I] interval
    change — runs in a background thread like trigger_fetch, so switching
    timeframe doesn't block the render loop while Deribit/Yahoo respond."""
    with LOCK:
        epoch = fetch_epoch
        bar_spec = dict(STATE["bar"])
    def _run():
        view_date = VIEW_DATE if HISTORICAL_MODE else None
        try:
            bars = fetch_ohlc(asset, bar_spec, view_date)
            with LOCK:
                if epoch != fetch_epoch:
                    return   # symbol changed while this was in flight — discard
                STATE["bars"] = bars
                STATE["error_msg"] = ""
        except Exception as e:
            with LOCK:
                if epoch == fetch_epoch:
                    STATE["error_msg"] = str(e)
    threading.Thread(target=_run, daemon=True).start()

ROWS_PER_SCROLL = 7.5   # terminal rows per ↑/↓ press (2.5x the original 3)

def _scroll_vert(direction):
    """Pan the visible price window up/down by a few terminal ROWS per press
    (not a % of price — that made one press jump most of the visible range),
    keeping whatever zoom level draw() is currently auto-fitting to (see draw()'s
    price-axis block) — this only overrides the CENTER, not the span, so
    scrolling doesn't rescale the chart, just shifts what's in frame. Uses
    row_price — the actual price-per-row from the LAST drawn frame (draw()
    returns it, curses_main stashes it here) — so the step is always "a few rows
    on screen" regardless of current zoom/symbol/price magnitude; falls back to
    a rough %-of-price estimate only before the first frame has ever drawn."""
    with LOCK:
        bars = STATE["bars"]
        ref_price = (bars[-1]["c"] if bars else STATE["meta"].get("spot")) or 0.0
        if STATE["vert_follow"] or STATE["vert_center"] is None:
            STATE["vert_center"] = ref_price
        row_price = STATE.get("row_price") or max(ref_price * 0.002, 0.01)
        STATE["vert_center"] += direction * ROWS_PER_SCROLL * row_price
        STATE["vert_follow"] = False

def do_fetch():
    with LOCK:
        asset = SYMBOL
        epoch = fetch_epoch
        bar_spec = dict(STATE["bar"])
    t0 = time.time()
    try:
        d = fetch_snapshot(asset)
        bars = fetch_ohlc(asset, bar_spec, None)   # do_fetch is live-only — never called in HISTORICAL_MODE
        elapsed = time.time() - t0
        with LOCK:
            if epoch != fetch_epoch:
                return   # symbol changed while this fetch was in flight — discard
            col = {"ts": datetime.now(), "spot": d["spot"], "charm": d["strikes"],
                   "oi_by_type": d.get("oi_by_type") or {},
                   "charm_by_type": d.get("charm_by_type") or {},
                   "atm_iv": d.get("atm_iv", 0.0), "T_years": d.get("T_years", 0.0),
                   "expiry_label": d.get("expiry_label"),
                   "is_0dte": d.get("is_0dte", True if is_crypto_symbol(asset) else False)}
            if bars:
                STATE["bars"] = bars
            STATE["meta"] = {k: v for k, v in d.items() if k != "strikes"}
            STATE["error_msg"] = ""
            STATE["last_fetch"] = time.time()
            STATE["fetch_dur"] = elapsed
            if col["charm"]:
                # Only append/log/export when there's actually a chain to show —
                # e.g. an equity ticker after market close legitimately has
                # nothing (see fetch_equity_charm's past-close skip). Appending
                # an empty placeholder column every cycle anyway would burn
                # through HISTORY_MAXLEN for no reason, evicting real earlier-
                # in-the-day data faster than necessary; nearest_charm_column's
                # carry-forward already shows the last real snapshot for any
                # bars during a genuinely-quiet stretch, so skipping here loses
                # nothing.
                local_max = chain_scale(col["charm"])
                STATE["scale_max"] = max(STATE["scale_max"], local_max)
                col["scale_at_ingest"] = STATE["scale_max"]
                STATE["history"].append(col)
                ok, err = append_log(asset, col)
                if ok:
                    STATE["log_rows"] += 1
                    STATE["log_err"] = None
                else:
                    STATE["log_err"] = err
                net_charm = sum(col["charm"].values())
                export_status_snapshot(asset, col, net_charm)
    except Exception as e:
        with LOCK:
            if epoch == fetch_epoch:
                STATE["error_msg"] = str(e)
    finally:
        with LOCK:
            if epoch == fetch_epoch:
                STATE["fetching"] = False

def trigger_fetch():
    with LOCK:
        if STATE["fetching"]:
            return
        STATE["fetching"] = True
        STATE["fetch_started"] = time.time()
    threading.Thread(target=do_fetch, daemon=True).start()

# ── CURSES MAIN ───────────────────────────────────────────────────────────
def curses_main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(150)
    init_colors()
    init_heat_pairs()

    with LOCK:
        if HISTORICAL_MODE:
            load_historical(SYMBOL)
        else:
            load_into(SYMBOL, VIEW_DATE)   # resume today's log if present

    transient_msg, transient_msg_time = None, 0.0

    # [I] opens a one-line prompt (typed into interval_input_buf) instead of cycling
    # a fixed list — Enter commits (validated via validate_bar_spec), Esc cancels.
    # Same convention as gex.py's own [S] symbol prompt / entering_symbol.
    entering_interval = False
    interval_input_buf = ""

    # [T] opens the same kind of prompt for REFRESH_SEC (the fetch cadence). No-op
    # in HISTORICAL_MODE, where nothing is being fetched on a timer anyway.
    entering_refresh = False
    refresh_input_buf = ""

    # [S] opens a one-line prompt to type any symbol (not case-sensitive) — ETH/BTC
    # route to Deribit, anything else is tried as a CBOE-listed equity/ETF ticker.
    # Directly ported from gex.py's own [S]/entering_symbol/_switch_symbol pattern.
    entering_symbol = False
    symbol_input_buf = ""

    if not HISTORICAL_MODE:
        trigger_fetch()
        h, w = stdscr.getmaxyx()
        msg = f"Fetching {SYMBOL} chain…"
        safe_add(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg, cp(P_CYAN))
        stdscr.refresh()

    while True:
        key = stdscr.getch()

        if entering_interval:
            if key in (10, 13, curses.KEY_ENTER):
                typed = interval_input_buf.strip()
                entering_interval = False
                interval_input_buf = ""
                if typed:
                    spec = parse_bar_arg(typed)
                    ok, err = validate_bar_spec(SYMBOL, spec) if spec else (False, f"couldn't parse '{typed}' — try e.g. 5m, 30s, 500v")
                    if ok:
                        with LOCK:
                            STATE["bar"] = spec
                        refetch_bars_only(SYMBOL)
                    else:
                        transient_msg, transient_msg_time = err, time.time()
            elif key == 27:   # Esc cancels, leaves the current interval untouched
                entering_interval = False
                interval_input_buf = ""
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                interval_input_buf = interval_input_buf[:-1]
            elif key != -1 and 32 <= key < 127 and len(interval_input_buf) < 12:
                interval_input_buf += chr(key)
        elif entering_refresh:
            if key in (10, 13, curses.KEY_ENTER):
                typed = refresh_input_buf.strip()
                entering_refresh = False
                refresh_input_buf = ""
                if typed:
                    try:
                        new_sec = max(MIN_REFRESH_SEC, int(typed))
                        global REFRESH_SEC
                        REFRESH_SEC = new_sec
                        transient_msg = f"Refresh interval set to {REFRESH_SEC}s"
                    except ValueError:
                        transient_msg = f"couldn't parse '{typed}' — enter a whole number of seconds"
                    transient_msg_time = time.time()
            elif key == 27:   # Esc cancels, leaves the current interval untouched
                entering_refresh = False
                refresh_input_buf = ""
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                refresh_input_buf = refresh_input_buf[:-1]
            elif key != -1 and 48 <= key < 58 and len(refresh_input_buf) < 6:   # digits only
                refresh_input_buf += chr(key)
        elif entering_symbol:
            if key in (10, 13, curses.KEY_ENTER):
                typed = symbol_input_buf.strip().upper()
                entering_symbol = False
                symbol_input_buf = ""
                if typed:
                    with LOCK:
                        switch_symbol(typed)
                    if not HISTORICAL_MODE:
                        trigger_fetch()   # outside the lock — trigger_fetch acquires it itself
            elif key == 27:   # Esc cancels, leaves the current symbol untouched
                entering_symbol = False
                symbol_input_buf = ""
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                symbol_input_buf = symbol_input_buf[:-1]
            elif key != -1 and 32 <= key < 127 and len(symbol_input_buf) < 10:
                symbol_input_buf += chr(key)
        elif key in (ord('q'), ord('Q')):
            break
        elif key in (ord('r'), ord('R')):
            if HISTORICAL_MODE:
                with LOCK:
                    load_historical(SYMBOL)
            else:
                trigger_fetch()
        elif key in (ord('p'), ord('P')):
            try:
                path = take_screenshot(SYMBOL)
                transient_msg = f"Saved screenshots/{os.path.basename(path)}"
            except Exception as e:
                transient_msg = f"Screenshot failed: {e}"
            transient_msg_time = time.time()
        elif key in (ord('i'), ord('I')):
            entering_interval = True
            interval_input_buf = ""
        elif key in (ord('t'), ord('T')):
            if HISTORICAL_MODE:
                transient_msg = "history mode has no live fetch — nothing to time"
                transient_msg_time = time.time()
            else:
                entering_refresh = True
                refresh_input_buf = ""
        elif key in (ord('s'), ord('S')):
            entering_symbol = True
            symbol_input_buf = ""
        elif key == curses.KEY_LEFT:
            with LOCK:
                if STATE["live_follow"]:
                    STATE["view_end_idx"] = len(STATE["bars"])
                STATE["live_follow"] = False
                STATE["view_end_idx"] = max(1, STATE["view_end_idx"] - ARROW_STEP)
        elif key == curses.KEY_RIGHT:
            with LOCK:
                if not STATE["live_follow"]:
                    STATE["view_end_idx"] = min(len(STATE["bars"]), STATE["view_end_idx"] + ARROW_STEP)
                    if STATE["view_end_idx"] >= len(STATE["bars"]):
                        STATE["live_follow"] = True
        elif key == curses.KEY_PPAGE:
            with LOCK:
                if STATE["live_follow"]:
                    STATE["view_end_idx"] = len(STATE["bars"])
                STATE["live_follow"] = False
                STATE["view_end_idx"] = max(1, STATE["view_end_idx"] - PAGE_STEP)
        elif key == curses.KEY_NPAGE:
            with LOCK:
                if not STATE["live_follow"]:
                    STATE["view_end_idx"] = min(len(STATE["bars"]), STATE["view_end_idx"] + PAGE_STEP)
                    if STATE["view_end_idx"] >= len(STATE["bars"]):
                        STATE["live_follow"] = True
        elif key == curses.KEY_UP:
            _scroll_vert(1)
        elif key == curses.KEY_DOWN:
            _scroll_vert(-1)
        elif key in (ord('c'), ord('C')):
            # Re-center the price axis on whatever's currently in view, WITHOUT
            # touching the time axis — distinct from z/End/Esc below, which resets
            # both. Useful after scrolling away vertically while deliberately
            # staying panned back in time.
            with LOCK:
                STATE["vert_follow"] = True
        elif key in (curses.KEY_END, ord('z'), ord('Z'), 27):
            with LOCK:
                STATE["live_follow"] = True
                STATE["vert_follow"] = True

        if not HISTORICAL_MODE:
            with LOCK:
                now_fetching = STATE["fetching"]
                elapsed_a = time.time() - STATE["last_fetch"] if STATE["last_fetch"] else 999
                dur_a = STATE["fetch_dur"]
            lead = max(1.0, dur_a)
            if elapsed_a >= (REFRESH_SEC - lead) and not now_fetching and STATE["last_fetch"] > 0:
                trigger_fetch()
            elif STATE["last_fetch"] == 0 and not now_fetching:
                trigger_fetch()

        with LOCK:
            cur_symbol  = SYMBOL
            cur_history = list(STATE["history"])
            cur_bars    = list(STATE["bars"])
            cur_meta    = dict(STATE["meta"])
            cur_error   = STATE["error_msg"]
            now_fetching = STATE["fetching"]
            elapsed = time.time() - STATE["last_fetch"] if STATE["last_fetch"] else 999
            ui = {"live_follow": STATE["live_follow"], "view_end_idx": STATE["view_end_idx"],
                  "vert_follow": STATE["vert_follow"], "vert_center": STATE["vert_center"],
                  "log_rows": STATE["log_rows"], "log_err": STATE["log_err"], "bar": dict(STATE["bar"]),
                  "entering_interval": entering_interval, "interval_input_buf": interval_input_buf,
                  "entering_refresh": entering_refresh, "refresh_input_buf": refresh_input_buf,
                  "entering_symbol": entering_symbol, "symbol_input_buf": symbol_input_buf}

        if HISTORICAL_MODE:
            status = "history mode — no live fetch"
        elif now_fetching:
            status = "↻ fetching…"
        else:
            next_in = max(0, int(REFRESH_SEC - elapsed))
            status = f"↻ in {next_in}s"
        if transient_msg and (time.time() - transient_msg_time) < 8:
            status = f"{transient_msg}  |  {status}"
        if cur_error:
            status += f"  ⚠ {cur_error}"

        row_price = draw(stdscr, cur_symbol, cur_history, cur_bars, cur_meta, status, ui)
        if row_price:
            with LOCK:
                STATE["row_price"] = row_price
        if entering_interval or entering_refresh or entering_symbol:
            curses.curs_set(1)
            h, w = stdscr.getmaxyx()
            if entering_interval:
                cursor_x = min(w - 1, len(INTERVAL_PROMPT) + len(interval_input_buf))
            elif entering_refresh:
                cursor_x = min(w - 1, len(REFRESH_PROMPT) + len(refresh_input_buf))
            else:
                cursor_x = min(w - 1, len(SYMBOL_PROMPT) + len(symbol_input_buf))
            try:
                stdscr.move(h - 1, cursor_x)
            except curses.error:
                pass
        else:
            curses.curs_set(0)
        curses.doupdate()

# ── HEADLESS LOGGER ──────────────────────────────────────────────────────
def headless_main():
    print(f"charm.py headless logger — {SYMBOL}, every {REFRESH_SEC}s")
    print("Ctrl+C to stop. Run a separate instance per symbol (same convention as gex.py).")
    asset = SYMBOL
    while True:
        t0 = time.time()
        ts = datetime.now()
        try:
            d = fetch_snapshot(asset)
            bars = fetch_ohlc(asset, STATE["bar"], None)
            col = {"ts": ts, "spot": d["spot"], "charm": d["strikes"],
                   "oi_by_type": d.get("oi_by_type") or {},
                   "charm_by_type": d.get("charm_by_type") or {},
                   "atm_iv": d.get("atm_iv", 0.0), "T_years": d.get("T_years", 0.0),
                   "expiry_label": d.get("expiry_label"),
                   "is_0dte": d.get("is_0dte", True if is_crypto_symbol(asset) else False)}
            ok, err = append_log(asset, col)
            net_charm = sum(col["charm"].values())
            export_status_snapshot(asset, col, net_charm)
            tag = "OK" if ok else f"LOG ERROR: {err}"
            print(f"[{ts.strftime('%H:%M:%S')}] {asset} spot={col['spot']:.2f} "
                  f"strikes={len(col['charm'])} bars={len(bars)} net_charm(5m)={fdollars_compact(net_charm)} {tag}")
        except Exception as e:
            print(f"[{ts.strftime('%H:%M:%S')}] {asset} fetch error: {e}")
        time.sleep(max(1.0, REFRESH_SEC - (time.time() - t0)))

def main():
    if HEADLESS:
        try:
            headless_main()
        except KeyboardInterrupt:
            pass
        print("\nCharm headless logger — stopped.")
        return
    try:
        curses.wrapper(curses_main)
    except KeyboardInterrupt:
        pass
    print("Charm Map — exited.")

if __name__ == "__main__":
    main()
