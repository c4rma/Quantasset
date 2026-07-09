#!/usr/bin/env python3
"""
cvd.py — Aggregate Cumulative Volume Delta (CVD) with Underlying Price Panel
Curses terminal chart, two stacked panels sharing one time axis:

  Price panel (top)  — OHLC candlesticks built from live trade prints,
                        blended across all three feeds (Phemex perp + Kraken
                        spot + Coinbase spot).
  CVD panel (bottom) — cumulative Σ(buy_qty − sell_qty) across ALL THREE
                        feeds, also rendered as OHLC candles (open = prior
                        bar's close, high/low = the running range within the
                        bar), colored the same way as price (green = close>=open).

CVD = the running total of signed trade volume using each print's taker side
(buy prints add, sell prints subtract). It answers "who has been more
aggressive, buyers or sellers" independent of price — divergence between a
rising price and a falling/flat CVD line is the classic "hidden selling"
signal traders watch this indicator for.

Data (real trade prints, not just OHLC volume) — depends on the symbol:
  ETH / BTC (crypto)     — Phemex (wss://ws.phemex.com trade_p.subscribe,
                           hedged USDT perp) + Kraken (wss://ws.kraken.com/v2
                           channel=trade, USD spot) + Coinbase
                           (wss://ws-feed.exchange.coinbase.com matches
                           channel, USD spot), run concurrently; every print
                           from ANY of the three feeds the SAME bar/CVD
                           accumulator, so "aggregate" means combined across
                           exchanges, not per-exchange lines. Phemex and
                           Kraken tag each trade's real taker side directly;
                           Coinbase's `side` field is the MAKER's side, so it
                           gets flipped (side=="sell" -> taker bought) before
                           being folded in — see fetch_coinbase_trades_range()
                           for the verification behind that flip.
  anything else (equity/ETF ticker, e.g. TLT/GLD/QQQ)
                         — Alpaca's free real-time IEX feed (single source —
                           IEX is ~1-3% of total US equity volume, not the
                           full consolidated tape; there's no free source
                           that offers more). Requires a free Alpaca
                           paper-trading account: set ALPACA_API_KEY_ID and
                           ALPACA_API_SECRET_KEY in .env. Alpaca trades have
                           no native buy/sell tag (true of most non-crypto
                           venues), so each print is classified live against
                           the running IEX quote via the quote-rule
                           (Lee-Ready: at/above the ask = buy, at/below the
                           bid = sell, tick-rule vs the previous trade price
                           as a fallback when a print lands inside the
                           spread or before any quote has arrived yet) —
                           the standard approach real CVD tools use when
                           trades aren't natively side-tagged.

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
  LIMITATION: Kraken's and Coinbase's public trade REST endpoints both
  support real pagination arbitrarily far back, but Phemex's public trade
  endpoint only ever returns the last ~1000 prints (~tens of minutes) with
  no pagination — there is no way to backfill deep Phemex history. So the
  backfilled portion of the window is **Kraken+Coinbase only** (missing
  Phemex); once live WS trades start flowing (immediately after backfill),
  CVD becomes the true triple-exchange aggregate again. The boundary
  timestamp is shown in the status bar so it's never ambiguous which
  portion is which.

  TIME BUDGET: cold start is capped at INITIAL_BACKFILL_BUDGET_SECS
  (~30s) regardless of how far back the ideal session-anchor window would
  otherwise reach — Coinbase's dust-trade volume alone can turn a full
  ~19-24h reconstruction into several minutes, so the actual amount loaded
  is whatever real data fits in the time budget, not the full session
  necessarily. Scrolling back past the loaded edge triggers
  extend_history_backward() to fetch the next chunk automatically, each
  one similarly capped at EXTEND_BACKWARD_BUDGET_SECS (~15s) — so reaching
  further back is always available, just incremental instead of upfront.

Usage:
  python cvd.py [SYMBOL] [--interval LABEL] [--date MM_DD_YYYY] [--headless]
                 [--backfill-hours N]
    SYMBOL               ETH or BTC for crypto (Phemex+Kraken+Coinbase aggregate), or
                         any equity/ETF ticker (e.g. TLT, GLD, QQQ) for
                         Alpaca's free IEX feed (needs ALPACA_API_KEY_ID /
                         ALPACA_API_SECRET_KEY in .env). Default BTC. Can
                         also be changed at runtime with the [S] key.
    --interval LABEL     bar size: 1s,5s,15s,30s,1m,3m,5m,15m,1H, or a volume
                         bar like 500V / 2500V (default 1m)
    --date MM_DD_YYYY    browse a past day's logged bars (playback, no live feed)
    --backfill-hours N   fixed hours of historical trades to reconstruct on a
                         cold start (default: since 19:00 CT session open;
                         0 disables backfill entirely)
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
  [+] / [-] (or [=] / [_]) zoom in/out — merges every N real bars into one
      displayed candle at each zoom-out step (pure visual OHLC/CVD
      aggregation via merge_bars; the underlying --interval, log, and CSV
      are completely untouched), roughly exponential per press. [+] undoes
      merging back toward the natural 1:1 floor; there's no "zoom in beyond
      1:1" (each real bar is already maximum detail). Note: ←/→ pan steps
      stay in raw-bar units regardless of zoom level, so at a heavy zoom-out
      the coarser [ / ] / { / } tiers will feel more responsive than ←/→.
  [T] toggles the Big Trade Detector overlay on the price panel — recreated
      from chart.py, same formula and thresholds: each bar's buy/sell
      "intensity" ((close−low)/range and (high−close)/range, each × the
      bar's total volume — chart.py's exact proxy, used as-is rather than
      cvd.py's real per-side buy_vol/sell_vol, which turned out to be too
      sparse/bursty bar-to-bar for a meaningful rolling baseline) is
      compared against a rolling z-score baseline from the preceding 10
      bars, flagging cyan (buy, below the low wick) or magenta (sell,
      above the high wick) blocks in 3 escalating tiers (3σ/4.5σ/6σ)
      exactly like chart.py's defaults.
  [S] switches the tracked symbol on the fly — type ETH/BTC for crypto or any
      equity/ETF ticker for Alpaca, hit Enter. Tears down the old feed(s),
      resets all state, and reloads/backfills the new symbol from scratch —
      same live-swap mechanism as [I]'s interval switch, just for the
      underlying instrument instead of the bar shape.
  [Q] / Esc quits.
"""

import sys
import os
import time
import json
import csv
import bisect
import locale
import threading
from pathlib import Path
from collections import deque
from datetime import datetime, timezone, timedelta

# Required for curses to correctly encode the box-drawing glyphs (█│─) used
# for candle bodies/wicks as UTF-8 when writing via addstr — without this,
# ncursesw falls back to the C locale and mangles anything outside ASCII.
# Must happen before curses.initscr()/wrapper() is ever called.
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

# ── .env loader (same pattern as chart.py) ──────────────────────────────────
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

ALPACA_API_KEY_ID = os.environ.get("ALPACA_API_KEY_ID", "")
ALPACA_API_SECRET_KEY = os.environ.get("ALPACA_API_SECRET_KEY", "")

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

