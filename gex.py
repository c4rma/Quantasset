#!/usr/bin/env python3
"""
gex.py — Gamma Exposure (GEX) Interval Map
Curses terminal chart: Y axis = strike, X axis = time (one column per refresh
interval), each cell a dot whose size/color is the net dealer gamma exposure
at that strike at that moment. Price is overlaid as a bright yellow marker in
the gap between whichever two strikes it currently sits between (sized like
the surrounding dots so you can tell what exposure zone price is actually in),
landing on a strike's own row only when price exactly equals that strike.

  Green dot = positive gamma (call-dominated). Dealer hedging dampens moves —
              these strikes act as magnets / pin / support-resistance.
  Red dot   = negative gamma (put-dominated). Dealer hedging amplifies moves —
              price accelerates through these zones.
  Dot size  = magnitude of net GEX at that strike, scaled against the session max.

GEX(strike) = Σ [gamma × open_interest × contract_mult × spot² × 0.01], calls
positive, puts negative — the standard "dealer is long calls / short puts"
positioning assumption used by public GEX tools. Not observed truth.

Bottom of the chart also shows, for whichever column is currently in view:
  Max Pain           strike minimizing total option-holder payout at expiry
  Net GEX            Σ net GEX across the whole chain — green if net positive
                      (dealers net long gamma, dampening), red if net negative
                      (dealers net short gamma, amplifying)
  Zero Gamma/GEX Flip  spot level where total dealer gamma exposure would flip
                        sign — below it dealer hedging is destabilizing, above
                        it stabilizing. Computed properly via Black-Scholes:
                        every contract's gamma is re-priced at a sweep of
                        hypothetical spot levels (not just today's actual
                        spot) using its own strike/IV/time-to-expiry, and the
                        flip is where that re-priced total crosses zero —
                        the same approach professional GEX tools use, not a
                        simpler "sum today's already-priced gamma by strike"
                        proxy (which only describes today's gamma shape, not
                        where the market would actually flip as spot moves).
                        The two listed strikes it falls between are
                        highlighted cyan on the price axis, and a wide white
                        dashed line is drawn across the chart at that level
                        (in the gap between the two strikes, like the price
                        marker, unless the flip lands exactly on a strike).
Max Pain and the flip are averaged over the last --smooth-n raw fetches
(default 5) rather than shown instantaneously — a single stale/transitional OI
snapshot or a large order on a thin strike can swing the raw flip $50+ for one
refresh and then revert; smoothing absorbs that without hiding a real,
sustained level change. Net GEX is shown raw (unsmoothed) since it's a sum
across the whole chain, not a sensitive zero-crossing point.
A --diag-threshold-sized jump ($30 default) in the *raw* (unsmoothed) flip
between two consecutive fetches is logged to gex_diag_<SYMBOL>_MM_DD_YYYY.jsonl
(full before/after OI+gamma per strike) and flashed in the status bar, so you
can tell a data artifact from a real move.

Data:
  ETH/BTC — Deribit REST, real-time, gamma+OI read directly off each ticker.
  QQQ     — CBOE delayed-quotes feed (~15m delay), gamma+OI read directly off
            each contract (CBOE publishes greeks, no Black-Scholes needed). GEX
            itself is computed against CBOE's own (delayed) reference price,
            since that's what its gamma values are relative to — but the
            displayed/plotted spot comes from Yahoo's near-real-time quote
            instead (falls back to CBOE's price if that fetch fails), so the
            header and price marker don't read ~15m stale.

Every refresh is appended to a per-day log (gex_<SYMBOL>_MM_DD_YYYY.jsonl, next
to this script) so history survives restarts. Launching on a day that already
has a log resumes it; `--date` opens a past day for pure playback/browsing.

IMPORTANT: neither Deribit nor CBOE expose *historical* per-strike gamma/OI —
only a live snapshot. There is no API to backfill history out of thin air on a
cold start; the only real history is whatever this tool (or a --headless
instance of it) actually logged while running. The live view always shows raw,
uncompressed minute-to-minute columns (same resolution as scrolling back with
the arrow keys) — a screen's worth at a time, growing further back into
whatever's been logged as you pan; run --headless continuously (e.g. via Task
Scheduler) to keep more history on disk to pan back into.

Usage:
  python gex.py [SYMBOL] [--interval SEC] [--all-exp] [--date MM_DD_YYYY]
                 [--headless] [--smooth-n N] [--diag-threshold N]
    SYMBOL               ETH or BTC (Deribit), or any CBOE-listed equity/ETF
                          ticker, e.g. QQQ, SPY (default: ETH). Not case-sensitive.
                          Can also be changed in-app with [S].
    --interval SEC       refresh interval in seconds (default 60)
    --all-exp            sum GEX across ALL expiries instead of just the nearest
                          (nearest/0DTE-style expiry is the default)
    --date MM_DD_YYYY    browse a past day's logged map instead of going live
    --headless           no UI — just fetch+log on schedule, for keeping a
                          continuous history running in the background
    --smooth-n N         raw fetches averaged into the displayed Max Pain /
                          GEX Flip (default 5; 1 = show the raw instantaneous value)
    --diag-threshold N   dollar move in the raw flip between consecutive
                          fetches that triggers a diagnostic log entry (default
                          30; 0 disables)

In-app: ←/→ pan time by a few columns, PgUp/PgDn pan time by a screenful.
↑/↓ scroll the price axis by one strike, [/] scroll it by a page, {/} jump to
the highest/lowest strike in the grid. [Z] or [End] resets: jumps back to the
live edge (time axis) and re-centers on whatever strike is nearest the current
spot (price axis) — both views' auto-follow default, in one keystroke either
way, however far you've scrolled off in either direction.
[R] refreshes now (live mode) or reloads the log from disk (history mode, in
case another instance is still writing to it).

[G] toggles a second view: GEX BY STRIKE — a bar chart (strike on the X axis,
GEX $ on the Y axis) matching the Barchart-style "Gamma Exposure by Strike"
chart, with call gamma (blue) and put gamma (orange) as separate bars per
strike — no aggregate line. [N] toggles that view into NET mode: one bar per
strike (call+put combined) instead of two, green if positive / red if negative
(same convention as the interval map's dots). A yellow vertical line marks the
current spot (in the gap between strikes if it's not sitting exactly on one),
alongside the white dashed Zero Gamma/GEX Flip line. Always shows the latest
fetched column (this view has no time axis to pause on);
←/→/PgUp/PgDn/↑/↓/[/]/{/} all scroll through strikes here instead (reusing the
same "which strike is centered" state as the interval map's price-axis scroll
— panning in one view carries over to the other). Max Pain / Net GEX / Zero
Gamma read the same either way.

[S] opens a one-line prompt to type any symbol (not case-sensitive) — ETH/BTC
route to Deribit, anything else is tried as a CBOE-listed equity/ETF ticker
(QQQ, SPY, AAPL, ...). Enter confirms, Esc cancels. Switching resets
history/scale/scroll state and (re)loads whatever's already logged for the
new symbol today (or at --date), same as if you'd launched the app with that
symbol from the start. [G]/[N] (which view, and whether it's net) carry over
across a switch; everything else resets.

[P] saves a plain-text dump of exactly what's on screen right now to
screenshots/gex_<SYMBOL>_YYYYMMDD_HHMMSS.txt, next to this script — works the
same in every mode (interval map, GEX by strike, separate or net), since it
just captures whatever's currently drawn. Same convention chart.py's [P]shot
already uses elsewhere in this repo.
"""

import sys
import time
import threading
import os
import json
import bisect
import math

# Windows compatibility — install windows-curses if _curses is missing
try:
    import curses
except ModuleNotFoundError:
    try:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "windows-curses"], stdout=subprocess.DEVNULL)
        import curses
    except Exception as e:
        print(f"Could not load curses: {e}")
        print("Run:  pip install windows-curses")
        sys.exit(1)
try:
    import requests
except ModuleNotFoundError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"], stdout=subprocess.DEVNULL)
    import requests

from collections import deque
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# Force UTF-8 stdout — the default Windows console codepage (cp1252) can't encode
# this file's unicode glyphs (Σ, ×, ², etc.), which would otherwise crash --help.
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
REFRESH_SEC = 60
if "--interval" in args:
    i = args.index("--interval")
    try:
        REFRESH_SEC = max(5, int(args[i + 1]))
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
DIAG_THRESHOLD = 30.0
if "--diag-threshold" in args:
    i = args.index("--diag-threshold")
    try:
        DIAG_THRESHOLD = float(args[i + 1])
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
SYMBOL = args[0].upper() if args else "ETH"
if not SYMBOL:
    print("Symbol can't be empty — e.g. ETH, BTC, QQQ, or any CBOE-listed equity/ETF ticker")
    sys.exit(1)

