#!/usr/bin/env python3
"""
cvd.py — Aggregate Cumulative Volume Delta (CVD) with Underlying Price Panel
Curses terminal chart, two stacked panels sharing one time axis:

  Price panel (top)  — OHLC candlesticks built from live trade prints,
                        blended across both feeds (Phemex perp + Kraken spot).
  CVD panel (bottom) — cumulative Σ(buy_qty − sell_qty) across BOTH feeds,
                        also rendered as OHLC candles (open = prior bar's
                        close, high/low = the running range within the bar),
                        colored the same way as price (green = close>=open).

CVD = the running total of signed trade volume using each print's taker side
(buy prints add, sell prints subtract). It answers "who has been more
aggressive, buyers or sellers" independent of price — divergence between a
rising price and a falling/flat CVD line is the classic "hidden selling"
signal traders watch this indicator for.

Data (real trade prints, not just OHLC volume):
  Phemex — wss://ws.phemex.com  trade_p.subscribe  (hedged USDT perp)
  Kraken — wss://ws.kraken.com/v2  channel=trade   (USD spot)
Both run concurrently in background threads; every individual print from
either exchange feeds the SAME bar/CVD accumulator, so "aggregate" means
combined across exchanges, not per-exchange lines.

Every closed bar is appended to a per-day log (cvd_<SYMBOL>_<bar>_MM_DD_YYYY.jsonl,
next to this script) so history survives restarts — launching on a day that
already has a log resumes it (CVD continues from the last logged value
instead of restarting at zero).

Bar type — TIME or VOLUME:
  --interval 1m            time bars: one bar per wall-clock interval
  --interval 500V           volume bars: one bar per 500 units (BTC/ETH) of
                            combined buy+sell volume traded, regardless of
                            how long that takes — the trade that crosses the
                            threshold closes out its bar in full (not split).

Historical backfill on cold start (default: since 19:00 CT session open):
  Only runs when there's no log for today yet (a resumed log already has
  continuity, so backfill is skipped to avoid double-counting). Reconstructs
  bars/CVD from real historical trades over the REST APIs before the live
  WS feeds take over — same ingest path as live trades, so it's exact, not
  an approximation. By default it reaches back to the most recent 19:00 CT
  daily session open (same anchor chart.py uses elsewhere in this repo) —
  --backfill-hours N overrides with a fixed window instead. IMPORTANT
  LIMITATION: Kraken's public Trades REST endpoint supports real pagination
  arbitrarily far back, but Phemex's public trade endpoint only ever returns
  the last ~1000 prints (~tens of minutes) with no pagination — there is no
  way to backfill deep Phemex history. So the backfilled portion of the
  window is **Kraken-only** (single exchange, not aggregate); once live WS
  trades start flowing (immediately after backfill), CVD becomes true
  dual-exchange aggregate again. The boundary timestamp is shown in the
  status bar so it's never ambiguous which portion is which.

Usage:
  python cvd.py [ETH|BTC] [--interval LABEL] [--date MM_DD_YYYY] [--headless]
                 [--backfill-hours N]
    --interval LABEL     bar size: 1s,5s,15s,30s,1m,3m,5m,15m,1H, or a volume
                         bar like 500V / 2500V (default 1m)
    --date MM_DD_YYYY    browse a past day's logged bars (playback, no live feed)
    --backfill-hours N   fixed hours of historical trades to reconstruct on a
                         cold start, Kraken-sourced (default: since 19:00 CT
                         session open; 0 disables backfill entirely)
    --headless           no UI — just ingest+log trades forever (e.g. Task
                        Scheduler), so a real history is on disk once you
                        open the full UI later. ALSO writes a separate,
                        always-1m OHLC(spot)+CVD CSV (cvd_1m_<SYMBOL>_
                        MM_DD_YYYY.csv, one row per minute, regardless of
                        --interval) — a new file starts automatically at
                        00:00 CT, matching gex.py's daily log rotation.

There is only ONE bar timeline (not a separate one for Goto/history) — the
same buffer the live WS feeds populate. Scrolling back past whatever's
currently loaded auto-fetches older real Kraken history and prepends it onto
that SAME buffer (bounded only by how far back Kraken's own trade history
goes — never a fixed window), whether you got there by panning back from a
fresh launch or by using [G]. Scrolling forward is never a special case
either: it's the same buffer the WS feeds keep extending live, so it just
naturally keeps going — there's no "caught up to now" wall to hit and no
"exit" transition needed.

In-app:
  [G] Goto — type a date/time (MM_DD_YYYY [HH:MM]) and hit Enter to jump
      straight to that moment. If the requested date isn't covered by
      what's already loaded, fetches real Kraken history backward (that
      day's own 19:00 CT session open through whatever's currently the
      oldest loaded bar) and prepends it — CVD is rebased so the join is
      continuous, not anchored at an arbitrary offset. The view then centers
      on the requested time. [L] / End jumps back to the live edge.
  [I] switches to a different interval on the fly — type any --interval spec
      (1m, 3m, 500V, ...) and hit Enter. Rebuilds bar history for the new
      shape (same backfill-from-19:00-CT path as a fresh launch); the WS
      feeds keep streaming the whole time, nothing reconnects. In --date
      playback mode it instead loads that day's log for the new interval if
      one exists.
  [Z] rebases CVD to 0 from this point forward (older bars in the log keep
      their original values — only the live view's baseline shifts).
  ←/→ pan by 1 bar, [ / ] by 10, { / } by 50 (PgUp/PgDn alias the 50-tier) —
      same step convention as quantasset_chart.py/charthacker.py. The first
      press activates a crosshair (same model as that file too) on the
      rightmost visible bar instead of immediately panning the whole view —
      further presses move the crosshair within the visible window, only
      panning once it reaches an edge. The crosshair highlights that bar
      yellow in both panels, draws a dotted vertical line through both plus
      a dotted horizontal line + value label at its price/CVD, and the
      status bar shows its exact O/H/L/C and CVD O/H/L/C instead of the
      live "last" readout. Panning right back past the true live edge exits
      the crosshair automatically.
  [L] / End jumps back to the live candle (also exits Goto mode/crosshair).
  [Q] / Esc quits.
"""

import sys
import os
import time
import json
import csv
import bisect
import threading
from collections import deque
from datetime import datetime, timezone, timedelta

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
    import websocket
except ModuleNotFoundError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websocket-client", "-q"], stdout=subprocess.DEVNULL)
    import websocket

try:
    import requests
except ModuleNotFoundError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"], stdout=subprocess.DEVNULL)
    import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── ARG PARSING ────────────────────────────────────────────────────────────
args = sys.argv[1:]
if "-h" in args or "--help" in args:
    print(__doc__.strip())
    sys.exit(0)
HEADLESS = "--headless" in args
args = [a for a in args if a != "--headless"]

INTERVAL_SECS = {
    "1s": 1, "5s": 5, "15s": 15, "30s": 30,
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "1H": 3600,
}

def parse_interval(raw):
    """Validate + decode an --interval-style string (a time label from
    INTERVAL_SECS, or a volume-bar spec like "500V") into
    (label, mode, secs, threshold), or return None if invalid. Shared by the
    CLI --interval flag and the in-app [I] interval-switch prompt so both
    accept exactly the same syntax."""
    if raw in INTERVAL_SECS:
        return raw, "time", INTERVAL_SECS[raw], None
    if raw[:-1].replace(".", "", 1).isdigit() and raw and raw[-1] in "vV":
        return raw[:-1] + "V", "volume", None, float(raw[:-1])
    return None

