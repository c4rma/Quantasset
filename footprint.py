#!/usr/bin/env python3
"""
footprint.py — terminal bid x ask footprint (order-flow) chart, crypto only (v1)

Usage:
  python footprint.py [ETH|BTC] [--interval <N>s|<N>m|<N>V] [--tick N]
                       [--imbalance N] [--stack N] [--min-imbalance-vol N]
                       [--big-trade-size N] [--backfill-hours N]
                       [--date MM_DD_YYYY] [--headless]

--interval takes any of three forms (trailing letter picks the unit, N is
any positive number): "<N>s" seconds (e.g. 45s), "<N>m" minutes (e.g. 5m,
0.5m), or "<N>V" a VOLUME bar — closes once combined buy+sell volume within
it reaches N units of the base asset instead of on a wall-clock boundary
(e.g. 500V — same convention as cvd.py's volume bars in this repo). Default
5m, --tick 1.00, --imbalance 3.0 (300%), --stack 3, --min-imbalance-vol 0,
--big-trade-size 100.

[I] opens a text prompt to change the bar interval in-app (same <N>s/<N>m/
<N>V syntax as --interval) without restarting — types the new interval once
and switches directly to it, rather than cycling through a fixed list and
waiting for a full backfill at every interval in between just to reach the
one you actually wanted. Rebuilds history the same way a fresh launch with
that --interval would. Everything else in this file works identically
regardless of how the interval was set (imbalance/stack/POC/grid-widening
all operate the same on a volume bar's levels as a time bar's).

[T] opens the same kind of text prompt for the price increment (--tick) —
type any positive $ amount (e.g. "0.25", "1", "10") and it rebuilds history
at that resolution, same mechanism as [I]. Every bar's price levels are
bucketed at a specific $ granularity, so changing it always triggers a
fresh rebuild rather than trying to re-bucket already-aggregated data.

[M] opens a text prompt to change the diagonal-imbalance ratio (--imbalance,
default 3.0 = 300%) — any number > 1.0. [B] opens the same kind of prompt
for the Big Trades filter size (--big-trade-size, default 100) — any
positive number. Unlike [I]/[T], neither of these needs a rebuild: both are
read fresh from already-accumulated bar data on the very next frame, so they
apply instantly (and work in --date historical playback too, not just live).

What it shows, per price level per bar:
  "<bid> x <ask>"   bid = aggressive SELL volume that hit the bid at that
                     price, ask = aggressive BUY volume that lifted the ask.
                     Only the IMBALANCED side of a cell is ever coloured —
                     everything else stays plain white/default.
  Diagonal imbalance (classic footprint-chart definition, compared
  top-right to bottom-left): ask volume at price P vs bid volume one tick
  BELOW P (buy imbalance — the ask number turns green-bold) — or bid volume
  at price P vs ask volume one tick ABOVE P (sell imbalance — the bid number
  turns red-bold). Ratio >= --imbalance (default 300%).
  Stacked imbalance: 3+ (--stack) consecutive price levels all imbalanced
  the same direction — same green/red colouring, plus reverse-video to make
  the run stand out from an isolated single-level imbalance.
  POC (Point of Control): the price level with the most total volume
  (bid+ask) in that bar — marked with a yellow ◆ in the left gutter and the
  whole "<bid> x <ask>" cell underlined, regardless of imbalance status.
  Big Trades filter (--big-trade-size, default 100, [B] to change): any
  bid/ask number whose OWN volume is >= the threshold turns cyan-bold —
  independent of imbalance, though an imbalanced number that's ALSO a big
  trade keeps its green/red imbalance colouring (imbalance is the rarer,
  more specific signal and takes priority when both apply to one number).
  Live price line/box: a "▶<price>" tag always shown on the axis at the
  current live price's row (exact price, not rounded to TICK), plus a
  dashed line at that row across the rest of the chart — drawn only through
  cells with no real data at that exact price, so it never overwrites an
  actual bid/ask number. Stays visible even scrolled back into history (as
  long as that price falls within the currently visible vertical window),
  so you can see where the live price sits relative to whatever you're
  looking at.

Price scale is $1.00 (--tick) increments by default — but if any SINGLE bar
currently on screen has its own traded (high-low) range too tall to show at
that resolution, the grid automatically widens (adjacent $1 levels
merged/summed) just enough to fit that candle without clipping; shown in the
header as "grid:$N" whenever this is active. This is deliberately based on
one bar's own range, not the combined span across every visible bar — a
longer --interval (10m/15m) naturally means more ordinary price drift adds
up across several columns even when no single candle is unusual, and that
should NOT widen the grid on its own. The vertical WINDOW itself is always a
fixed height (fills the terminal) — only the per-row $ amount ever adapts,
never the window size (an auto-fit-the-whole-window version was tried and
reverted: it left most of the screen blank whenever the traded range was
smaller than the terminal).

Vertical centering follows what's actually on screen, not a fixed target —
always the MIDPOINT of the traded range across every currently visible bar
(live edge included), not just the rightmost one and not the tick-by-tick
last trade price. Centering on the raw last price flickered (it updates on
every single print, shifting the whole window every frame even within an
already-established range) and, like centering on only the rightmost bar,
skewed the window toward wherever that one reference point sits, leaving
other visible bars at a different price trailing off-center and clipped
instead of evenly fit in frame. Midpoint-of-visible-levels only moves when
a bar's range genuinely extends to a new high/low, so panning through
history doesn't also require manually readjusting [↑/↓] every time just to
keep everything in view. The price scale/resolution itself never changes
because of this, only where the window is centered. [↑/↓]/[PgUp/PgDn] override this with a
manual pan (any press disables auto-centering until re-enabled). [Home]/[L]
re-enables it AND returns to the live edge; [C] re-enables it WITHOUT
moving your horizontal position — useful if you're deliberately scrolled
back through history and just want vertical auto-centering back on for
wherever you currently are, without being yanked back to live.

Loads real trade history from the PREVIOUS day's 00:00 CT (midnight) on
startup — always a full day back, regardless of what time it currently is —
via Kraken + Coinbase REST backfill (Phemex has no historical trades API —
live only), same triple-exchange aggregate convention as cvd.py in this
repo. Live prices stream from Phemex + Kraken + Coinbase WebSocket feeds.

The initial backfill is capped at ~30s so the app is usable reasonably
quickly, not a hard cutoff on how far back you can go: scrolling ([←/→]/
[[/]]) near the oldest loaded bar transparently fetches more real history in
the background and prepends it — keep scrolling left and it keeps loading,
same lazy-load-on-demand convention cvd.py uses.

Navigation: [←/→] pan time 1 bar, [[/]] pan time 10 bars, [↑/↓] pan price,
[PgUp/PgDn] pan price (bigger step), [Home]/[L] return to live, [C] re-center
vertically without leaving your current scroll position, [I] change bar
interval, [T] change price increment, [M] change imbalance ratio, [B] change
Big Trades size, [P] screenshot, [Q]/Esc quit.

v1 scope (crypto only — equities are a planned follow-up once this is
confirmed working): no crosshair/goto.
"""

import sys
import os
import time
import json
import locale
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

try:
    locale.setlocale(locale.LC_ALL, "")
except Exception:
    pass

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

# ── ARG PARSING ──────────────────────────────────────────────────────────────
args = sys.argv[1:]
if "-h" in args or "--help" in args:
    print(__doc__.strip())
    sys.exit(0)
HEADLESS = "--headless" in args
args = [a for a in args if a != "--headless"]

