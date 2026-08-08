#!/usr/bin/env python3
"""
drift.py — Net Drift (Premium) Chart
Curses terminal chart: cumulative net options premium flow for calls (green)
and puts (red), plotted against the underlying's live price (blue, right
axis), with a per-interval $ volume panel below — styled after the
"Net Drift" tab of retail options-flow dashboards (colored-dot legend up
top, dual Y axis, green volume area, shared time axis at the bottom).

NET DRIFT METHODOLOGY — differs by source, because Deribit exposes a real
trade tape (ground-truth buy/sell) but CBOE's free feed only exposes
periodic snapshots:

  ETH / BTC (crypto) — EXACT, not a proxy. Every --interval seconds, pulls
    every real trade print on the nearest-expiry chain since the last poll
    from Deribit's public trade tape (get_last_trades_by_currency_and_time,
    kind=option, filtered to the nearest-expiry instrument set). Each
    trade's own `direction` field ("buy"/"sell", Deribit's real taker side)
    signs it — no guessing. usd = price(coin) * amount * index_price(USD).
    On the very first poll of a fresh session this only records a cursor
    (no trades pulled yet, so no double count); resuming from an existing
    log instead resumes the cursor from the log's last timestamp, so a
    restart mid-day backfills whatever traded while the tool was down
    rather than silently skipping it — Deribit's trade tape makes that
    possible where CBOE's snapshot-only feed (below) can't.

  anything else (equity/ETF ticker) — approximate, via CBOE's delayed
    quotes feed (~15m delay, same feed chain.py/gex.py/charm.py use),
    which has no trade-side tape at all, only periodic per-contract
    snapshots. Every --interval seconds, for each contract:
      delta = cumulative_volume_now - cumulative_volume_prev   (skipped on
              the very first poll, which only sets the baseline — so a
              restart never double-counts, though unlike crypto it also
              can't backfill downtime, since CBOE has no historical tape)
      usd   = delta * last_trade_price * 100   (standard equity contract
              size, applied only to that poll's own volume delta)
      sign  = +1 if last_trade_price >= mid(bid, ask) else -1   ("aggressor
              proxy" — nearer the ask looks buyer-initiated, nearer the bid
              looks seller-initiated; 0/no attribution if bid+ask are both
              unavailable, so an unclassifiable print still counts toward
              the volume panel but isn't forced into either line)

  Both sides: calls' signed usd accumulates into one running total, puts
  into another, logged to logs/YYYY/MM/DD/drift_<SYMBOL>_MM_DD_YYYY.jsonl
  (same layout convention as charm.py's own append_log). The bottom panel
  is unsigned |usd| summed across calls+puts each poll — total $ premium
  activity that interval, not contract count.

RESETS — two independent triggers, whichever fires first:
  1. New calendar day (local time) — a new log file starts, so the running
     totals implicitly start back at $0. Matches every other tool in this
     repo (charm.py/gex.py/chain.py all log one file per day).
  2. The tracked nearest-expiry chain itself changes identity — "Net
     Drift" is flow within THE CHAIN currently being watched, so once
     that chain expires and the next one becomes nearest, the running
     total resets rather than silently blending two unrelated chains'
     flow into one number (see _maybe_reset_for_expiry). For equities
     this coincides with trigger #1 (CBOE's 0DTE listing flips to a new
     date at the same local-midnight moment the log file rotates) — one
     reset a day, same as before. For ETH/BTC it does NOT coincide:
     Deribit expires daily at 03:00 CT, three hours after local midnight,
     so a 0DTE-tracked crypto symbol resets TWICE a day, not once.

Usage: python drift.py SYMBOL [--interval SEC] [--date MM_DD_YYYY]
  SYMBOL            ETH or BTC for crypto (Deribit); any other ticker is
                     tried as a CBOE-listed equity/ETF (e.g. QQQ, SPY).
  --interval SEC    poll cadence, default 15s, floor 5s. Changeable live
                     with [I].
  --date MM_DD_YYYY browse a previously logged day for SYMBOL (read-only,
                     no live polling).

Keys: [S] switch symbol   [H] browse history   [I] change interval
      [R] refresh now      [Q] quit
"""

import sys
import os


def _ensure(pkg, import_as=None):
    import importlib
    import subprocess
    name = import_as or pkg.replace("-", "_")
    try:
        return importlib.import_module(name)
    except ImportError:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", pkg, "-q",
            "--break-system-packages",
        ])
        return importlib.import_module(name)