# Crypto symbols route through Phemex+Kraken+Coinbase (real triple-exchange
# aggregate); anything else is treated as a US equity/ETF ticker routed
# through Alpaca's free IEX feed instead (single-source, quote-rule-
# classified — see ws_alpaca()/fetch_alpaca_trades_range() for why real CVD
# is still possible there without a paid consolidated-tape subscription).
CRYPTO_SYMBOLS = {"ETH", "BTC"}

SYMBOL = args[0].upper() if args else "BTC"
IS_CRYPTO = SYMBOL in CRYPTO_SYMBOLS
if not IS_CRYPTO and not (ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY):
    print(f"'{SYMBOL}' isn't a crypto symbol ({', '.join(sorted(CRYPTO_SYMBOLS))}), so it's "
          f"treated as an equity/ETF ticker — that needs Alpaca credentials.")
    print("Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env (free paper-trading account).")
    sys.exit(1)

PHEMEX_SYMBOLS   = {"ETH": "ETHUSDT", "BTC": "BTCUSDT"}
KRAKEN_WS_PAIRS  = {"ETH": "ETH/USD", "BTC": "BTC/USD"}
COINBASE_PRODUCT_IDS = {"ETH": "ETH-USD", "BTC": "BTC-USD"}
PHEMEX_WS_URL = "wss://ws.phemex.com"
KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
COINBASE_WS_URL   = "wss://ws-feed.exchange.coinbase.com"
COINBASE_REST_URL = "https://api.exchange.coinbase.com"
# A fresh requests.get() per page pays a full TCP+TLS handshake every time —
# measured ~4s for the first request, ~0.04s for every subsequent one on the
# same connection. Coinbase's trade volume is high enough that a backfill
# needs many pages, so reusing one session across the whole process (not
# just one fetch call) turns what would be minutes of pure connection
# overhead into a rounding error.
_coinbase_session = requests.Session()
ALPACA_WS_URL = "wss://stream.data.alpaca.markets/v2/iex"
ALPACA_REST_URL = "https://data.alpaca.markets/v2"

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

def fetch_kraken_trades_range(pair, since_ts, until_ts, progress=None, deadline=None):
    """Page forward through Kraken's public Trades endpoint from since_ts up
    to until_ts, returning a chronological list of (ts, price, qty, is_buy)
    tuples. Real historical prints, not synthetic — Kraken's public trade
    history goes back arbitrarily far for major pairs. Dedupes the one-trade
    overlap the API leaves at each page boundary. until_ts may be in the
    future (e.g. "now") — pagination just naturally stops once it catches up
    to whatever's actually been traded so far.

    deadline (absolute time.time() cutoff, optional): stops paging once
    reached, returning whatever's been gathered so far (a partial window,
    not all the way back to since_ts) instead of blocking indefinitely.
    Kraken itself is fast enough that this rarely engages in practice —
    it's here mainly so fetch_trades_range() can enforce one shared time
    budget across both crypto sources without special-casing either one."""
    since = int(since_ts * 1e9)
    out = []
    last_id = None
    for page in range(2000):   # safety cap, not expected to be hit
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

# ── COINBASE (second real crypto exchange — public, no API key needed) ─────
def fetch_coinbase_trades_range(product_id, since_ts, until_ts, progress=None, deadline=None):
    """Page backward through Coinbase Exchange's public trades endpoint
    (trade_id-cursor based — no direct timestamp filter like Kraken's
    `since`), collecting (ts, price, qty, is_buy) tuples for
    [since_ts, until_ts]. Real historical prints, no key needed (confirmed
    live: GET /products/<id>/trades returns 200 with no auth headers at all).

    IMPORTANT: Coinbase's `side` field is the MAKER's side, not the
    taker/aggressor's (confirmed against Coinbase's docs) — side=="sell"
    means a resting sell/ask order got hit, i.e. the TAKER bought (an
    up-tick). is_buy is therefore the FLIP of the raw field
    (`t["side"] == "sell"`), matching Kraken/Phemex's convention where
    is_buy already means "aggressive buy volume". Getting this backwards
    would silently invert Coinbase's whole contribution to the aggregate
    CVD with no visible error.

    Always starts from the newest trade and pages backward — there's no way
    to jump straight to an arbitrary point in time on this endpoint — skipping
    anything newer than until_ts before it starts collecting. Cheap when
    until_ts is close to "now" (the common case: session backfill, [S]
    switch); costs extra pages the further until_ts is from "now" (e.g. an
    old [G]oto target).

    deadline (absolute time.time() cutoff, optional): stops paging once
    reached, returning whatever's been gathered so far. Coinbase's BTC-USD
    dust-trade volume is high enough (~15s of real fetch time per hour of
    window, even after connection-reuse) that this is the fetcher that
    actually needs the cap in practice — see fetch_trades_range()."""
    out = []
    after_cursor = None
    for _ in range(5000):   # safety cap, not expected to be hit
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
        for t in page:   # newest-first within the page
            try:
                ts = _parse_rfc3339(t["time"])
                price = float(t["price"])
                qty = float(t["size"])
            except Exception:
                continue
            if ts > until_ts:
                continue   # still ahead of the requested window — skip, keep paging back
            if ts < since_ts:
                hit_old_end = True
                continue
            out.append((ts, price, qty, t["side"] == "sell"))
        if progress and out:
            progress(len(out), datetime.fromtimestamp(out[-1][0]))
        after_cursor = r.headers.get("cb-after")
        if hit_old_end or not after_cursor:
            break
        time.sleep(0.2)   # polite pacing on the public endpoint
    out.reverse()   # collected newest-to-oldest; callers expect chronological ascending
    return out

# ── ALPACA (equity/ETF trade data — free IEX feed, quote-rule classified) ──
def _parse_rfc3339(ts_str):
    """Alpaca timestamps carry up to 9 fractional digits (nanoseconds);
    Python's datetime only parses up to 6 (microseconds) — truncate rather
    than lose the whole field. Sub-microsecond precision doesn't matter for
    bucketing trades into bars."""
    ts_str = ts_str.rstrip("Z")
    if "." in ts_str:
        base, frac = ts_str.split(".")
        ts_str = f"{base}.{frac[:6]}"
    return datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc).timestamp()

def _alpaca_headers():
    return {"APCA-API-KEY-ID": ALPACA_API_KEY_ID, "APCA-API-SECRET-KEY": ALPACA_API_SECRET_KEY}

