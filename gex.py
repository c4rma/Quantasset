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
  Zero Gamma/GEX Flip  strike level where cumulative net GEX crosses zero —
                        below it dealer hedging is destabilizing, above it
                        stabilizing. The two strikes it falls between are
                        highlighted cyan on the price axis.
Both are averaged over the last --smooth-n raw fetches (default 5) rather than
shown instantaneously — a single stale/transitional OI snapshot or a large
order on a thin strike can swing the raw flip $50+ for one refresh and then
revert; smoothing absorbs that without hiding a real, sustained level change.
A --diag-threshold-sized jump ($30 default) in the *raw* (unsmoothed) flip
between two consecutive fetches is logged to gex_diag_<SYMBOL>_MM_DD_YYYY.jsonl
(full before/after OI+gamma per strike) and flashed in the status bar, so you
can tell a data artifact from a real move.

Data:
  ETH/BTC — Deribit REST, real-time, gamma+OI read directly off each ticker.
  QQQ     — CBOE delayed-quotes feed (~15m delay), gamma+OI read directly off
            each contract (CBOE publishes greeks, no Black-Scholes needed).

Every refresh is appended to a per-day log (gex_<SYMBOL>_MM_DD_YYYY.jsonl, next
to this script) so history survives restarts. Launching on a day that already
has a log resumes it; `--date` opens a past day for pure playback/browsing.