requests = _ensure("requests")

import curses
import json
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

DERIBIT_BASE = "https://www.deribit.com/api/v2"
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"

DEFAULT_INTERVAL = 15
MIN_INTERVAL = 5

LOG_DIR = os.path.dirname(os.path.abspath(__file__))


# ── DATA FETCHERS ────────────────────────────────────────────────────────────
def _deribit_api(path, **params):
    r = requests.get(DERIBIT_BASE + path, params=params, timeout=12)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"]["message"])
    return j["result"]


def fetch_deribit_nearest_expiry(currency):
    """Nearest non-expired expiry's instrument set for ETH/BTC. Returns
    ({instrument_name, ...}, expiry_label) — just the instrument list, no
    per-contract ticker fetch, since the live trade tape (see
    fetch_deribit_trades) is what actually drives the drift calc now."""
    instruments = _deribit_api("/public/get_instruments", currency=currency, kind="option", expired="false")
    now_ms = int(time.time() * 1000)
    by_exp = {}
    for ins in instruments:
        by_exp.setdefault(ins["expiration_timestamp"], []).append(ins)
    target_exp = min((e for e in by_exp if e > now_ms), default=None)
    if not target_exp:
        raise RuntimeError(f"no active {currency} option expiry found")
    instrument_set = {ins["instrument_name"] for ins in by_exp[target_exp]}
    expiry_label = datetime.fromtimestamp(target_exp / 1000, tz=timezone.utc).strftime("%d %b %Y")
    return instrument_set, expiry_label


def fetch_deribit_index(currency):
    result = _deribit_api("/public/get_index_price", index_name=f"{currency.lower()}_usd")
    return result.get("index_price")


def fetch_deribit_trades(currency, start_ms, end_ms, instrument_set):
    """Every real option trade print for `currency` between start_ms/end_ms
    (exclusive of anything already seen — callers pass start_ms = last
    poll's end_ms), filtered down to `instrument_set` (the nearest-expiry
    contracts). Ground-truth taker direction straight from Deribit — no
    aggressor-proxy guessing needed, unlike the CBOE path. Paginates via
    has_more/sorting=asc, capped generously since a personal CLI tool
    should never actually need thousands of trades in one poll."""
    trades = []
    cursor = start_ms
    for _ in range(20):   # hard cap on pagination rounds — safety net, not expected to hit
        result = _deribit_api(
            "/public/get_last_trades_by_currency_and_time",
            currency=currency, kind="option", start_timestamp=cursor, end_timestamp=end_ms,
            count=1000, include_old="true", sorting="asc",
        )
        batch = result.get("trades") or []
        trades.extend(t for t in batch if t.get("instrument_name") in instrument_set)
        if not result.get("has_more") or not batch:
            break
        cursor = batch[-1]["timestamp"] + 1
    return trades


def fetch_cboe_chain(symbol):
    """Nearest expiry (today if it has listed contracts, else the closest
    future date) for an equity/ETF ticker via CBOE's delayed-quotes feed.
    Returns {"contracts": [...], "spot": float, "expiry_label": str}.
    Option symbol parsing (flag/expiry/strike from the OCC-style name)
    mirrors charthacker.py's fetch_cboe_option_chain."""
    r = requests.get(CBOE_URL.format(symbol), timeout=15)
    r.raise_for_status()
    payload = r.json().get("data") or {}
    if not payload:
        raise RuntimeError(f"empty CBOE response for {symbol}")
    spot = payload.get("current_price")
    today = datetime.now().strftime("%y%m%d")
    by_exp = {}
    for o in payload.get("options") or []:
        name = o.get("option") or ""
        if len(name) < 15:
            continue
        exp = name[-15:-9]
        by_exp.setdefault(exp, []).append(o)
    if not by_exp:
        raise RuntimeError(f"no options found for {symbol}")
    if today in by_exp:
        target_exp = today
    else:
        future = sorted(e for e in by_exp if e >= today)
        target_exp = future[0] if future else min(by_exp)

    contracts = []
    for o in by_exp[target_exp]:
        name = o["option"]
        otype = "call" if name[-9] == "C" else "put"
        last = float(o.get("last_trade_price") or 0.0)
        vol = float(o.get("volume") or 0.0)
        contracts.append({
            "key": name,
            "otype": otype,
            "bid": float(o.get("bid") or 0.0),
            "ask": float(o.get("ask") or 0.0),
            "last": last,
            "baseline_value": vol,
            "multiplier": last * 100.0,
        })
    expiry_label = f"20{target_exp[:2]}-{target_exp[2:4]}-{target_exp[4:6]}"
    return {"contracts": contracts, "spot": spot, "expiry_label": expiry_label}