# SYMBOL/IS_CRYPTO/MULT/BAND_PCT are reassigned at runtime by curses_main's
# _switch_symbol (the 's' key) — every function below reads them as globals at call
# time, so switching just these four is enough to retarget the whole fetch/log/display
# pipeline at a different symbol without restarting the process.
IS_CRYPTO = SYMBOL in ("ETH", "BTC")
MULT      = 1 if IS_CRYPTO else 100     # Deribit = 1 coin/contract, equities = 100 shares/contract
BAND_PCT  = 0.20 if IS_CRYPTO else 0.12 # strike-grid band around spot, as a fraction of spot

# Black-Scholes hypothetical-spot sweep for the GEX Flip (see bs_gamma/build_bs_gex_curve):
# how far above/below spot to re-price gamma, and how many points across that span.
BS_SWEEP_PCT    = 0.20
BS_SWEEP_POINTS = 61

BASE_URL = "https://www.deribit.com/api/v2"
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"

COL_W         = 2       # terminal columns per time interval
HISTORY_MAXLEN = 1500   # ~25h of 1-min columns

TODAY_STR       = datetime.now().strftime("%m_%d_%Y")
VIEW_DATE       = LOAD_DATE or TODAY_STR
HISTORICAL_MODE = LOAD_DATE is not None and LOAD_DATE != TODAY_STR   # pure playback, no live fetch

LOG_DIR = os.path.dirname(os.path.abspath(__file__))
ARROW_STEP = 5    # columns per Left/Right press (time axis)
PAGE_STEP  = 30   # columns per PgUp/PgDn press (time axis)
VERT_STEP      = 1    # strikes per Up/Down press (price axis)
VERT_PAGE_STEP = 8    # strikes per [/] press (price axis)
SYMBOL_PROMPT  = " Enter symbol (Enter=confirm, Esc=cancel): "

# ── DERIBIT HELPERS (crypto) ─────────────────────────────────────────────────
def api(path, **params):
    r = requests.get(BASE_URL + path, params=params, timeout=12)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"]["message"])
    return j["result"]

def fetch_ticker(name):
    return name, api("/public/ticker", instrument_name=name)

def countdown(ts_ms):
    ms = ts_ms - int(time.time() * 1000)
    if ms <= 0:
        return "EXPIRED"
    h, rem = divmod(ms // 1000, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def fetch_eth(currency, all_exp):
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

    with ThreadPoolExecutor(max_workers=40) as ex:
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

    strikes     = {}
    oi_by_type  = {}   # strike -> {"call": raw_oi, "put": raw_oi}, unweighted — for Max Pain
    gex_by_type = {}   # strike -> {"call": call_gex, "put": put_gex} — for the by-strike bar chart
    contracts   = []   # (strike, otype, oi, iv_decimal, T_years) — for the BS GEX-flip curve
    for ins in chain_ins:
        t = tickers.get(ins["instrument_name"])
        if not t:
            continue
        greeks = t.get("greeks") or {}
        gamma  = greeks.get("gamma") or 0.0
        oi     = t.get("open_interest") or 0.0
        gex    = gamma * oi * MULT * spot * spot * 0.01
        otype  = ins["option_type"]
        if otype == "put":
            gex = -gex
        strikes[ins["strike"]] = strikes.get(ins["strike"], 0.0) + gex
        oi_by_type.setdefault(ins["strike"], {"call": 0.0, "put": 0.0})[otype] += oi
        gex_by_type.setdefault(ins["strike"], {"call": 0.0, "put": 0.0})[otype] += gex

        iv_pct = t.get("mark_iv") or 0.0   # Deribit reports this in percentage points (e.g. 65.3)
        exp_ms = ins["expiration_timestamp"]
        T = max(exp_ms - now_ms, 60_000) / 1000.0 / 86400.0 / 365.0   # floor 60s to avoid T~0
        if oi > 0 and iv_pct > 0:
            contracts.append((ins["strike"], otype, oi, iv_pct / 100.0, T))

    bs_gex_curve = build_bs_gex_curve(contracts, spot, MULT)

    if target_exp:
        exp_label = datetime.fromtimestamp(target_exp / 1000, tz=timezone.utc).strftime("%d%b%y").upper()
        ttl       = countdown(target_exp)
    else:
        exp_label = f"ALL({len(by_exp)})"
        ttl       = None

    return {"spot": spot, "strikes": strikes, "oi_by_type": oi_by_type, "gex_by_type": gex_by_type,
            "bs_gex_curve": bs_gex_curve,
            "expiry_label": exp_label, "ttl": ttl, "fetched_at": datetime.now().strftime("%H:%M:%S")}

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
_YAHOO_HEADERS  = {"User-Agent": "Mozilla/5.0"}

def fetch_live_price(symbol):
    """Best-effort near-real-time quote via Yahoo's v8 chart 'meta' block — NOT the
    OHLC bars, which Yahoo delays ~15m for equities same as CBOE, but meta's own
    regularMarketPrice/regularMarketTime tick live (confirmed: its timestamp tracks
    within seconds of wall-clock). Used only to display/position price for QQQ, whose
    options feed (CBOE) is itself ~15m delayed. Returns None on any failure — caller
    falls back to the options feed's own reference price rather than blocking on this."""
    try:
        r = requests.get(YAHOO_CHART_URL.format(symbol), headers=_YAHOO_HEADERS,
                          params={"interval": "1m", "range": "1d"}, timeout=6)
        r.raise_for_status()
        meta  = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        return float(price) if price else None
    except Exception:
        return None

def _cboe_time_to_expiry_years(exp_str):
    """exp_str: 'YYMMDD'. Time to expiry in years, treating expiry as market close
    (15:00 local — matches this repo's other CT-based tools) on that date. Crude for
    non-0DTE expiries (ignores intraday time-of-day drift beyond "today"), but BS gamma
    cares far more about T being near-zero than about being off by an hour — the floor
    below is what actually matters for 0DTE numerical stability."""
    exp_date = datetime.strptime(exp_str, "%y%m%d").date()
    close_dt = datetime(exp_date.year, exp_date.month, exp_date.day, 15, 0, 0)
    seconds  = (close_dt - datetime.now()).total_seconds()
    return max(seconds, 60.0) / 86400.0 / 365.0   # floor 60s to avoid T~0 blowups

# ── CBOE HELPERS (equities/ETFs) ────────────────────────────────────────────
def fetch_equity(symbol, all_exp):
    """Any CBOE-listed, optionable equity/ETF ticker — QQQ was the first one wired up,
    but the OCC option-symbol parsing below is offset-from-the-end (date+type+strike are
    always the trailing 15 chars regardless of ticker length), so this generalizes to
    any ticker CBOE's delayed-quotes endpoint recognizes without further changes."""
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

    strikes     = {}
    oi_by_type  = {}   # strike -> {"call": raw_oi, "put": raw_oi}, unweighted — for Max Pain
    gex_by_type = {}   # strike -> {"call": call_gex, "put": put_gex} — for the by-strike bar chart
    contracts   = []   # (strike, otype, oi, iv_decimal, T_years) — for the BS GEX-flip curve
    for exp in target_exps:
        T_exp = _cboe_time_to_expiry_years(exp)
        for cp_flag, strike, o in by_exp[exp]:
            gamma = float(o.get("gamma") or 0)
            oi    = float(o.get("open_interest") or 0)
            gex   = gamma * oi * MULT * price * price * 0.01
            if cp_flag == "P":
                gex = -gex
            strikes[strike] = strikes.get(strike, 0.0) + gex
            otype = "call" if cp_flag == "C" else "put"
            oi_by_type.setdefault(strike, {"call": 0.0, "put": 0.0})[otype] += oi
            gex_by_type.setdefault(strike, {"call": 0.0, "put": 0.0})[otype] += gex

            iv = float(o.get("iv") or 0)   # CBOE reports this as a decimal already (e.g. 0.33 = 33%)
            if oi > 0 and iv > 0:
                contracts.append((strike, otype, oi, iv, T_exp))

    bs_gex_curve = build_bs_gex_curve(contracts, price, MULT)

    is_0dte   = (not all_exp) and target_exps[0] == today
    exp_label = target_exps[0] if len(target_exps) == 1 else f"ALL({len(target_exps)})"

    # GEX itself is computed above against CBOE's own (delayed) reference price, since
    # that's the price its gamma values are relative to. Display/positioning uses a
    # near-real-time quote instead so the header and price marker aren't ~15m stale —
    # falls back to CBOE's price if the live quote fetch fails.
    live_price = fetch_live_price(symbol)
    spot = live_price if live_price else price

    return {"spot": spot, "spot_is_live": live_price is not None, "cboe_ref_price": price,
            "strikes": strikes, "oi_by_type": oi_by_type, "gex_by_type": gex_by_type,
            "bs_gex_curve": bs_gex_curve,
            "expiry_label": exp_label,
            "ttl": None, "is_0dte": is_0dte, "fetched_at": datetime.now().strftime("%H:%M:%S")}

def fetch_snapshot():
    if IS_CRYPTO:
        return fetch_eth(SYMBOL, ALL_EXP)
    return fetch_equity(SYMBOL, ALL_EXP)

# ── PERSISTENCE — one JSON-lines file per symbol/day, next to this script ────
# JSONL (not wide CSV) because each column is a variable-size {strike: gex}
# dict — this maps directly onto the in-memory column format with no reshaping.
def log_path(date_str):
    suffix = "_allexp" if ALL_EXP else ""
    return os.path.join(LOG_DIR, f"gex_{SYMBOL}{suffix}_{date_str}.jsonl")

def append_log(col):
    """Append one column to today's log. Returns (ok, err_str_or_None)."""
    try:
        with open(log_path(datetime.now().strftime("%m_%d_%Y")), "a", encoding="utf-8") as f:
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

def diag_log_path(date_str):
    suffix = "_allexp" if ALL_EXP else ""
    return os.path.join(LOG_DIR, f"gex_diag_{SYMBOL}{suffix}_{date_str}.jsonl")

def append_diag(prev_col, prev_level, new_col, new_level):
    """Log a raw-flip jump exceeding DIAG_THRESHOLD: full before/after OI+gamma per
    strike, so a stale-OI-snapshot artifact can be told apart from a real move."""
    try:
        with open(diag_log_path(datetime.now().strftime("%m_%d_%Y")), "a", encoding="utf-8") as f:
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

def check_flip_jump(prev_col, new_col):
    """Compare raw (unsmoothed) flip levels between two consecutive columns; if the
    jump exceeds DIAG_THRESHOLD, log it and return a short status message, else None."""
    if DIAG_THRESHOLD <= 0:
        return None
    prev_flip = _find_nearest_zero_crossing(prev_col.get("bs_gex_curve") or [], prev_col["spot"])
    new_flip  = _find_nearest_zero_crossing(new_col.get("bs_gex_curve") or [], new_col["spot"])
    if not prev_flip or not new_flip:
        return None
    delta = new_flip[2] - prev_flip[2]
    if abs(delta) < DIAG_THRESHOLD:
        return None
    append_diag(prev_col, prev_flip[2], new_col, new_flip[2])
    sign = "+" if delta > 0 else "-"
    return f"⚠ flip jumped {sign}{fstrike(abs(delta))} @ {new_col['ts'].strftime('%H:%M:%S')} (logged)"

def smoothed_max_pain_and_flip(history, end_idx, n):
    """Average Max Pain and the GEX-flip level over the last n raw columns ending at
    end_idx, to absorb single-snapshot OI/IV artifacts (see check_flip_jump/module
    docstring). Returns (smoothed_max_pain_or_None, smoothed_flip_level_or_None)."""
    window = history[max(0, end_idx - n):end_idx]
    if not window:
        return None, None
    mps, levels = [], []
    for c in window:
        mp = compute_max_pain(c.get("oi_by_type"))
        if mp is not None:
            mps.append(mp)
        fl = _find_nearest_zero_crossing(c.get("bs_gex_curve") or [], c["spot"])
        if fl:
            levels.append(fl[2])
    smoothed_mp    = sum(mps) / len(mps) if mps else None
    smoothed_level = sum(levels) / len(levels) if levels else None
    return smoothed_mp, smoothed_level

def bounding_strikes(sorted_strikes, level):
    """The two adjacent grid strikes that bracket `level` (equal if level lands exactly
    on one, or is beyond either end of the grid)."""
    i = bisect.bisect_left(sorted_strikes, level)
    if i <= 0:
        return sorted_strikes[0], sorted_strikes[0]
    if i >= len(sorted_strikes):
        return sorted_strikes[-1], sorted_strikes[-1]
    if sorted_strikes[i] == level:
        return sorted_strikes[i], sorted_strikes[i]
    return sorted_strikes[i - 1], sorted_strikes[i]

def resolve_marker_row(lo_s, hi_s, row_of):
    """Row for a level given its two bounding strikes (from bounding_strikes): that
    strike's own row if lo==hi (exact hit, or clamped to a grid edge), else the spacer
    row between them if both are currently rendered, else None (off-screen). Shared by
    the price marker and the GEX-flip reference line so both use the same placement."""
    if lo_s == hi_s:
        return row_of.get(lo_s)
    if hi_s in row_of and lo_s in row_of:
        return row_of[hi_s] + 1   # hi_s renders above lo_s (descending strike list)
    return None

def resolve_marker_col(lo_s, hi_s, col_of):
    """Column for a level given its two bounding strikes, for the GEX-by-strike bar
    chart (ascending left-to-right strike layout — the mirror image of
    resolve_marker_row's descending row layout): that strike's own column if lo==hi,
    else the midpoint between the two strikes' columns if both are rendered, else None."""
    if lo_s == hi_s:
        return col_of.get(lo_s)
    if lo_s in col_of and hi_s in col_of:
        return (col_of[lo_s] + col_of[hi_s]) // 2
    return None

def load_log(date_str):
    """Read a day's log back into column dicts (no 'nearest' yet — ingest_column adds it)."""
    path = log_path(date_str)
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
                    # older logs predate oi_by_type/gex_by_type/bs_gex_curve — .get(...) so
                    # Max Pain / the by-strike bar chart / GEX Flip just show N/A or empty
                    # for columns logged before each of these was added
                    "oi_by_type": {float(k): v for k, v in (d.get("oi_by_type") or {}).items()},
                    "gex_by_type": {float(k): v for k, v in (d.get("gex_by_type") or {}).items()},
                    "bs_gex_curve": d.get("bs_gex_curve") or [],
                    "expiry_label": d.get("expiry_label"), "is_0dte": d.get("is_0dte"),
                })
            except Exception:
                continue
    return cols