IMPORTANT: neither Deribit nor CBOE expose *historical* per-strike gamma/OI —
only a live snapshot. There is no API to backfill "the last 6 hours" out of
thin air on a cold start; the only real history is whatever this tool (or a
--headless instance of it) actually logged while running. The live view
defaults to showing the trailing --window-hours of whatever's been logged so
far today (compressing columns to fit if there's more than fits on screen);
run --headless continuously (e.g. via Task Scheduler) to keep that window full.

Usage:
  python gex.py [ETH|BTC|QQQ] [--interval SEC] [--all-exp] [--date MM_DD_YYYY]
                 [--window-hours N] [--headless] [--smooth-n N] [--diag-threshold N]
    --interval SEC       refresh interval in seconds (default 60)
    --all-exp            sum GEX across ALL expiries instead of just the nearest
                          (nearest/0DTE-style expiry is the default)
    --date MM_DD_YYYY    browse a past day's logged map instead of going live
    --window-hours N     trailing window the live view targets (default 6)
    --headless           no UI — just fetch+log on schedule, for keeping a
                          continuous history running in the background
    --smooth-n N         raw fetches averaged into the displayed Max Pain /
                          GEX Flip (default 5; 1 = show the raw instantaneous value)
    --diag-threshold N   dollar move in the raw flip between consecutive
                          fetches that triggers a diagnostic log entry (default
                          30; 0 disables)

In-app: ←/→ pan by a few columns, PgUp/PgDn pan by a screenful, End jumps back
to live. [R] refreshes now (live mode) or reloads the log from disk (history
mode, in case another instance is still writing to it).
"""

import sys
import time
import threading
import os
import json
import bisect

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
from datetime import datetime, timezone, timedelta
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
WINDOW_HOURS = 6.0
if "--window-hours" in args:
    i = args.index("--window-hours")
    try:
        WINDOW_HOURS = max(0.1, float(args[i + 1]))
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
if SYMBOL not in ("ETH", "BTC", "QQQ"):
    print(f"Unknown symbol '{SYMBOL}' — use ETH, BTC, or QQQ")
    sys.exit(1)

IS_CRYPTO = SYMBOL in ("ETH", "BTC")
MULT      = 1 if IS_CRYPTO else 100     # Deribit = 1 coin/contract, equities = 100 shares/contract
BAND_PCT  = 0.20 if IS_CRYPTO else 0.12 # strike-grid band around spot, as a fraction of spot

BASE_URL = "https://www.deribit.com/api/v2"
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"

COL_W         = 2       # terminal columns per time interval
HISTORY_MAXLEN = 1500   # ~25h of 1-min columns

TODAY_STR       = datetime.now().strftime("%m_%d_%Y")
VIEW_DATE       = LOAD_DATE or TODAY_STR
HISTORICAL_MODE = LOAD_DATE is not None and LOAD_DATE != TODAY_STR   # pure playback, no live fetch

LOG_DIR = os.path.dirname(os.path.abspath(__file__))
ARROW_STEP = 5    # columns per Left/Right press
PAGE_STEP  = 30   # columns per PgUp/PgDn press
WINDOW_SEC = WINDOW_HOURS * 3600   # trailing window the live view targets

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

    strikes    = {}
    oi_by_type = {}   # strike -> {"call": raw_oi, "put": raw_oi}, unweighted — for Max Pain
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

    if target_exp:
        exp_label = datetime.fromtimestamp(target_exp / 1000, tz=timezone.utc).strftime("%d%b%y").upper()
        ttl       = countdown(target_exp)
    else:
        exp_label = f"ALL({len(by_exp)})"
        ttl       = None

    return {"spot": spot, "strikes": strikes, "oi_by_type": oi_by_type, "expiry_label": exp_label,
            "ttl": ttl, "fetched_at": datetime.now().strftime("%H:%M:%S")}

# ── CBOE HELPERS (QQQ) ────────────────────────────────────────────────────────
def fetch_qqq(all_exp):
    r = requests.get(CBOE_URL.format("QQQ"), timeout=15)
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

    strikes    = {}
    oi_by_type = {}   # strike -> {"call": raw_oi, "put": raw_oi}, unweighted — for Max Pain
    for exp in target_exps:
        for cp_flag, strike, o in by_exp[exp]:
            gamma = float(o.get("gamma") or 0)
            oi    = float(o.get("open_interest") or 0)
            gex   = gamma * oi * MULT * price * price * 0.01
            if cp_flag == "P":
                gex = -gex
            strikes[strike] = strikes.get(strike, 0.0) + gex
            otype = "call" if cp_flag == "C" else "put"
            oi_by_type.setdefault(strike, {"call": 0.0, "put": 0.0})[otype] += oi

    is_0dte   = (not all_exp) and target_exps[0] == today
    exp_label = target_exps[0] if len(target_exps) == 1 else f"ALL({len(target_exps)})"

    return {"spot": price, "strikes": strikes, "oi_by_type": oi_by_type, "expiry_label": exp_label,
            "ttl": None, "is_0dte": is_0dte, "fetched_at": datetime.now().strftime("%H:%M:%S")}

def fetch_snapshot():
    if SYMBOL == "QQQ":
        return fetch_qqq(ALL_EXP)
    return fetch_eth(SYMBOL, ALL_EXP)

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
    prev_flip = compute_gamma_flip(prev_col["gex"], prev_col["spot"])
    new_flip  = compute_gamma_flip(new_col["gex"], new_col["spot"])
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
        fl = compute_gamma_flip(c["gex"], c["spot"])
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
                    # older logs predate oi_by_type — .get(...) so Max Pain just shows N/A for them
                    "oi_by_type": {float(k): v for k, v in (d.get("oi_by_type") or {}).items()},
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

def compute_gamma_flip(strikes, spot):
    """Zero Gamma / GEX Flip: the strike level where the cumulative net GEX (summed low
    to high strike) crosses zero. Returns (low_strike, high_strike, interpolated_level)
    for the crossing nearest spot, or None if the cumulative sum never crosses zero."""
    sorted_strikes = sorted(strikes)
    if len(sorted_strikes) < 2:
        return None
    running, cum = 0.0, []
    for s in sorted_strikes:
        running += strikes[s]
        cum.append(running)
    crossings = []
    for i in range(len(sorted_strikes) - 1):
        c0, c1 = cum[i], cum[i + 1]
        if c0 == 0:
            crossings.append((sorted_strikes[i], sorted_strikes[i], sorted_strikes[i]))
        elif (c0 < 0) != (c1 < 0):
            s0, s1 = sorted_strikes[i], sorted_strikes[i + 1]
            level = s0 + (s1 - s0) * (-c0 / (c1 - c0))
            crossings.append((s0, s1, level))
    if not crossings:
        return None
    return min(crossings, key=lambda c: abs(c[2] - spot))

def bucket_columns(cols, n_buckets):
    """Downsample a chronological column list to at most n_buckets entries by keeping
    the most recent raw column in each even time-slice — used to fit --window-hours of
    real logged history into however many character-columns the terminal actually has,
    rather than only ever showing a raw-column-per-refresh sliver of the trailing window."""
    total = len(cols)
    if total <= n_buckets or n_buckets <= 0:
        return cols
    out, prev_end = [], 0
    for b in range(n_buckets):
        end = max((b + 1) * total // n_buckets, prev_end + 1)
        out.append(cols[end - 1])
        prev_end = end
    return out

# ── COLOUR PAIRS ───────────────────────────────────────────────────────────
P_DEFAULT, P_DIM, P_CYAN, P_YELLOW, P_GREEN, P_RED, P_STATUS, P_ATM = range(1, 9)

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

def cp(pair, bold=False, dim=False):
    a = curses.color_pair(pair)
    if bold: a |= curses.A_BOLD
    if dim:  a |= curses.A_DIM
    return a

def safe_add(win, y, x, s, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0:
        return
    avail = w - x - 1
    if avail <= 0:
        return
    try:
        win.addstr(y, x, s[:avail], attr)
    except curses.error:
        pass

def fstrike(strike):
    if IS_CRYPTO:
        return f"${strike:,.0f}"
    return f"${strike:,.2f}"

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

# ── DRAW ──────────────────────────────────────────────────────────────────
def draw(win, history, grid, scale_max, meta, status, ui):
    h, w = win.getmaxyx()
    win.erase()

    live_follow = ui["live_follow"]
    n_hist      = len(history)
    end_idx     = n_hist if live_follow else max(1, min(ui["view_end_idx"], n_hist))

    label  = meta.get("expiry_label", "—")
    ttl    = meta.get("ttl")
    is_0dte = meta.get("is_0dte", True if IS_CRYPTO else False)
    fetched = meta.get("fetched_at", "—")

    # ── Row 0: header ────────────────────────────────────────────────────
    if live_follow:
        mode_piece = ("● LIVE", cp(P_GREEN, bold=True))
        spot = history[-1]["spot"] if history else meta.get("spot", 0.0)
    else:
        viewed_ts = history[end_idx - 1]["ts"].strftime("%H:%M:%S") if history else "—"
        mode_piece = (f"⏸ HISTORY @{viewed_ts}", cp(P_YELLOW, bold=True))
        spot = history[end_idx - 1]["spot"] if history else meta.get("spot", 0.0)

    pieces = [
        ("GEX MAP", cp(P_CYAN, bold=True)),
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
    else:
        src_tag = f"  ({'live' if IS_CRYPTO else '~15m delay'})"
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
        ("cyan", cp(P_CYAN, bold=True)), ("=GEX flip strikes", cp(P_DIM, dim=True)),
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
    atm_idx = min(range(len(grid_sorted)), key=lambda i: abs(grid_sorted[i] - spot))
    half = avail_rows // 2
    lo = max(0, atm_idx - half)
    hi = min(len(grid_sorted), lo + avail_rows)
    lo = max(0, hi - avail_rows)
    visible = list(reversed(grid_sorted[lo:hi]))   # highest strike at top

    usable_w = max(0, w - axis_w - 1)
    n_cols   = max(1, usable_w // COL_W)

    if live_follow:
        # Default view: the trailing WINDOW_HOURS of whatever's actually been logged,
        # compressed (most-recent-per-slice) to fit the screen if it's more than fits raw.
        cutoff = history[-1]["ts"] - timedelta(seconds=WINDOW_SEC)
        windowed = [c for c in history if c["ts"] >= cutoff]
        cols = bucket_columns(windowed, n_cols)
    else:
        # Panning (arrow/PgUp/PgDn) inspects raw, uncompressed columns at 1:1 resolution.
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
        if lo_s == hi_s:
            target_row, net_src = row_of.get(lo_s), lo_s
        elif hi_s in row_of and lo_s in row_of:
            target_row = row_of[hi_s] + 1   # hi_s renders above lo_s (descending list)
            net_src = lo_s if abs(spot_c - lo_s) <= abs(spot_c - hi_s) else hi_s
        else:
            target_row, net_src = None, None
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

    # ── Max Pain / Zero Gamma (GEX Flip) ────────────────────────────────
    mp_str = fstrike(max_pain) if max_pain is not None else "N/A"
    if flip:
        lo, hi, level = flip
        flip_str = (fstrike(level) if lo == hi else
                    f"{fstrike(level)}  (between {fstrike(lo)} & {fstrike(hi)})")
    else:
        flip_str = "N/A"
    info_pieces = [
        (" Max Pain ", cp(P_DIM, dim=True)), (mp_str, cp(P_YELLOW, bold=True)),
        ("    Zero Gamma/GEX Flip ", cp(P_DIM, dim=True)), (flip_str, cp(P_CYAN, bold=True)),
        (f"    (smoothed over last {SMOOTH_N})", cp(P_DIM, dim=True)),
    ]
    info_row = h - bottom_reserved + 1
    ix = 0
    for text, attr in info_pieces:
        safe_add(win, info_row, ix, text, attr)
        ix += len(text)

    # ── Status bar ───────────────────────────────────────────────────────
    bot = h - 1
    exp_tag = "ALL-EXP" if ALL_EXP else "0DTE"
    refresh_hint = "r=reload" if HISTORICAL_MODE else "r=refresh"
    win_tag = f"window={WINDOW_HOURS:g}h" if live_follow else "1:1 pan"
    hint = f" q=quit  {refresh_hint}  ←/→ PgUp/PgDn=pan  End=live  [{SYMBOL}]  [{exp_tag}]  [{win_tag}]  {status}"
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

    # Scroll state: live_follow=True always shows the newest columns; panning
    # back sets an absolute view_end_idx so the frozen window doesn't drift
    # as new columns keep landing in the background.
    live_follow = True
    view_end_idx = 0

    def _load_into(date_str):
        """(Re)populate history/grid/scale_max/meta from a day's log on disk."""
        nonlocal scale_max, meta, log_rows
        grid.clear()
        history.clear()
        scale_max = 0.0
        for c in load_log(date_str):
            c, local_max = ingest_column(c, grid)
            history.append(c)
            scale_max = max(scale_max, local_max)
        if history:
            last = history[-1]
            meta = {"spot": last["spot"], "expiry_label": last.get("expiry_label") or "—",
                    "is_0dte": last.get("is_0dte"), "fetched_at": last["ts"].strftime("%H:%M:%S")}
        log_rows = len(history)

    with lock:
        _load_into(VIEW_DATE)   # resume today's log if present, or load the --date file

    def do_fetch():
        nonlocal error_msg, last_fetch, fetching, fetch_dur, scale_max, meta, log_rows, log_err
        nonlocal diag_count, diag_msg, diag_msg_time
        t0 = time.time()
        try:
            d = fetch_snapshot()
            elapsed = time.time() - t0
            with lock:
                prev_col = history[-1] if history else None
                col = {"ts": datetime.now(), "spot": d["spot"], "gex": d["strikes"],
                       "oi_by_type": d.get("oi_by_type") or {},
                       "expiry_label": d.get("expiry_label"),
                       "is_0dte": d.get("is_0dte", True if IS_CRYPTO else False)}
                col, local_max = ingest_column(col, grid)
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
                error_msg = str(e)
        finally:
            with lock:
                fetching = False

    def trigger_fetch():
        nonlocal fetching, fetch_started
        with lock:
            if fetching:
                return
            fetching = True
            fetch_started = time.time()
        threading.Thread(target=do_fetch, daemon=True).start()

    if not HISTORICAL_MODE:
        trigger_fetch()
        h, w = stdscr.getmaxyx()
        msg = f"Fetching {SYMBOL} chain…"
        safe_add(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg, cp(P_CYAN))
        stdscr.refresh()

    while True:
        key = stdscr.getch()
        if key in (ord('q'), ord('Q'), 27):
            break
        if key in (ord('r'), ord('R')):
            if HISTORICAL_MODE:
                with lock:
                    _load_into(VIEW_DATE)
            else:
                trigger_fetch()
        elif key == curses.KEY_LEFT:
            with lock:
                if live_follow:
                    view_end_idx = len(history)
                live_follow = False
                view_end_idx = max(1, view_end_idx - ARROW_STEP)
        elif key == curses.KEY_RIGHT:
            with lock:
                if not live_follow:
                    view_end_idx = min(len(history), view_end_idx + ARROW_STEP)
                    if view_end_idx >= len(history):
                        live_follow = True
        elif key == curses.KEY_PPAGE:
            with lock:
                if live_follow:
                    view_end_idx = len(history)
                live_follow = False
                view_end_idx = max(1, view_end_idx - PAGE_STEP)
        elif key == curses.KEY_NPAGE:
            with lock:
                if not live_follow:
                    view_end_idx = min(len(history), view_end_idx + PAGE_STEP)
                    if view_end_idx >= len(history):
                        live_follow = True
        elif key == curses.KEY_END:
            with lock:
                live_follow = True

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
        if cur_error:
            status += f"  ⚠ {cur_error}"

        draw(stdscr, cur_history, cur_grid, cur_scale, cur_meta, status, ui)
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