def midpoint(bid, ask):
    bid = bid or 0.0
    ask = ask or 0.0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if ask > 0:
        return ask
    if bid > 0:
        return bid
    return None


def classify_sign(last, mid):
    if not last or mid is None:
        return 0.0
    return 1.0 if last >= mid else -1.0


# ── PERSISTENCE — logs/YYYY/MM/DD/drift_<SYMBOL>_MM_DD_YYYY.jsonl ───────────
def _date_folder(date_str):
    mm, dd, yyyy = date_str.split("_")
    folder = os.path.join(LOG_DIR, "logs", yyyy, mm, dd)
    os.makedirs(folder, exist_ok=True)
    return folder


def log_path(symbol, date_str):
    return os.path.join(_date_folder(date_str), f"drift_{symbol}_{date_str}.jsonl")


def append_log(symbol, sample):
    try:
        with open(log_path(symbol, datetime.now().strftime("%m_%d_%Y")), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.fromtimestamp(sample["ts"]).isoformat(),
                "calls": sample["calls"], "puts": sample["puts"],
                "spot": sample["spot"], "vol": sample["vol"],
                "expiry": sample.get("expiry"),
            }) + "\n")
    except Exception:
        pass


def load_log(symbol, date_str):
    path = log_path(symbol, date_str)
    samples = []
    if not os.path.exists(path):
        return samples
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                samples.append({
                    "ts": datetime.fromisoformat(d["ts"]).timestamp(),
                    "calls": d["calls"], "puts": d["puts"],
                    "spot": d["spot"], "vol": d["vol"],
                    "expiry": d.get("expiry"),
                })
            except Exception:
                continue
    return samples


# ── LIVE STATE ────────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.history = []          # [{"ts","calls","puts","spot","vol"}, ...]
        self.calls_cum = 0.0
        self.puts_cum = 0.0
        self.baseline = {}         # CBOE only: contract key -> last-seen volume
        self.crypto_cursor_ms = None   # Deribit only: trade tape read up to here
        self.have_baseline = False
        self.spot = None
        self.status = "starting…"
        self.last_poll = 0.0
        self.expiry_label = None


class Controller:
    """live_state is ALWAYS what the background poll thread writes into for
    the current symbol — it keeps updating even while the UI is browsing a
    historical date, same "logging never stops regardless of what's on
    screen" convention as charm.py. `state` is whatever's currently
    DISPLAYED: state is live_state itself while live, or a separate
    read-only snapshot loaded from a past day's log while browsing ([H])."""
    def __init__(self, symbol, is_crypto, interval):
        self.lock = threading.Lock()
        self.symbol = symbol
        self.is_crypto = is_crypto
        self.interval = interval
        self.live_state = State()
        self.state = self.live_state
        self.live = True
        self.view_date = None
        self.next_poll_ts = time.time()
        self.refresh_event = threading.Event()
        self.stop = False


def seed_today(state, symbol):
    date_str = datetime.now().strftime("%m_%d_%Y")
    samples = load_log(symbol, date_str)
    if samples:
        state.history = samples
        state.calls_cum = samples[-1]["calls"]
        state.puts_cum = samples[-1]["puts"]
        state.spot = samples[-1]["spot"]
        state.expiry_label = samples[-1]["expiry"]   # lets the next poll detect
        # a rollover that happened while the tool was offline, not just live


def _maybe_reset_for_expiry(state, expiry_label):
    """Resets the running totals whenever the tracked nearest-expiry chain
    itself changes — the whole point of "Net Drift" is flow within THE
    CHAIN currently being watched, so once that chain expires and rolls to
    the next one, continuing the same running total would silently blend
    two unrelated chains' flow into one number. This is a SEPARATE reset
    from the once-daily new-log-file reset (see log_path/_date_folder):
    for anything whose expiry time of day doesn't line up with local
    midnight — ETH/BTC roll at 03:00 CT on Deribit, not 00:00 — this fires
    as a second reset later the same day. Equities don't get a second
    event: CBOE's 0DTE listing flips to a new date at the same moment the
    local calendar day does, so the two reset triggers coincide.

    Also fires on the very first poll after a restart if the log-seeded
    expiry_label (see seed_today) already differs from what's nearest
    right now — otherwise a restart that happens to land just after a
    real rollover would keep carrying the now-stale chain's totals
    forward under the new chain's name. Must be called with state.lock
    already held. Returns a status suffix describing what happened."""
    rolled = state.expiry_label is not None and expiry_label != state.expiry_label
    if rolled:
        old = state.expiry_label
        state.calls_cum = 0.0
        state.puts_cum = 0.0
    state.expiry_label = expiry_label
    return f" (expiry rolled {old} -> {expiry_label} — drift reset)" if rolled else ""