INTERVAL_LABEL = "1m"
if "--interval" in args:
    i = args.index("--interval")
    try:
        lbl = args[i + 1]
    except IndexError:
        print("--interval requires a value")
        sys.exit(1)
    parsed = parse_interval(lbl)
    if parsed is None:
        print(f"--interval must be one of {list(INTERVAL_SECS)}, or a volume bar like 500V")
        sys.exit(1)
    INTERVAL_LABEL = parsed[0]
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]

if INTERVAL_LABEL in INTERVAL_SECS:
    BAR_MODE = "time"
    BAR_SECS = INTERVAL_SECS[INTERVAL_LABEL]
    VOL_THRESHOLD = None
else:
    BAR_MODE = "volume"
    BAR_SECS = None
    VOL_THRESHOLD = float(INTERVAL_LABEL[:-1])

def _session_start_ts(now=None):
    """Most recent 19:00 CT daily session open, on/before `now`. Matches
    chart.py's session_bounds() daily-anchor convention exactly (assumes the
    local machine clock is already CT — same simplification used there,
    no explicit UTC/zoneinfo conversion)."""
    now = now or datetime.now()
    today_open = datetime(now.year, now.month, now.day, 19, 0, 0)
    if now < today_open:
        today_open -= timedelta(days=1)
    return today_open.timestamp()

BACKFILL_HOURS = None   # None = default: since the most recent 19:00 CT session open
if "--backfill-hours" in args:
    i = args.index("--backfill-hours")
    try:
        BACKFILL_HOURS = max(0.0, float(args[i + 1]))
    except (IndexError, ValueError):
        print("--backfill-hours requires a number")
        sys.exit(1)
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]
if BACKFILL_HOURS is None:
    BACKFILL_HOURS = max(0.0, (time.time() - _session_start_ts()) / 3600.0)

LOAD_DATE = None
if "--date" in args:
    i = args.index("--date")
    try:
        LOAD_DATE = args[i + 1]
        datetime.strptime(LOAD_DATE, "%m_%d_%Y")
    except (IndexError, ValueError):
        print("--date requires MM_DD_YYYY format, e.g. --date 07_01_2026")
        sys.exit(1)
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]

SYMBOL = args[0].upper() if args else "BTC"
if SYMBOL not in ("ETH", "BTC"):
    print(f"Unknown symbol '{SYMBOL}' — use ETH or BTC")
    sys.exit(1)

PHEMEX_SYMBOLS  = {"ETH": "ETHUSDT", "BTC": "BTCUSDT"}
KRAKEN_WS_PAIRS = {"ETH": "ETH/USD", "BTC": "BTC/USD"}
PHEMEX_WS_URL = "wss://ws.phemex.com"
KRAKEN_WS_URL = "wss://ws.kraken.com/v2"

TODAY_STR       = datetime.now().strftime("%m_%d_%Y")
VIEW_DATE       = LOAD_DATE or TODAY_STR
HISTORICAL_MODE = LOAD_DATE is not None and LOAD_DATE != TODAY_STR

LOG_DIR = os.path.dirname(os.path.abspath(__file__))

# Pan step tiers — same convention as quantasset_chart.py: plain arrow = 1,
# [ / ] = 10, { / } = 50 (negative = pan left/older, positive = pan right/newer).
# PgUp/PgDn are kept as aliases for the coarse tier for convenience.
PAN_KEYS = {
    curses.KEY_LEFT: -1, ord('['): -10, ord('{'): -50, curses.KEY_PPAGE: -50,
    curses.KEY_RIGHT: 1, ord(']'): 10, ord('}'): 50, curses.KEY_NPAGE: 50,
}

# ── PERSISTENCE ─────────────────────────────────────────────────────────────
def log_path(date_str):
    return os.path.join(LOG_DIR, f"cvd_{SYMBOL}_{INTERVAL_LABEL}_{date_str}.jsonl")

def append_log(bar):
    try:
        with open(log_path(TODAY_STR), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": bar["ts"], "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"],
                "buy_vol": bar["buy_vol"], "sell_vol": bar["sell_vol"], "delta": bar["delta"],
                "cvd": bar["cvd"], "cvd_open": bar["cvd_open"],
                "cvd_high": bar["cvd_high"], "cvd_low": bar["cvd_low"],
            }) + "\n")
        return True, None
    except Exception as e:
        return False, str(e)[:60]