def parse_interval(raw):
    """Decode a bar-interval spec into (label, mode, secs, threshold), or
    None if invalid. Three forms, matched by the trailing unit letter:
      "<N>s" -> time bar, N seconds (any positive number, e.g. "45s")
      "<N>m" -> time bar, N minutes (e.g. "5m", "10m", "0.5m")
      "<N>V" -> volume bar, closes once combined buy+sell volume in it
                reaches N units of the base asset (e.g. "500V") — same
                convention as cvd.py's volume bars in this repo.
    Shared by the CLI --interval flag and the in-app [I] prompt so both
    accept exactly the same syntax."""
    if not raw or len(raw) < 2:
        return None
    unit = raw[-1].lower()
    num_part = raw[:-1]
    try:
        num = float(num_part)
    except ValueError:
        return None
    if num <= 0:
        return None
    if unit == "s":
        secs = int(round(num))
        if secs < 1:
            return None
        return f"{num_part}s", "time", secs, None
    if unit == "m":
        secs = int(round(num * 60))
        if secs < 1:
            return None
        return f"{num_part}m", "time", secs, None
    if unit == "v":
        return f"{num_part}V", "volume", None, num
    return None

INTERVAL_LABEL = "5m"
if "--interval" in args:
    i = args.index("--interval")
    try:
        lbl = args[i + 1]
    except IndexError:
        print("--interval requires a value"); sys.exit(1)
    parsed = parse_interval(lbl)
    if parsed is None:
        print("--interval must be like <N>s, <N>m, or <N>V (e.g. 45s, 5m, 500V)")
        sys.exit(1)
    INTERVAL_LABEL = parsed[0]
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]
_, BAR_MODE, BAR_SECS, VOL_THRESHOLD = parse_interval(INTERVAL_LABEL)

CRYPTO_SYMBOLS = {"ETH", "BTC"}
SYMBOL = args[0].upper() if args and not args[0].startswith("--") else "BTC"
if args and not args[0].startswith("--"):
    args = args[1:]
if SYMBOL not in CRYPTO_SYMBOLS:
    print(f"'{SYMBOL}' isn't supported yet — footprint.py v1 is crypto-only ({', '.join(sorted(CRYPTO_SYMBOLS))}).")
    print("Equity/ETF footprint support is a planned follow-up once crypto is confirmed working.")
    sys.exit(1)

def parse_tick(raw):
    """Decode a --tick/[T]-prompt price-increment string into a positive
    float, or None if invalid. Accepts a bare number and tolerates an
    optional leading "$" (e.g. "0.25", "$0.25", "10") — shared by the CLI
    --tick flag and the in-app [T] prompt so both accept the same syntax."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("$"):
        raw = raw[1:]
    try:
        val = float(raw)
    except ValueError:
        return None
    return val if val > 0 else None

TICK = 1.0   # $1.00 increments by default; the display grid widens on its
             # own (see draw()'s group_size) only when a bar's actual range
             # wouldn't fit on screen at this resolution — --tick overrides
             # this base resolution, not the adaptive widening.
if "--tick" in args:
    i = args.index("--tick")
    try:
        raw_tick = args[i + 1]
    except IndexError:
        print("--tick requires a value"); sys.exit(1)
    parsed_tick = parse_tick(raw_tick)
    if parsed_tick is None:
        print("--tick requires a positive number (e.g. 1, 0.25, 10)"); sys.exit(1)
    TICK = parsed_tick
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]

def parse_imbalance_ratio(raw):
    """Decode a --imbalance/[M]-prompt ratio string into a float > 1.0, or
    None if invalid — shared by the CLI flag and the in-app [M] prompt."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 1.0 else None

IMBALANCE_RATIO = 3.0
if "--imbalance" in args:
    i = args.index("--imbalance")
    try:
        raw_imb = args[i + 1]
    except IndexError:
        print("--imbalance requires a value"); sys.exit(1)
    parsed_imb = parse_imbalance_ratio(raw_imb)
    if parsed_imb is None:
        print("--imbalance requires a number > 1.0 (e.g. 3.0 = 300%)"); sys.exit(1)
    IMBALANCE_RATIO = parsed_imb
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]

STACK_COUNT = 3
if "--stack" in args:
    i = args.index("--stack")
    try:
        STACK_COUNT = int(args[i + 1])
        if STACK_COUNT < 2:
            raise ValueError
    except (IndexError, ValueError):
        print("--stack requires an integer >= 2"); sys.exit(1)
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]

MIN_IMBALANCE_VOL = 0.0
if "--min-imbalance-vol" in args:
    i = args.index("--min-imbalance-vol")
    try:
        MIN_IMBALANCE_VOL = max(0.0, float(args[i + 1]))
    except (IndexError, ValueError):
        print("--min-imbalance-vol requires a number"); sys.exit(1)
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]

def parse_big_trade_size(raw):
    """Decode a --big-trade-size/[B]-prompt string into a positive float,
    or None if invalid — shared by the CLI flag and the in-app [B] prompt."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None

BIG_TRADE_SIZE = 100.0   # [B] Big Trades filter: any bid/ask cell whose OWN
                         # volume is >= this gets highlighted, independent
                         # of imbalance status
if "--big-trade-size" in args:
    i = args.index("--big-trade-size")
    try:
        raw_big = args[i + 1]
    except IndexError:
        print("--big-trade-size requires a value"); sys.exit(1)
    parsed_big = parse_big_trade_size(raw_big)
    if parsed_big is None:
        print("--big-trade-size requires a positive number"); sys.exit(1)
    BIG_TRADE_SIZE = parsed_big
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]

LOAD_DATE = None
if "--date" in args:
    i = args.index("--date")
    try:
        LOAD_DATE = args[i + 1]
        datetime.strptime(LOAD_DATE, "%m_%d_%Y")
    except (IndexError, ValueError):
        print("--date requires MM_DD_YYYY format, e.g. --date 07_01_2026"); sys.exit(1)
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]

def _session_start_ts(now=None):
    """The PREVIOUS calendar day's 00:00 CT (midnight) — always, regardless
    of what time "now" actually is. footprint.py deliberately does NOT reset
    to a fresh/mostly-empty session at today's own midnight (that left very
    little history loaded for anyone launching soon after midnight); anchoring
    a full day earlier guarantees at least a full day of backfilled context on
    every launch. Different from cvd.py/gex.py's 19:00 CT overnight-session
    anchor — per explicit user request."""
    now = now or datetime.now()
    yesterday = now - timedelta(days=1)
    return datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0).timestamp()

BACKFILL_HOURS = None
if "--backfill-hours" in args:
    i = args.index("--backfill-hours")
    try:
        BACKFILL_HOURS = max(0.0, float(args[i + 1]))
    except (IndexError, ValueError):
        print("--backfill-hours requires a number"); sys.exit(1)
    args = [a for j, a in enumerate(args) if j not in (i, i + 1)]
if BACKFILL_HOURS is None:
    BACKFILL_HOURS = max(0.0, (time.time() - _session_start_ts()) / 3600.0)

TODAY_STR       = datetime.now().strftime("%m_%d_%Y")
VIEW_DATE       = LOAD_DATE or TODAY_STR
HISTORICAL_MODE = LOAD_DATE is not None and LOAD_DATE != TODAY_STR

LOG_DIR = os.path.dirname(os.path.abspath(__file__))

PHEMEX_SYMBOLS       = {"ETH": "ETHUSDT", "BTC": "BTCUSDT"}
KRAKEN_WS_PAIRS      = {"ETH": "ETH/USD", "BTC": "BTC/USD"}
KRAKEN_REST_PAIRS    = {"ETH": "ETHUSD", "BTC": "XBTUSD"}
COINBASE_PRODUCT_IDS = {"ETH": "ETH-USD", "BTC": "BTC-USD"}
PHEMEX_WS_URL      = "wss://ws.phemex.com"
KRAKEN_WS_URL      = "wss://ws.kraken.com/v2"
KRAKEN_TRADES_URL  = "https://api.kraken.com/0/public/Trades"
COINBASE_WS_URL    = "wss://ws-feed.exchange.coinbase.com"
COINBASE_REST_URL  = "https://api.exchange.coinbase.com"
_coinbase_session = requests.Session()

# ── PERSISTENCE ──────────────────────────────────────────────────────────────
def log_path(date_str):
    # TICK is in the filename for the same reason INTERVAL_LABEL is: a
    # bar's "levels" dict is bucketed at a specific $ granularity, so
    # switching tick in-app (see switch_tick) must never append
    # differently-bucketed bars into the same file.
    tick_str = f"{TICK:g}"
    return os.path.join(LOG_DIR, f"footprint_{SYMBOL}_{INTERVAL_LABEL}_{tick_str}_{date_str}.jsonl")

def _serialize_bar(bar):
    return {
        "ts": bar["ts"], "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"],
        "buy_vol": bar["buy_vol"], "sell_vol": bar["sell_vol"], "delta": bar["delta"],
        "tick": TICK,
        "levels": {str(k): v for k, v in bar["levels"].items()},
    }

def append_log(bar):
    try:
        with open(log_path(TODAY_STR), "a", encoding="utf-8") as f:
            f.write(json.dumps(_serialize_bar(bar)) + "\n")
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
                    row = json.loads(line)
                    row["levels"] = {int(k): v for k, v in row.get("levels", {}).items()}
                    bars.append(row)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return bars

# ── HISTORICAL BACKFILL (Kraken + Coinbase REST — Phemex has no history API) ─
def fetch_kraken_trades_range(pair, since_ts, until_ts, progress=None, deadline=None):
    """Page forward through Kraken's public Trades endpoint, real historical
    prints. deadline (time.time() cutoff) stops paging early, returning
    whatever's gathered so far rather than blocking indefinitely."""
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
        if progress:
            progress(len(out), datetime.fromtimestamp(trades[-1][2]))
        if hit_end or trades[-1][2] >= until_ts:
            break
        since = int(d["result"]["last"])
        time.sleep(0.25)
    return out