def _record_sample(symbol, state, d_calls, d_puts, spot, expiry_label, interval_vol, live_status):
    suffix = _maybe_reset_for_expiry(state, expiry_label)
    state.calls_cum += d_calls
    state.puts_cum += d_puts
    sample = {"ts": time.time(), "calls": state.calls_cum, "puts": state.puts_cum,
              "spot": spot, "vol": interval_vol, "expiry": expiry_label}
    state.history.append(sample)
    append_log(symbol, sample)
    state.have_baseline = True
    state.spot = spot
    state.status = live_status + suffix
    state.last_poll = time.time()


def poll_once_crypto(symbol, state):
    """Ground-truth trade-tape path — see module docstring. Reads the whole
    gap since the last poll (or since the last logged sample, if resuming),
    so it backfills downtime instead of just skipping it."""
    now_ms = int(time.time() * 1000)
    with state.lock:
        cursor_ms = state.crypto_cursor_ms
        if cursor_ms is None and state.history:
            cursor_ms = int(state.history[-1]["ts"] * 1000)
        first_poll = cursor_ms is None

    instrument_set, expiry_label = fetch_deribit_nearest_expiry(symbol)
    spot = fetch_deribit_index(symbol)

    if first_poll:
        with state.lock:
            state.crypto_cursor_ms = now_ms
            state.have_baseline = True
            state.spot = spot
            suffix = _maybe_reset_for_expiry(state, expiry_label)
            state.status = "baseline set — first sample next poll" + suffix
            state.last_poll = time.time()
        return

    trades = fetch_deribit_trades(symbol, cursor_ms, now_ms, instrument_set)
    d_calls = d_puts = interval_vol = 0.0
    for t in trades:
        usd = float(t["price"]) * float(t["amount"]) * float(t["index_price"])
        sign = 1.0 if t.get("direction") == "buy" else -1.0
        otype = "call" if t["instrument_name"].rsplit("-", 1)[-1] == "C" else "put"
        interval_vol += usd
        if otype == "call":
            d_calls += usd * sign
        else:
            d_puts += usd * sign

    with state.lock:
        _record_sample(symbol, state, d_calls, d_puts, spot, expiry_label, interval_vol, "live")
        state.crypto_cursor_ms = now_ms


def poll_once_cboe(symbol, state):
    """Snapshot-diff aggressor-proxy path — see module docstring."""
    chain = fetch_cboe_chain(symbol)
    contracts = chain["contracts"]
    with state.lock:
        first_poll = not state.have_baseline
        d_calls = d_puts = interval_vol = 0.0
        for c in contracts:
            key = c["key"]
            prev = state.baseline.get(key)
            cur = c["baseline_value"]
            state.baseline[key] = cur
            if prev is None or cur <= prev:
                continue
            usd = (cur - prev) * c["multiplier"]
            if usd <= 0:
                continue
            sign = classify_sign(c["last"], midpoint(c["bid"], c["ask"]))
            interval_vol += usd
            if c["otype"] == "call":
                d_calls += usd * sign
            else:
                d_puts += usd * sign

        if first_poll:
            state.have_baseline = True
            state.spot = chain["spot"]
            suffix = _maybe_reset_for_expiry(state, chain["expiry_label"])
            state.status = "baseline set — first sample next poll" + suffix
            state.last_poll = time.time()
        else:
            _record_sample(symbol, state, d_calls, d_puts, chain["spot"], chain["expiry_label"], interval_vol, "live")


def poll_once(symbol, is_crypto, state):
    if is_crypto:
        poll_once_crypto(symbol, state)
    else:
        poll_once_cboe(symbol, state)