def load_log(date_str):
    bars = []
    try:
        with open(log_path(date_str), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    bars.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return bars

# ── HISTORICAL BACKFILL (Kraken REST only — see docstring for why) ─────────
KRAKEN_REST_PAIRS = {"ETH": "ETHUSD", "BTC": "XBTUSD"}
KRAKEN_TRADES_URL = "https://api.kraken.com/0/public/Trades"

def fetch_kraken_trades_range(pair, since_ts, until_ts, progress=None):
    """Page forward through Kraken's public Trades endpoint from since_ts up
    to until_ts, returning a chronological list of (ts, price, qty, is_buy)
    tuples. Real historical prints, not synthetic — Kraken's public trade
    history goes back arbitrarily far for major pairs. Dedupes the one-trade
    overlap the API leaves at each page boundary. until_ts may be in the
    future (e.g. "now") — pagination just naturally stops once it catches up
    to whatever's actually been traded so far."""
    since = int(since_ts * 1e9)
    out = []
    last_id = None
    for page in range(2000):   # safety cap, not expected to be hit
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
                continue   # boundary overlap with the previous page
            last_id = tid
            ts = float(t[2])
            if ts > until_ts:
                hit_end = True
                break
            out.append((ts, float(t[0]), float(t[1]), t[3] == "b"))
        if progress:
            progress(len(out), datetime.fromtimestamp(trades[-1][2]))
        if hit_end or trades[-1][2] >= until_ts:
            break
        since = int(d["result"]["last"])
        time.sleep(0.25)   # polite pacing on the public endpoint
    return out

def fetch_kraken_trades_since(pair, hours, progress=None):
    """Trailing-window convenience wrapper: from `hours` ago up to right now."""
    now = time.time()
    return fetch_kraken_trades_range(pair, now - hours * 3600, now, progress=progress)

def backfill_kraken(hours, progress=None, reset=True):
    """Reconstruct bars/CVD from real Kraken trade history before live feeds
    start. Runs the exact same ingest_trade() path as live trades, so the
    resulting bars are exact, not approximated — just single-exchange for
    this stretch (Phemex has no historical trades API, see docstring).

    reset=True (default) truncates today's on-disk log and clears in-memory
    state first, so the backfill always supersedes whatever partial/stray log
    might already exist for today — "resume" only ever existed as a
    workaround for data sources with NO real history API (gex.py's Deribit/
    CBOE); Kraken actually has one, so a fresh reconstruction is strictly
    better than resuming a stale local file. Nothing is touched until the
    fetch actually returns data, so a network hiccup can't wipe real history."""
    pair = KRAKEN_REST_PAIRS[SYMBOL]
    trades = fetch_kraken_trades_since(pair, hours, progress=progress)
    if not trades:
        return 0
    if reset:
        try:
            open(log_path(TODAY_STR), "w", encoding="utf-8").close()
        except Exception:
            pass
        if HEADLESS:
            # truncate the 1m CSV for every day this backfill window touches,
            # so re-running headless with backfill on doesn't duplicate rows
            # already written by a prior run
            for d in {TODAY_STR, datetime.fromtimestamp(time.time() - hours * 3600).strftime("%m_%d_%Y")}:
                try:
                    open(csv_path(d), "w", encoding="utf-8").close()
                except Exception:
                    pass
    with state.lock:
        if reset:
            state.history.clear()
            state.live = None
            state.raw_cvd = 0.0
            state.cvd_offset = 0.0
            state.log_rows = 0
            state.log_err = None
            state.pending_shift = 0
            state.history_loading_older = False
            csv_state.live = None
            csv_state.day_str = None
        for ts, price, qty, is_buy in trades:
            ingest_trade(ts, price, qty, is_buy)
        state.backfill_boundary_ts = trades[-1][0]
    return len(trades)

def initialize_today(progress=None):
    """Populate state for a live (non --date) launch, OR to rebuild state
    after an in-app interval switch (see switch_interval()): backfill takes
    priority whenever it succeeds (see backfill_kraken docstring for why),
    falling back to resuming today's existing log only when backfill is
    disabled (--backfill-hours 0) or the fetch came back empty. Returns a
    short status string for the caller to display/print.
    Locked even in the fallback path — harmless at cold start (nothing else
    is running yet) but required when called mid-session, since the live WS
    threads are already mutating state.history/live/raw_cvd concurrently."""
    if BACKFILL_HOURS > 0:
        n = backfill_kraken(BACKFILL_HOURS, progress=progress, reset=True)
        if n:
            return f"backfilled {n} Kraken trades since {datetime.fromtimestamp(time.time() - BACKFILL_HOURS*3600).strftime('%H:%M:%S')}"
    existing = load_log(TODAY_STR)
    if existing:
        with state.lock:
            for row in existing:
                state.history.append(row)
            state.raw_cvd = state.history[-1]["cvd"]
        return f"resumed {len(existing)} bars from today's log (backfill unavailable)"
    return "starting fresh — no backfill, no existing log"

def switch_interval(label, mode, secs, threshold, progress=None):
    """Change bar shape (time or volume) while the app keeps running. The
    live WS feeds (Phemex + Kraken) are untouched — they keep streaming
    trades the whole time; only the bar-shape globals change and the bar
    history is rebuilt from scratch for the new shape via the exact same
    path used at cold start (initialize_today: backfill from 19:00 CT if
    enabled, else resume/fresh-start), so continuity is preserved the same
    way a fresh launch with a different --interval would behave.
    Must clear ALL bar-shape-dependent state before initialize_today runs —
    its resume-fallback path only appends, it doesn't clear, and the old
    interval's bars are still sitting in state.history from before the
    switch."""
    global INTERVAL_LABEL, BAR_MODE, BAR_SECS, VOL_THRESHOLD
    with state.lock:
        state.history.clear()
        state.live = None
        state.raw_cvd = 0.0
        state.cvd_offset = 0.0
        state.log_rows = 0
        state.log_err = None
        state.backfill_boundary_ts = None
        state.pending_shift = 0
        state.history_loading_older = False
        INTERVAL_LABEL, BAR_MODE, BAR_SECS, VOL_THRESHOLD = label, mode, secs, threshold
    return initialize_today(progress=progress)

# ── SHARED STATE ─────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.history = []                              # closed bars, dicts — plain list,
                                                         # no maxlen: Goto/backward-extend
                                                         # prepend older bars, and a bounded
                                                         # deque would silently evict live/
                                                         # recent data off the OTHER end
        self.live = None                              # currently-forming bar
        self.raw_cvd = 0.0                             # never-reset running sum
        self.cvd_offset = 0.0                          # [Z] rebases display from here
        self.log_rows = 0
        self.log_err = None
        self.phemex_status = "connecting…"
        self.kraken_status = "connecting…"
        self.last_price = None
        self.session = 0
        self.backfill_boundary_ts = None   # set once: everything <= this is Kraken-only
        self.history_loading_older = False   # guards extend_history_backward from overlapping itself
        self.pending_shift = 0    # bars just prepended by extend_history_backward,
                                  # not yet folded into the caller's view index
        self.goto_label = None   # last [G] target, for the header display only

state = State()

def _bucket_ts(ts):
    return int(ts // BAR_SECS * BAR_SECS)

def _new_bar(ts, price, cvd_open):
    """cvd_open is the running displayed CVD carried over from the previous
    bar's close (or 0 at the very start of a session) — makes the CVD panel
    a proper OHLC-style candle series (open/high/low/close), not just one
    point per bar, exactly mirroring the price panel's candles."""
    return {"ts": ts, "o": price, "h": price, "l": price, "c": price,
            "buy_vol": 0.0, "sell_vol": 0.0, "delta": 0.0,
            "cvd_open": cvd_open, "cvd_high": cvd_open, "cvd_low": cvd_open, "cvd": cvd_open}

def _close_live():
    """Finalize state.live into history + log. Caller holds state.lock and
    replaces state.live afterward (or sets it to None)."""
    live = state.live
    state.history.append(live)
    ok, err = append_log(live)
    if ok:
        state.log_rows += 1
        state.log_err = None
    else:
        state.log_err = err

def ingest_trade(ts, price, qty, is_buy):
    """Fold one trade print (from either exchange, or historical backfill)
    into the live bar / CVD. Must be called with state.lock held."""
    live = state.live
    cvd_open = state.raw_cvd - state.cvd_offset

    if BAR_MODE == "time":
        bucket_ts = _bucket_ts(ts)
        if live is None:
            state.live = _new_bar(bucket_ts, price, cvd_open)
            live = state.live
        elif bucket_ts > live["ts"]:
            _close_live()
            state.live = _new_bar(bucket_ts, price, cvd_open)
            live = state.live
        elif bucket_ts < live["ts"]:
            # late/out-of-order print for an already-closed bucket — ignore
            # rather than reopening a bar that's already been logged
            return
    else:
        if live is None:
            state.live = _new_bar(ts, price, cvd_open)
            live = state.live

    live["h"] = max(live["h"], price)
    live["l"] = min(live["l"], price)
    live["c"] = price
    if is_buy:
        live["buy_vol"] += qty
        state.raw_cvd += qty
    else:
        live["sell_vol"] += qty
        state.raw_cvd -= qty
    live["delta"] = live["buy_vol"] - live["sell_vol"]
    state.last_price = price

    disp_cvd = state.raw_cvd - state.cvd_offset
    live["cvd"] = disp_cvd
    live["cvd_high"] = max(live["cvd_high"], disp_cvd)
    live["cvd_low"] = min(live["cvd_low"], disp_cvd)

    if HEADLESS:
        # independent, always-1m OHLC+CVD accumulator feeding the headless
        # CSV log — separate from the --interval bar builder above (which
        # may be volume bars or a different time period)
        ingest_trade_csv(ts, price, qty, is_buy)

    if BAR_MODE == "volume" and (live["buy_vol"] + live["sell_vol"]) >= VOL_THRESHOLD:
        # trade that crosses the threshold closes its own bar in full (not
        # split) — the next trade opens a fresh one
        _close_live()
        state.live = None

# ── HEADLESS CSV LOG (fixed 1m OHLC + CVD, independent of --interval) ──────
CSV_BAR_SECS = 60
CSV_HEADER = ["timestamp", "datetime", "open", "high", "low", "close",
              "buy_vol", "sell_vol", "delta", "cvd_open", "cvd_high", "cvd_low", "cvd_close"]

class CsvState:
    def __init__(self):
        self.live = None      # currently-forming 1m bar for the CSV
        self.day_str = None   # MM_DD_YYYY of the file csv.live currently belongs to

csv_state = CsvState()

def csv_path(date_str):
    return os.path.join(LOG_DIR, f"cvd_1m_{SYMBOL}_{date_str}.csv")

def _new_csv_bar(ts, price, cvd_open):
    return {"ts": ts, "o": price, "h": price, "l": price, "c": price,
            "buy_vol": 0.0, "sell_vol": 0.0, "delta": 0.0,
            "cvd_open": cvd_open, "cvd_high": cvd_open, "cvd_low": cvd_open, "cvd": cvd_open}

def _csv_append(bar, day_str):
    path = csv_path(day_str)
    is_new = (not os.path.exists(path)) or os.path.getsize(path) == 0
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(CSV_HEADER)
            w.writerow([
                bar["ts"], datetime.fromtimestamp(bar["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
                bar["o"], bar["h"], bar["l"], bar["c"],
                bar["buy_vol"], bar["sell_vol"], bar["delta"],
                bar["cvd_open"], bar["cvd_high"], bar["cvd_low"], bar["cvd"],
            ])
        return True, None
    except Exception as e:
        return False, str(e)[:60]

def ingest_trade_csv(ts, price, qty, is_buy):
    """Fixed 1m OHLC(spot)+CVD accumulator for the --headless CSV log, fed by
    the same trades (live + backfilled) as the main --interval bar builder,
    but always 1m regardless of --interval — headless data collection wants
    a stable, familiar granularity, not whatever shape the interactive view
    happens to be using (which could be volume bars). Uses state.raw_cvd
    directly (not the offset-adjusted display value) since this CSV is a
    permanent record, unaffected by the interactive [Z] rebase key.
    Called from inside ingest_trade(), AFTER state.raw_cvd already reflects
    this trade — so the pre-trade baseline for a fresh bar's cvd_open is
    reconstructed by subtracting this trade's own signed qty back out."""
    day_str = datetime.fromtimestamp(ts).strftime("%m_%d_%Y")
    bucket_ts = int(ts // CSV_BAR_SECS * CSV_BAR_SECS)
    this_delta = qty if is_buy else -qty
    cvd_before = state.raw_cvd - this_delta
    live = csv_state.live

    if csv_state.day_str is not None and day_str != csv_state.day_str:
        # midnight rollover — flush whatever was forming into the OLD day's
        # file, then start clean on the new day (new file, per user request)
        if live is not None:
            _csv_append(live, csv_state.day_str)
        csv_state.live = None
        live = None
    csv_state.day_str = day_str

    if live is None:
        csv_state.live = _new_csv_bar(bucket_ts, price, cvd_before)
        live = csv_state.live
    elif bucket_ts > live["ts"]:
        _csv_append(live, csv_state.day_str)
        csv_state.live = _new_csv_bar(bucket_ts, price, cvd_before)
        live = csv_state.live
    elif bucket_ts < live["ts"]:
        return   # late/out-of-order print for an already-written minute — ignore

    live["h"] = max(live["h"], price)
    live["l"] = min(live["l"], price)
    live["c"] = price
    if is_buy:
        live["buy_vol"] += qty
    else:
        live["sell_vol"] += qty
    live["delta"] = live["buy_vol"] - live["sell_vol"]
    live["cvd"] = state.raw_cvd
    live["cvd_high"] = max(live["cvd_high"], state.raw_cvd)
    live["cvd_low"] = min(live["cvd_low"], state.raw_cvd)

def build_bars_from_trades(trades, cvd_baseline):
    """Pure bar-builder for [G]oto browsing — folds a chronological
    (ts, price, qty, is_buy) list into fully-closed bars (no dangling "live"
    bar; the last bar just ends wherever the fetched window ends) using the
    CURRENT BAR_MODE/BAR_SECS/VOL_THRESHOLD globals, seeded with
    cvd_baseline as the running CVD entering the first bar. Mirrors
    ingest_trade()'s bucketing logic exactly, but operates on a local
    list/dict instead of state.*/the on-disk log — doesn't touch live state
    at all, so goto browsing can never corrupt or be corrupted by the live
    feed. Returns (bars, final_raw_cvd) — the latter lets a forward-extend
    seed its own baseline for exact continuity without any shift needed."""
    bars = []
    live = None
    raw = cvd_baseline
    for ts, price, qty, is_buy in trades:
        cvd_open = raw
        if BAR_MODE == "time":
            bucket_ts = _bucket_ts(ts)
            if live is None:
                live = _new_bar(bucket_ts, price, cvd_open)
            elif bucket_ts > live["ts"]:
                bars.append(live)
                live = _new_bar(bucket_ts, price, cvd_open)
            elif bucket_ts < live["ts"]:
                continue
        else:
            if live is None:
                live = _new_bar(ts, price, cvd_open)
        live["h"] = max(live["h"], price)
        live["l"] = min(live["l"], price)
        live["c"] = price
        if is_buy:
            live["buy_vol"] += qty
            raw += qty
        else:
            live["sell_vol"] += qty
            raw -= qty
        live["delta"] = live["buy_vol"] - live["sell_vol"]
        live["cvd"] = raw
        live["cvd_high"] = max(live["cvd_high"], raw)
        live["cvd_low"] = min(live["cvd_low"], raw)
        if BAR_MODE == "volume" and (live["buy_vol"] + live["sell_vol"]) >= VOL_THRESHOLD:
            bars.append(live)
            live = None
    if live is not None:
        bars.append(live)
    return bars, raw

# ── GOTO + BACKWARD SCROLLBACK (one unified timeline, no separate buffer) ──
# Design: there is only ONE bar timeline, state.history/state.live — the same
# one the live WS feeds populate. Goto never creates a separate copy of it;
# it only (a) prepends older real Kraken history onto the SAME buffer if the
# requested date isn't covered yet, then (b) repositions view_end_idx/
# live_follow to look at that point. Scrolling forward from anywhere then
# just naturally reaches the live edge with zero special-casing — it's the
# exact same buffer the WS feeds keep extending forever, so there is nothing
# to "catch up to" or "exit out of". Scrolling backward past whatever's
# currently loaded (whether you got there via Goto or just panning) triggers
# the same extend_history_backward() regardless of how you got there.
GOTO_WINDOW_HOURS = 3.0     # size of each extend_history_backward chunk
GOTO_EDGE_TRIGGER = 20      # bars-from-edge that triggers an auto-extend fetch

def goto_to(target_ts, progress=None):
    """Ensure state.history/state.live covers target_ts, fetching real
    Kraken history backward from the current oldest bar if it doesn't
    already (same continuity-shift trick as extend_history_backward — a
    fresh fetch always starts its own local baseline at 0, so it gets
    rebased to join seamlessly with whatever's already loaded). If the
    target is already covered, this is a pure no-op — no fetch at all.
    since_ts is always that DATE's own 19:00 CT (not "whichever session is
    active right now" — see the historical note on this in git blame /
    memory: a bare date or an early time-of-day must not silently roll back
    to the previous day). Returns True if target_ts ends up covered
    (already, or after fetching), False if genuinely unavailable (e.g.
    before Kraken listed the pair)."""
    with state.lock:
        oldest_bar = state.history[0] if state.history else state.live
        oldest_ts = oldest_bar["ts"] if oldest_bar else None
    if oldest_ts is not None and target_ts >= oldest_ts:
        return True
    target_dt = datetime.fromtimestamp(target_ts)
    since_ts = datetime(target_dt.year, target_dt.month, target_dt.day, 19, 0, 0).timestamp()
    until_ts = oldest_ts if oldest_ts is not None else time.time()
    if since_ts >= until_ts:
        return oldest_ts is not None
    pair = KRAKEN_REST_PAIRS[SYMBOL]
    trades = fetch_kraken_trades_range(pair, since_ts, until_ts, progress=progress)
    new_bars, raw_final = build_bars_from_trades(trades, 0.0)
    if not new_bars:
        return False
    with state.lock:
        join_bar = state.history[0] if state.history else state.live
        join_baseline = join_bar["cvd_open"] if join_bar else (state.raw_cvd - state.cvd_offset)
        shift = join_baseline - raw_final
        for b in new_bars:
            b["cvd"] += shift
            b["cvd_open"] += shift
            b["cvd_high"] += shift
            b["cvd_low"] += shift
        state.history = new_bars + state.history
        state.pending_shift += len(new_bars)
    return True

def extend_history_backward():
    """Fetch the chunk immediately before the oldest currently-loaded bar and
    prepend it — triggered whenever panning gets near the start of
    state.history, regardless of whether that history got there via the
    initial 19:00 CT backfill, a [G]oto fetch, or a previous call to this
    same function. Runs in a background thread; safe to call even if it
    finds nothing (the boundary still advances so we don't re-fetch the
    same empty range forever)."""
    with state.lock:
        if state.history_loading_older or not (state.history or state.live):
            return
        state.history_loading_older = True
        oldest_bar = state.history[0] if state.history else state.live
        old_since = oldest_bar["ts"]
        join_baseline = oldest_bar["cvd_open"]
    pair = KRAKEN_REST_PAIRS[SYMBOL]
    new_since = old_since - GOTO_WINDOW_HOURS * 3600
    trades = fetch_kraken_trades_range(pair, new_since, old_since, progress=None)
    new_bars, raw_final = build_bars_from_trades(trades, 0.0)
    if new_bars:
        shift = join_baseline - raw_final
        for b in new_bars:
            b["cvd"] += shift
            b["cvd_open"] += shift
            b["cvd_high"] += shift
            b["cvd_low"] += shift
    with state.lock:
        state.history = new_bars + state.history
        state.history_loading_older = False
        state.pending_shift += len(new_bars)   # caller's view index must shift by this

# ── KRAKEN TRADE FEED ────────────────────────────────────────────────────────
def ws_kraken(session):
    pair = KRAKEN_WS_PAIRS[SYMBOL]

    def stale():
        return state.session != session

    def on_open(ws):
        ws.send(json.dumps({"method": "subscribe", "params": {"channel": "trade", "symbol": [pair]}}))
        with state.lock:
            if stale(): return
            state.kraken_status = "live"

    def on_message(ws, message):
        if stale(): return
        try:
            msg = json.loads(message)
        except Exception:
            return
        if msg.get("channel") != "trade":
            return
        # Only live increments — we never request snapshot:true, and (like
        # the Phemex handler) don't want any surprise historical replay
        # corrupting volume-bar ordering. Real history comes from backfill_kraken().
        if msg.get("type") != "update":
            return
        for t in msg.get("data", []):
            try:
                ts_str = t["timestamp"].replace("Z", "+00:00")
                ts = datetime.fromisoformat(ts_str).timestamp()
                price = float(t["price"])
                qty = float(t["qty"])
                is_buy = t["side"] == "buy"
            except Exception:
                continue
            with state.lock:
                if stale(): return
                ingest_trade(ts, price, qty, is_buy)
                state.kraken_status = "live"

    def on_error(ws, err):
        if stale(): return
        with state.lock:
            if stale(): return
            state.kraken_status = f"err: {str(err)[:30]}"

    def on_close(ws, code, msg):
        if stale(): return
        with state.lock:
            if stale(): return
            state.kraken_status = "reconnecting…"

    backoff = 1
    while not stale():
        ws_app = websocket.WebSocketApp(
            KRAKEN_WS_URL, on_open=on_open, on_message=on_message,
            on_error=on_error, on_close=on_close,
        )
        ws_app.run_forever(ping_interval=30, ping_timeout=10)
        if stale():
            break
        with state.lock:
            if stale(): break
            state.kraken_status = f"reconnecting… ({backoff}s)"
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

# ── PHEMEX TRADE FEED ────────────────────────────────────────────────────────
def ws_phemex(session):
    symbol = PHEMEX_SYMBOLS[SYMBOL]

    def stale():
        return state.session != session

    def on_open(ws):
        ws.send(json.dumps({"id": 1, "method": "trade_p.subscribe", "params": [symbol]}))
        with state.lock:
            if stale(): return
            state.phemex_status = "live"

    def on_message(ws, message):
        if stale(): return
        try:
            msg = json.loads(message)
        except Exception:
            return
        if msg.get("result") == {"status": "success"} or msg.get("result") == "pong":
            return
        # Phemex replays up to 1000 historical trades (newest-first) as a
        # "snapshot" on every (re)connect — not new prints. Ingesting it would
        # double-count volume on every reconnect and, worse, feed prints
        # out of chronological order into the bar builder (harmless for time
        # bars thanks to the late-print guard, but corrupts volume-bar
        # ordering outright). Real historical reach is handled separately by
        # backfill_kraken(); only fold in genuine live prints here.
        if msg.get("type") == "snapshot":
            return
        trades = msg.get("trades_p")
        if not trades:
            return
        if msg.get("symbol") and msg["symbol"] != symbol:
            return
        for row in trades:
            try:
                ts = row[0] / 1e9          # ns -> s
                is_buy = row[1] == "Buy"
                price = float(row[2])
                qty = float(row[3])
            except Exception:
                continue
            with state.lock:
                if stale(): return
                ingest_trade(ts, price, qty, is_buy)
                state.phemex_status = "live"

    def on_error(ws, err):
        if stale(): return
        with state.lock:
            if stale(): return
            state.phemex_status = f"err: {str(err)[:30]}"

    def on_close(ws, code, msg):
        if stale(): return
        with state.lock:
            if stale(): return
            state.phemex_status = "reconnecting…"

    _ping_ws = [None]
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

    threading.Thread(target=_heartbeat, daemon=True).start()

    backoff = 1
    while not stale():
        ws_app = websocket.WebSocketApp(
            PHEMEX_WS_URL, on_open=on_open, on_message=on_message,
            on_error=on_error, on_close=on_close,
        )
        _ping_ws[0] = ws_app
        ws_app.run_forever(ping_interval=25, ping_timeout=10)
        if stale():
            break
        with state.lock:
            if stale(): break
            state.phemex_status = f"reconnecting… ({backoff}s)"
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)
        _ping_stop.set()
        _ping_stop.clear()

# ── COLOUR PAIRS ─────────────────────────────────────────────────────────────
P_DEFAULT, P_DIM, P_CYAN, P_YELLOW, P_GREEN, P_RED, P_STATUS = range(1, 8)

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

def fmt_price(p):
    if p is None:
        return "—"
    return f"{p:,.2f}" if p >= 1 else f"{p:.5f}"

def fmt_qty(q):
    if abs(q) >= 1000:
        return f"{q/1000:,.1f}k"
    return f"{q:,.2f}"

def fmt_time(ts):
    fine = BAR_MODE == "volume" or BAR_SECS < 60
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S" if fine else "%H:%M")

def draw_candles(win, visible, rows, ohlc_fn, plot_w, zero_line=False, cursor_idx=-1, fmt_fn=None):
    """Shared OHLC candlestick renderer — used for both the price panel and
    the CVD panel (candles built from cvd_open/high/low/close) so the two
    panels share identical visual language. Returns (vmin, vmax) of the
    value range actually plotted, for the caller's axis-label ticks.

    cursor_idx (crosshair, like charthacker.py/quantasset_chart.py): if it
    names a visible column, that candle is highlighted yellow and a dotted
    vertical line is drawn through the whole panel at that column, plus a
    dotted horizontal line at the candle's close value with a value label on
    the right axis (fmt_fn formats it — price vs CVD panels format
    differently). Lines never overwrite an actual candle glyph — every cell
    a candle/wick touches is tracked in `occupied` and skipped.

    zero_line=True draws a dotted zero-reference line for the CVD panel, but
    (like the price panel) the vertical range is ALWAYS scaled purely to
    whatever's actually visible — it does NOT force 0 into the range. CVD can
    sit entirely far from zero (e.g. a session running +2000 the whole time
    visible on screen); forcing 0 into the range there would squash all the
    real variation into a sliver at the top/bottom instead of using the full
    panel height, exactly the "flat-looking" bug this fixed. The zero line
    itself is only drawn when 0 actually falls within that auto-scaled
    range — otherwise there's nothing meaningful to draw."""
    highs = [ohlc_fn(b)[1] for b in visible]
    lows  = [ohlc_fn(b)[2] for b in visible]
    vmax, vmin = max(highs), min(lows)
    if vmax == vmin:
        vmax += 1e-9
    n_rows = len(rows)

    def to_row(v):
        frac = (v - vmin) / (vmax - vmin)
        r = int(round((1 - frac) * (n_rows - 1)))
        return rows[max(0, min(n_rows - 1, r))]

    if zero_line and vmin <= 0.0 <= vmax:
        safe_add(win, to_row(0.0), 0, "·" * max(1, plot_w), cp(P_DIM))

    occupied = set()
    cursor_row = cursor_val = None
    for i, b in enumerate(visible):
        o, hi, lo, c = ohlc_fn(b)
        r_hi, r_lo, r_op, r_cl = to_row(hi), to_row(lo), to_row(o), to_row(c)
        is_cursor = (i == cursor_idx)
        up = c >= o
        color = cp(P_YELLOW, bold=True) if is_cursor else (cp(P_GREEN, bold=True) if up else cp(P_RED, bold=True))
        for r in range(r_hi, r_lo + 1):
            win.addch(r, i, ord("│"), cp(P_DIM))
            occupied.add((r, i))
        body_top, body_bot = min(r_op, r_cl), max(r_op, r_cl)
        for r in range(body_top, body_bot + 1):
            win.addch(r, i, ord("█"), color)
            occupied.add((r, i))
        if body_top == body_bot:
            win.addch(body_top, i, ord("─"), color)
            occupied.add((body_top, i))
        if is_cursor:
            cursor_row, cursor_val = r_cl, c

    if 0 <= cursor_idx < len(visible):
        for r in rows:
            if (r, cursor_idx) not in occupied:
                win.addch(r, cursor_idx, ord(":"), cp(P_YELLOW, dim=True))
    if cursor_row is not None:
        for c2 in range(plot_w):
            if (cursor_row, c2) not in occupied:
                win.addch(cursor_row, c2, ord("-"), cp(P_YELLOW, dim=True))
        if fmt_fn:
            safe_add(win, cursor_row, plot_w + 1, fmt_fn(cursor_val), cp(P_YELLOW, bold=True))

    return vmin, vmax

# ── DRAW ──────────────────────────────────────────────────────────────────
def draw(win, bars, live_bar, status_line, cursor_idx=-1):
    """Returns n_vis — the number of bars actually rendered this frame — so
    the caller can track it for the NEXT frame's crosshair/pan arithmetic
    (the exact visible count depends on terminal width, only known here)."""
    h, w = win.getmaxyx()
    win.erase()

    header = f" AGGREGATE CVD — {SYMBOL}  bar:{INTERVAL_LABEL}  "
    safe_add(win, 0, 0, header.ljust(w), cp(P_STATUS))

    all_bars = bars + ([live_bar] if live_bar else [])
    if len(all_bars) < 1:
        safe_add(win, h // 2, max(0, (w - 20) // 2), "waiting for trades…", cp(P_CYAN))
        win.noutrefresh()
        return 0

    axis_w = 12               # right-side price/cvd axis label gutter
    plot_w = max(1, w - axis_w)
    n = min(len(all_bars), plot_w)
    visible = all_bars[-n:]
    cursor_idx = cursor_idx if 0 <= cursor_idx < n else -1

    bottom_reserved = 2       # time axis row + status bar row
    top = 1
    divider_row = top + (h - top - bottom_reserved) * 55 // 100
    price_rows = list(range(top, divider_row))
    cvd_rows   = list(range(divider_row + 1, h - bottom_reserved))
    if not price_rows or not cvd_rows:
        win.noutrefresh()
        return n

    safe_add(win, divider_row, 0, ("─" * plot_w) + " CVD ".center(axis_w, "─"), cp(P_DIM))

    # ---- price panel ----
    def _price_ohlc(b):
        return b["o"], b["h"], b["l"], b["c"]

    pmin, pmax = draw_candles(win, visible, price_rows, _price_ohlc, plot_w,
                              cursor_idx=cursor_idx, fmt_fn=fmt_price)
    prows = len(price_rows)
    for tick_frac in (0.0, 0.5, 1.0):
        r = price_rows[int(round(tick_frac * (prows - 1)))]
        p = pmax - tick_frac * (pmax - pmin)
        safe_add(win, r, plot_w + 1, fmt_price(p), cp(P_DIM))

    # ---- CVD panel (candles, same shape as the price panel above) ----
    def _cvd_ohlc(b):
        c = b["cvd"] if b.get("cvd") is not None else state.raw_cvd - state.cvd_offset
        o = b.get("cvd_open", c)
        hi = b.get("cvd_high", max(o, c))
        lo = b.get("cvd_low", min(o, c))
        return o, hi, lo, c

    cmin, cmax = draw_candles(win, visible, cvd_rows, _cvd_ohlc, plot_w, zero_line=True,
                              cursor_idx=cursor_idx, fmt_fn=fmt_qty)
    crows = len(cvd_rows)
    for tick_frac in (0.0, 0.5, 1.0):
        r = cvd_rows[int(round(tick_frac * (crows - 1)))]
        v = cmax - tick_frac * (cmax - cmin)
        safe_add(win, r, plot_w + 1, fmt_qty(v), cp(P_DIM))

    # ---- time axis ----
    axis_row = h - bottom_reserved
    lbl_w = len(fmt_time(visible[0]["ts"]))
    step = max(lbl_w + 2, n // 6)   # never place labels closer than their own width
    last_end = -1
    for i in range(0, n, step):
        if i == cursor_idx:
            continue   # cursor gets its own highlighted label, drawn below
        lbl = fmt_time(visible[i]["ts"])
        x = max(0, i - len(lbl) // 2)
        if x <= last_end:      # still overlapping (narrow window) — skip
            continue
        safe_add(win, axis_row, x, lbl, cp(P_DIM))
        last_end = x + len(lbl)
    if cursor_idx >= 0:
        lbl = fmt_time(visible[cursor_idx]["ts"])
        safe_add(win, axis_row, max(0, cursor_idx - len(lbl) // 2), lbl, cp(P_YELLOW, bold=True))

    # ---- status bar ----
    if cursor_idx >= 0:
        sel = visible[cursor_idx]
        o, hi, lo, c = _price_ohlc(sel)
        co, ch_, cl, cc = _cvd_ohlc(sel)
        info = (f" [{fmt_time(sel['ts'])}] O:{fmt_price(o)} H:{fmt_price(hi)} "
                f"L:{fmt_price(lo)} C:{fmt_price(c)}  "
                f"CVD O:{fmt_qty(co)} H:{fmt_qty(ch_)} L:{fmt_qty(cl)} C:{fmt_qty(cc)} "
                f"Δ:{sel.get('delta', 0.0):+,.2f}  {status_line}")
    else:
        last_price = state.last_price
        last_cvd = _cvd_ohlc(visible[-1])[3] if visible else 0.0
        last_delta = visible[-1].get("delta", 0.0) if visible else 0.0
        info = (f" px:{fmt_price(last_price)}  CVD:{last_cvd:+,.2f}  Δbar:{last_delta:+,.2f}  "
                f"Phemex:{state.phemex_status}  Kraken:{state.kraken_status}  "
                f"log:{state.log_rows}  {status_line}")
    if state.backfill_boundary_ts:
        boundary = datetime.fromtimestamp(state.backfill_boundary_ts).strftime("%H:%M:%S")
        info += f"  |  Kraken-only before {boundary}, aggregate after"
    safe_add(win, h - 1, 0, info.ljust(w), cp(P_STATUS))
    return n

def _prompt_text(stdscr, prompt):
    """Blocking text-input line at the bottom of the screen — shared by the
    [I] interval prompt and the [G] goto prompt. Returns the typed string
    (stripped), or None if the user cancelled (Esc) or left it empty."""
    h, w = stdscr.getmaxyx()
    row = h - 1
    buf = ""
    curses.curs_set(1)
    stdscr.nodelay(False)
    try:
        while True:
            safe_add(stdscr, row, 0, (prompt + buf).ljust(w), cp(P_STATUS))
            stdscr.move(row, min(w - 1, len(prompt) + len(buf)))
            stdscr.refresh()
            ch = stdscr.getch()
            if ch == 27:
                return None
            elif ch in (curses.KEY_ENTER, 10, 13):
                break
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                buf = buf[:-1]
            elif 32 <= ch < 127:
                buf += chr(ch)
    finally:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)
    buf = buf.strip()
    return buf if buf else None

def _prompt_interval(stdscr):
    """[I] key — accepts exactly the same syntax as --interval (time label or
    a volume bar like 500V). Returns the parsed (label, mode, secs,
    threshold) tuple, or None if cancelled/empty/invalid."""
    buf = _prompt_text(stdscr, "New interval (e.g. 1m, 3m, 500V) — Enter=apply, Esc=cancel: ")
    return parse_interval(buf) if buf else None

def parse_goto_datetime(raw):
    """MM_DD_YYYY HH:MM, or just MM_DD_YYYY (time defaults to 00:00) — same
    date format as --date, with an optional time-of-day added. Also accepts
    / or - in place of _ as the date separator (MM_DD_YYYY is easy to
    mistype/forget when it's the only format the CLI's --date flag ever
    established) so a natural "07/03/2026" or "07-03-2026" isn't silently
    rejected. Returns None if truly unparseable — the caller is responsible
    for telling the user that, rather than silently leaving the view
    unchanged (see the [G] handler in curses_main)."""
    raw = raw.strip().replace("/", "_").replace("-", "_")
    for fmt in ("%m_%d_%Y %H:%M", "%m_%d_%Y"):
        try:
            return datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    return None

def _prompt_goto(stdscr):
    """[G] key — returns the raw typed string, or None if cancelled/empty.
    Deliberately does NOT parse here: a genuine cancel (Esc/empty) should be
    silent, but a typo'd date should tell the user it failed rather than
    silently leaving the view unchanged — the caller (curses_main) parses
    the returned string itself so it can tell the two apart and report the
    latter."""
    return _prompt_text(stdscr, "Goto date/time (MM_DD_YYYY [HH:MM]) — Enter=apply, Esc=cancel: ")

def _move_cursor(step, cursor_idx, view_end_idx, total, n_vis, allow_exit_to_live):
    """Crosshair + pan, same model as charthacker.py/quantasset_chart.py: a
    plain pan step first moves the cursor WITHIN the currently-visible
    window; only once the cursor would go past either edge of that window
    does it trigger panning the underlying view, re-pinning the cursor to
    the edge it just reached. Returns (new_cursor_idx, new_view_end_idx,
    exited_to_live) — exited_to_live is only ever True when
    allow_exit_to_live and panning right catches all the way up to `total`."""
    move = abs(step)
    if step < 0:
        if cursor_idx < 0:
            cursor_idx = max(0, n_vis - 1)   # activate at the rightmost visible bar
        if cursor_idx >= move:
            cursor_idx -= move
        else:
            overflow = move - cursor_idx
            view_end_idx = max(n_vis if n_vis else 1, view_end_idx - overflow)
            cursor_idx = 0
        return cursor_idx, view_end_idx, False
    else:
        if cursor_idx < 0:
            if view_end_idx >= total:
                return cursor_idx, view_end_idx, False   # genuinely at the edge, nothing to do
            # cursor_idx==-1 does NOT always mean "at the edge" — a Goto view can open
            # centered well short of `total` with no cursor active yet. Activate at the
            # left edge of the current view and fall through, rather than silently
            # no-op'ing a forward pan that should actually do something.
            cursor_idx = 0
        room_right = max(0, n_vis - 1) - cursor_idx
        if move <= room_right:
            return cursor_idx + move, view_end_idx, False
        overflow = move - room_right
        new_view_end = min(total, view_end_idx + overflow)
        if allow_exit_to_live and new_view_end >= total:
            return -1, total, True
        return max(0, n_vis - 1), new_view_end, False

# ── CURSES MAIN ──────────────────────────────────────────────────────────
def curses_main(stdscr):
    global INTERVAL_LABEL, BAR_MODE, BAR_SECS, VOL_THRESHOLD
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(200)
    init_colors()

    live_follow = True
    view_end_idx = 0
    cursor_idx = -1     # crosshair: -1 = none (pure live / plain view)
    last_n_vis = 0      # bars actually rendered last frame — for cursor activation/pan math

    if HISTORICAL_MODE:
        for row in load_log(VIEW_DATE):
            state.history.append(row)
        status_line = "history mode — no live feed"
    else:
        def _progress(n, last_dt):
            h, w = stdscr.getmaxyx()
            msg = f"Backfilling {BACKFILL_HOURS:g}h from Kraken… {n} trades ({last_dt.strftime('%H:%M:%S')})"
            stdscr.erase()
            safe_add(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg, cp(P_CYAN))
            stdscr.refresh()
        init_status = initialize_today(progress=_progress)
        init_shown_at = time.time()
        state.session += 1
        session = state.session
        threading.Thread(target=ws_kraken, args=(session,), daemon=True).start()
        threading.Thread(target=ws_phemex, args=(session,), daemon=True).start()
        status_line = init_status

    while True:
        key = stdscr.getch()
        if key in (ord('q'), ord('Q'), 27):
            break
        elif key in (ord('z'), ord('Z')):
            with state.lock:
                state.cvd_offset = state.raw_cvd
        elif key in PAN_KEYS and not HISTORICAL_MODE:
            step = PAN_KEYS[key]
            with state.lock:
                total = len(state.history) + (1 if state.live else 0)
            if step < 0 and live_follow:
                view_end_idx = total
                live_follow = False
            cursor_idx, view_end_idx, exited = _move_cursor(
                step, cursor_idx, view_end_idx, total, last_n_vis, allow_exit_to_live=True)
            if exited:
                live_follow = True
        elif key in (curses.KEY_END, ord('l'), ord('L')):
            live_follow = True
            cursor_idx = -1
        elif key in (ord('g'), ord('G')) and not HISTORICAL_MODE:
            raw_goto = _prompt_goto(stdscr)
            if raw_goto is not None:
                target_ts = parse_goto_datetime(raw_goto)
                if target_ts is None:
                    status_line = f"Invalid date/time '{raw_goto}' — use MM_DD_YYYY [HH:MM]"
                    init_shown_at = time.time()
                else:
                    def _goto_progress(n, last_dt):
                        hh, ww = stdscr.getmaxyx()
                        tgt = datetime.fromtimestamp(target_ts).strftime('%m/%d/%Y %H:%M')
                        msg = f"Loading {tgt}… {n} trades ({last_dt.strftime('%H:%M:%S')})"
                        stdscr.erase()
                        safe_add(stdscr, hh // 2, max(0, (ww - len(msg)) // 2), msg, cp(P_CYAN))
                        stdscr.refresh()
                    ok = goto_to(target_ts, progress=_goto_progress)
                    tgt_label = datetime.fromtimestamp(target_ts).strftime('%m/%d/%Y %H:%M')
                    if ok:
                        with state.lock:
                            tss = [b["ts"] for b in state.history] + ([state.live["ts"]] if state.live else [])
                        total = len(tss)
                        raw_idx = bisect.bisect_left(tss, target_ts)
                        if raw_idx <= 0 or raw_idx >= total:
                            # target_ts falls entirely outside what's loaded — most
                            # commonly because no time-of-day was given (defaults to
                            # 00:00, before that day's own 19:00 CT start) or an early
                            # time was given. Centering on it would clip to a tiny
                            # sliver right at whichever edge bisect landed on instead
                            # of showing the day the user actually asked for.
                            view_end_idx = total
                            live_follow = True
                        else:
                            view_end_idx = min(total, raw_idx + 1 + 40)   # roughly centered
                            live_follow = (view_end_idx >= total)
                        state.goto_label = tgt_label
                        status_line = f"Goto {tgt_label}"
                        cursor_idx = -1
                    else:
                        status_line = f"No Kraken data found around {tgt_label}"
                    init_shown_at = time.time()
        elif key in (ord('i'), ord('I')):
            parsed = _prompt_interval(stdscr)
            if parsed is not None:
                label, mode, secs, threshold = parsed
                if HISTORICAL_MODE:
                    INTERVAL_LABEL, BAR_MODE, BAR_SECS, VOL_THRESHOLD = label, mode, secs, threshold
                    state.history.clear()
                    rows = load_log(VIEW_DATE)
                    for row in rows:
                        state.history.append(row)
                    status_line = (f"loaded {len(rows)} bars for {label} (history)" if rows
                                   else f"no {label} log found for {VIEW_DATE}")
                else:
                    def _switch_progress(n, last_dt):
                        hh, ww = stdscr.getmaxyx()
                        msg = f"Switching to {label}… backfilling… {n} trades ({last_dt.strftime('%H:%M:%S')})"
                        stdscr.erase()
                        safe_add(stdscr, hh // 2, max(0, (ww - len(msg)) // 2), msg, cp(P_CYAN))
                        stdscr.refresh()
                    status_line = switch_interval(label, mode, secs, threshold, progress=_switch_progress)
                live_follow = True
                view_end_idx = 0
                cursor_idx = -1
                init_shown_at = time.time()

        if not HISTORICAL_MODE:
            with state.lock:
                shift = state.pending_shift
                state.pending_shift = 0
            view_end_idx += shift
            # "near the old edge" means the OLDEST VISIBLE bar (view_end_idx -
            # last_n_vis, i.e. the left edge of the on-screen window) is close
            # to index 0 of the whole buffer — NOT that view_end_idx itself is
            # small. _move_cursor clamps view_end_idx to never go below
            # last_n_vis (a full screen's worth of bars, e.g. 60-150+), which
            # is normally much bigger than GOTO_EDGE_TRIGGER (20) — checking
            # view_end_idx directly against that threshold could only ever
            # fire once, right at the very start when the whole buffer itself
            # has under ~20 bars, then never again no matter how far back you
            # scroll afterward. This was the actual reason it stopped extending
            # after one chunk instead of continuing indefinitely.
            with state.lock:
                near_old = not state.history_loading_older and (view_end_idx - last_n_vis) <= GOTO_EDGE_TRIGGER
            if near_old:
                threading.Thread(target=extend_history_backward, daemon=True).start()

        with state.lock:
            all_bars = list(state.history) + ([state.live] if state.live else [])
            if live_follow or HISTORICAL_MODE:
                cur_bars = all_bars
            else:
                cur_bars = all_bars[:view_end_idx]
            cur_error = state.log_err

        if not cur_bars:
            draw_bars, draw_live = [], None
        else:
            draw_bars = cur_bars[:-1] if (state.live and cur_bars and cur_bars[-1] is state.live) else cur_bars
            draw_live = state.live if (state.live and cur_bars and cur_bars[-1] is state.live) else None

        if HISTORICAL_MODE:
            sl = status_line
        else:
            sl = "● LIVE" if live_follow else f"⏸ paused ({len(cur_bars)}/{len(all_bars)})"
            if time.time() - init_shown_at < 15:
                sl = f"{status_line}  |  {sl}"
        if cur_error:
            sl += f"  ⚠ log: {cur_error}"

        last_n_vis = draw(stdscr, draw_bars, draw_live, sl, cursor_idx=cursor_idx) or 0
        curses.doupdate()

# ── HEADLESS ────────────────────────────────────────────────────────────
def headless_main():
    print(f"cvd.py headless logger — {SYMBOL} @ {INTERVAL_LABEL} -> {log_path(TODAY_STR)}")
    print(f"1m OHLC+CVD CSV -> {csv_path(TODAY_STR)} (new file each day at 00:00 CT)")
    print("Ctrl+C to stop.")
    def _progress(n, last_dt):
        print(f"  backfilling… {n} trades ({last_dt.strftime('%H:%M:%S')})", end="\r")
    init_status = initialize_today(progress=_progress)
    print(f"\n{init_status} ({state.log_rows} bars logged) — live from here on.")
    state.session += 1
    session = state.session
    threading.Thread(target=ws_kraken, args=(session,), daemon=True).start()
    threading.Thread(target=ws_phemex, args=(session,), daemon=True).start()

    last_logged = state.log_rows
    while True:
        time.sleep(2)
        with state.lock:
            rows = state.log_rows
            last_price = state.last_price
            cvd = state.raw_cvd - state.cvd_offset
        if rows != last_logged:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] bars={rows} px={fmt_price(last_price)} cvd={cvd:+,.2f}")
            last_logged = rows

def main():
    if HISTORICAL_MODE and HEADLESS:
        print("--headless has nothing to do with --date (playback is instant, no live feed)")
        sys.exit(1)
    if HEADLESS:
        try:
            headless_main()
        except KeyboardInterrupt:
            pass
        print("\ncvd.py headless logger — stopped.")
        return
    try:
        curses.wrapper(curses_main)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