def ingest_column(col, grid):
    """Grows `grid` (the strike universe) in place and tags col with its nearest-to-spot
    strike. Returns (col, local_max_abs_gex) — caller folds local_max into scale_max."""
    spot = col["spot"]
    band = spot * BAND_PCT
    grid.update(s for s in col["gex"] if abs(s - spot) <= band)
    col["nearest"] = min(grid, key=lambda s: abs(s - spot)) if grid else None
    local_max = max((abs(v) for v in col["gex"].values()), default=0.0)
    return col, local_max

def compute_max_pain(oi_by_type):
    """Strike where total intrinsic payout to option holders is minimized at expiry —
    Σ max(K-strike,0)*call_oi + Σ max(strike-K,0)*put_oi, minimized over candidate K."""
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

def _find_nearest_zero_crossing(points, ref):
    """points: [(x, y), ...] sorted ascending by x. Returns (x_lo, x_hi, interpolated_x)
    for the y-crossing nearest `ref`, or None if y never crosses zero (x_lo==x_hi if the
    crossing lands exactly on a point). Shared by build_bs_gex_curve's hypothetical-spot
    GEX-flip curve — the one and only crossing-finder in this file now that the flip is
    computed via Black-Scholes re-pricing rather than a raw per-strike cumulative sum."""
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