def poll_loop(ctrl):
    """Always polls into ctrl.live_state for whatever the current symbol is
    — independent of ctrl.state/ctrl.live, so background tracking keeps
    running while the UI browses a historical date via [H]."""
    while not ctrl.stop:
        with ctrl.lock:
            symbol, is_crypto, state, interval = ctrl.symbol, ctrl.is_crypto, ctrl.live_state, ctrl.interval
        try:
            poll_once(symbol, is_crypto, state)
        except Exception as e:
            with state.lock:
                state.status = f"error: {e}"
        with ctrl.lock:
            ctrl.next_poll_ts = time.time() + interval
        ctrl.refresh_event.wait(timeout=interval)
        ctrl.refresh_event.clear()


def switch_symbol(ctrl, raw):
    sym = raw.strip().upper()
    if not sym:
        return
    is_crypto = sym in ("ETH", "BTC")
    new_state = State()
    try:
        seed_today(new_state, sym)
    except Exception:
        pass
    with ctrl.lock:
        ctrl.symbol = sym
        ctrl.is_crypto = is_crypto
        ctrl.live_state = new_state
        ctrl.state = new_state
        ctrl.live = True
        ctrl.view_date = None
    ctrl.refresh_event.set()   # wake poll_loop immediately for the new symbol


def view_historical(ctrl, date_str):
    """[H]: load a past day's log for the current symbol into a standalone
    snapshot and display it, read-only. Does NOT touch live_state — the
    background poll thread keeps tracking the live session the whole time,
    so [H] again with 'live' snaps straight back to an up-to-date chart."""
    with ctrl.lock:
        symbol = ctrl.symbol
    samples = load_log(symbol, date_str)
    hist_state = State()
    hist_state.history = samples
    hist_state.have_baseline = True
    if samples:
        hist_state.calls_cum = samples[-1]["calls"]
        hist_state.puts_cum = samples[-1]["puts"]
        hist_state.spot = samples[-1]["spot"]
        hist_state.expiry_label = samples[-1]["expiry"]
        hist_state.status = f"historical — {date_str}"
    else:
        hist_state.status = f"no log found for {date_str}"
    with ctrl.lock:
        ctrl.state = hist_state
        ctrl.live = False
        ctrl.view_date = date_str


def return_to_live(ctrl):
    with ctrl.lock:
        ctrl.state = ctrl.live_state
        ctrl.live = True
        ctrl.view_date = None


# ── FORMATTING ────────────────────────────────────────────────────────────────
def fmt_money(v, decimals=2):
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}${v / 1_000_000:.{decimals}f}M"
    if v >= 1000:
        return f"{sign}${v / 1000:.{decimals}f}K"
    return f"{sign}${v:.{decimals}f}"


def fmt_axis_money(v):
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}${v / 1_000_000:.1f}M"
    if v >= 1000:
        return f"{sign}${v / 1000:.0f}K"
    return f"{sign}${v:.0f}"


def fmt_price(p):
    if p is None:
        return "—"
    return f"${p:,.2f}" if p >= 1 else f"${p:.5f}"


def fmt_time(ts, is_crypto):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    # equities: always ET, matching the market's own convention regardless
    # of where this is run. crypto has no exchange timezone, so show
    # whatever clock the person watching the chart is actually on.
    dt = dt.astimezone(ET) if not is_crypto else dt.astimezone()
    return dt.strftime("%I:%M %p").lstrip("0")


def is_market_open_et(now=None):
    now = now or datetime.now(tz=ET)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def session_bounds(history, live):
    """x-axis always fits the data actually on hand, rather than a fixed
    session anchor (today 9:30 ET / today 00:00 UTC) — with a personal CLI
    tool there's no guarantee polling started at the session open, and
    anchoring there anyway left most of a freshly-started chart empty (all
    the real samples squeezed into a sliver at the very edge). If nothing's
    been collected yet, start at "now" and grow live. In live mode the
    window keeps extending to the current moment; browsing a past day (not
    live) instead ends at that day's last logged sample — extending it to
    today's "now" would squeeze the whole historical day into a hairline
    at the left edge."""
    if not history:
        now = time.time()
        return now, now + 300
    x_start = history[0]["ts"]
    x_last_end = max(time.time(), x_start + 300) if live else max(history[-1]["ts"], x_start + 300)
    return x_start, x_last_end