def _parse_rfc3339(ts_str):
    ts_str = ts_str.rstrip("Z")
    if "." in ts_str:
        base, frac = ts_str.split(".")
        ts_str = f"{base}.{frac[:6]}"
    return datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc).timestamp()

def fetch_coinbase_trades_range(product_id, since_ts, until_ts, progress=None, deadline=None):
    """Page backward through Coinbase Exchange's public trades endpoint (no
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
            r = _coinbase_session.get(f"{COINBASE_REST_URL}/products/{product_id}/trades",
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
        if progress and out:
            progress(len(out), datetime.fromtimestamp(out[-1][0]))
        after_cursor = r.headers.get("cb-after")
        if hit_old_end or not after_cursor:
            break
        time.sleep(0.2)
    out.reverse()
    return out

def fetch_trades_range(since_ts, until_ts, progress=None, deadline=None):
    """Kraken + Coinbase concurrently, merge-sorted chronologically (NOT a
    plain concatenation — ingest_trade drops late/out-of-order prints for an
    already-closed bucket, so an unsorted replay would silently corrupt
    bars). See cvd.py's fetch_trades_range for the same pattern/reasoning."""
    results = {}
    progress_lock = threading.Lock()
    def safe_progress(n, dt):
        if progress:
            with progress_lock:
                progress(n, dt)
    def _fetch_kraken():
        results["kraken"] = fetch_kraken_trades_range(
            KRAKEN_REST_PAIRS[SYMBOL], since_ts, until_ts, progress=safe_progress, deadline=deadline)
    def _fetch_coinbase():
        results["coinbase"] = fetch_coinbase_trades_range(
            COINBASE_PRODUCT_IDS[SYMBOL], since_ts, until_ts, progress=safe_progress, deadline=deadline)
    t1 = threading.Thread(target=_fetch_kraken, daemon=True)
    t2 = threading.Thread(target=_fetch_coinbase, daemon=True)
    t1.start(); t2.start()
    t1.join(); t2.join()
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

def fetch_trades_since(hours, progress=None, deadline=None):
    now = time.time()
    return fetch_trades_range(now - hours * 3600, now, progress=progress, deadline=deadline)

INITIAL_BACKFILL_BUDGET_SECS = 30   # was 15s — too little history loaded meant
                                    # scrolling back hit the oldest loaded bar
                                    # (and had to wait on extend_history_backward)
                                    # too quickly. Whatever still doesn't fit in
                                    # this budget isn't a hard loss either way:
                                    # extend_history_backward() (see below)
                                    # transparently loads more, in the background,
                                    # as you scroll near the loaded edge, same
                                    # lazy-load convention cvd.py uses for its own
                                    # GOTO_EDGE_TRIGGER.

def backfill_trades(hours, progress=None, reset=True):
    trades = fetch_trades_since(hours, progress=progress, deadline=time.time() + INITIAL_BACKFILL_BUDGET_SECS)
    if not trades:
        return 0
    if reset:
        try:
            open(log_path(TODAY_STR), "w", encoding="utf-8").close()
        except Exception:
            pass
    with state.lock:
        if reset:
            state.history.clear()
            state.live = None
            state.log_rows = 0
            state.log_err = None
        for ts, price, qty, is_buy in trades:
            ingest_trade(ts, price, qty, is_buy)
        state.backfill_boundary_ts = trades[-1][0]
    return len(trades)

def initialize_today(progress=None):
    if BACKFILL_HOURS > 0:
        n = backfill_trades(BACKFILL_HOURS, progress=progress, reset=True)
        if n:
            since_str = datetime.fromtimestamp(time.time() - BACKFILL_HOURS * 3600).strftime('%H:%M:%S')
            return f"backfilled {n} Kraken+Coinbase trades since {since_str}"
    existing = load_log(TODAY_STR)
    if existing:
        with state.lock:
            for row in existing:
                state.history.append(row)
        return f"resumed {len(existing)} bars from today's log (backfill unavailable)"
    return "starting fresh — no backfill, no existing log"

EXTEND_WINDOW_HOURS = 6    # how much real history to request per backward extend
EXTEND_BUDGET_SECS = 20    # time budget for that one fetch
EDGE_TRIGGER_BARS = 5      # how close to the oldest loaded bar (in on-screen
                           # columns) before triggering the next extend

def _build_bars(trades):
    """Same per-trade bucketing/level-accumulation logic as ingest_trade,
    but builds a fresh, isolated list of bars from a batch of (ts, price,
    qty, is_buy) trades instead of mutating state.live/state.history
    directly. Used by extend_history_backward() to build OLDER bars for
    prepending without disturbing the live bar currently in progress.
    Deliberately does NOT emit the final still-open bar — it would be a
    partial bar bordering the already-loaded oldest bar, and appending a
    partial/duplicate boundary bar risks corrupting continuity at the join
    point; that small boundary gap is an accepted tradeoff (same one
    cvd.py's own history-extend makes at its boundaries)."""
    bars = []
    live = None
    for ts, price, qty, is_buy in trades:
        if BAR_MODE == "time":
            bucket_ts = _bucket_ts(ts)
            if live is None:
                live = _new_bar(bucket_ts, price)
            elif bucket_ts > live["ts"]:
                bars.append(live)
                live = _new_bar(bucket_ts, price)
            elif bucket_ts < live["ts"]:
                continue
        else:
            if live is None:
                live = _new_bar(ts, price)
        live["h"] = max(live["h"], price)
        live["l"] = min(live["l"], price)
        live["c"] = price
        lvl = round(price / TICK)
        cell = live["levels"].setdefault(lvl, [0.0, 0.0])
        if is_buy:
            cell[1] += qty
            live["buy_vol"] += qty
        else:
            cell[0] += qty
            live["sell_vol"] += qty
        live["delta"] = live["buy_vol"] - live["sell_vol"]
        if BAR_MODE == "volume" and (live["buy_vol"] + live["sell_vol"]) >= VOL_THRESHOLD:
            bars.append(live)
            live = None
    return bars