def _fetch_alpaca_trades_raw(symbol, since_ts, until_ts, progress=None):
    """Historical trade prints (IEX feed) — (ts, price, size), no side yet.
    Calls progress() after every page (same as fetch_kraken_trades_range) so
    a slow fetch for a liquid symbol still shows visible movement instead of
    looking hung."""
    out = []
    page_token = None
    start_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(until_ts, tz=timezone.utc).isoformat()
    for _ in range(2000):
        params = {"symbols": symbol, "start": start_iso, "end": end_iso, "limit": 10000, "feed": "iex"}
        if page_token:
            params["page_token"] = page_token
        try:
            r = requests.get(f"{ALPACA_REST_URL}/stocks/trades", params=params, headers=_alpaca_headers(), timeout=15)
            d = r.json()
        except Exception:
            break
        page_trades = (d.get("trades") or {}).get(symbol) or []
        for t in page_trades:
            out.append((_parse_rfc3339(t["t"]), t["p"], t["s"]))
        if progress and page_trades:
            progress(len(out), datetime.fromtimestamp(out[-1][0]))
        page_token = d.get("next_page_token")
        if not page_token:
            break
        time.sleep(0.2)
    return out

def classify_one_trade(price, bid, ask, prev_price, prev_side):
    """Quote-rule (Lee-Ready) classification for a single trade, shared by
    both the historical batch classifier below and ws_alpaca's live path so
    the two can never drift apart. A trade at/above the prevailing ask is
    buyer-initiated, at/below the bid is seller-initiated (standard — this is
    what real CVD tools do when trades aren't natively side-tagged, which is
    the norm outside a few crypto venues). Falls back to a tick-rule (vs the
    previous trade's price) when the trade prints inside the spread or no
    quote is available yet, and to "assume buy" for the very first trade
    with neither a quote nor prior context to go on."""
    if ask is not None and price >= ask:
        return True
    if bid is not None and price <= bid:
        return False
    if prev_price is not None:
        return price > prev_price if price != prev_price else prev_side
    return True

def classify_trades_quote_rule(raw_trades, quotes):
    """Turn (ts, price, size) trades + (ts, bid, ask) quotes into (ts, price,
    qty, is_buy) — same tuple shape fetch_kraken_trades_range produces, so
    build_bars_from_trades/ingest_trade don't need to know or care which
    exchange/asset-class a trade came from. See classify_one_trade for the
    actual classification rule."""
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

def fetch_alpaca_trades_range(symbol, since_ts, until_ts, progress=None):
    """Real historical Alpaca trade prints, classified into the same (ts,
    price, qty, is_buy) shape the Kraken fetcher produces. IEX feed only
    (free tier) — a real but honest ceiling of roughly 1-3% of total US
    equity volume, not the full consolidated tape (see module docstring).

    Historical/backfilled bars use TICK-RULE classification only (no quotes
    fetched here) — deliberately, not an oversight: measured live, a single
    liquid symbol (QQQ) generates ~31,000 NBBO quote updates in just 5
    minutes (~375k/hour). A backfill spanning many hours would need to page
    through millions of quote rows sequentially with no way to show
    meaningful progress in between, which is indistinguishable from a hung
    app (confirmed — this is exactly what happened before this fix). Trade
    counts are far more tractable (IEX being a small slice of consolidated
    volume). Live bars still get the more accurate quote-rule classification
    via ws_alpaca's own continuously-updated running quote, which has none
    of this scaling problem since it only ever sees new updates as they
    arrive, never a historical backlog."""
    raw_trades = _fetch_alpaca_trades_raw(symbol, since_ts, until_ts, progress=progress)
    return classify_trades_quote_rule(raw_trades, [])

def fetch_trades_range(since_ts, until_ts, progress=None, deadline=None):
    """Dispatch to whichever data source SYMBOL actually belongs to — every
    caller (goto_to, extend_history_backward, backfill_trades) goes through
    this instead of hardcoding Kraken, so the entire ingest/bar-building
    pipeline downstream is asset-class-agnostic.

    Crypto fetches Kraken AND Coinbase concurrently (separate threads, not
    sequential — Coinbase's dust-trade volume makes it far slower per hour
    than Kraken, so running them in parallel lets Coinbase use nearly the
    whole `deadline` budget instead of whatever's left over after Kraken
    finishes first) and merge-sorts them into one chronological list before
    returning — NOT a simple concatenation. ingest_trade()'s bucket-close
    logic silently drops any trade that arrives "late" for an already-closed
    bucket (see its docstring); replaying two sources back-to-back un-merged
    would mean every trade from the second source that belongs earlier than
    the first source's last bucket gets silently dropped, corrupting bars
    instead of just erroring.

    deadline (absolute time.time() cutoff, optional): under a deadline the
    two sources can end up covering DIFFERENT depths (Kraken finishes the
    full requested range in seconds; Coinbase may still be mid-fetch when
    the deadline hits) — trimmed to the LATER (more recent) of the two
    sources' own earliest-reached timestamps, so the returned window never
    contains a stretch where only one exchange's data is present but gets
    treated as if it were the same consistent aggregate as the rest. Phemex
    has no historical trades API at all (unchanged limitation, see
    backfill_trades' docstring), so it only ever contributes live, same as
    before — unaffected by any of this."""
    if IS_CRYPTO:
        results = {}
        # Worker threads must NEVER call the caller's `progress` directly —
        # in curses_main it touches stdscr (erase/addstr/refresh), and
        # curses/ncurses is only safe to drive from the thread that owns
        # the screen (the main thread there). A real interactive launch
        # hung indefinitely after this was first written calling progress()
        # straight from both fetch threads (confirmed: process alive,
        # accumulating real CPU time, but never got past backfill — the
        # bug wasn't caught by earlier testing because that always used
        # progress=None or a plain print callback, never the actual
        # curses-touching one). Fix: threads only record their own latest
        # (n, dt) into `latest`; the ORIGINAL calling thread (the caller's
        # own thread — main thread for curses_main, since backfill runs
        # there synchronously) polls it and is the only one that ever
        # invokes the real progress() callback.
        latest = {}
        progress_state_lock = threading.Lock()
        def make_tracker(key):
            def tracker(n, dt):
                with progress_state_lock:
                    latest[key] = (n, dt)
            return tracker
        def _fetch_kraken():
            results["kraken"] = fetch_kraken_trades_range(
                KRAKEN_REST_PAIRS[SYMBOL], since_ts, until_ts, progress=make_tracker("kraken"), deadline=deadline)
        def _fetch_coinbase():
            results["coinbase"] = fetch_coinbase_trades_range(
                COINBASE_PRODUCT_IDS[SYMBOL], since_ts, until_ts, progress=make_tracker("coinbase"), deadline=deadline)
        t1 = threading.Thread(target=_fetch_kraken, daemon=True)
        t2 = threading.Thread(target=_fetch_coinbase, daemon=True)
        t1.start(); t2.start()
        last_reported = None
        def _relay_progress():
            nonlocal last_reported
            if not progress:
                return
            with progress_state_lock:
                snapshot = dict(latest)
            if not snapshot:
                return
            total_n = sum(v[0] for v in snapshot.values())
            latest_dt = max(v[1] for v in snapshot.values())
            if (total_n, latest_dt) != last_reported:
                progress(total_n, latest_dt)
                last_reported = (total_n, latest_dt)
        while t1.is_alive() or t2.is_alive():
            _relay_progress()
            time.sleep(0.1)
        t1.join(); t2.join()
        _relay_progress()   # final flush — the last update might have landed
                            # right as the threads finished, after the loop's
                            # last check but before this
        kraken_trades = results.get("kraken", [])
        coinbase_trades = results.get("coinbase", [])
        # Only a source that actually returned something can veto how far
        # back the OTHER source's data is trusted — a source returning
        # nothing doesn't necessarily mean "reached zero coverage", it can
        # also mean "ran out of deadline before reaching this window at
        # all" (real case: Coinbase always pages backward from its own
        # newest trade, so under a tight deadline, asking for an
        # already-old until_ts — exactly what extend_history_backward
        # does on every call past the first — can burn the whole budget
        # just skipping past everything newer than until_ts, collecting
        # nothing). Treating that as "0 coverage" and using it to discard
        # the other source's perfectly good data would be strictly worse
        # than not having deadline-trimming at all. So: an empty result
        # opts OUT of the trim decision entirely, not in at until_ts.
        starts = [since_ts]
        if kraken_trades:
            starts.append(kraken_trades[0][0])
        if coinbase_trades:
            starts.append(coinbase_trades[0][0])
        effective_since = max(starts)
        combined = [t for t in (kraken_trades + coinbase_trades) if t[0] >= effective_since]
        combined.sort(key=lambda t: t[0])
        return combined
    return fetch_alpaca_trades_range(SYMBOL, since_ts, until_ts, progress=progress)