# ── CURSES RENDERING ──────────────────────────────────────────────────────────
P_DEFAULT, P_DIM, P_GREEN, P_RED, P_BLUE, P_YELLOW, P_STATUS = range(1, 8)


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    BG = -1
    curses.init_pair(P_DEFAULT, curses.COLOR_WHITE, BG)
    curses.init_pair(P_DIM, curses.COLOR_WHITE, BG)
    curses.init_pair(P_GREEN, curses.COLOR_GREEN, BG)
    curses.init_pair(P_RED, curses.COLOR_RED, BG)
    curses.init_pair(P_BLUE, curses.COLOR_BLUE, BG)
    curses.init_pair(P_YELLOW, curses.COLOR_YELLOW, BG)
    curses.init_pair(P_STATUS, curses.COLOR_BLACK, curses.COLOR_WHITE)


def cp(pair, bold=False, dim=False):
    a = curses.color_pair(pair)
    if bold:
        a |= curses.A_BOLD
    if dim:
        a |= curses.A_DIM
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


def draw_segments(win, y, x, segments):
    cx = x
    for text, attr in segments:
        safe_add(win, y, cx, text, attr)
        cx += len(text)
    return cx


def to_row(v, vmin, vmax, row_top, row_bot):
    if vmax == vmin:
        vmax = vmin + 1e-9
    frac = (v - vmin) / (vmax - vmin)
    frac = min(1.0, max(0.0, frac))
    r = row_top + int(round((1 - frac) * (row_bot - row_top)))
    return max(row_top, min(row_bot, r))


def build_columns(history, x_start, x_end, plot_w):
    span = max(1.0, x_end - x_start)
    cols_calls = [None] * plot_w
    cols_puts = [None] * plot_w
    cols_spot = [None] * plot_w
    cols_vol = [0.0] * plot_w
    for s in history:
        frac = (s["ts"] - x_start) / span
        frac = min(1.0, max(0.0, frac))
        c = int(frac * (plot_w - 1)) if plot_w > 1 else 0
        cols_calls[c] = s["calls"]
        cols_puts[c] = s["puts"]
        cols_spot[c] = s["spot"]
        cols_vol[c] += s["vol"] or 0.0

    def ffill(col):
        last = None
        for i in range(len(col)):
            if col[i] is None:
                col[i] = last
            else:
                last = col[i]
        return col

    return ffill(cols_calls), ffill(cols_puts), ffill(cols_spot), cols_vol