def extend_history_backward():
    """Fetch and prepend more real history once the user scrolls near the
    oldest already-loaded bar — the counterpart to the shorter
    INITIAL_BACKFILL_BUDGET_SECS: whatever the initial backfill didn't have
    time for is picked up transparently here instead, in the background, as
    it's actually needed. Deliberately does NOT adjust hscroll_bars (an
    earlier version tried to "compensate" it for the growing total, which
    was backwards — hscroll_bars already means "N bars back from the
    newest", which prepending OLDER bars at the front doesn't change at
    all; adjusting it actively moved the view backward on every successful
    extend, including at the live edge). Guarded by
    state.history_loading_older so a fast repeated trigger (e.g. holding
    the pan key) can't overlap itself with a second fetch.
    Also guarded against [I]/[T] switching interval or tick WHILE this
    fetch is in flight — the trades in flight were bucketed for the OLD
    resolution, so committing them after a switch would silently mix
    differently-bucketed bars into the freshly-rebuilt history; bailing out
    is safe since switch_interval/switch_tick already did their own fresh
    backfill by the time this would otherwise notice."""
    started_interval, started_tick = INTERVAL_LABEL, TICK
    with state.lock:
        if state.history_loading_older or not state.history:
            return
        state.history_loading_older = True
        oldest_ts = state.history[0]["ts"]
    try:
        until_ts = oldest_ts
        since_ts = until_ts - EXTEND_WINDOW_HOURS * 3600
        trades = fetch_trades_range(since_ts, until_ts, deadline=time.time() + EXTEND_BUDGET_SECS)
        if not trades or INTERVAL_LABEL != started_interval or TICK != started_tick:
            return
        new_bars = _build_bars(trades)
        if not new_bars:
            return
        with state.lock:
            if INTERVAL_LABEL != started_interval or TICK != started_tick:
                return
            # only prepend bars strictly older than whatever's there NOW
            # (state.history[0] may have moved if another extend ran while
            # this fetch was in flight) — never insert stale/overlapping bars
            cutoff = state.history[0]["ts"] if state.history else oldest_ts
            new_bars = [b for b in new_bars if b["ts"] < cutoff]
            if new_bars:
                state.history = new_bars + state.history
    finally:
        with state.lock:
            state.history_loading_older = False

def switch_interval(label, mode, secs, threshold, progress=None):
    """Change bar shape (time or volume) while the app keeps running — the
    live WS feeds (Phemex/Kraken/Coinbase) are untouched, only the
    bar-shape globals change and history is rebuilt from scratch for the
    new shape via the same backfill-from-00:00-CT-or-resume path used at
    cold start."""
    global INTERVAL_LABEL, BAR_MODE, BAR_SECS, VOL_THRESHOLD
    with state.lock:
        state.history.clear()
        state.live = None
        state.log_rows = 0
        state.log_err = None
        state.backfill_boundary_ts = None
        INTERVAL_LABEL, BAR_MODE, BAR_SECS, VOL_THRESHOLD = label, mode, secs, threshold
    return initialize_today(progress=progress)

def switch_tick(new_tick, progress=None):
    """Change the price-increment (TICK) while the app keeps running. Every
    existing bar's "levels" dict is bucketed at the OLD tick, so — same as
    switch_interval — history is cleared and rebuilt from scratch at the
    new resolution via the same backfill-from-00:00-CT-or-resume path used
    at cold start, rather than trying to re-bucket already-aggregated data
    (which would lose the original per-trade price precision)."""
    global TICK
    with state.lock:
        state.history.clear()
        state.live = None
        state.log_rows = 0
        state.log_err = None
        state.backfill_boundary_ts = None
        TICK = new_tick
    return initialize_today(progress=progress)

def set_imbalance_ratio(new_ratio):
    """Change the diagonal-imbalance ratio ([M]) live. Unlike TICK/interval,
    this needs no rebuild — compute_imbalances reads IMBALANCE_RATIO fresh
    from already-accumulated level data on every draw() frame, so simply
    reassigning it takes effect on the very next frame."""
    global IMBALANCE_RATIO
    IMBALANCE_RATIO = new_ratio

def set_big_trade_size(new_size):
    """Change the Big Trades highlight threshold ([B]) live — same
    no-rebuild-needed reasoning as set_imbalance_ratio."""
    global BIG_TRADE_SIZE
    BIG_TRADE_SIZE = new_size

# ── SHARED STATE ─────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.history = []          # closed bars, dicts, chronological
        self.live = None           # currently-forming bar
        self.log_rows = 0
        self.log_err = None
        self.phemex_status = "connecting…"
        self.kraken_status = "connecting…"
        self.coinbase_status = "connecting…"
        self.last_price = None
        self.session = 0
        self.backfill_boundary_ts = None   # everything <= this is Kraken+Coinbase only (no Phemex)
        self.history_loading_older = False   # guards extend_history_backward from overlapping itself

state = State()