def fetch_trades_since(hours, progress=None, deadline=None):
    """Trailing-window wrapper, asset-class-aware: equities' free-tier REST
    is 15-minute delayed (crypto's isn't), so "now" would otherwise ask for
    data that doesn't exist yet — capped accordingly. The live WebSocket
    feed picks up the remaining gap in real time regardless."""
    now = time.time()
    until_ts = now if IS_CRYPTO else now - 900
    return fetch_trades_range(now - hours * 3600, until_ts, progress=progress, deadline=deadline)

INITIAL_BACKFILL_BUDGET_SECS = 22   # target ceiling is 30s; set at 22 here because
                                    # the deadline is only checked BETWEEN pages —
                                    # whatever page/thread is already in flight when
                                    # it fires still has to finish (measured
                                    # ~1-4s of real overshoot beyond the deadline
                                    # across test runs, occasionally more under
                                    # network variance). Cold-start / [S] switch /
                                    # interval switch — all "fresh start" scenarios,
                                    # capped so the app is always usable quickly
                                    # regardless of how far back the ideal
                                    # session-anchor window would otherwise reach.
                                    # Whatever doesn't fit loads on demand via
                                    # extend_history_backward() as the user scrolls
                                    # back past the loaded edge.

def backfill_trades(hours, progress=None, reset=True):
    """Reconstruct bars/CVD from real historical trade data before live feeds
    start — Kraken+Coinbase (merged chronologically) for crypto, Alpaca (IEX,
    quote-rule classified) for equities/ETFs, via fetch_trades_since(). Runs
    the exact same ingest_trade() path as live trades, so the resulting bars
    are exact, not approximated — crypto is just missing Phemex for this
    stretch (it has no historical trades API; Alpaca's free tier is
    IEX-only, see module docstring for both).

    Bounded by INITIAL_BACKFILL_BUDGET_SECS — `hours` is still the IDEAL
    request (e.g. all the way back to the 19:00 CT session anchor), but the
    actual amount loaded is whatever fits in that time budget; the rest is
    picked up transparently later by extend_history_backward() once the user
    scrolls near the loaded edge (see GOTO_EDGE_TRIGGER). Every current
    caller of this function (cold start, [S] symbol switch, in-app interval
    switch) is a "fresh start" scenario, so this budget applies unconditionally.

    reset=True (default) truncates today's on-disk log and clears in-memory
    state first, so the backfill always supersedes whatever partial/stray log
    might already exist for today — "resume" only ever existed as a
    workaround for data sources with NO real history API (gex.py's Deribit/
    CBOE); both Kraken and Alpaca actually have one, so a fresh reconstruction
    is strictly better than resuming a stale local file. Nothing is touched
    until the fetch actually returns data, so a network hiccup can't wipe
    real history."""
    trades = fetch_trades_since(hours, progress=progress, deadline=time.time() + INITIAL_BACKFILL_BUDGET_SECS)
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
    priority whenever it succeeds (see backfill_trades docstring for why),
    falling back to resuming today's existing log only when backfill is
    disabled (--backfill-hours 0) or the fetch came back empty. Returns a
    short status string for the caller to display/print.
    Locked even in the fallback path — harmless at cold start (nothing else
    is running yet) but required when called mid-session, since the live WS
    threads are already mutating state.history/live/raw_cvd concurrently."""
    if BACKFILL_HOURS > 0:
        n = backfill_trades(BACKFILL_HOURS, progress=progress, reset=True)
        if n:
            src = "Kraken+Coinbase" if IS_CRYPTO else "Alpaca (IEX)"
            since_str = datetime.fromtimestamp(time.time() - BACKFILL_HOURS * 3600).strftime('%H:%M:%S')
            return f"backfilled {n} {src} trades since {since_str}"
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
        self.coinbase_status = "connecting…"
        self.alpaca_status = "connecting…"   # only used for equity/ETF symbols
        self.last_price = None
        self.session = 0
        self.backfill_boundary_ts = None   # set once: everything <= this is Kraken+Coinbase only (no Phemex)
        self.history_loading_older = False   # guards extend_history_backward from overlapping itself
        self.pending_shift = 0    # bars just prepended by extend_history_backward,
                                  # not yet folded into the caller's view index
        self.goto_label = None   # last [G] target, for the header display only
        self.alpaca_ws_app = None   # live WebSocketApp ref, so it can be force-closed
                                     # (session switch / quit) instead of waiting for
                                     # Alpaca's own dead-socket detection — see stop_alpaca_ws()

state = State()

def stop_alpaca_ws():
    """Force-close any live Alpaca WS connection right now, rather than
    leaving it to time out on Alpaca's side. Alpaca's free/IEX tier allows
    exactly ONE concurrent connection per account — if a previous run was
    killed (or a symbol switch just abandons the old socket), the server
    can take a while to notice the dead connection, and every reconnect
    attempt in the meantime gets rejected with "connection limit exceeded".
    Calling this on quit and before every symbol switch releases the slot
    immediately instead of relying on that server-side timeout."""
    app = state.alpaca_ws_app
    if app is not None:
        try:
            app.close()
        except Exception:
            pass
        state.alpaca_ws_app = None

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
    trades = fetch_trades_range(since_ts, until_ts, progress=progress)
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

EXTEND_BACKWARD_BUDGET_SECS = 10   # target ceiling is 15s; set at 10 here for
                                   # the same in-flight-page overshoot margin as
                                   # INITIAL_BACKFILL_BUDGET_SECS (measured up to
                                   # ~4s overshoot on an unlucky page under real
                                   # network variance). GOTO_WINDOW_HOURS below is
                                   # just the ideal ask ceiling; how far back a call
                                   # actually reaches depends on how fast that
                                   # stretch fetches (quiet periods reach further,
                                   # busy ones less)

def extend_history_backward():
    """Fetch the chunk immediately before the oldest currently-loaded bar and
    prepend it — triggered whenever panning gets near the start of
    state.history, regardless of whether that history got there via the
    initial 19:00 CT backfill, a [G]oto fetch, or a previous call to this
    same function. Runs in a background thread; safe to call even if it
    finds nothing (the boundary still advances so we don't re-fetch the
    same empty range forever). Bounded by EXTEND_BACKWARD_BUDGET_SECS so a
    single scroll-triggered fetch never blocks the UI for long — scrolling
    further just triggers another call for the next chunk."""
    with state.lock:
        if state.history_loading_older or not (state.history or state.live):
            return
        state.history_loading_older = True
        oldest_bar = state.history[0] if state.history else state.live
        old_since = oldest_bar["ts"]
        join_baseline = oldest_bar["cvd_open"]
    new_since = old_since - GOTO_WINDOW_HOURS * 3600
    trades = fetch_trades_range(new_since, old_since, progress=None, deadline=time.time() + EXTEND_BACKWARD_BUDGET_SECS)
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

def start_feeds(session):
    """Start the live WS feed(s) for the current SYMBOL — Phemex+Kraken+Coinbase
    (true triple-exchange aggregate) for crypto, or the single Alpaca IEX feed
    for equities/ETFs. Shared by curses_main and headless_main so a [S]ymbol
    switch and a fresh launch use identically-behaving startup logic."""
    if IS_CRYPTO:
        threading.Thread(target=ws_kraken, args=(session,), daemon=True).start()
        threading.Thread(target=ws_phemex, args=(session,), daemon=True).start()
        threading.Thread(target=ws_coinbase, args=(session,), daemon=True).start()
    else:
        threading.Thread(target=ws_alpaca, args=(session,), daemon=True).start()

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
        # corrupting volume-bar ordering. Real history comes from backfill_trades().
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
        # backfill_trades(); only fold in genuine live prints here.
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

# ── COINBASE TRADE FEED ──────────────────────────────────────────────────────
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
        # "last_match" fires once right after subscribing (a snapshot of
        # whatever traded just before we connected) — skip it, same reason
        # ws_kraken/ws_phemex skip their own snapshot-on-connect quirks: real
        # history comes from backfill_trades(), not a surprise replay here
        # that could land out of order relative to what backfill already covered.
        if mtype != "match":
            return
        try:
            ts = _parse_rfc3339(msg["time"])
            price = float(msg["price"])
            qty = float(msg["size"])
            # side is the MAKER's side (see fetch_coinbase_trades_range's
            # docstring) — flip it to get the taker/aggressor side
            is_buy = msg["side"] == "sell"
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
        ws_app = websocket.WebSocketApp(
            COINBASE_WS_URL, on_open=on_open, on_message=on_message,
            on_error=on_error, on_close=on_close,
        )
        ws_app.run_forever(ping_interval=30, ping_timeout=10)
        if stale():
            break
        with state.lock:
            if stale(): break
            state.coinbase_status = f"reconnecting… ({backoff}s)"
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

# ── ALPACA TRADE FEED (equities/ETFs — single source, quote-rule classified) ─
def ws_alpaca(session):
    """Real-time IEX trades+quotes for an equity/ETF symbol. Alpaca has no
    native buy/sell tag on trades (same as most non-crypto venues), so this
    maintains a running bid/ask from the quote stream and classifies each
    trade live via classify_one_trade() — the exact same rule
    fetch_alpaca_trades_range() uses for backfill, so the live and
    backfilled portions are computed identically, not just similarly."""
    def stale():
        return state.session != session

    latest = {"bid": None, "ask": None, "prev_price": None, "prev_side": True}

    def on_open(ws):
        ws.send(json.dumps({"action": "auth", "key": ALPACA_API_KEY_ID, "secret": ALPACA_API_SECRET_KEY}))

    def on_message(ws, message):
        if stale(): return
        try:
            msgs = json.loads(message)
        except Exception:
            return
        if not isinstance(msgs, list):
            msgs = [msgs]
        for msg in msgs:
            mtype = msg.get("T")
            if mtype == "success" and msg.get("msg") == "authenticated":
                ws.send(json.dumps({"action": "subscribe", "trades": [SYMBOL], "quotes": [SYMBOL]}))
                with state.lock:
                    if stale(): return
                    state.alpaca_status = "live"
            elif mtype == "error":
                with state.lock:
                    if stale(): return
                    state.alpaca_status = f"err: {str(msg.get('msg', ''))[:30]}"
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
                with state.lock:
                    if stale(): return
                    ingest_trade(ts, price, qty, is_buy)
                    state.alpaca_status = "live"

    last_err = {"text": ""}

    def on_error(ws, err):
        if stale(): return
        text = str(err)[:30]
        last_err["text"] = text
        with state.lock:
            if stale(): return
            state.alpaca_status = f"err: {text}"

    def on_close(ws, code, msg):
        if stale(): return
        with state.lock:
            if stale(): return
            state.alpaca_status = "reconnecting…"

    backoff = 1
    while not stale():
        ws_app = websocket.WebSocketApp(
            ALPACA_WS_URL, on_open=on_open, on_message=on_message,
            on_error=on_error, on_close=on_close,
        )
        state.alpaca_ws_app = ws_app
        ws_app.run_forever(ping_interval=30, ping_timeout=10)
        state.alpaca_ws_app = None
        if stale():
            break
        # "connection limit exceeded" means Alpaca's server still thinks a
        # previous connection is live (e.g. a killed process it hasn't
        # detected as dead yet) — retrying fast just gets rejected again,
        # so floor the wait well above the normal 1s/2s/4s ramp.
        if "connection limit" in last_err["text"].lower():
            backoff = max(backoff, 15)
        with state.lock:
            if stale(): break
            state.alpaca_status = f"reconnecting… ({backoff}s)"
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)

# ── COLOUR PAIRS ─────────────────────────────────────────────────────────────
P_DEFAULT, P_DIM, P_CYAN, P_YELLOW, P_GREEN, P_RED, P_STATUS, P_MAGENTA = range(1, 9)

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
    curses.init_pair(P_MAGENTA, curses.COLOR_MAGENTA, BG)   # BTD sell signal (buy uses P_CYAN)

def cp(pair, bold=False, dim=False):
    a = curses.color_pair(pair)
    if bold: a |= curses.A_BOLD
    if dim:  a |= curses.A_DIM
    return a

_shadow_buf = None   # [P] Screenshot support — see _shadow_put()

def _shadow_put(y, x, s):
    """Mirror a just-drawn string into the screenshot shadow buffer, one
    real character per column. Screenshots read from THIS, never from
    curses' own win.instr() — instr() reads back through the narrow
    chtype API, which can't correctly reconstruct the multi-byte Unicode
    box/block glyphs (█ │ ─) this app draws with, corrupting/misaligning
    exactly those characters (same class of bug as the Termux addch()
    glyph corruption, on the read side this time). Tracking what we
    actually asked curses to draw sidesteps the round-trip entirely."""
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
    """Dump the last-rendered frame to screenshots/cvd_<SYMBOL>_<ts>.txt
    (same convention as charthacker.py's [P] key). Reads from the
    _shadow_buf tracked alongside every real draw call (see _shadow_put) —
    NOT from curses' win.instr(), which corrupts the multi-byte candle
    glyphs on readback. win is only used as a last-resort size fallback
    if no frame has been drawn yet (shouldn't happen in practice: draw()
    always runs at least once before the first key is read)."""
    folder = os.path.join(os.path.dirname(__file__), "screenshots")
    os.makedirs(folder, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fn = os.path.join(folder, f"cvd_{SYMBOL}_{ts}.txt")
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

def fmt_qty(q):
    if abs(q) >= 1000:
        return f"{q/1000:,.1f}k"
    return f"{q:,.2f}"

def fmt_time(ts):
    fine = BAR_MODE == "volume" or BAR_SECS < 60
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S" if fine else "%H:%M")

# ── ZOOM (merge N real bars into 1 displayed candle — [+]/[-]) ────────────
ZOOM_MAX_GROUP = 500

def merge_bars(group):
    """Combine consecutive closed bars into one OHLC+CVD candle — used only
    for the [+]/[-] zoom display (pure visual aggregation; the underlying
    --interval, log, and CSV are completely untouched). Standard OHLC merge
    for price; CVD open/close come directly from the group's first/last
    values (already a continuous running series), high/low are the extremes
    actually reached anywhere within the group."""
    if len(group) == 1:
        return group[0]
    o, c = group[0]["o"], group[-1]["c"]
    h = max(b["h"] for b in group)
    l = min(b["l"] for b in group)
    buy_vol = sum(b["buy_vol"] for b in group)
    sell_vol = sum(b["sell_vol"] for b in group)
    cvd_open = group[0].get("cvd_open", group[0].get("cvd", 0.0))
    cvd = group[-1].get("cvd", cvd_open)
    cvd_high = max(b.get("cvd_high", b.get("cvd", cvd_open)) for b in group)
    cvd_low = min(b.get("cvd_low", b.get("cvd", cvd_open)) for b in group)
    return {"ts": group[0]["ts"], "o": o, "h": h, "l": l, "c": c,
            "buy_vol": buy_vol, "sell_vol": sell_vol, "delta": buy_vol - sell_vol,
            "cvd_open": cvd_open, "cvd_high": cvd_high, "cvd_low": cvd_low, "cvd": cvd}

def zoom_bars(all_bars, group_size):
    """Resample the WHOLE bar sequence (not just the visible tail) at the
    given group_size, so bars just before the visible window are still
    available at the correct zoom level for BTD's lookback baseline.
    group_size=1 is the identity (no zoom)."""
    if group_size <= 1 or not all_bars:
        return all_bars
    return [merge_bars(all_bars[i:i + group_size]) for i in range(0, len(all_bars), group_size)]

def zoom_step(size, direction):
    """One [+]/[-] press: roughly exponential in/out so it feels like a zoom
    rather than a linear scroll — direction>0 zooms out (bigger groups, more
    history per screen), direction<0 zooms in (smaller groups, toward the
    natural 1:1 floor)."""
    if direction > 0:
        return min(ZOOM_MAX_GROUP, size + max(1, size // 3) + 1)
    return max(1, size - max(1, size // 4) - 1)

# ── BIG TRADE DETECTOR (recreated from chart.py) ────────────────────────────
# Same statistical test as chart.py: a bar's buy/sell volume is compared
# against a rolling z-score baseline from the preceding BTD_LOOKBACK bars
# (not including itself); T1/T2/T3 tiers at sigma/sigma+1.5/sigma+3.0 match
# chart.py's exact thresholds and defaults. The one deliberate difference:
# chart.py has no real trade-side data, so it infers a buy/sell split from
# where each candle's close sits within its own high-low range, weighted by
# total volume — cvd.py already has the REAL buy_vol/sell_vol per bar (from
# actual trade tape), so this feeds the identical test with that strictly
# more accurate input instead of chart.py's proxy.
BTD_LOOKBACK = 10
BTD_SIGMA = 3.0

LIVE_LINE_CH = "─"   # live-price line on the price panel, matching footprint.py's

def compute_btd_signals(bars, lookback=BTD_LOOKBACK, sigma=BTD_SIGMA):
    """Returns {index: {"buy": tier|None, "sell": tier|None}} for every bar
    in `bars` that qualifies (index >= lookback, matching chart.py's own
    warm-up gate). Both buy and sell are checked independently (not
    elif) — matching chart.py exactly, a single bar can in principle flag
    both sides simultaneously, though it's rare in real data.

    IMPORTANT: uses chart.py's EXACT input formula —
      buy_intensity  = (close - low)  / (high - low) * total_volume
      sell_intensity = (high - close) / (high - low) * total_volume
    — NOT raw buy_vol/sell_vol directly, even though cvd.py has that real
    per-bar data and chart.py has to infer this proxy from candle shape
    (it has no real trade-side data at all). Tried raw buy_vol/sell_vol
    first; empirically it over-triggered massively (~17% of real 1m bars
    vs a "3-sigma" event's theoretical ~0.1%) because raw per-side volume
    is extremely sparse/bursty bar-to-bar (long stretches near zero on one
    side purely from random trade timing, not genuine calm), so the
    rolling baseline's mean/stdev were themselves often tiny — trivially
    cleared by an unremarkable bar. chart.py's proxy is anchored to each
    bar's TOTAL volume (buy_vol+sell_vol, rarely near-zero) and varies
    continuously with candle shape, giving the well-behaved baseline the
    sigma=3.0 threshold was actually designed against — matching chart.py's
    formula, not just its threshold constants, is what reproduces its
    actual (much rarer) signal frequency."""
    buy_iv, sell_iv = [], []
    for b in bars:
        rng = b["h"] - b["l"]
        vol = b["buy_vol"] + b["sell_vol"]
        if rng > 0:
            buy_iv.append((b["c"] - b["l"]) / rng * vol)
            sell_iv.append((b["h"] - b["c"]) / rng * vol)
        else:
            buy_iv.append(0.0)
            sell_iv.append(0.0)

    signals = {}
    n = len(bars)
    for i in range(lookback, n):
        wb = buy_iv[i - lookback:i]
        ws = sell_iv[i - lookback:i]
        m = len(wb)
        if m < 2:
            continue
        mb, ms = sum(wb) / m, sum(ws) / m
        sdb = (sum((x - mb) ** 2 for x in wb) / (m - 1)) ** 0.5
        sds = (sum((x - ms) ** 2 for x in ws) / (m - 1)) ** 0.5
        cb, cs = buy_iv[i], sell_iv[i]
        entry = {}
        t1b, t2b, t3b = mb + sdb * sigma, mb + sdb * (sigma + 1.5), mb + sdb * (sigma + 3.0)
        if cb > t1b:
            entry["buy"] = 3 if cb > t3b else (2 if cb > t2b else 1)
        t1s, t2s, t3s = ms + sds * sigma, ms + sds * (sigma + 1.5), ms + sds * (sigma + 3.0)
        if cs > t1s:
            entry["sell"] = 3 if cs > t3s else (2 if cs > t2s else 1)
        if entry:
            signals[i] = entry
    return signals

def draw_candles(win, visible, rows, ohlc_fn, plot_w, zero_line=False, cursor_idx=-1, fmt_fn=None,
                  btd_signals=None, live_price=None):
    """Shared OHLC candlestick renderer — used for both the price panel and
    the CVD panel (candles built from cvd_open/high/low/close) so the two
    panels share identical visual language. Returns (vmin, vmax, live_row) —
    live_row (see live_price below) is returned so the caller can avoid
    overwriting that row with a regular axis tick label.

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
    range — otherwise there's nothing meaningful to draw.

    btd_signals (Big Trade Detector, recreated from chart.py — see
    compute_btd_signals): {column_index: {"buy": tier, "sell": tier}}. Buy
    markers render as reversed cyan block(s) just below the bar's low wick,
    sell as reversed magenta just above the high wick — tier 1 = single
    cell, tier 2 = 1-wide x 2-tall, tier 3 = 3-wide x 2-tall, matching
    chart.py's exact tier shapes. Marked cells are added to `occupied` so
    the crosshair's guide lines skip over them instead of overwriting.

    live_price (same feature as footprint.py's live price line/box, just
    adapted to this app's right-side axis instead of footprint's left-side
    one): when given and it falls within [vmin, vmax], draws a dashed line
    across the whole panel at that row (skipping any occupied cell, same as
    the crosshair does — runs behind the data, never through it) plus a
    "▶<price>" tag on the right axis in reverse-video so it reads as a solid
    tag. Stays visible even when scrolled back into history, as long as that
    price still falls within whatever range is currently on screen — it
    simply doesn't appear otherwise, same as any other value would be."""
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

    live_row = to_row(live_price) if (live_price is not None and vmin <= live_price <= vmax) else None

    def _w(y, x, s, attr):
        win.addstr(y, x, s, attr)
        _shadow_put(y, x, s)

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
            _w(r, i, "│", cp(P_DIM))
            occupied.add((r, i))
        body_top, body_bot = min(r_op, r_cl), max(r_op, r_cl)
        for r in range(body_top, body_bot + 1):
            _w(r, i, "█", color)
            occupied.add((r, i))
        if body_top == body_bot:
            _w(body_top, i, "─", color)
            occupied.add((body_top, i))
        if is_cursor:
            cursor_row, cursor_val = r_cl, c

        if btd_signals and i in btd_signals:
            sig = btd_signals[i]
            rev = curses.A_BOLD | curses.A_REVERSE
            if "buy" in sig:
                tier = sig["buy"]
                row_b = min(rows[-1], r_lo + 1)
                width = 3 if tier == 3 else 1
                for dx in range(-(width // 2), width // 2 + 1):
                    col = i + dx
                    # tier 3 spans 3 columns (matching chart.py), which can reach an
                    # ADJACENT candle's own column — never overwrite a cell that
                    # candle already drew its body/wick into (or that a prior BTD
                    # marker already claimed), or the marker corrupts real candle
                    # data instead of just annotating alongside it
                    if 0 <= col < plot_w and (row_b, col) not in occupied:
                        _w(row_b, col, "#", cp(P_CYAN) | rev)
                        occupied.add((row_b, col))
                        if tier >= 2 and row_b + 1 <= rows[-1] and (row_b + 1, col) not in occupied:
                            _w(row_b + 1, col, "#", cp(P_CYAN) | rev)
                            occupied.add((row_b + 1, col))
            if "sell" in sig:
                tier = sig["sell"]
                row_s = max(rows[0], r_hi - 1)
                width = 3 if tier == 3 else 1
                for dx in range(-(width // 2), width // 2 + 1):
                    col = i + dx
                    if 0 <= col < plot_w and (row_s, col) not in occupied:
                        _w(row_s, col, "#", cp(P_MAGENTA) | rev)
                        occupied.add((row_s, col))
                        if tier >= 2 and row_s - 1 >= rows[0] and (row_s - 1, col) not in occupied:
                            _w(row_s - 1, col, "#", cp(P_MAGENTA) | rev)
                            occupied.add((row_s - 1, col))

    if 0 <= cursor_idx < len(visible):
        for r in rows:
            if (r, cursor_idx) not in occupied:
                _w(r, cursor_idx, ":", cp(P_YELLOW, dim=True))
    if cursor_row is not None:
        for c2 in range(plot_w):
            if (cursor_row, c2) not in occupied:
                _w(cursor_row, c2, "-", cp(P_YELLOW, dim=True))
        if fmt_fn:
            safe_add(win, cursor_row, plot_w + 1, fmt_fn(cursor_val), cp(P_YELLOW, bold=True))

    # live price line/box — drawn last so it sits on top of everything else
    # already in `occupied` (candles, BTD markers, crosshair), but still
    # never overwrites an actual candle glyph itself, same rule as the
    # crosshair's own guide lines
    if live_row is not None:
        for c2 in range(plot_w):
            if (live_row, c2) not in occupied:
                _w(live_row, c2, LIVE_LINE_CH, cp(P_YELLOW, dim=True))
        if fmt_fn:
            safe_add(win, live_row, plot_w + 1, f"▶{fmt_fn(live_price)}",
                     cp(P_YELLOW, bold=True) | curses.A_REVERSE)

    return vmin, vmax, live_row

# ── DRAW ──────────────────────────────────────────────────────────────────
def draw(win, bars, live_bar, status_line, cursor_idx=-1, zoom_group=1, show_btd=False):
    """Returns n_vis — the number of bars actually rendered this frame — so
    the caller can track it for the NEXT frame's crosshair/pan arithmetic
    (the exact visible count depends on terminal width, only known here)."""
    global _shadow_buf
    h, w = win.getmaxyx()
    win.erase()
    _shadow_buf = [[" "] * w for _ in range(h)]

    zoom_tag = f"  zoom:{zoom_group}x" if zoom_group > 1 else ""
    btd_tag = "  BTD:ON" if show_btd else ""
    header = f" AGGREGATE CVD — {SYMBOL}  bar:{INTERVAL_LABEL}{zoom_tag}{btd_tag}  "
    safe_add(win, 0, 0, header.ljust(w), cp(P_STATUS))

    all_bars = bars + ([live_bar] if live_bar else [])
    if len(all_bars) < 1:
        safe_add(win, h // 2, max(0, (w - 20) // 2), "waiting for trades…", cp(P_CYAN))
        win.noutrefresh()
        return 0

    all_bars = zoom_bars(all_bars, zoom_group)
    btd_signals_full = compute_btd_signals(all_bars) if show_btd else None

    axis_w = 12               # right-side price/cvd axis label gutter
    plot_w = max(1, w - axis_w)
    n = min(len(all_bars), plot_w)
    visible = all_bars[-n:]
    cursor_idx = cursor_idx if 0 <= cursor_idx < n else -1
    offset = len(all_bars) - n
    btd_signals = ({i: btd_signals_full[offset + i] for i in range(n) if (offset + i) in btd_signals_full}
                   if btd_signals_full else None)

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

    pmin, pmax, live_row = draw_candles(win, visible, price_rows, _price_ohlc, plot_w,
                              cursor_idx=cursor_idx, fmt_fn=fmt_price, btd_signals=btd_signals,
                              live_price=state.last_price)
    prows = len(price_rows)
    for tick_frac in (0.0, 0.5, 1.0):
        r = price_rows[int(round(tick_frac * (prows - 1)))]
        if r == live_row:
            continue   # the live-price tag already occupies this row — don't overwrite it
        p = pmax - tick_frac * (pmax - pmin)
        safe_add(win, r, plot_w + 1, fmt_price(p), cp(P_DIM))

    # ---- CVD panel (candles, same shape as the price panel above) ----
    def _cvd_ohlc(b):
        c = b["cvd"] if b.get("cvd") is not None else state.raw_cvd - state.cvd_offset
        o = b.get("cvd_open", c)
        hi = b.get("cvd_high", max(o, c))
        lo = b.get("cvd_low", min(o, c))
        return o, hi, lo, c

    cmin, cmax, _ = draw_candles(win, visible, cvd_rows, _cvd_ohlc, plot_w, zero_line=True,
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
        feed_status = (f"Phemex:{state.phemex_status}  Kraken:{state.kraken_status}  Coinbase:{state.coinbase_status}" if IS_CRYPTO
                       else f"Alpaca(IEX):{state.alpaca_status}")
        info = (f" px:{fmt_price(last_price)}  CVD:{last_cvd:+,.2f}  Δbar:{last_delta:+,.2f}  "
                f"{feed_status}  log:{state.log_rows}  {status_line}")
    if state.backfill_boundary_ts:
        boundary = datetime.fromtimestamp(state.backfill_boundary_ts).strftime("%H:%M:%S")
        if IS_CRYPTO:
            info += f"  |  Kraken+Coinbase-only before {boundary}, Phemex-inclusive aggregate after"
        else:
            info += f"  |  backfilled before {boundary} (Alpaca IEX, quote-rule classified throughout)"
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

def _prompt_symbol(stdscr):
    """[S] key — returns the typed symbol uppercased, or None if
    cancelled/empty."""
    buf = _prompt_text(stdscr, "New symbol (crypto: ETH/BTC, or any equity/ETF ticker) — Enter=apply, Esc=cancel: ")
    return buf.strip().upper() if buf else None

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
    global INTERVAL_LABEL, BAR_MODE, BAR_SECS, VOL_THRESHOLD, SYMBOL, IS_CRYPTO
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(200)
    init_colors()

    live_follow = True
    view_end_idx = 0
    cursor_idx = -1     # crosshair: -1 = none (pure live / plain view)
    last_n_vis = 0      # bars actually rendered last frame — for cursor activation/pan math
    zoom_group = 1      # [+]/[-]: how many real bars are merged into 1 displayed candle
    show_btd = False    # [T]: Big Trade Detector overlay on the price panel
    screenshot_msg = None   # [P]: transient confirmation, shown for 5s
    screenshot_until = 0

    if HISTORICAL_MODE:
        for row in load_log(VIEW_DATE):
            state.history.append(row)
        status_line = "history mode — no live feed"
    else:
        def _progress(n, last_dt):
            h, w = stdscr.getmaxyx()
            src = "Kraken+Coinbase" if IS_CRYPTO else "Alpaca"
            msg = f"Backfilling {BACKFILL_HOURS:g}h from {src}… {n} trades ({last_dt.strftime('%H:%M:%S')})"
            stdscr.erase()
            safe_add(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg, cp(P_CYAN))
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
        elif key in (ord('z'), ord('Z')):
            with state.lock:
                state.cvd_offset = state.raw_cvd
        elif key in (ord('t'), ord('T')):
            show_btd = not show_btd
        elif key in (ord('+'), ord('=')):
            zoom_group = zoom_step(zoom_group, -1)
        elif key in (ord('-'), ord('_')):
            zoom_group = zoom_step(zoom_group, 1)
        elif key in (ord('p'), ord('P')):
            fn = take_screenshot(stdscr)
            screenshot_msg = f"Screenshot: {os.path.basename(fn)}"
            screenshot_until = time.time() + 5
        elif key in (ord('s'), ord('S')) and not HISTORICAL_MODE:
            new_symbol = _prompt_symbol(stdscr)
            if new_symbol is not None:
                new_is_crypto = new_symbol in CRYPTO_SYMBOLS
                if not new_is_crypto and not (ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY):
                    status_line = f"'{new_symbol}' needs ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY in .env"
                    init_shown_at = time.time()
                else:
                    # bump session FIRST so the old symbol's WS threads notice
                    # they're stale and stop touching state as early as
                    # possible, before the reset below even begins
                    state.session += 1
                    session = state.session
                    stop_alpaca_ws()   # release Alpaca's connection slot now, don't
                                       # wait for its server-side dead-socket timeout
                    with state.lock:
                        state.history.clear()
                        state.live = None
                        state.raw_cvd = 0.0
                        state.cvd_offset = 0.0
                        state.log_rows = 0
                        state.log_err = None
                        state.pending_shift = 0
                        state.history_loading_older = False
                        state.backfill_boundary_ts = None
                        state.phemex_status = "connecting…"
                        state.kraken_status = "connecting…"
                        state.coinbase_status = "connecting…"
                        state.alpaca_status = "connecting…"
                        csv_state.live = None
                        csv_state.day_str = None
                    SYMBOL, IS_CRYPTO = new_symbol, new_is_crypto

                    def _symbol_progress(n, last_dt):
                        hh, ww = stdscr.getmaxyx()
                        msg = f"Loading {new_symbol}… {n} trades ({last_dt.strftime('%H:%M:%S')})"
                        stdscr.erase()
                        safe_add(stdscr, hh // 2, max(0, (ww - len(msg)) // 2), msg, cp(P_CYAN))
                        stdscr.refresh()
                    status_line = initialize_today(progress=_symbol_progress)
                    start_feeds(session)
                    live_follow = True
                    view_end_idx = 0
                    cursor_idx = -1
                init_shown_at = time.time()
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
        if screenshot_msg and time.time() < screenshot_until:
            sl = f"{screenshot_msg}  |  {sl}"

        last_n_vis = draw(stdscr, draw_bars, draw_live, sl, cursor_idx=cursor_idx,
                          zoom_group=zoom_group, show_btd=show_btd) or 0
        curses.doupdate()

    stop_alpaca_ws()   # release Alpaca's connection slot immediately on quit,
                       # instead of leaving it to the server's own timeout

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
    start_feeds(session)

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
        stop_alpaca_ws()
        print("\ncvd.py headless logger — stopped.")
        return
    try:
        curses.wrapper(curses_main)
    except KeyboardInterrupt:
        pass
    stop_alpaca_ws()

if __name__ == "__main__":
    main()