def draw(stdscr, ctrl):
    with ctrl.lock:
        symbol, is_crypto, state, interval, live = ctrl.symbol, ctrl.is_crypto, ctrl.state, ctrl.interval, ctrl.live
        view_date, next_poll_ts = ctrl.view_date, ctrl.next_poll_ts
    with state.lock:
        history = list(state.history)
        calls_cum, puts_cum = state.calls_cum, state.puts_cum
        spot, status, expiry = state.spot, state.status, state.expiry_label
        last_poll = state.last_poll

    h, w = stdscr.getmaxyx()
    stdscr.erase()
    if h < 14 or w < 50:
        safe_add(stdscr, 0, 0, "terminal too small — resize", cp(P_YELLOW))
        stdscr.refresh()
        return

    title = f" Net Drift (Premium) — {symbol} "
    if not live:
        title += f"(historical — {view_date}) "
    safe_add(stdscr, 0, 0, title.ljust(w), cp(P_STATUS, bold=True))

    calls_str, puts_str, spot_str = fmt_money(calls_cum), fmt_money(puts_cum), fmt_price(spot)
    draw_segments(stdscr, 1, 1, [
        ("● ", cp(P_GREEN, bold=True)), (f"Calls ({calls_str})   ", cp(P_DEFAULT)),
        ("● ", cp(P_RED, bold=True)), (f"Puts ({puts_str})   ", cp(P_DEFAULT)),
        ("● ", cp(P_BLUE, bold=True)), (f"{symbol} ({spot_str})", cp(P_DEFAULT)),
    ])
    info = (f"exp {expiry}  " if expiry else "") + status
    if not is_crypto and not is_market_open_et():
        info = "market closed — " + info
    safe_add(stdscr, 1, max(0, w - len(info) - 1), info, cp(P_DIM))

    LEFT_W, RIGHT_W = 8, 10
    FOOTER_ROWS, TIME_ROWS = 1, 1
    top = 3
    available = h - top - FOOTER_ROWS - TIME_ROWS
    if available < 8:
        stdscr.refresh()
        return
    main_h = max(5, int(available * 0.68))
    vol_h = max(3, available - main_h - 1)
    main_top = top
    vol_top = main_top + main_h + 1
    plot_w = max(1, w - LEFT_W - RIGHT_W - 1)

    x_start, x_end = session_bounds(history, live)
    cols_calls, cols_puts, cols_spot, cols_vol = build_columns(history, x_start, x_end, plot_w)

    prem_vals = [v for v in cols_calls + cols_puts if v is not None] + [0.0]
    pmin, pmax = min(prem_vals), max(prem_vals)
    if pmin == pmax:
        pmin, pmax = pmin - 1, pmax + 1
    pad = (pmax - pmin) * 0.08
    pmin, pmax = pmin - pad, pmax + pad

    price_vals = [v for v in cols_spot if v is not None]
    if price_vals:
        smin, smax = min(price_vals), max(price_vals)
        if smin == smax:
            smin, smax = smin - 1, smax + 1
        spad = (smax - smin) * 0.10
        smin, smax = smin - spad, smax + spad
    else:
        smin, smax = 0.0, 1.0

    main_bot = main_top + main_h - 1
    N_TICKS = max(2, min(6, main_h // 2))
    for k in range(N_TICKS):
        row = main_top + int(k * (main_h - 1) / max(1, N_TICKS - 1))
        frac = (row - main_top) / max(1, main_h - 1)
        pval = pmax - frac * (pmax - pmin)
        sval = smax - frac * (smax - smin)
        lbl = fmt_axis_money(pval)
        safe_add(stdscr, row, max(0, LEFT_W - 1 - len(lbl)), lbl, cp(P_DIM))
        rlbl = fmt_price(sval)
        safe_add(stdscr, row, LEFT_W + plot_w + 1, rlbl, cp(P_DIM))

    if pmin <= 0.0 <= pmax:
        zero_row = to_row(0.0, pmin, pmax, main_top, main_bot)
        safe_add(stdscr, zero_row, LEFT_W, "·" * plot_w, cp(P_DIM))

    def plot_line(cols, vmin, vmax, glyph, attr):
        prev_row = None
        for i, v in enumerate(cols):
            if v is None:
                prev_row = None
                continue
            r = to_row(v, vmin, vmax, main_top, main_bot)
            if prev_row is not None:
                r0, r1 = sorted((prev_row, r))
                for rr in range(r0, r1 + 1):
                    safe_add(stdscr, rr, LEFT_W + i, "│", attr)
            safe_add(stdscr, r, LEFT_W + i, glyph, attr)
            prev_row = r

    plot_line(cols_spot, smin, smax, "●", cp(P_BLUE, bold=True))
    plot_line(cols_puts, pmin, pmax, "●", cp(P_RED, bold=True))
    plot_line(cols_calls, pmin, pmax, "●", cp(P_GREEN, bold=True))

    vol_bot = vol_top + vol_h - 1
    max_vol = max(cols_vol) if cols_vol else 0.0
    safe_add(stdscr, vol_top, max(0, LEFT_W - 1 - len(fmt_axis_money(max_vol))), fmt_axis_money(max_vol), cp(P_DIM))
    safe_add(stdscr, vol_bot, max(0, LEFT_W - 2), "$0", cp(P_DIM))
    for i, v in enumerate(cols_vol):
        if max_vol <= 0 or not v:
            continue
        bar_h = max(1, int(round(v / max_vol * vol_h)))
        for rr in range(vol_bot, vol_bot - bar_h, -1):
            safe_add(stdscr, rr, LEFT_W + i, "█", cp(P_GREEN, dim=True))

    time_row = vol_bot + 1
    n_time_labels = max(2, plot_w // 14)
    for k in range(n_time_labels):
        i = int(k * (plot_w - 1) / max(1, n_time_labels - 1))
        ts = x_start + (i / max(1, plot_w - 1)) * (x_end - x_start)
        lbl = fmt_time(ts, is_crypto)
        cx = LEFT_W + max(0, i - len(lbl) // 2)
        safe_add(stdscr, time_row, cx, lbl, cp(P_DIM))

    last_str = datetime.fromtimestamp(last_poll).strftime("%H:%M:%S") if last_poll else "—"
    if live:
        countdown = f"next refresh in {max(0, int(round(next_poll_ts - time.time())))}s"
    else:
        countdown = "live tracking continues in background"
    footer = (f" [S] switch   [H] history   [I] interval   [R] refresh now   [Q] quit    "
              f"every {interval}s    {countdown}    last update {last_str} ")
    safe_add(stdscr, h - 1, 0, footer.ljust(w), cp(P_STATUS))
    stdscr.refresh()


def prompt_text(stdscr, prompt, max_len=40):
    h, w = stdscr.getmaxyx()
    buf = ""
    stdscr.timeout(-1)
    curses.curs_set(1)
    try:
        while True:
            safe_add(stdscr, h - 1, 0, (prompt + buf).ljust(w), cp(P_STATUS))
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (curses.KEY_ENTER, 10, 13):
                return buf.strip()
            if ch == 27:
                return None
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                buf = buf[:-1]
            elif 32 <= ch < 127 and len(buf) < max_len:
                buf += chr(ch)
    finally:
        curses.curs_set(0)
        stdscr.timeout(200)


def curses_main(stdscr, ctrl):
    curses.curs_set(0)
    stdscr.timeout(200)
    init_colors()
    while True:
        try:
            draw(stdscr, ctrl)
        except curses.error:
            pass
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1
        if ch == -1:
            continue
        if ch in (ord('q'), ord('Q')):
            break
        elif ch in (ord('s'), ord('S')):
            raw = prompt_text(stdscr, " New symbol (ETH/BTC or equity ticker) — Enter=confirm, Esc=cancel: ")
            if raw:
                switch_symbol(ctrl, raw)
        elif ch in (ord('h'), ord('H')):
            raw = prompt_text(
                stdscr,
                " Date to browse MM_DD_YYYY, or 'live' to return — Enter=confirm, Esc=cancel: ")
            if raw is not None:
                low = raw.lower()
                if low in ("", "live", "l"):
                    if raw:  # non-empty "live"/"l" — blank Enter is a no-op cancel
                        return_to_live(ctrl)
                else:
                    view_historical(ctrl, raw)
        elif ch in (ord('i'), ord('I')):
            with ctrl.lock:
                cur_interval = ctrl.interval
            raw = prompt_text(
                stdscr,
                f" New refresh interval in seconds, min {MIN_INTERVAL} (currently {cur_interval}) — "
                f"Enter=confirm, Esc=cancel: ")
            if raw:
                try:
                    new_interval = max(MIN_INTERVAL, int(raw))
                except ValueError:
                    new_interval = None
                if new_interval is not None:
                    with ctrl.lock:
                        ctrl.interval = new_interval
                    ctrl.refresh_event.set()   # apply it immediately instead of finishing the old wait
        elif ch in (ord('r'), ord('R')):
            ctrl.refresh_event.set()


USAGE = """Usage: python drift.py SYMBOL [--interval SEC] [--date MM_DD_YYYY]

  SYMBOL             ETH or BTC (Deribit crypto options), or any other
                      ticker tried as a CBOE-listed equity/ETF (QQQ, SPY, ...)
  --interval SEC      poll cadence in seconds (default 15, floor 5)
  --date MM_DD_YYYY    open straight into a previously logged day for
                        SYMBOL (read-only) — live tracking still starts in
                        the background; press [H] then 'live' to jump to it

Keys while running: [S] switch symbol   [H] browse a past day   [I] change interval
                    [R] refresh now   [Q] quit
"""


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0 if args else 1)

    symbol = args[0].upper()
    rest = args[1:]
    interval = DEFAULT_INTERVAL
    date_str = None
    i = 0
    while i < len(rest):
        if rest[i] == "--interval" and i + 1 < len(rest):
            try:
                interval = max(MIN_INTERVAL, int(rest[i + 1]))
            except ValueError:
                pass
            i += 2
        elif rest[i] == "--date" and i + 1 < len(rest):
            date_str = rest[i + 1]
            i += 2
        else:
            i += 1

    is_crypto = symbol in ("ETH", "BTC")
    ctrl = Controller(symbol, is_crypto, interval)
    seed_today(ctrl.live_state, symbol)   # background live tracking always resumes today's log
    if date_str:
        view_historical(ctrl, date_str)   # --date just changes what's DISPLAYED at startup

    thread = threading.Thread(target=poll_loop, args=(ctrl,), daemon=True)
    thread.start()

    try:
        curses.wrapper(curses_main, ctrl)
    finally:
        ctrl.stop = True
        ctrl.refresh_event.set()   # wake poll_loop out of its wait so it can see ctrl.stop and exit promptly
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