def _bucket_ts(ts):
    return int(ts // BAR_SECS * BAR_SECS)

def _new_bar(ts, price):
    return {"ts": ts, "o": price, "h": price, "l": price, "c": price,
            "buy_vol": 0.0, "sell_vol": 0.0, "delta": 0.0, "levels": {}}

def _close_live():
    """Finalize state.live into history + log. Caller holds state.lock."""
    live = state.live
    state.history.append(live)
    ok, err = append_log(live)
    if ok:
        state.log_rows += 1
        state.log_err = None
    else:
        state.log_err = err

def ingest_trade(ts, price, qty, is_buy):
    """Fold one trade print into the live bar's OHLC + per-price-level
    bid/ask volume. Must be called with state.lock held.

    Time bars: bucket-floor by wall clock, drop late/out-of-order prints for
    an already-closed bucket (same as cvd.py). Volume bars: accumulate until
    combined buy+sell volume reaches VOL_THRESHOLD; the trade that crosses
    the threshold closes its bar IN FULL (not split), and the next trade
    opens a fresh one — same convention as cvd.py's "<N>V" bars."""
    live = state.live
    if BAR_MODE == "time":
        bucket_ts = _bucket_ts(ts)
        if live is None:
            state.live = _new_bar(bucket_ts, price)
            live = state.live
        elif bucket_ts > live["ts"]:
            _close_live()
            state.live = _new_bar(bucket_ts, price)
            live = state.live
        elif bucket_ts < live["ts"]:
            return   # late/out-of-order print for an already-closed bucket
    else:
        if live is None:
            state.live = _new_bar(ts, price)
            live = state.live

    live["h"] = max(live["h"], price)
    live["l"] = min(live["l"], price)
    live["c"] = price
    lvl = round(price / TICK)
    cell = live["levels"].setdefault(lvl, [0.0, 0.0])   # [bid_vol, ask_vol]
    if is_buy:
        cell[1] += qty
        live["buy_vol"] += qty
    else:
        cell[0] += qty
        live["sell_vol"] += qty
    live["delta"] = live["buy_vol"] - live["sell_vol"]
    state.last_price = price

    if BAR_MODE == "volume" and (live["buy_vol"] + live["sell_vol"]) >= VOL_THRESHOLD:
        _close_live()
        state.live = None
        return
    state.last_price = price

# ── LIVE TRADE FEEDS ─────────────────────────────────────────────────────────
def start_feeds(session):
    threading.Thread(target=ws_kraken, args=(session,), daemon=True).start()
    threading.Thread(target=ws_phemex, args=(session,), daemon=True).start()
    threading.Thread(target=ws_coinbase, args=(session,), daemon=True).start()

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
        if msg.get("channel") != "trade" or msg.get("type") != "update":
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
        ws_app = websocket.WebSocketApp(KRAKEN_WS_URL, on_open=on_open, on_message=on_message,
                                         on_error=on_error, on_close=on_close)
        ws_app.run_forever(ping_interval=30, ping_timeout=10)
        if stale():
            break
        with state.lock:
            if stale(): break
            state.kraken_status = f"reconnecting… ({backoff}s)"
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

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
        # Phemex replays up to 1000 historical trades as a "snapshot" on
        # every (re)connect — not new prints; skip entirely (see cvd.py).
        if msg.get("type") == "snapshot":
            return
        trades = msg.get("trades_p")
        if not trades:
            return
        if msg.get("symbol") and msg["symbol"] != symbol:
            return
        for row in trades:
            try:
                ts = row[0] / 1e9
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
        ws_app = websocket.WebSocketApp(PHEMEX_WS_URL, on_open=on_open, on_message=on_message,
                                         on_error=on_error, on_close=on_close)
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

def ws_coinbase(session):
    product_id = COINBASE_PRODUCT_IDS[SYMBOL]

    def stale():
        return state.session != session

    def on_open(ws):
        ws.send(json.dumps({"type": "subscribe",
                             "channels": [{"name": "matches", "product_ids": [product_id]}]}))
        with state.lock:
            if stale(): return
            state.coinbase_status = "live"

    def on_message(ws, message):
        if stale(): return
        try:
            msg = json.loads(message)
        except Exception:
            return
        mtype = msg.get("type")
        if mtype == "error":
            with state.lock:
                if stale(): return
                state.coinbase_status = f"err: {str(msg.get('message', ''))[:30]}"
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
        with state.lock:
            if stale(): return
            ingest_trade(ts, price, qty, is_buy)
            state.coinbase_status = "live"

    def on_error(ws, err):
        if stale(): return
        with state.lock:
            if stale(): return
            state.coinbase_status = f"err: {str(err)[:30]}"

    def on_close(ws, code, msg):
        if stale(): return
        with state.lock:
            if stale(): return
            state.coinbase_status = "reconnecting…"

    backoff = 1
    while not stale():
        ws_app = websocket.WebSocketApp(COINBASE_WS_URL, on_open=on_open, on_message=on_message,
                                         on_error=on_error, on_close=on_close)
        ws_app.run_forever(ping_interval=30, ping_timeout=10)
        if stale():
            break
        with state.lock:
            if stale(): break
            state.coinbase_status = f"reconnecting… ({backoff}s)"
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

# ── FOOTPRINT ANALYSIS (imbalance / stacked imbalance / grouping / POC) ─────
def compute_imbalances(levels, ratio=None, min_vol=None):
    """Classic diagonal footprint imbalance: ask volume at price P vs bid
    volume one tick BELOW it (buy imbalance — buyers at P are competing
    against resting sellers one tick down), or bid volume at P vs ask volume
    one tick ABOVE it (sell imbalance). Returns {level: "buy"|"sell"}.

    A level with genuinely ZERO opposing volume (the neighbor DID trade —
    it's a real key in `levels` — just with nothing on that side) is an
    infinite ratio and qualifies. A neighbor that's simply ABSENT (nothing
    traded there at all within this bar) is treated as "no evidence either
    way" and does NOT qualify — without this distinction, the bar's own
    true top and bottom level would always trivially flag as an imbalance
    just for being the edge of the bar's range, which is noise, not signal."""
    ratio = IMBALANCE_RATIO if ratio is None else ratio
    min_vol = MIN_IMBALANCE_VOL if min_vol is None else min_vol
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

def compute_stacks(imbalances, stack_count=None):
    """Consecutive tick levels (adjacent integer keys) flagged the same
    imbalance direction, grouped into runs >= stack_count. Returns the set
    of levels that are part of a qualifying stack."""
    stack_count = STACK_COUNT if stack_count is None else stack_count
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
    """Collapse a bar's raw per-TICK levels dict into display rows. Used
    only when the visible bars' actual traded range wouldn't fit on screen
    at the base $1.00 (TICK) resolution — see draw()'s group_size
    computation. group_size<=1 is the identity. Floor-division grouping
    (not anchored to any particular price) keeps group indices consecutive
    integers, so compute_imbalances/compute_stacks/POC all work on a
    grouped dict exactly like they do on a raw one."""
    if group_size <= 1:
        return {lvl: [cell[0], cell[1]] for lvl, cell in levels.items()}
    grouped = {}
    for lvl, cell in levels.items():
        g = lvl // group_size
        gc = grouped.setdefault(g, [0.0, 0.0])
        gc[0] += cell[0]
        gc[1] += cell[1]
    return grouped

def compute_poc(levels):
    """Point of Control: the level with the most total volume (bid+ask) in
    a bar. Returns None for an empty bar."""
    if not levels:
        return None
    return max(levels, key=lambda lvl: levels[lvl][0] + levels[lvl][1])

# ── COLOUR PAIRS ─────────────────────────────────────────────────────────────
P_DEFAULT, P_DIM, P_CYAN, P_YELLOW, P_GREEN, P_RED, P_STATUS = range(1, 8)

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    BG = -1
    curses.init_pair(P_DEFAULT, curses.COLOR_WHITE,  BG)
    curses.init_pair(P_DIM,     curses.COLOR_WHITE,  BG)
    curses.init_pair(P_CYAN,    curses.COLOR_CYAN,   BG)   # [B] Big Trades filter
    curses.init_pair(P_YELLOW,  curses.COLOR_YELLOW, BG)
    curses.init_pair(P_GREEN,   curses.COLOR_GREEN,  BG)
    curses.init_pair(P_RED,     curses.COLOR_RED,    BG)
    curses.init_pair(P_STATUS,  curses.COLOR_BLACK,  curses.COLOR_WHITE)

def cp(pair, bold=False, dim=False):
    a = curses.color_pair(pair)
    if bold: a |= curses.A_BOLD
    if dim:  a |= curses.A_DIM
    return a

_shadow_buf = None   # [P] screenshot support

def _shadow_put(y, x, s):
    if _shadow_buf is None:
        return
    if not (0 <= y < len(_shadow_buf)):
        return
    row = _shadow_buf[y]
    w = len(row)
    for i, ch in enumerate(s):
        col = x + i
        if 0 <= col < w:
            row[col] = ch

def safe_add(win, y, x, s, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0:
        return
    avail = w - x - 1
    if avail <= 0:
        return
    text = s[:avail]
    try:
        win.addstr(y, x, text, attr)
        _shadow_put(y, x, text)
    except curses.error:
        pass

def take_screenshot(win=None):
    folder = os.path.join(os.path.dirname(__file__), "screenshots")
    os.makedirs(folder, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fn = os.path.join(folder, f"footprint_{SYMBOL}_{ts}.txt")
    if _shadow_buf is not None:
        buf = _shadow_buf
    elif win is not None:
        h, w = win.getmaxyx()
        buf = [[" "] * w for _ in range(h)]
    else:
        buf = []
    lines = ["".join(row).rstrip() for row in buf]
    with open(fn, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return fn

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

def fmt_time(ts):
    fine = BAR_MODE == "volume" or BAR_SECS < 300
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S" if fine else "%H:%M")

def fmt_bar_progress(live, now=None):
    """Status-header text describing how close the CURRENTLY FORMING bar
    (`live`) is to closing: a countdown for time bars, or an
    accumulated/threshold ratio for volume bars. Returns None if there's no
    live bar to report on (before the first trade of a session, or in
    --date historical playback, which has no live bar at all)."""
    if live is None:
        return None
    if BAR_MODE == "time":
        now = now if now is not None else time.time()
        remaining = max(0, (live["ts"] + BAR_SECS) - now)
        mins, secs = divmod(int(remaining), 60)
        return f"closes in {mins}:{secs:02d}"
    accumulated = live["buy_vol"] + live["sell_vol"]
    return f"{fmt_lvl_qty(accumulated)} / {VOL_THRESHOLD:g}"

# ── DRAW ──────────────────────────────────────────────────────────────────
CELL_TXT_W = 15   # "1,234.5 x 1,234.5"-worst-case text width
COL_W = 1 + CELL_TXT_W + 1   # marker gutter + text + trailing gap
AXIS_W = 10       # left-side price-axis gutter width — a module constant
                  # (not just a local in draw()) so curses_main can compute
                  # the SAME n-visible-columns math for its own hscroll_bars
                  # clamping/edge-trigger without duplicating a magic number
VSTEP = 5         # ticks per Up/Down press
VSTEP_BIG = 25    # ticks per PgUp/PgDn press
POC_MARKER = "◆"
LIVE_LINE_CH = "─"   # live-price line, drawn only through empty cells

def draw(win, status_line, vscroll_center, vfollow_price, hscroll_bars):
    """Renders one frame. Returns the number of bar-columns actually drawn.

    Fixed-height vertical window: always exactly plot_h rows (fills the
    terminal), centered on vscroll_center (or the live price, when
    vfollow_price). Resolution is TICK ($1.00 by default) per row UNLESS the
    bars currently visible have a combined traded range too tall to fit in
    plot_h rows at that resolution — group_size then widens just enough
    (adjacent raw ticks merged/summed) so nothing gets clipped. This is
    deliberately NOT the same thing as auto-fitting the whole window to the
    data's size (that was tried and reverted — it left most of the screen
    blank whenever the range was smaller than the terminal); the window
    itself never shrinks, only the per-row $ resolution ever changes, and
    only to grow."""
    global _shadow_buf
    h, w = win.getmaxyx()
    win.erase()
    _shadow_buf = [[" "] * w for _ in range(h)]

    with state.lock:
        all_bars = list(state.history) + ([state.live] if state.live else [])
        cur_error = state.log_err
        last_price = state.last_price
        live_bar = state.live

    top_reserved = 1
    bottom_reserved = 2
    axis_w = AXIS_W
    plot_h = h - top_reserved - bottom_reserved
    plot_w = max(1, w - axis_w)

    if not all_bars or plot_h <= 0:
        header = f" FOOTPRINT — {SYMBOL}  bar:{INTERVAL_LABEL}  tick:{fmt_price(TICK)}  "
        safe_add(win, 0, 0, header.ljust(w), cp(P_STATUS))
        safe_add(win, h // 2, max(0, (w - 20) // 2), "waiting for trades…", cp(P_CYAN))
        win.noutrefresh()
        return 0

    n = max(1, plot_w // COL_W)
    end = max(n, min(len(all_bars), len(all_bars) - hscroll_bars))
    start = max(0, end - n)
    visible = all_bars[start:end]

    if vfollow_price:
        # Always center on the MIDPOINT of the traded range across ALL
        # currently visible bars — including at the live edge. Two real
        # bugs came from special-casing the live edge to center on
        # last_price directly: (1) last_price updates on EVERY trade tick,
        # so the whole window shifted up/down on every single print even
        # within an already-established range — visible flicker; (2) it
        # skewed the window toward wherever the live price happens to sit,
        # leaving OTHER visible bars (often at a meaningfully different
        # price — normal for volume bars, or just an active session) cramped
        # off-center instead of evenly fit in frame, the same skew problem
        # already fixed for the scrolled-back case. Centering on the
        # min/max of visible LEVELS (not tick-by-tick last price) only
        # moves when a bar's range genuinely extends to a new high/low —
        # same "recompute every frame but only really change when the data
        # changes" principle as a normal candle chart's price axis.
        all_visible_lvls = [lvl for bar in visible for lvl in bar["levels"]]
        if all_visible_lvls:
            center_lvl = (max(all_visible_lvls) + min(all_visible_lvls)) // 2
        else:
            center_lvl = round((last_price or visible[-1]["c"]) / TICK)
    else:
        center_lvl = vscroll_center

    # "outsized candle" means one BAR's own high-low range doesn't fit — NOT
    # the combined span across every visible bar. Using the union across all
    # visible bars was wrong: at a longer interval (10m/15m) each column
    # covers more time, so ordinary price drift across several columns adds
    # up to a wide combined span even when no single candle is unusual,
    # which was incorrectly widening the grid on nothing more than an
    # interval switch. Keying off the tallest single bar's own range fixes
    # that — grouping now only kicks in for a genuine outsized candle.
    required_span = 1
    for bar in visible:
        lvls = bar["levels"]
        if lvls:
            required_span = max(required_span, max(lvls) - min(lvls) + 1)
    group_size = max(1, -(-required_span // plot_h)) if required_span > plot_h else 1

    group_center = center_lvl // group_size
    half = plot_h // 2
    top_group = group_center + half
    bot_group = top_group - plot_h + 1
    row_groups = list(range(top_group, bot_group - 1, -1))
    group_to_row = {g: r_i for r_i, g in enumerate(row_groups)}

    # live price line/box: which row (if any, within the current vertical
    # window) the current live price falls on. Shown regardless of scroll
    # position — even scrolled back into history, it's useful to see where
    # the live price currently sits relative to what's on screen; it simply
    # doesn't appear if that's outside the visible window (same as any
    # other row would be).
    live_row_y = None
    live_group = None
    if last_price is not None:
        live_group = round(last_price / TICK) // group_size
        live_r_i = group_to_row.get(live_group)
        if live_r_i is not None:
            live_row_y = top_reserved + live_r_i

    bar_progress = fmt_bar_progress(live_bar)
    header = (f" FOOTPRINT — {SYMBOL}  bar:{INTERVAL_LABEL} [I]  tick:${fmt_price(TICK)} [T]  "
              f"imb:{IMBALANCE_RATIO:g}x [M]  stack:{STACK_COUNT}+  big:${fmt_price(BIG_TRADE_SIZE)}+ [B]"
              f"{f'  grid:${fmt_price(TICK * group_size)}' if group_size > 1 else ''}"
              f"{f'  |  {bar_progress}' if bar_progress else ''}  ")
    safe_add(win, 0, 0, header.ljust(w), cp(P_STATUS))

    col_x = [axis_w + i * COL_W for i in range(len(visible))]

    # price axis labels — each row's label is the top (highest) price
    # covered by whatever raw ticks are merged into that row
    label_step = max(1, plot_h // 12)
    for r_i, g in enumerate(row_groups):
        if r_i % label_step == 0 or r_i == len(row_groups) - 1:
            top_price = ((g + 1) * group_size - 1) * TICK
            safe_add(win, top_reserved + r_i, 0, fmt_price(top_price).rjust(axis_w - 1), cp(P_DIM))

    # live price box — always shown on its own row regardless of the
    # sampled label_step above (which would otherwise often skip it),
    # reverse-video so it reads as a solid highlighted tag, not just text
    if live_row_y is not None:
        live_label = f"▶{fmt_price(last_price)}"
        safe_add(win, live_row_y, 0, live_label.rjust(axis_w - 1), cp(P_YELLOW, bold=True) | curses.A_REVERSE)

    # footprint cells
    for i, bar in enumerate(visible):
        levels = bar["levels"]
        cx = col_x[i]
        has_live_cell = False
        if levels:
            glevels = group_levels(levels, group_size)
            imbalances = compute_imbalances(glevels)
            stacked = compute_stacks(imbalances)
            poc_g = compute_poc(glevels)
            has_live_cell = live_group in glevels
            for g, cell in glevels.items():
                r_i = group_to_row.get(g)
                if r_i is None:
                    continue
                row_y = top_reserved + r_i
                bid, ask = cell[0], cell[1]
                bid_txt, ask_txt = fmt_lvl_qty(bid), fmt_lvl_qty(ask)
                mid = " x "
                pad = max(0, CELL_TXT_W - (len(bid_txt) + len(mid) + len(ask_txt)))
                x0 = cx + 1 + pad // 2   # +1 for the marker gutter

                direction = imbalances.get(g)
                extra = curses.A_REVERSE if g in stacked else 0
                is_poc = (g == poc_g)
                poc_attr = curses.A_UNDERLINE if is_poc else 0
                # Big Trades filter ([B]): a cell's own bid/ask volume >=
                # BIG_TRADE_SIZE is highlighted cyan — independent of imbalance,
                # but imbalance direction (a rarer, more specific signal) still
                # takes priority when a level happens to be both. Cyan (not
                # magenta) specifically because magenta reads too close to red
                # at a glance — cyan sits far enough from both red and green to
                # stay unambiguous.
                if direction == "sell":
                    bid_color = cp(P_RED, bold=True) | extra | poc_attr
                elif bid >= BIG_TRADE_SIZE:
                    bid_color = cp(P_CYAN, bold=True) | poc_attr
                else:
                    bid_color = cp(P_DEFAULT) | poc_attr
                if direction == "buy":
                    ask_color = cp(P_GREEN, bold=True) | extra | poc_attr
                elif ask >= BIG_TRADE_SIZE:
                    ask_color = cp(P_CYAN, bold=True) | poc_attr
                else:
                    ask_color = cp(P_DEFAULT) | poc_attr

                safe_add(win, row_y, x0, bid_txt, bid_color)
                safe_add(win, row_y, x0 + len(bid_txt), mid, cp(P_DIM) | poc_attr)
                safe_add(win, row_y, x0 + len(bid_txt) + len(mid), ask_txt, ask_color)
                if is_poc:
                    safe_add(win, row_y, cx, POC_MARKER, cp(P_YELLOW, bold=True))

        # live price line — drawn through this column only where there's no
        # real cell already sitting at that exact row, so it never overwrites
        # actual bid/ask numbers (runs behind the data, not through it)
        if live_row_y is not None and not has_live_cell:
            safe_add(win, live_row_y, cx, LIVE_LINE_CH * (COL_W - 1), cp(P_YELLOW, dim=True))

    # time axis
    axis_row = h - bottom_reserved
    for i, bar in enumerate(visible):
        lbl = fmt_time(bar["ts"])
        safe_add(win, axis_row, col_x[i], lbl.center(COL_W - 1), cp(P_DIM))

    # status bar
    buy_tot = sum(b["buy_vol"] for b in visible)
    sell_tot = sum(b["sell_vol"] for b in visible)
    feed_status = f"Phemex:{state.phemex_status}  Kraken:{state.kraken_status}  Coinbase:{state.coinbase_status}"
    info = (f" px:{fmt_price(last_price)}  visible Δ:{buy_tot - sell_tot:+,.2f} "
            f"(buy:{buy_tot:,.2f} sell:{sell_tot:,.2f})  {feed_status}  log:{state.log_rows}  {status_line}")
    if state.backfill_boundary_ts:
        boundary = datetime.fromtimestamp(state.backfill_boundary_ts).strftime("%H:%M:%S")
        info += f"  |  Kraken+Coinbase-only before {boundary}, Phemex-inclusive aggregate after"
    if cur_error:
        info += f"  ⚠ log: {cur_error}"
    safe_add(win, h - 1, 0, info.ljust(w), cp(P_STATUS))
    win.noutrefresh()
    return len(visible)

def _prompt_text(stdscr, prompt):
    """Blocking text-input line at the bottom of the screen — used for the
    [I] interval prompt. Returns the typed string (stripped), or None if
    the user cancelled (Esc) or left it empty."""
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
                return buf.strip() or None
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                buf = buf[:-1]
            elif 32 <= ch <= 126:
                buf += chr(ch)
    finally:
        curses.curs_set(0)
        stdscr.nodelay(True)

def _prompt_interval(stdscr):
    return _prompt_text(stdscr, "Interval (e.g. 45s, 5m, 500V): ")

def _prompt_tick(stdscr):
    return _prompt_text(stdscr, "Price increment $ (e.g. 1, 0.25, 10): ")

def _prompt_imbalance(stdscr):
    return _prompt_text(stdscr, "Imbalance ratio (e.g. 3.0 = 300%): ")

def _prompt_big_trade(stdscr):
    return _prompt_text(stdscr, "Big Trades size (e.g. 100): ")

# ── MAIN LOOPS ────────────────────────────────────────────────────────────
def curses_main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(200)
    init_colors()

    hscroll_bars = 0
    vfollow_price = True
    vscroll_center = 0
    screenshot_msg = None
    screenshot_until = 0

    if HISTORICAL_MODE:
        for row in load_log(VIEW_DATE):
            state.history.append(row)
        status_line = "history mode — no live feed"
        # vfollow_price stays True (its default) — there's no live price in
        # historical mode, so draw() automatically centers on whichever bar
        # is currently at the right edge of the view instead, updating as
        # you scroll (same auto-centering used in live mode once scrolled
        # back — see draw()'s center_lvl logic).
        init_shown_at = 0
    else:
        def _progress(n, last_dt):
            hh, ww = stdscr.getmaxyx()
            msg = f"Backfilling since yesterday 00:00 CT… {n} trades ({last_dt.strftime('%H:%M:%S')})"
            stdscr.erase()
            safe_add(stdscr, hh // 2, max(0, (ww - len(msg)) // 2), msg, cp(P_CYAN))
            stdscr.refresh()
        init_status = initialize_today(progress=_progress)
        init_shown_at = time.time()
        state.session += 1
        session = state.session
        start_feeds(session)
        status_line = init_status

    while True:
        key = stdscr.getch()
        if key in (ord('q'), ord('Q'), 27):
            break
        elif key == curses.KEY_LEFT:
            hscroll_bars += 1
        elif key == ord('['):
            hscroll_bars += 10
        elif key == curses.KEY_RIGHT:
            hscroll_bars = max(0, hscroll_bars - 1)
        elif key == ord(']'):
            hscroll_bars = max(0, hscroll_bars - 10)
        elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            step = VSTEP_BIG if key in (curses.KEY_PPAGE, curses.KEY_NPAGE) else VSTEP
            direction = 1 if key in (curses.KEY_UP, curses.KEY_PPAGE) else -1
            if vfollow_price:
                base = round((state.last_price or 0) / TICK)
            else:
                base = vscroll_center
            vscroll_center = base + direction * step
            vfollow_price = False
        elif key in (curses.KEY_HOME, ord('l'), ord('L')):
            hscroll_bars = 0
            vfollow_price = True
        elif key in (ord('c'), ord('C')):
            # Re-center vertically without touching horizontal position —
            # [Home]/[L] also jumps back to the live edge (hscroll_bars=0),
            # which is wrong if you're deliberately scrolled back through
            # history and just want auto-centering back on for wherever you
            # currently are. Any Up/Down/PgUp/PgDn press disables
            # auto-centering permanently (by design — it's a manual
            # override) until re-enabled; this is that re-enable, usable at
            # any time regardless of scroll position.
            vfollow_price = True
        elif key in (ord('p'), ord('P')):
            fn = take_screenshot(stdscr)
            screenshot_msg = f"Screenshot: {os.path.basename(fn)}"
            screenshot_until = time.time() + 5
        elif key in (ord('i'), ord('I')) and not HISTORICAL_MODE:
            raw_interval = _prompt_interval(stdscr)
            if raw_interval is not None:
                parsed = parse_interval(raw_interval)
                if parsed is None:
                    status_line = f"Invalid interval '{raw_interval}' — use <N>s, <N>m, or <N>V"
                    init_shown_at = time.time()
                else:
                    next_label, next_mode, next_secs, next_threshold = parsed
                    def _switch_progress(n, last_dt):
                        hh, ww = stdscr.getmaxyx()
                        msg = f"Switching to {next_label}… backfilling… {n} trades ({last_dt.strftime('%H:%M:%S')})"
                        stdscr.erase()
                        safe_add(stdscr, hh // 2, max(0, (ww - len(msg)) // 2), msg, cp(P_CYAN))
                        stdscr.refresh()
                    status_line = switch_interval(next_label, next_mode, next_secs, next_threshold, progress=_switch_progress)
                    init_shown_at = time.time()
                    hscroll_bars = 0
                    vfollow_price = True
        elif key in (ord('t'), ord('T')) and not HISTORICAL_MODE:
            raw_tick = _prompt_tick(stdscr)
            if raw_tick is not None:
                new_tick = parse_tick(raw_tick)
                if new_tick is None:
                    status_line = f"Invalid price increment '{raw_tick}' — enter a positive $ amount"
                    init_shown_at = time.time()
                else:
                    def _tick_progress(n, last_dt):
                        hh, ww = stdscr.getmaxyx()
                        msg = f"Switching to ${new_tick:g} increments… backfilling… {n} trades ({last_dt.strftime('%H:%M:%S')})"
                        stdscr.erase()
                        safe_add(stdscr, hh // 2, max(0, (ww - len(msg)) // 2), msg, cp(P_CYAN))
                        stdscr.refresh()
                    status_line = switch_tick(new_tick, progress=_tick_progress)
                    init_shown_at = time.time()
                    hscroll_bars = 0
                    vfollow_price = True
        elif key in (ord('m'), ord('M')):
            raw_ratio = _prompt_imbalance(stdscr)
            if raw_ratio is not None:
                new_ratio = parse_imbalance_ratio(raw_ratio)
                if new_ratio is None:
                    status_line = f"Invalid imbalance ratio '{raw_ratio}' — enter a number > 1.0 (e.g. 3.0)"
                else:
                    set_imbalance_ratio(new_ratio)
                    status_line = f"Imbalance ratio set to {new_ratio:g}x"
                init_shown_at = time.time()
        elif key in (ord('b'), ord('B')):
            raw_size = _prompt_big_trade(stdscr)
            if raw_size is not None:
                new_size = parse_big_trade_size(raw_size)
                if new_size is None:
                    status_line = f"Invalid Big Trades size '{raw_size}' — enter a positive number"
                else:
                    set_big_trade_size(new_size)
                    status_line = f"Big Trades size set to {new_size:g}"
                init_shown_at = time.time()

        if not HISTORICAL_MODE:
            with state.lock:
                total = len(state.history) + (1 if state.live else 0)

            # Clamp hscroll_bars to what's actually loaded. Without this it
            # could grow past the point where panning has any visible effect
            # (draw() itself has always clamped the DISPLAYED window, but
            # never reported that back — so a long run of holding [Left]
            # left hscroll_bars far beyond the real edge, and pressing
            # [Right] just decremented that runaway number with zero visible
            # change until it wandered back into range — the exact "locked
            # in place, can't scroll forward" symptom this fixes).
            hh, ww = stdscr.getmaxyx()
            n_est = max(1, max(1, ww - AXIS_W) // COL_W)
            hscroll_bars = max(0, min(hscroll_bars, max(0, total - n_est)))

            # Trigger the next backward extend once the oldest VISIBLE bar is
            # close to the oldest LOADED bar — same "near the edge of what's
            # loaded" convention as cvd.py's GOTO_EDGE_TRIGGER. Gated on
            # hscroll_bars > 0 (genuinely scrolled back) — NOT merely on
            # "total is small", which is true right after every launch/switch
            # (the initial backfill is intentionally short) and would
            # otherwise keep firing at the live edge on its own. hscroll_bars
            # itself needs NO adjustment when bars are prepended: it's
            # already "N bars back from the newest", which prepending OLDER
            # bars at the front doesn't change — a previous version of this
            # code tried to "compensate" by increasing hscroll_bars to match
            # the growing total, which was backwards and was the actual
            # cause of the view silently jumping back several bars every
            # time a background extend completed, including while sitting
            # at the live edge doing nothing.
            near_old_edge = (hscroll_bars > 0
                              and not state.history_loading_older
                              and (total - n_est - hscroll_bars) <= EDGE_TRIGGER_BARS)
            if near_old_edge:
                threading.Thread(target=extend_history_backward, daemon=True).start()

        if HISTORICAL_MODE:
            sl = status_line
        else:
            sl = "● LIVE" if hscroll_bars == 0 else f"⏸ paused ({hscroll_bars} bars back) — [L] to live"
            if time.time() - init_shown_at < 15:
                sl = f"{status_line}  |  {sl}"
        if screenshot_msg and time.time() < screenshot_until:
            sl = f"{screenshot_msg}  |  {sl}"

        draw(stdscr, sl, vscroll_center, vfollow_price, hscroll_bars)
        curses.doupdate()

def headless_main():
    print(f"footprint.py headless logger — {SYMBOL} @ {INTERVAL_LABEL}, tick {TICK} -> {log_path(TODAY_STR)}")
    print("Ctrl+C to stop.")
    def _progress(n, last_dt):
        print(f"  backfilling… {n} trades ({last_dt.strftime('%H:%M:%S')})", end="\r")
    init_status = initialize_today(progress=_progress)
    print(f"\n{init_status} ({state.log_rows} bars logged) — live from here on.")
    state.session += 1
    session = state.session
    start_feeds(session)

    last_logged = state.log_rows
    while True:
        time.sleep(2)
        with state.lock:
            rows = state.log_rows
            last_price = state.last_price
            n_levels = len(state.live["levels"]) if state.live else 0
        if rows != last_logged:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] bars={rows} px={fmt_price(last_price)} live_levels={n_levels}")
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
        print("\nfootprint.py headless logger — stopped.")
        return
    try:
        curses.wrapper(curses_main)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