def bs_gamma(S, K, T, sigma):
    """Black-Scholes gamma (identical formula for calls and puts) at spot S, strike K,
    time-to-expiry T (years), annualized vol sigma (decimal, e.g. 0.30 = 30%). r=0 —
    a standard simplification for short-dated options where the rate barely moves d1."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * S * sigma * math.sqrt(T))

def build_bs_gex_curve(contracts, spot, mult, n_points=BS_SWEEP_POINTS, sweep_pct=BS_SWEEP_PCT):
    """The real Zero Gamma / GEX Flip calculation: re-prices every contract's gamma at a
    sweep of hypothetical spot levels (not just today's actual spot) via Black-Scholes,
    and returns the resulting [(hyp_spot, total_net_gex), ...] curve — this is what
    professional GEX tools (SpotGamma, Barchart) compute, as opposed to a simpler "sum
    each contract's already-priced gamma by strike" proxy, which only tells you today's
    gamma distribution, not where the *market* would flip sign as spot actually moves.

    contracts: [(strike, "call"|"put", oi, iv_decimal, T_years), ...]. IV/OI/T are
    floored to strictly positive by the caller — a contract with no priced IV or OI
    contributes nothing (correctly: BS gamma is undefined/meaningless there anyway).
    The returned curve is what gets persisted per column (compact: n_points entries,
    not one per contract) and is what _find_nearest_zero_crossing scans for the flip."""
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

# ── COLOUR PAIRS ───────────────────────────────────────────────────────────
P_DEFAULT, P_DIM, P_CYAN, P_YELLOW, P_GREEN, P_RED, P_STATUS, P_ATM, P_BLUE, P_ORANGE = range(1, 11)

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
    curses.init_pair(P_BLUE,    curses.COLOR_BLUE,   BG)    # call gamma (GEX-by-strike mode)
    curses.init_pair(P_ORANGE,  curses.COLOR_YELLOW, BG)    # put gamma — base 8-color curses has
                                                             # no true orange; yellow is the closest

def cp(pair, bold=False, dim=False):
    a = curses.color_pair(pair)
    if bold: a |= curses.A_BOLD
    if dim:  a |= curses.A_DIM
    return a

_screen_mirror = []   # plain-text mirror of the current frame, for take_screenshot() —
                      # see its docstring for why this is kept instead of reading curses
                      # back via win.instr()/win.inch()

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

def take_screenshot():
    """Dump the current frame to screenshots/gex_<SYMBOL>_YYYYMMDD_HHMMSS.txt — same
    plain-text convention chart.py's [P]shot already uses in this repo. Reads back
    _screen_mirror (a plain-Python copy of every string safe_add has written this frame)
    rather than curses' own buffer (win.instr()/win.inch()): this file's dot/line
    characters (●, ─, │, etc.) are multi-byte, and whether curses' internal storage
    round-trips them losslessly depends on the underlying build — chart.py sidesteps the
    same uncertainty the same way, with its own DoubleBuffer. Works identically for every
    chart mode, since it just dumps whatever's already been drawn — no per-mode logic."""
    folder = os.path.join(LOG_DIR, "screenshots")
    os.makedirs(folder, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(folder, f"gex_{SYMBOL}_{ts}.txt")
    lines = ["".join(row).rstrip() for row in _screen_mirror]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path

def fstrike(strike):
    if IS_CRYPTO:
        return f"${strike:,.0f}"
    return f"${strike:,.2f}"

def fdollars_compact(v):
    """Compact signed dollar format for totals that can run into the billions
    (e.g. Net GEX summed across a whole chain) — $1.23B / $45.6M / $789.1K / $12.34."""
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
    """Size-tier character for |net| as a fraction of scale_max — shared by dot_repr
    (green/red) and price_marker_repr (forced yellow) so both use the same scale."""
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
        return "●", frac  # ●

def dot_repr(net, scale_max):
    """Character + colour attr for one GEX cell, scaled against the session max."""
    ch, frac = magnitude_char(net, scale_max)
    if ch == " ":
        return " ", 0
    pair = P_GREEN if net > 0 else P_RED
    return ch, cp(pair, bold=(frac >= 0.35))

def price_marker_repr(net, scale_max):
    """Price-row glyph: same size tiers as dot_repr (so the gamma magnitude under the
    current price stays visible — large/med/small/tiny) but forced yellow, and floored
    to a small dot rather than blank so the price track never vanishes in dead zones."""
    ch, frac = magnitude_char(net, scale_max, floor_char="·")
    return ch, cp(P_YELLOW, bold=(frac >= 0.35))

def build_header(history, meta, ui, live_follow, end_idx, title="GEX MAP"):
    """(spot, pieces) for header row 0 — shared by both chart modes (interval map and
    GEX-by-strike). `live_follow`/`end_idx` mean "is the latest column being shown" /
    "index of whichever column IS being shown"; the by-strike mode always passes
    live_follow=True, end_idx=len(history) since it has no time axis to pause on."""
    label   = meta.get("expiry_label", "—")
    ttl     = meta.get("ttl")
    is_0dte = meta.get("is_0dte", True if IS_CRYPTO else False)
    fetched = meta.get("fetched_at", "—")

    if live_follow:
        mode_piece = ("● LIVE", cp(P_GREEN, bold=True))
        spot = history[-1]["spot"] if history else meta.get("spot", 0.0)
    else:
        viewed_ts = history[end_idx - 1]["ts"].strftime("%H:%M:%S") if history else "—"
        mode_piece = (f"⏸ HISTORY @{viewed_ts}", cp(P_YELLOW, bold=True))
        spot = history[end_idx - 1]["spot"] if history else meta.get("spot", 0.0)

    pieces = [
        (title, cp(P_CYAN, bold=True)),
        ("  │  ", cp(P_DIM, dim=True)),
        (f"{SYMBOL}", cp(P_DEFAULT, bold=True)),
        ("  ", cp(P_DIM, dim=True)),
        mode_piece,
        ("  Spot ", cp(P_DIM, dim=True)),
        (f"${spot:,.2f}" if spot else "—", cp(P_YELLOW, bold=True)),
        ("  Expiry ", cp(P_DIM, dim=True)),
        (f"{label}{'*' if is_0dte else ''}", cp(P_DEFAULT)),
    ]
    if ttl and live_follow:
        pieces += [("  TTL ", cp(P_DIM, dim=True)), (ttl, cp(P_YELLOW, bold=True))]
    if HISTORICAL_MODE:
        src_tag = f"  (historical playback — {VIEW_DATE})"
    elif IS_CRYPTO:
        src_tag = "  (live)"
    elif meta.get("spot_is_live"):
        src_tag = "  (spot live via Yahoo, GEX ~15m delay via CBOE)"
    else:
        src_tag = "  (spot+GEX ~15m delay — live quote fetch failed)"
    pieces += [("  Updated ", cp(P_DIM, dim=True)), (fetched, cp(P_DEFAULT)),
               (src_tag, cp(P_DIM, dim=True))]
    log_rows, log_err = ui.get("log_rows", 0), ui.get("log_err")
    if log_err:
        pieces += [(f"  Log ⚠{log_err[:20]}", cp(P_RED, bold=True))]
    else:
        pieces += [(f"  Log {log_rows}", cp(P_DIM, dim=True))]
    diag_count = ui.get("diag_count", 0)
    if diag_count:
        pieces += [(f"  Jumps {diag_count}", cp(P_RED, bold=True))]
    return spot, pieces

# ── DRAW: TIME-INTERVAL MAP ─────────────────────────────────────────────────
def draw(win, history, grid, scale_max, meta, status, ui):
    h, w = win.getmaxyx()
    win.erase()
    _mirror_reset(h, w)

    live_follow = ui["live_follow"]
    n_hist      = len(history)
    end_idx     = n_hist if live_follow else max(1, min(ui["view_end_idx"], n_hist))

    # ── Row 0: header ────────────────────────────────────────────────────
    spot, pieces = build_header(history, meta, ui, live_follow, end_idx)
    x = 0
    for text, attr in pieces:
        safe_add(win, 0, x, text, attr)
        x += len(text)

    # ── Row 1: legend ────────────────────────────────────────────────────
    legend = [
        ("● ", cp(P_GREEN, bold=True)), ("large  ", cp(P_DIM, dim=True)),
        ("O ", cp(P_GREEN)), ("med  ", cp(P_DIM, dim=True)),
        ("o ", cp(P_GREEN)), ("small  ", cp(P_DIM, dim=True)),
        (". ", cp(P_GREEN)), ("tiny   ", cp(P_DIM, dim=True)),
        ("green", cp(P_GREEN, bold=True)), ("=+gamma(pin/support)  ", cp(P_DIM, dim=True)),
        ("red", cp(P_RED, bold=True)), ("=-gamma(accelerant)  ", cp(P_DIM, dim=True)),
        ("●", cp(P_YELLOW, bold=True)), ("=price(sized)  ", cp(P_DIM, dim=True)),
        ("cyan", cp(P_CYAN, bold=True)), ("=GEX flip strikes  ", cp(P_DIM, dim=True)),
        ("----", cp(P_DEFAULT, bold=True)), ("=GEX flip level", cp(P_DIM, dim=True)),
    ]
    x = 0
    for text, attr in legend:
        safe_add(win, 1, x, text, attr)
        x += len(text)

    if not history or not grid:
        msg = (f"No log found for {VIEW_DATE} — nothing to show." if HISTORICAL_MODE
               else "Waiting for first snapshot…")
        safe_add(win, h // 2, 2, msg, cp(P_CYAN))
        bot = h - 1
        hint = f" q=quit  r=refresh  [{SYMBOL}]  {status}"
        safe_add(win, bot, 0, hint.ljust(w - 1)[:w - 1], cp(P_STATUS))
        win.noutrefresh()
        return

    # ── Grid geometry ────────────────────────────────────────────────────
    axis_w = max(len(fstrike(s)) for s in grid) + 2
    top    = 3
    bottom_reserved = 3   # time-axis row + Max Pain/GEX Flip row + status bar
    row_h  = 2            # 1 content row + 1 blank spacer row, for readability between strikes
    avail_rows = max(1, (h - top - bottom_reserved) // row_h)

    grid_sorted = sorted(grid)
    if ui["vert_follow"]:
        # Default: auto-center on whatever strike is currently nearest spot.
        center_idx = min(range(len(grid_sorted)), key=lambda i: abs(grid_sorted[i] - spot))
    else:
        # Manually scrolled (↑/↓, [/], {/}) — frozen index, doesn't drift as spot moves.
        center_idx = max(0, min(len(grid_sorted) - 1, ui["vert_center_idx"]))
    half = avail_rows // 2
    lo = max(0, center_idx - half)
    hi = min(len(grid_sorted), lo + avail_rows)
    lo = max(0, hi - avail_rows)
    visible = list(reversed(grid_sorted[lo:hi]))   # highest strike at top

    usable_w = max(0, w - axis_w - 1)
    n_cols   = max(1, usable_w // COL_W)

    # Always raw, uncompressed minute-to-minute columns — live mode just keeps end_idx
    # pinned to len(history) so it tracks the latest column; panning freezes end_idx.
    # Older history is reached the same way in both cases: scroll back with the arrows.
    start_idx = max(0, end_idx - n_cols)
    cols = history[start_idx:end_idx]

    # Max Pain + Zero Gamma/GEX Flip: averaged over the last SMOOTH_N raw fetches ending
    # at whatever column is currently in view (live edge, or the paused/scrolled-to one —
    # same one driving "spot" above) to absorb single-snapshot OI/IV artifacts. See
    # module docstring / check_flip_jump for why raw values jump around.
    max_pain, flip_level = smoothed_max_pain_and_flip(history, end_idx, SMOOTH_N)
    flip = None
    if flip_level is not None:
        flip = bounding_strikes(grid_sorted, flip_level) + (flip_level,)
    flip_strikes = (flip[0], flip[1]) if flip else ()

    # ── Rows ─────────────────────────────────────────────────────────────
    row_of = {}   # strike -> rendered row, for the price-marker pass below
    for ri, strike in enumerate(visible):
        row = top + ri * row_h
        if row >= h - bottom_reserved:
            break
        row_of[strike] = row
        is_current = cols and cols[-1].get("nearest") == strike
        if is_current:
            lbl_attr = cp(P_ATM, bold=True)
        elif strike in flip_strikes:
            lbl_attr = cp(P_CYAN, bold=True)
        else:
            lbl_attr = cp(P_DIM, dim=True)
        safe_add(win, row, 0, fstrike(strike).rjust(axis_w - 1), lbl_attr)

        cx = axis_w
        for col in cols:
            if cx >= w - 1:
                break
            net = col["gex"].get(strike, 0.0)
            ch, attr = dot_repr(net, scale_max)
            safe_add(win, row, cx, ch, attr)
            cx += COL_W

    # ── GEX-flip reference line — a wide dashed white line at the (possibly-between-
    # strikes) row for the smoothed flip level, spanning every rendered time column.
    # Drawn before the price markers so a price dot on the same row still shows through.
    if flip:
        flip_row = resolve_marker_row(flip[0], flip[1], row_of)
        if flip_row is not None and top <= flip_row < h - bottom_reserved:
            cx = axis_w
            for _ in cols:
                if cx >= w - 1:
                    break
                safe_add(win, flip_row, cx, "-", cp(P_DEFAULT, bold=True))
                cx += COL_W

    # ── Price markers — plotted in the spacer row between the two strikes spot
    # actually sits between (only landing on a strike's own row if spot exactly
    # equals it), so the dot no longer implies price is "at" whichever strike
    # happened to be nearest. Drawn after the grid so it isn't overwritten.
    for ci, col in enumerate(cols):
        cx = axis_w + ci * COL_W
        if cx >= w - 1:
            break
        spot_c = col["spot"]
        lo_s, hi_s = bounding_strikes(grid_sorted, spot_c)
        target_row = resolve_marker_row(lo_s, hi_s, row_of)
        net_src = lo_s if lo_s == hi_s or abs(spot_c - lo_s) <= abs(spot_c - hi_s) else hi_s
        if target_row is not None and top <= target_row < h - bottom_reserved:
            ch, attr = price_marker_repr(col["gex"].get(net_src, 0.0), scale_max)
            safe_add(win, target_row, cx, ch, attr)

    # ── Time axis ────────────────────────────────────────────────────────
    axis_row = h - bottom_reserved
    label_every_cols = max(1, 8 // COL_W)   # ~8-char spacing so "HH:MM" labels don't collide
    cx = axis_w
    for ci, col in enumerate(cols):
        if cx >= w - 6:
            break
        if ci % label_every_cols == 0 or ci == len(cols) - 1:
            ts = col["ts"].strftime("%H:%M")
            safe_add(win, axis_row, cx, ts, cp(P_DIM, dim=True))
        cx += COL_W

    # ── Max Pain / Net GEX / Zero Gamma (GEX Flip) ──────────────────────
    mp_str = fstrike(max_pain) if max_pain is not None else "N/A"
    net_gex = sum(cols[-1]["gex"].values()) if cols else None
    if net_gex is None:
        net_gex_str, net_gex_attr = "N/A", cp(P_DIM, dim=True)
    else:
        net_gex_str = fdollars_compact(net_gex)
        net_gex_attr = cp(P_GREEN if net_gex >= 0 else P_RED, bold=True)
    if flip:
        lo, hi, level = flip
        flip_str = (fstrike(level) if lo == hi else
                    f"{fstrike(level)}  (between {fstrike(lo)} & {fstrike(hi)})")
    else:
        flip_str = "N/A"
    info_pieces = [
        (" Max Pain ", cp(P_DIM, dim=True)), (mp_str, cp(P_YELLOW, bold=True)),
        ("    Net GEX ", cp(P_DIM, dim=True)), (net_gex_str, net_gex_attr),
        ("    Zero Gamma/GEX Flip ", cp(P_DIM, dim=True)), (flip_str, cp(P_CYAN, bold=True)),
        (f"    (Max Pain/Flip smoothed over last {SMOOTH_N})", cp(P_DIM, dim=True)),
    ]
    info_row = h - bottom_reserved + 1
    ix = 0
    for text, attr in info_pieces:
        safe_add(win, info_row, ix, text, attr)
        ix += len(text)

    # ── Status bar ───────────────────────────────────────────────────────
    bot = h - 1
    if ui.get("entering_symbol"):
        prompt = SYMBOL_PROMPT + ui.get("symbol_input_buf", "")
        safe_add(win, bot, 0, prompt.ljust(w - 1)[:w - 1], cp(P_STATUS))
    else:
        exp_tag = "ALL-EXP" if ALL_EXP else "0DTE"
        refresh_hint = "r=reload" if HISTORICAL_MODE else "r=refresh"
        vert_tag = "" if ui["vert_follow"] else "[↕scrolled]"
        hint = (f" q=quit  {refresh_hint}  time:←/→/PgUp/PgDn  strikes:↑/↓/[/]/{{/}}  z/End=reset  "
                f"g=by-strike  s=symbol  p=screenshot  [{SYMBOL}] [{exp_tag}] {vert_tag} {status}")
        safe_add(win, bot, 0, hint.ljust(w - 1)[:w - 1], cp(P_STATUS))

    win.noutrefresh()

# ── DRAW: GEX BY STRIKE (bar chart) ─────────────────────────────────────────
def draw_by_strike(win, history, grid, meta, status, ui):
    """Strike on the X axis, GEX $ on the Y axis, matching the Barchart-style 'Gamma
    Exposure by Strike' chart — no aggregate line. Two sub-modes, toggled with 'n':
      separate (default) — call gamma (blue) and put gamma (orange) as two bars per strike
      net ('n' toggles)  — one bar per strike (call+put combined), green if positive
                           (call-dominated) / red if negative (put-dominated), matching
                           the interval map's dot-color convention
    Always shows the latest fetched column (this mode has no time axis to pause on);
    horizontal strike scrolling reuses the same vert_follow/vert_center_idx state the
    interval map uses for its vertical strike scroll — same underlying idea ("which
    strike is centered"), just applied to columns here instead of rows."""
    h, w = win.getmaxyx()
    win.erase()
    _mirror_reset(h, w)

    net_mode = ui.get("by_strike_net", False)
    title = "GEX BY STRIKE (NET)" if net_mode else "GEX BY STRIKE"
    spot, pieces = build_header(history, meta, ui, True, len(history), title=title)
    x = 0
    for text, attr in pieces:
        safe_add(win, 0, x, text, attr)
        x += len(text)

    if net_mode:
        legend = [
            ("█ ", cp(P_GREEN, bold=True)), ("net +gamma (call-dominated)  ", cp(P_DIM, dim=True)),
            ("█ ", cp(P_RED, bold=True)), ("net -gamma (put-dominated)  ", cp(P_DIM, dim=True)),
            ("yellow", cp(P_ATM, bold=True)), ("=current price (strike label + line)  ", cp(P_DIM, dim=True)),
            ("cyan", cp(P_CYAN, bold=True)), ("=GEX flip strikes  ", cp(P_DIM, dim=True)),
            ("|", cp(P_DEFAULT, bold=True)), ("=GEX flip level", cp(P_DIM, dim=True)),
        ]
    else:
        legend = [
            ("█ ", cp(P_BLUE, bold=True)), ("call gamma  ", cp(P_DIM, dim=True)),
            ("█ ", cp(P_ORANGE, bold=True)), ("put gamma  ", cp(P_DIM, dim=True)),
            ("yellow", cp(P_ATM, bold=True)), ("=current price (strike label + line)  ", cp(P_DIM, dim=True)),
            ("cyan", cp(P_CYAN, bold=True)), ("=GEX flip strikes  ", cp(P_DIM, dim=True)),
            ("|", cp(P_DEFAULT, bold=True)), ("=GEX flip level", cp(P_DIM, dim=True)),
        ]
    x = 0
    for text, attr in legend:
        safe_add(win, 1, x, text, attr)
        x += len(text)

    if not history or not grid:
        msg = (f"No log found for {VIEW_DATE} — nothing to show." if HISTORICAL_MODE
               else "Waiting for first snapshot…")
        safe_add(win, h // 2, 2, msg, cp(P_CYAN))
        bot = h - 1
        hint = f" q=quit  r=refresh  [{SYMBOL}]  {status}"
        safe_add(win, bot, 0, hint.ljust(w - 1)[:w - 1], cp(P_STATUS))
        win.noutrefresh()
        return

    ref_col     = history[-1]
    gex_by_type = ref_col.get("gex_by_type") or {}
    nearest_strike = ref_col.get("nearest")

    top             = 3
    bottom_reserved = 3   # x-axis strike-label row + Max Pain/Net GEX/Flip row + status bar
    grid_sorted     = sorted(grid)
    strike_col_w    = 3
    y_axis_w        = 10   # room for "$XXX.XXM"-style gridline labels on the left

    usable_w      = max(0, w - y_axis_w - 1)
    n_strike_cols = max(1, usable_w // strike_col_w)

    if ui["vert_follow"]:
        center_idx = min(range(len(grid_sorted)), key=lambda i: abs(grid_sorted[i] - spot))
    else:
        center_idx = max(0, min(len(grid_sorted) - 1, ui["vert_center_idx"]))
    half = n_strike_cols // 2
    lo = max(0, center_idx - half)
    hi = min(len(grid_sorted), lo + n_strike_cols)
    lo = max(0, hi - n_strike_cols)
    visible_strikes = grid_sorted[lo:hi]   # ascending — lowest strike on the left

    # Scale scoped to what's visible. Net mode scales off the *combined* per-strike
    # value (usually much smaller than either raw side alone, due to call/put
    # cancellation) — sharing the separate-mode scale would make net bars look
    # artificially tiny, so each sub-mode gets its own scale.
    scale = 0.0
    if net_mode:
        for strike in visible_strikes:
            scale = max(scale, abs(ref_col["gex"].get(strike, 0.0)))
    else:
        for strike in visible_strikes:
            entry = gex_by_type.get(strike, {})
            scale = max(scale, abs(entry.get("call", 0.0)), abs(entry.get("put", 0.0)))

    avail_v    = max(2, h - top - bottom_reserved)
    zero_row   = top + avail_v // 2
    avail_up   = zero_row - top
    avail_down = (h - bottom_reserved - 1) - zero_row

    # Max Pain / Net GEX / Zero Gamma — identical calcs to the interval map, always
    # anchored to the latest column since this mode has no time axis to pause on.
    max_pain, flip_level = smoothed_max_pain_and_flip(history, len(history), SMOOTH_N)
    flip = None
    if flip_level is not None:
        flip = bounding_strikes(grid_sorted, flip_level) + (flip_level,)
    flip_strikes = (flip[0], flip[1]) if flip else ()
    net_gex = sum(ref_col["gex"].values())

    # Column positions computed before anything is drawn, so the flip line (drawn first)
    # and the bars (drawn after, so they show through if they land on the same column)
    # agree on where each strike sits.
    col_of = {}
    for si, strike in enumerate(visible_strikes):
        cx = y_axis_w + si * strike_col_w
        if cx >= w - 1:
            break
        col_of[strike] = cx

    if flip:
        flip_col = resolve_marker_col(flip[0], flip[1], col_of)
        if flip_col is not None:
            for ry in range(top, h - bottom_reserved):
                safe_add(win, ry, flip_col, "|", cp(P_DEFAULT, bold=True))

    # Price reference — a yellow vertical line at the (possibly between-strikes) column
    # for the current spot, same placement logic as the flip line. Drawn after it (so
    # price wins the single cell where they'd otherwise coincide) but still before the
    # bars, so a bar on the same column shows through on top of either line.
    price_lo, price_hi = bounding_strikes(grid_sorted, spot)
    price_col = resolve_marker_col(price_lo, price_hi, col_of)
    if price_col is not None:
        for ry in range(top, h - bottom_reserved):
            safe_add(win, ry, price_col, ":", cp(P_ATM, bold=True))

    # Zero baseline — dim reference line spanning the chart, drawn before the bars so a
    # bar at a near-zero strike still shows visibly on top of it.
    for cx in range(y_axis_w, w - 1):
        safe_add(win, zero_row, cx, "─", cp(P_DIM, dim=True))

    # Y-axis (dollar value) gridlines.
    safe_add(win, top, 0, fdollars_compact(scale).rjust(y_axis_w - 1), cp(P_DIM, dim=True))
    safe_add(win, zero_row, 0, "$0".rjust(y_axis_w - 1), cp(P_DIM, dim=True))
    safe_add(win, h - bottom_reserved - 1, 0, fdollars_compact(-scale).rjust(y_axis_w - 1), cp(P_DIM, dim=True))

    # Bars + x-axis strike labels (thinned so labels don't collide).
    label_every = max(1, 12 // strike_col_w)
    for si, strike in enumerate(visible_strikes):
        cx = col_of.get(strike)
        if cx is None:
            break

        if net_mode:
            net_val = ref_col["gex"].get(strike, 0.0)
            rows = round(abs(net_val) / scale * (avail_up if net_val >= 0 else avail_down)) if scale > 0 else 0
            pair = P_GREEN if net_val >= 0 else P_RED
            step = -1 if net_val >= 0 else 1   # up for positive, down for negative
            for r in range(1, rows + 1):
                safe_add(win, zero_row + step * r, cx, "█", cp(pair, bold=True))
        else:
            entry = gex_by_type.get(strike, {})
            call_val, put_val = entry.get("call", 0.0), entry.get("put", 0.0)
            call_rows = round(abs(call_val) / scale * avail_up) if scale > 0 else 0
            put_rows  = round(abs(put_val) / scale * avail_down) if scale > 0 else 0
            for r in range(1, call_rows + 1):
                safe_add(win, zero_row - r, cx, "█", cp(P_BLUE, bold=True))
            for r in range(1, put_rows + 1):
                safe_add(win, zero_row + r, cx, "█", cp(P_ORANGE, bold=True))

        if si % label_every == 0 or si == len(visible_strikes) - 1:
            if strike == nearest_strike:
                lbl_attr = cp(P_ATM, bold=True)
            elif strike in flip_strikes:
                lbl_attr = cp(P_CYAN, bold=True)
            else:
                lbl_attr = cp(P_DIM, dim=True)
            safe_add(win, h - bottom_reserved, max(0, cx - 1), fstrike(strike), lbl_attr)

    # ── Max Pain / Net GEX / Zero Gamma (GEX Flip) ──────────────────────
    mp_str = fstrike(max_pain) if max_pain is not None else "N/A"
    net_gex_str  = fdollars_compact(net_gex)
    net_gex_attr = cp(P_GREEN if net_gex >= 0 else P_RED, bold=True)
    if flip:
        lo_f, hi_f, level = flip
        flip_str = (fstrike(level) if lo_f == hi_f else
                    f"{fstrike(level)}  (between {fstrike(lo_f)} & {fstrike(hi_f)})")
    else:
        flip_str = "N/A"
    info_pieces = [
        (" Max Pain ", cp(P_DIM, dim=True)), (mp_str, cp(P_YELLOW, bold=True)),
        ("    Net GEX ", cp(P_DIM, dim=True)), (net_gex_str, net_gex_attr),
        ("    Zero Gamma/GEX Flip ", cp(P_DIM, dim=True)), (flip_str, cp(P_CYAN, bold=True)),
        (f"    (Max Pain/Flip smoothed over last {SMOOTH_N})", cp(P_DIM, dim=True)),
    ]
    info_row = h - bottom_reserved + 1
    ix = 0
    for text, attr in info_pieces:
        safe_add(win, info_row, ix, text, attr)
        ix += len(text)

    # ── Status bar ───────────────────────────────────────────────────────
    bot = h - 1
    if ui.get("entering_symbol"):
        prompt = SYMBOL_PROMPT + ui.get("symbol_input_buf", "")
        safe_add(win, bot, 0, prompt.ljust(w - 1)[:w - 1], cp(P_STATUS))
    else:
        exp_tag = "ALL-EXP" if ALL_EXP else "0DTE"
        vert_tag = "" if ui["vert_follow"] else "[scrolled]"
        hint = (f" q=quit  r=refresh  ←/→/PgUp/PgDn/↑/↓/[/]/{{/}}=pan strikes  z/End=reset  "
                f"g=interval map  n=net/separate  s=symbol  p=screenshot  "
                f"[{SYMBOL}] [{exp_tag}] {vert_tag} {status}")
        safe_add(win, bot, 0, hint.ljust(w - 1)[:w - 1], cp(P_STATUS))

    win.noutrefresh()

# ── CURSES MAIN ─────────────────────────────────────────────────────────────
def curses_main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(250)
    init_colors()

    history   = deque(maxlen=HISTORY_MAXLEN)
    grid      = set()
    scale_max = 0.0
    meta      = {}
    error_msg = ""
    last_fetch = 0.0
    fetch_started = 0.0
    fetch_dur = 5.0
    fetching = False
    log_rows = 0
    log_err  = None
    diag_count = 0
    diag_msg   = None
    diag_msg_time = 0.0
    lock = threading.Lock()
    # Bumped on every symbol switch; a fetch in flight for the old symbol checks this
    # when it completes and discards its result rather than merging stale data into
    # the new symbol's just-reset history (see _switch_symbol / do_fetch).
    fetch_epoch = 0

    # "interval" = the time-interval dot map, "strike" = the GEX-by-strike bar chart.
    # Toggled with 'g'; both modes share the fetch/log pipeline below, only the
    # renderer and what Left/Right/PgUp/PgDn control (time vs. strikes) differ.
    mode = "interval"
    # Within GEX-by-strike mode: False = separate call(blue)/put(orange) bars per
    # strike, True = one net bar per strike (green/red). Toggled with 'n'.
    by_strike_net = False

    # 's' opens a one-line text prompt (typed into symbol_input_buf) instead of
    # cycling a fixed list — Enter commits (case-insensitive, uppercased on submit),
    # Esc cancels. While entering_symbol is True, every other key below is swallowed
    # by the input-mode branch rather than falling through to its usual handler.
    entering_symbol = False
    symbol_input_buf = ""

    # 'p' saves a plain-text dump of whatever's currently on screen (either chart mode).
    screenshot_msg = None
    screenshot_msg_time = 0.0

    # Scroll state: live_follow=True always shows the newest columns; panning
    # back sets an absolute view_end_idx so the frozen window doesn't drift
    # as new columns keep landing in the background.
    live_follow = True
    view_end_idx = 0

    # Same pattern for the price axis: vert_follow=True auto-centers on whatever
    # strike is nearest spot; scrolling (↑/↓, [/], {/}) freezes an absolute index
    # into the sorted strike grid so it doesn't drift as price moves.
    vert_follow = True
    vert_center_idx = 0

    def _atm_idx():
        """Index of the strike nearest the latest spot — used to seed vert_center_idx
        the moment the user starts scrolling away from auto-center."""
        if not grid or not history:
            return 0
        gs = sorted(grid)
        spot_now = history[-1]["spot"]
        return min(range(len(gs)), key=lambda i: abs(gs[i] - spot_now))

    def _vert_step(delta):
        """Move vert_center_idx by delta strikes, seeding it from the currently
        auto-centered strike the first time (so scrolling starts from wherever you're
        already looking, not index 0). Shared by the interval map's ↑/↓/[/] (vertical,
        rows) and the by-strike chart's ←/→/PgUp/PgDn (horizontal, columns) — same
        "which strike is centered" state either way, just rendered differently."""
        nonlocal vert_follow, vert_center_idx
        if vert_follow:
            vert_center_idx = _atm_idx()
        vert_follow = False
        vert_center_idx = max(0, min(max(0, len(grid) - 1), vert_center_idx + delta))

    def _load_into(date_str):
        """(Re)populate history/grid/scale_max/meta from a day's log on disk."""
        nonlocal scale_max, meta, log_rows
        grid.clear()
        history.clear()
        scale_max = 0.0
        last_expiry = None
        for c in load_log(date_str):
            c, local_max = ingest_column(c, grid)
            # A new expiry (e.g. Deribit's daily ~03:00 CT rollover) starts with far less
            # built-up OI than a day-old chain — carrying the old expiry's scale forward
            # makes the new (smaller but real) values round down to invisible dots.
            if last_expiry is not None and c.get("expiry_label") != last_expiry:
                scale_max = 0.0
            last_expiry = c.get("expiry_label")
            history.append(c)
            scale_max = max(scale_max, local_max)
        if history:
            last = history[-1]
            meta = {"spot": last["spot"], "expiry_label": last.get("expiry_label") or "—",
                    "is_0dte": last.get("is_0dte"), "fetched_at": last["ts"].strftime("%H:%M:%S")}
        log_rows = len(history)

    with lock:
        _load_into(VIEW_DATE)   # resume today's log if present, or load the --date file

    def do_fetch(epoch):
        """epoch: fetch_epoch at the moment this fetch was triggered. If the symbol has
        been switched (fetch_epoch bumped) by the time this completes, its result is for
        a symbol nobody's looking at anymore — discarded rather than merged into the new
        symbol's freshly-reset history."""
        nonlocal error_msg, last_fetch, fetching, fetch_dur, scale_max, meta, log_rows, log_err
        nonlocal diag_count, diag_msg, diag_msg_time
        t0 = time.time()
        try:
            d = fetch_snapshot()
            elapsed = time.time() - t0
            with lock:
                if epoch != fetch_epoch:
                    return
                prev_col = history[-1] if history else None
                col = {"ts": datetime.now(), "spot": d["spot"], "gex": d["strikes"],
                       "oi_by_type": d.get("oi_by_type") or {},
                       "gex_by_type": d.get("gex_by_type") or {},
                       "bs_gex_curve": d.get("bs_gex_curve") or [],
                       "expiry_label": d.get("expiry_label"),
                       "is_0dte": d.get("is_0dte", True if IS_CRYPTO else False)}
                col, local_max = ingest_column(col, grid)
                # Same expiry-rollover reset as _load_into (see its comment) — a fresh
                # expiry's naturally smaller scale shouldn't be dwarfed by the old one's.
                if prev_col is not None and prev_col.get("expiry_label") != col.get("expiry_label"):
                    scale_max = 0.0
                history.append(col)
                scale_max = max(scale_max, local_max)
                meta = {k: v for k, v in d.items() if k != "strikes"}
                error_msg = ""
                last_fetch = time.time()
                fetch_dur = elapsed
                ok, err = append_log(col)
                if ok:
                    log_rows += 1
                    log_err = None
                else:
                    log_err = err
                if prev_col is not None:
                    msg = check_flip_jump(prev_col, col)
                    if msg:
                        diag_count += 1
                        diag_msg = msg
                        diag_msg_time = time.time()
        except Exception as e:
            with lock:
                if epoch == fetch_epoch:
                    error_msg = str(e)
        finally:
            with lock:
                if epoch == fetch_epoch:
                    fetching = False

    def trigger_fetch():
        nonlocal fetching, fetch_started
        with lock:
            if fetching:
                return
            fetching = True
            fetch_started = time.time()
            epoch = fetch_epoch
        threading.Thread(target=do_fetch, args=(epoch,), daemon=True).start()

    def _switch_symbol(new_symbol):
        """Retarget the whole app at a different symbol without restarting: reset every
        piece of per-symbol state, then (re)load whatever's already logged for the new
        symbol at VIEW_DATE (mirrors what happens at startup). Caller must hold `lock`;
        does not itself call trigger_fetch (that acquires `lock` too — call it after
        releasing, same convention as the 'r'/reload key handler below)."""
        nonlocal error_msg, last_fetch, fetch_dur, fetching, fetch_epoch
        nonlocal diag_count, diag_msg, diag_msg_time
        nonlocal live_follow, view_end_idx, vert_follow, vert_center_idx
        global SYMBOL, IS_CRYPTO, MULT, BAND_PCT
        SYMBOL    = new_symbol
        IS_CRYPTO = SYMBOL in ("ETH", "BTC")
        MULT      = 1 if IS_CRYPTO else 100
        BAND_PCT  = 0.20 if IS_CRYPTO else 0.12
        fetch_epoch += 1   # invalidate any fetch already in flight for the old symbol
        error_msg = ""
        last_fetch = 0.0
        fetch_dur = 5.0
        fetching = False
        diag_count = 0
        diag_msg = None
        diag_msg_time = 0.0
        live_follow = True
        view_end_idx = 0
        vert_follow = True
        vert_center_idx = 0
        _load_into(VIEW_DATE)   # note: not HISTORICAL_MODE-gated — mode/by_strike_net persist

    if not HISTORICAL_MODE:
        trigger_fetch()
        h, w = stdscr.getmaxyx()
        msg = f"Fetching {SYMBOL} chain…"
        safe_add(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg, cp(P_CYAN))
        stdscr.refresh()

    while True:
        key = stdscr.getch()

        if entering_symbol:
            # Every key is captured for the typed buffer here — q/Q/Esc/etc. must NOT
            # fall through to their usual meanings while typing (a ticker could contain
            # any letter, including 'q'), so this branch is fully separate from the
            # normal dispatch below rather than layered on top of it.
            if key in (10, 13, curses.KEY_ENTER):
                typed = symbol_input_buf.strip().upper()
                entering_symbol = False
                symbol_input_buf = ""
                if typed:
                    with lock:
                        _switch_symbol(typed)
                    if not HISTORICAL_MODE:
                        trigger_fetch()   # outside the lock — trigger_fetch acquires it itself
            elif key == 27:   # Esc cancels, leaves the current symbol untouched
                entering_symbol = False
                symbol_input_buf = ""
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                symbol_input_buf = symbol_input_buf[:-1]
            elif key != -1 and 32 <= key < 127 and len(symbol_input_buf) < 10:
                symbol_input_buf += chr(key)
        else:
            if key in (ord('q'), ord('Q'), 27):
                break
            if key in (ord('r'), ord('R')):
                if HISTORICAL_MODE:
                    with lock:
                        _load_into(VIEW_DATE)
                else:
                    trigger_fetch()
            elif key in (ord('g'), ord('G')):
                with lock:
                    mode = "strike" if mode == "interval" else "interval"
            elif key in (ord('n'), ord('N')):
                with lock:
                    by_strike_net = not by_strike_net
            elif key in (ord('s'), ord('S')):
                entering_symbol = True
                symbol_input_buf = ""
            elif key in (ord('p'), ord('P')):
                try:
                    path = take_screenshot()
                    screenshot_msg = f"📸 Saved screenshots/{os.path.basename(path)}"
                except Exception as e:
                    screenshot_msg = f"⚠ Screenshot failed: {e}"
                screenshot_msg_time = time.time()
            elif key == curses.KEY_LEFT:
                with lock:
                    if mode == "strike":
                        _vert_step(-VERT_STEP)
                    else:
                        if live_follow:
                            view_end_idx = len(history)
                        live_follow = False
                        view_end_idx = max(1, view_end_idx - ARROW_STEP)
            elif key == curses.KEY_RIGHT:
                with lock:
                    if mode == "strike":
                        _vert_step(VERT_STEP)
                    elif not live_follow:
                        view_end_idx = min(len(history), view_end_idx + ARROW_STEP)
                        if view_end_idx >= len(history):
                            live_follow = True
            elif key == curses.KEY_PPAGE:
                with lock:
                    if mode == "strike":
                        _vert_step(-VERT_PAGE_STEP)
                    else:
                        if live_follow:
                            view_end_idx = len(history)
                        live_follow = False
                        view_end_idx = max(1, view_end_idx - PAGE_STEP)
            elif key == curses.KEY_NPAGE:
                with lock:
                    if mode == "strike":
                        _vert_step(VERT_PAGE_STEP)
                    elif not live_follow:
                        view_end_idx = min(len(history), view_end_idx + PAGE_STEP)
                        if view_end_idx >= len(history):
                            live_follow = True
            elif key in (curses.KEY_END, ord('z'), ord('Z')):
                # Reset: jump back to the live edge (time axis) and re-center on
                # whatever strike is nearest the current spot (price axis) — both
                # views' "auto-follow" default, in one keystroke either way.
                with lock:
                    live_follow = True
                    vert_follow = True
            elif key == curses.KEY_UP:
                with lock:
                    _vert_step(VERT_STEP)
            elif key == curses.KEY_DOWN:
                with lock:
                    _vert_step(-VERT_STEP)
            elif key == ord('['):
                with lock:
                    _vert_step(VERT_PAGE_STEP)
            elif key == ord(']'):
                with lock:
                    _vert_step(-VERT_PAGE_STEP)
            elif key == ord('{'):
                with lock:
                    vert_follow = False
                    vert_center_idx = max(0, len(grid) - 1)   # jump to the highest strike in the grid
            elif key == ord('}'):
                with lock:
                    vert_follow = False
                    vert_center_idx = 0                        # jump to the lowest strike in the grid

        if not HISTORICAL_MODE:
            with lock:
                now_fetching = fetching
                elapsed = time.time() - last_fetch if last_fetch else 999
                cur_fetch_dur = fetch_dur

            lead = max(1.0, cur_fetch_dur)
            if elapsed >= (REFRESH_SEC - lead) and not now_fetching and last_fetch > 0:
                trigger_fetch()
        else:
            now_fetching = False
            elapsed = 999

        with lock:
            cur_history = list(history)
            cur_grid = set(grid)
            cur_scale = scale_max
            cur_meta = dict(meta)
            cur_error = error_msg
            cur_diag_count = diag_count
            cur_diag_msg = diag_msg if diag_msg and (time.time() - diag_msg_time) < 120 else None
            ui = {"live_follow": live_follow, "view_end_idx": view_end_idx,
                  "vert_follow": vert_follow, "vert_center_idx": vert_center_idx,
                  "by_strike_net": by_strike_net,
                  "entering_symbol": entering_symbol, "symbol_input_buf": symbol_input_buf,
                  "log_rows": log_rows, "log_err": log_err, "diag_count": cur_diag_count}

        if HISTORICAL_MODE:
            status = "history mode — no live fetch"
        elif now_fetching:
            status = "↻ fetching…"
        else:
            next_in = max(0, int(REFRESH_SEC - elapsed))
            status = f"↻ in {next_in}s"
        if cur_diag_msg:
            status = f"{cur_diag_msg}  |  {status}"
        if screenshot_msg and (time.time() - screenshot_msg_time) < 8:
            status = f"{screenshot_msg}  |  {status}"
        if cur_error:
            status += f"  ⚠ {cur_error}"

        if mode == "strike":
            draw_by_strike(stdscr, cur_history, cur_grid, cur_meta, status, ui)
        else:
            draw(stdscr, cur_history, cur_grid, cur_scale, cur_meta, status, ui)

        if entering_symbol:
            curses.curs_set(1)
            h, w = stdscr.getmaxyx()
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
    """No UI — just fetch+log on REFRESH_SEC forever. Meant to be left running
    (Task Scheduler, a minimized terminal, etc.) so a real trailing window of
    history is actually on disk by the time someone opens the full map."""
    print(f"gex.py headless logger — {SYMBOL}{' (all-exp)' if ALL_EXP else ''}, "
          f"every {REFRESH_SEC}s -> {log_path(datetime.now().strftime('%m_%d_%Y'))}")
    print("Ctrl+C to stop.")
    prev_col = None
    while True:
        t0 = time.time()
        ts = datetime.now()
        try:
            d = fetch_snapshot()
            col = {"ts": ts, "spot": d["spot"], "gex": d["strikes"],
                   "oi_by_type": d.get("oi_by_type") or {},
                   "gex_by_type": d.get("gex_by_type") or {},
                   "bs_gex_curve": d.get("bs_gex_curve") or [],
                   "expiry_label": d.get("expiry_label"),
                   "is_0dte": d.get("is_0dte", True if IS_CRYPTO else False)}
            ok, err = append_log(col)
            tag = "OK" if ok else f"LOG ERROR: {err}"
            print(f"[{ts.strftime('%H:%M:%S')}] spot={col['spot']:.2f} "
                  f"strikes={len(col['gex'])} {tag}")
            if prev_col is not None:
                msg = check_flip_jump(prev_col, col)
                if msg:
                    print(f"  {msg}")
            prev_col = col
        except Exception as e:
            print(f"[{ts.strftime('%H:%M:%S')}] fetch error: {e}")
        time.sleep(max(1.0, REFRESH_SEC - (time.time() - t0)))

def main():
    if HEADLESS:
        try:
            headless_main()
        except KeyboardInterrupt:
            pass
        print("\nGEX headless logger — stopped.")
        return
    try:
        curses.wrapper(curses_main)
    except KeyboardInterrupt:
        pass
    print("GEX Map — exited.")

if __name__ == "__main__":
    main()
