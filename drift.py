#!/usr/bin/env python3
"""
drift.py — Net Drift (Premium) Chart
Curses terminal chart: cumulative net options premium flow for calls (green)
and puts (red), plotted against the underlying's live price (blue, right
axis) with a live-price reference line, plus a bottom panel toggleable via
[N] between Net Volume (default — cumulative signed contract-count area,
green above zero / red below, QuantData-style) and the original Volume
(unsigned per-bucket $ premium bars) — styled after the "Net Drift" tab of
retail options-flow dashboards (colored-dot legend up top, dual Y axis,
shared time axis at the bottom).

[F] FILTERED MODE — a toggle that switches every panel to a second,
parallel-tracked running total built from only a subset of trades. Two
filters are actually implemented: **0DTE** and **OTM**. Both ledgers (all
trades, and OTM-only) are computed every poll regardless of which one is
currently displayed, so [F] takes effect instantly with no re-poll.

  This started as a request for SIX filters (0DTE, OTM, exclude complex/
  tied/floor/cancelled trades) modeled on a real retail flow-scanner
  tool. Verified live before building anything: pulled a real Deribit
  trade record and a real OKX trade batch and inspected every field —
  neither (nor Bybit, per its docs) carries a complex/spread, stock-tied,
  floor-vs-electronic, or cancelled/busted flag. Those four are OPRA (the
  licensed options tape) trade-condition-code concepts; the tool this was
  modeled on gets them from paid OPRA access, not anything free. CBOE's
  free delayed-quotes feed (the equity path) is further still from having
  this — it's snapshot volume diffing, not a trade tape, so there's no
  per-trade anything to classify. This isn't unfinished work — the raw
  data those four filters need isn't present in any of the four sources
  this tool has free access to. "Exclude Floor Trades" is additionally a
  non-concept for crypto specifically (100% electronic, no floor exists).
  So [F] genuinely applies 2 of 6 — see _is_otm/_parse_strike_otype and
  the 0DTE note below for exactly what each one does.

NET DRIFT METHODOLOGY — differs by source. Equities have only one source
(CBOE, snapshot-only, no trade tape). Crypto (ETH/BTC) AGGREGATES real
trade-level flow across three exchanges — Deribit, OKX, Bybit — each with
its own data-quality ceiling, summed into one number:

  ETH / BTC (crypto) — every --interval seconds, each of the three venues
    is polled in parallel (see poll_once_crypto), and each venue's own
    sub-total (state.venues["deribit"/"okx"/"bybit"]) accumulates
    independently before being summed into the displayed total. A venue
    that errors this poll just contributes $0 for that interval — it does
    not stop the other two or crash the poll ([V] shows a live per-venue
    breakdown, the info line shows each venue's health at a glance):
      Deribit — EXACT, ground truth. get_last_trades_by_currency_and_time
                gives a TRUE start/end timestamp range query, so it can
                backfill any gap (e.g. resuming after a restart, seeded
                from the log's last timestamp). Each trade's own
                `direction` field signs it directly — no guessing.
                usd = price(coin) * amount * index_price(USD).
      OKX     — EXACT while live, but NOT backfillable. option-trades is a
                FIXED ~100-most-recent-trades snapshot (confirmed live —
                tried both `limit` and `after` params, neither changes the
                response). Dedups against previously-seen tradeIds instead
                of windowing by time, so a gap wider than "however long it
                takes ~100 new trades to happen" silently misses whatever
                aged out. Real `side`. usd = px * ctVal * ctMult * sz *
                idxPx (ctMult confirmed via OKX's own instrument data:
                0.01 for BTC options, 0.1 for ETH).
      Bybit   — same recent-N/dedup shape and limitation as OKX (up to
                1000 trades via execId dedup), and real `side`, per
                Bybit's own docs. usd = price * size directly — Bybit
                options are USDC-settled per Bybit's docs, so price is
                already ~USD. UNTESTABLE, confirmed, not just from the dev
                sandbox: api.bybit.com's CloudFront distribution actively
                blocks the request by country ("The Amazon CloudFront
                distribution is configured to block access from your
                country") — confirmed both from the build environment AND
                a direct curl from a real deployment, so this is a
                jurisdictional block on Bybit's own end, not fixable
                client-side. [V]'s Bybit line reading "—✗" and
                contributing $0 is therefore the EXPECTED steady state in
                a blocked region, not a bug — the aggregate is still
                correct on Deribit+OKX alone. Left wired up rather than
                removed: it costs one fast-failing request per poll and
                would "just work" from an unrestricted network (a cloud
                VPS in a different region, or if the block changes).

  anything else (equity/ETF ticker) — approximate, single-source, via
    CBOE's delayed quotes feed (~15m delay, same feed chain.py/gex.py/
    charm.py use), which has no trade-side tape at all, only periodic
    per-contract snapshots. Every --interval seconds, for each contract:
      delta = cumulative_volume_now - cumulative_volume_prev   (skipped on
              the very first poll, which only sets the baseline — so a
              restart never double-counts, though it also can't backfill
              downtime, since CBOE has no historical tape)
      usd   = delta * last_trade_price * 100   (standard equity contract
              size, applied only to that poll's own volume delta)
      sign  = +1 if last_trade_price >= mid(bid, ask) else -1   ("aggressor
              proxy" — nearer the ask looks buyer-initiated, nearer the bid
              looks seller-initiated; 0/no attribution if bid+ask are both
              unavailable, so an unclassifiable print still counts toward
              the volume panel but isn't forced into either line), further
              softened by --confidence-deadzone below

  Both sides: the aggregate calls total / puts total gets logged to
  logs/YYYY/MM/DD/drift_<SYMBOL>_MM_DD_YYYY.jsonl (same layout convention
  as charm.py's own append_log), plus — for crypto — a "venues" breakdown
  of each exchange's own sub-total, so a restart or [H] historical browse
  can restore the full picture, not just the combined number. The bottom
  panel is unsigned |usd| summed across every venue and both sides each
  poll — total $ premium activity that interval, not contract count.

ALWAYS-ON BACKGROUND TRACKING — ETH and QQQ (BG_SYMBOLS) are tracked
continuously for the program's whole lifetime, regardless of [S] switching
the display to something else, same feature charm.py already built for
this exact pair (BG_SYMBOLS/bg_tick/asset_active_now, charm.py:1810-1909).
NOT a literal port, though: charm.py's per-tick snapshots tolerate two
independent fetchers briefly overlapping, but drift.py's calls_cum/
puts_cum/net_vol_cum are running totals built from a cursor/baseline —
two independent pollers on the same symbol would each keep their own
cursor, compute a different total, and both write to the same log file,
producing corrupted, alternating entries. So instead of charm.py's
"parallel fetchers, skip when redundant," there's exactly ONE
BackgroundTracker per symbol, and switching [S] onto ETH/QQQ just points
the display at that SAME State object rather than starting a second
poller — see BackgroundTracker/_focused_tuning/poll_loop's background
check. QQQ gates on is_market_open_et() (idles outside the session rather
than polling CBOE for nothing new); ETH has no such gate, crypto is 24/7.
[I]/[M]/[C]/[R] tune whichever symbol is currently focused — for ETH/QQQ
that's their own persistent tracker (settings survive switching away and
back), for anything else it's the same one-off ad-hoc behavior as before
this feature existed.

RESETS — two independent triggers, whichever fires first:
  1. New calendar day (local time) — see _maybe_reset_for_new_day, called
     EVERY poll (not just at startup). log_path/_date_folder already
     rotate the log FILENAME on their own (computed fresh each
     append_log call), matching every other tool in this repo (charm.py/
     gex.py/chain.py all log one file per day) — but a continuously-
     running process crossing local midnight needs its IN-MEMORY running
     totals explicitly reset too, or they'd carry straight across the
     boundary into the new file uninterrupted (this was a real bug here
     until it wasn't: confirmed live — a long-running session's first
     sample of a new day had the exact same totals as the previous day's
     last sample, because nothing was re-checking the date once a process
     was already up).
  2. A tracked nearest-expiry chain changes identity — "Net Drift" is flow
     within THE CHAIN currently being watched, so once that chain expires
     and the next one becomes nearest, the running total resets rather
     than silently blending two unrelated chains' flow into one number.
     For crypto this is scoped PER VENUE (see poll_once_crypto): each of
     the three exchanges rolls on its own calendar (Deribit daily at 03:00
     CT; OKX/Bybit on their own listing schedules), so one venue rolling
     only zeroes ITS OWN sub-total, not the other two's — the aggregate
     just dips by whatever that venue had contributed, never a hard reset
     to zero from one venue alone. Equities have a single source, so its
     one reset trigger coincides with trigger #1 (CBOE's 0DTE listing
     flips to a new date at the same local-midnight moment the log file
     rotates) — one reset a day.
  Both resets zero the [F] filtered ledger (calls_cum_f/puts_cum_f/
  net_vol_cum_f) and Net Volume (net_vol_cum) right alongside the
  unfiltered ones, same trigger, same scope — they're not separately
  tracked resets, just more fields reset by the same two triggers above.

FILTERS — noise-reduction, both applied prospectively only (already-logged
history keeps whatever was computed under whatever settings were active at
the time; changing a filter never retroactively rewrites the past):
  --min-trade-usd ([M]) — a trade (crypto) or a single poll's aggregate
     contract-volume delta (equities — CBOE has no per-trade granularity,
     see poll_once_cboe) below this $ premium is dropped ENTIRELY: not
     counted toward calls/puts, not toward the volume panel either. Cuts
     small/dust prints (1-lot retail 0DTE tickets and similar) that add
     noise without much signal. Default $100; 0 disables it.
  --confidence-deadzone ([C], equities only) — classify_sign's old rule was
     a hard binary split at the bid-ask midpoint with zero margin, so a
     print landing almost exactly at mid (genuinely a coin flip) still got
     forced into calls or puts. This carves out a "too close to call" zone
     around mid, sized as a % of the bid-ask spread — a print inside it is
     now 0/unclassified (counts toward volume, not direction) instead of
     an arbitrary guess. Default 20%; 0 recovers the exact old always-
     classify behavior. Crypto has real trade-side data and ignores this.

Usage: python drift.py SYMBOL [--interval SEC] [--bar-interval SEC]
                        [--min-trade-usd USD] [--confidence-deadzone PCT]
                        [--filtered] [--volume] [--date MM_DD_YYYY]
  SYMBOL             ETH or BTC for crypto (Deribit+OKX+Bybit aggregate);
                      any other ticker is tried as a CBOE-listed equity/ETF
                      (e.g. QQQ, SPY).
  --interval SEC     how often to POLL for new data, default 15s, floor 5s.
                      Changeable live with [I].
  --bar-interval SEC  chart bucket size — how much wall-clock time each
                      plotted point represents (60=1m, 300=5m, 900=15m),
                      independent of --interval. Default 60s, floor 5s.
                      Changeable live with [B]. A bigger bar shows more
                      history on screen at once; [←/→] pans back further
                      at whatever bar size is currently set.
  --min-trade-usd USD        see FILTERS above. Default 100, floor 0.
  --confidence-deadzone PCT  see FILTERS above. Default 20, range 0-100.
  --date MM_DD_YYYY  browse a previously logged day for SYMBOL (read-only,
                      no live polling).

Keys: [←/→] pan through history (1 bar; Shift+arrow or [/]=10; Ctrl+arrow
      or {/}=50 — same jump scheme as charthacker.py's view_offset scroll,
      see chart_x_end), [Esc] snap back to the live edge
      [S] switch symbol   [H] browse history   [I] refresh interval
      [B] chart bar size   [M] min trade $   [C] equities deadzone
      [F] filtered mode (0DTE+OTM — see above)
      [N] bottom panel: Net Volume / Volume
      [V] per-venue breakdown (crypto only)   [R] refresh now   [Q] quit
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

DERIBIT_BASE = "https://www.deribit.com/api/v2"
OKX_BASE = "https://www.okx.com"
BYBIT_BASE = "https://api.bybit.com"
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"

DEFAULT_INTERVAL = 15
MIN_INTERVAL = 5
DEFAULT_BAR_INTERVAL = 60   # chart bucket size, e.g. "1m bars" — independent
MIN_BAR_INTERVAL = 5        # of the poll/refresh cadence above (see [B]/[I])

DEFAULT_MIN_TRADE_USD = 100.0     # [M] — trades/deltas below this $ premium
                                   # are dropped entirely (not just left
                                   # unsigned) — cuts retail dust noise
DEFAULT_CONFIDENCE_DEADZONE = 0.20   # [C], equities only — fraction of the
# bid-ask spread, centered on mid, treated as "too close to call" rather
# than forced into a call/put guess (see classify_sign). 0 = old exact
# binary split; crypto never uses this, it has real trade-side data.

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


# ── OKX (verified live: free/unauthenticated, but the public option-trades
# endpoint is a FIXED ~100-most-recent-trades snapshot — tested both `limit`
# and `after` params directly and neither changes the response, so there is
# no real pagination/backfill available here, unlike Deribit) ───────────────
def _okx_api(path, **params):
    r = requests.get(OKX_BASE + path, params=params, timeout=12)
    r.raise_for_status()
    j = r.json()
    if j.get("code") not in ("0", 0):
        raise RuntimeError(j.get("msg") or f"OKX error {j.get('code')}")
    return j.get("data") or []


def fetch_okx_nearest_expiry(currency):
    """Nearest non-expired expiry for BTC/ETH options on OKX. Returns
    (instrument_set, ct_val, ct_mult, expiry_label). ct_val/ct_mult are
    OKX's own notional-value fields (contract multiplier: 0.01 for BTC
    options, 0.1 for ETH — i.e. 1 BTC option contract = 0.01 BTC notional,
    confirmed via OKX's own instrument data + help docs, NOT hardcoded from
    memory) — usd_premium = px * ct_val * ct_mult * sz * idxPx."""
    instruments = _okx_api("/api/v5/public/instruments", instType="OPTION", uly=f"{currency}-USD")
    now_ms = int(time.time() * 1000)
    by_exp = {}
    for ins in instruments:
        exp_ms = int(ins["expTime"])
        if exp_ms > now_ms:
            by_exp.setdefault(exp_ms, []).append(ins)
    if not by_exp:
        raise RuntimeError(f"no active OKX {currency} option expiry found")
    target_exp = min(by_exp)
    chain_ins = by_exp[target_exp]
    instrument_set = {ins["instId"] for ins in chain_ins}
    ct_val = float(chain_ins[0]["ctVal"])
    ct_mult = float(chain_ins[0]["ctMult"])
    expiry_label = datetime.fromtimestamp(target_exp / 1000, tz=timezone.utc).strftime("%d %b %Y")
    return instrument_set, ct_val, ct_mult, expiry_label


def fetch_okx_option_trades(currency):
    """Fixed ~100-most-recent-trades snapshot across the WHOLE currency
    family (like Deribit's per-currency scope) — no time-range query
    exists for this endpoint, so callers dedup against previously-seen
    tradeIds rather than windowing by timestamp."""
    return _okx_api("/api/v5/public/option-trades", instFamily=f"{currency}-USD")


# ── Bybit (docs confirm free/unauthenticated + real taker `side`, but the
# recent-trade endpoint is explicitly "recent" only — no startTime/endTime
# — so same dedup-not-window approach as OKX. IMPORTANT: api.bybit.com
# returned a 403 CloudFront geo-block from the dev sandbox this was built
# in, so — unlike Deribit and OKX — this integration could NOT be verified
# against a live response. It's built strictly from Bybit's official V5
# docs (instrument/trade field names and the USDC-settlement claim below);
# treat its numbers as unverified until confirmed on a machine that can
# actually reach api.bybit.com.) ────────────────────────────────────────────
def _bybit_api(path, **params):
    r = requests.get(BYBIT_BASE + path, params=params, timeout=12)
    r.raise_for_status()
    j = r.json()
    if j.get("retCode") != 0:
        raise RuntimeError(j.get("retMsg") or f"Bybit error {j.get('retCode')}")
    return j.get("result") or {}


def fetch_bybit_nearest_expiry(currency):
    """Nearest active (status=Trading) expiry for BTC/ETH options on Bybit.
    Returns (instrument_set, expiry_label)."""
    result = _bybit_api("/v5/market/instruments-info", category="option", baseCoin=currency, limit=1000)
    instruments = result.get("list") or []
    now_ms = int(time.time() * 1000)
    by_exp = {}
    for ins in instruments:
        if ins.get("status") != "Trading":
            continue
        exp_ms = int(ins["deliveryTime"])
        if exp_ms > now_ms:
            by_exp.setdefault(exp_ms, []).append(ins)
    if not by_exp:
        raise RuntimeError(f"no active Bybit {currency} option expiry found")
    target_exp = min(by_exp)
    instrument_set = {ins["symbol"] for ins in by_exp[target_exp]}
    expiry_label = datetime.fromtimestamp(target_exp / 1000, tz=timezone.utc).strftime("%d %b %Y")
    return instrument_set, expiry_label


def fetch_bybit_option_trades(currency):
    """Fixed most-recent-trades snapshot (up to 1000) — see module note
    above on why this is dedup-based rather than a time-range query."""
    result = _bybit_api("/v5/market/recent-trade", category="option", baseCoin=currency, limit=1000)
    return result.get("list") or []


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
        try:
            strike = int(name[-8:]) / 1000.0
        except ValueError:
            strike = None
        contracts.append({
            "key": name,
            "otype": otype,
            "strike": strike,
            "bid": float(o.get("bid") or 0.0),
            "ask": float(o.get("ask") or 0.0),
            "last": last,
            "baseline_value": vol,
            "multiplier": last * 100.0,
        })
    expiry_label = f"20{target_exp[:2]}-{target_exp[2:4]}-{target_exp[4:6]}"
    # is_0dte: did we actually get TODAY's listing, or fall back to the next
    # future date because today has none? Filtered/[F] mode's 0DTE filter
    # means NOT accepting that fallback — see poll_once_cboe.
    return {"contracts": contracts, "spot": spot, "expiry_label": expiry_label,
            "is_0dte": target_exp == today}


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


def _parse_strike_otype(dashed_name):
    """Deribit/OKX/Bybit instrument identifiers all end in
    ...-<STRIKE>-<C|P> (e.g. "ETH-25SEP26-2300-C",
    "BTC-USD-260810-64250-C") — confirmed identical convention across all
    three live. Returns (strike: float, otype: "call"/"put")."""
    parts = dashed_name.rsplit("-", 2)
    return float(parts[-2]), ("call" if parts[-1] == "C" else "put")


def _is_otm(otype, strike, spot):
    """[F] filtered mode's OTM filter: call OTM means strike > spot, put
    OTM means strike < spot; exactly-at-the-money (strike == spot) and
    anything we can't classify (missing strike or spot) is excluded from
    the filtered set — strict definition, no benefit of the doubt."""
    if strike is None or spot is None:
        return False
    return strike > spot if otype == "call" else strike < spot


def classify_sign(last, bid, ask, deadzone_frac):
    """+1/-1 aggressor-proxy classification (nearer ask = buy, nearer bid
    = sell) — same idea as before, but now with a "deadzone" around the
    midpoint instead of a hard binary split with zero margin: a print
    landing within deadzone_frac of the bid-ask SPREAD width (centered on
    mid) is genuinely closer to a coin flip than a confident read, so it's
    treated as 0/unclassified — still counts toward the volume panel via
    the caller, same as the existing "bid+ask both unavailable" case
    already was, just no longer forced into calls or puts on a guess.
    deadzone_frac=0 recovers the exact old always-classify behavior; 1.0
    would make the whole spread ambiguous (equities/CBOE only — crypto
    has real trade-side data and never calls this)."""
    mid = midpoint(bid, ask)
    if not last or mid is None:
        return 0.0
    if bid and ask and ask > bid:
        spread = ask - bid
        if abs(last - mid) < spread * deadzone_frac / 2.0:
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
                "venues": sample.get("venues"),   # crypto only — per-exchange breakdown
                "calls_f": sample.get("calls_f"), "puts_f": sample.get("puts_f"),   # [F] filtered mode
                "net_vol": sample.get("net_vol"), "net_vol_f": sample.get("net_vol_f"),  # Net Volume panel
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
                    "venues": d.get("venues"),   # absent in logs written before multi-venue support
                    # absent in logs written before [F]/Net Volume support — default to 0
                    # ("no filtered data recorded") rather than crashing OR fabricating a
                    # value by falling back to the unfiltered total, which would silently
                    # imply "everything passed the filter" and isn't actually known
                    "calls_f": d.get("calls_f", 0.0), "puts_f": d.get("puts_f", 0.0),
                    "net_vol": d.get("net_vol", 0.0), "net_vol_f": d.get("net_vol_f", 0.0),
                })
            except Exception:
                continue
    return samples


# ── LIVE STATE ────────────────────────────────────────────────────────────────
# One sub-accumulator per crypto venue. `cursor` means something different
# per venue: an int ms timestamp for Deribit (true time-range query, so a
# restart can seed it from the log and backfill), or a set of already-seen
# trade IDs for OKX/Bybit (their endpoints are recent-N snapshots with no
# time-range query, so dedup is the only option — see module docstring).
CRYPTO_VENUES = ("deribit", "okx", "bybit")


def _new_venue():
    return {
        "calls_cum": 0.0, "puts_cum": 0.0,        # unfiltered, $ premium
        "calls_cum_f": 0.0, "puts_cum_f": 0.0,    # [F] filtered (OTM only — see module docstring), $ premium
        "net_vol_cum": 0.0, "net_vol_cum_f": 0.0,  # signed CONTRACT count, calls+puts combined
        "expiry_label": None, "cursor": None,
    }


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.history = []          # [{"ts","calls","puts","calls_f","puts_f","net_vol","net_vol_f","spot","vol","expiry","venues"}, ...]
        self.calls_cum = 0.0       # equities: written directly. crypto: kept in sync with
        self.puts_cum = 0.0        # sum(venues[*]["calls_cum"/"puts_cum"]) via _sync_totals
        self.calls_cum_f = 0.0     # [F] filtered mode — see _new_venue/poll_once_cboe
        self.puts_cum_f = 0.0
        self.net_vol_cum = 0.0     # Net Volume panel — signed contract count, unfiltered
        self.net_vol_cum_f = 0.0   # same, filtered
        self.baseline = {}         # CBOE only: contract key -> last-seen volume
        self.expiry_label = None   # CBOE only — crypto tracks expiry per-venue instead
        self.venues = {name: _new_venue() for name in CRYPTO_VENUES}   # crypto only
        self.venue_status = {}     # crypto only — {"deribit": "live"/"error: ...", ...}
        self.have_baseline = False
        self.spot = None
        self.status = "starting…"
        self.last_poll = 0.0
        self.last_log_date = None  # "MM_DD_YYYY" — see _maybe_reset_for_new_day


# ── ALWAYS-ON BACKGROUND TRACKING (ETH + QQQ, independent of what's
# displayed) — same feature charm.py already built for this exact pair
# (BG_SYMBOLS/bg_tick/asset_active_now, charm.py:1810-1909), adapted for
# drift.py's different data model. charm.py keeps a fully separate
# background state and just SKIPS its own fetch when the displayed symbol
# happens to coincide — safe there because its per-tick snapshots have no
# cumulative-consistency requirement between polls. drift.py's
# calls_cum/puts_cum/net_vol_cum are RUNNING TOTALS, each built from its
# own cursor/baseline (poll_venue_deribit's cursor_prev, CBOE's
# state.baseline) to avoid re-counting a trade twice — two independent
# pollers touching the same symbol would each keep their own cursor and
# computed total, and both would append_log() to the SAME file, producing
# alternating, mutually inconsistent calls_cum values in one log. So
# instead of "parallel fetchers, skip when redundant," there is exactly
# ONE authoritative State per background symbol, and the display just
# points at it (shares the object) whenever you're looking at ETH/QQQ —
# never two pollers on the same symbol at once, by construction.
BG_SYMBOLS = ("ETH", "QQQ")


class BackgroundTracker:
    """One per BG_SYMBOL, tracked continuously for the program's whole
    lifetime regardless of what's displayed (see background_poll_loop).
    Deliberately the SAME attribute names as Controller's own
    interval/min_trade_usd/confidence_deadzone/next_poll_ts/refresh_event,
    so _focused_tuning can return either polymorphically — tuning ETH's
    poll cadence while it's focused adjusts ITS tracker (persists across
    switching away and back via [S]), not some separate ad-hoc setting."""
    def __init__(self, symbol, is_crypto):
        self.symbol = symbol
        self.is_crypto = is_crypto
        self.state = State()
        self.interval = DEFAULT_INTERVAL
        self.min_trade_usd = DEFAULT_MIN_TRADE_USD
        self.confidence_deadzone = DEFAULT_CONFIDENCE_DEADZONE
        self.next_poll_ts = time.time()
        self.refresh_event = threading.Event()


def _focused_tuning(ctrl, symbol):
    """Whatever [I]/[M]/[C]/[R] and draw()'s countdown should read/write
    for the CURRENTLY FOCUSED symbol — its own BackgroundTracker if it's
    ETH/QQQ, else ctrl itself (today's ad-hoc, single-symbol behavior,
    unchanged for anything outside BG_SYMBOLS)."""
    return ctrl.background[symbol] if symbol in ctrl.background else ctrl


class Controller:
    """live_state is ALWAYS what gets polled into for the current symbol —
    it keeps updating even while the UI is browsing a historical date,
    same "logging never stops regardless of what's on screen" convention
    as charm.py. `state` is whatever's currently DISPLAYED: state is
    live_state itself while live, or a separate read-only snapshot loaded
    from a past day's log while browsing ([H]). When the focused symbol is
    ETH or QQQ, live_state IS that symbol's BackgroundTracker.state (same
    object, not a copy) — see BG_SYMBOLS above for why that sharing
    matters here, not just for convenience."""
    def __init__(self, symbol, is_crypto, interval, bar_interval=DEFAULT_BAR_INTERVAL,
                 min_trade_usd=DEFAULT_MIN_TRADE_USD, confidence_deadzone=DEFAULT_CONFIDENCE_DEADZONE):
        self.lock = threading.Lock()
        self.symbol = symbol
        self.background = {sym: BackgroundTracker(sym, sym in ("ETH", "BTC")) for sym in BG_SYMBOLS}
        self.is_crypto = is_crypto
        self.interval = interval          # [I] — how often we poll for new data
        self.bar_interval = bar_interval  # [B] — chart bucket size, independent of polling
        self.min_trade_usd = min_trade_usd            # [M] — drop trades/deltas below this $
        self.confidence_deadzone = confidence_deadzone  # [C] — equities only, see classify_sign
        self.filtered_mode = False   # [F] — display the OTM-filtered ledger instead of the
        # unfiltered one; pure display flag, both ledgers are always tracked every poll
        self.net_volume_mode = True  # [N] — bottom panel: True = Net Volume (cumulative
        # signed area), False = old-style Volume (unsigned per-bucket bars). Both are
        # always computed by build_columns; this only picks which one gets drawn.
        self.live_state = self.background[symbol].state if symbol in self.background else State()
        self.state = self.live_state
        self.live = True
        self.view_date = None
        self.view_offset = 0   # pan/scroll, in BARS behind the live/anchor
        # edge — ported from charthacker.py's view_offset scheme (see
        # chart_x_end and the arrow/bracket key handling in curses_main)
        self.next_poll_ts = time.time()
        self.refresh_event = threading.Event()
        self.stop = False


def _seed_state_from_samples(state, samples, is_crypto):
    """Shared by seed_today (live resume) and view_historical ([H] browse):
    populates state from the last of a list of previously-logged samples.
    For crypto, restores each venue's own sub-totals from the sample's
    "venues" breakdown when present. Older logs written before multi-venue
    support have no "venues" key — they're entirely Deribit-sourced data,
    so the whole total is attributed there rather than discarded. Only
    Deribit's cursor is seeded from the resume point (a real ms timestamp
    it can backfill from); OKX/Bybit have no time-range query to seed —
    their next poll just re-baselines from a fresh snapshot, same as any
    other first poll."""
    if not samples:
        return
    state.history = samples
    last = samples[-1]
    # so a resumed same-day session doesn't spuriously think a day just
    # rolled the moment it starts polling (see _maybe_reset_for_new_day) —
    # this always ends up being TODAY's date for seed_today (the log it
    # read only ever contains today's samples); harmless/unused for
    # view_historical's read-only, never-polled snapshot
    state.last_log_date = datetime.fromtimestamp(last["ts"]).strftime("%m_%d_%Y")
    state.spot = last["spot"]
    state.calls_cum = last["calls"]
    state.puts_cum = last["puts"]
    state.calls_cum_f = last.get("calls_f", 0.0)
    state.puts_cum_f = last.get("puts_f", 0.0)
    state.net_vol_cum = last.get("net_vol", 0.0)
    state.net_vol_cum_f = last.get("net_vol_f", 0.0)
    if is_crypto:
        breakdown = last.get("venues")
        if breakdown:
            for name, v in breakdown.items():
                if name in state.venues:
                    state.venues[name]["calls_cum"] = v.get("calls", 0.0)
                    state.venues[name]["puts_cum"] = v.get("puts", 0.0)
                    state.venues[name]["calls_cum_f"] = v.get("calls_f", 0.0)
                    state.venues[name]["puts_cum_f"] = v.get("puts_f", 0.0)
                    state.venues[name]["net_vol_cum"] = v.get("net_vol", 0.0)
                    state.venues[name]["net_vol_cum_f"] = v.get("net_vol_f", 0.0)
                    state.venues[name]["expiry_label"] = v.get("expiry")
        else:
            state.venues["deribit"]["calls_cum"] = last["calls"]
            state.venues["deribit"]["puts_cum"] = last["puts"]
            state.venues["deribit"]["calls_cum_f"] = last.get("calls_f", 0.0)
            state.venues["deribit"]["puts_cum_f"] = last.get("puts_f", 0.0)
            state.venues["deribit"]["net_vol_cum"] = last.get("net_vol", 0.0)
            state.venues["deribit"]["net_vol_cum_f"] = last.get("net_vol_f", 0.0)
            state.venues["deribit"]["expiry_label"] = last.get("expiry")
        state.venues["deribit"]["cursor"] = int(last["ts"] * 1000)
    else:
        state.expiry_label = last.get("expiry")


def seed_today(state, symbol, is_crypto):
    date_str = datetime.now().strftime("%m_%d_%Y")
    samples = load_log(symbol, date_str)
    _seed_state_from_samples(state, samples, is_crypto)


def _maybe_reset_for_new_day(state):
    """Calendar-day reset — RESETS trigger #1 in the module docstring.
    log_path/_date_folder already rotate the LOG FILENAME automatically
    (computed fresh on every append_log call), but that alone does
    nothing to the in-memory running totals of a process that's still
    running when local midnight passes — nothing else in the poll path
    re-checks the date once a process is already up, so without this,
    calls_cum/puts_cum/etc would carry straight across the boundary
    uninterrupted (confirmed live: a long-running session's first sample
    of a new day showed the EXACT same totals as the previous day's last
    sample — this check didn't exist before, and that's why).

    Must be called EVERY poll (matching how _maybe_reset_for_expiry
    re-fetches and compares the live expiry every poll, not just once at
    startup) with state.lock already held. Only resets the flat/top-level
    fields — crypto's per-venue dicts are the caller's responsibility
    (see poll_once_crypto), since this function has no venue concept.
    Returns True if a reset just happened."""
    today = datetime.now().strftime("%m_%d_%Y")
    rolled = state.last_log_date is not None and today != state.last_log_date
    if rolled:
        state.calls_cum = 0.0
        state.puts_cum = 0.0
        state.calls_cum_f = 0.0
        state.puts_cum_f = 0.0
        state.net_vol_cum = 0.0
        state.net_vol_cum_f = 0.0
    state.last_log_date = today
    return rolled


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
        state.calls_cum_f = 0.0
        state.puts_cum_f = 0.0
        state.net_vol_cum = 0.0
        state.net_vol_cum_f = 0.0
    state.expiry_label = expiry_label
    return f" (expiry rolled {old} -> {expiry_label} — drift reset)" if rolled else ""


def _record_sample(symbol, state, d_calls, d_puts, d_calls_f, d_puts_f, d_netvol, d_netvol_f,
                    spot, expiry_label, interval_vol, live_status):
    day_suffix = " (new day — drift reset)" if _maybe_reset_for_new_day(state) else ""
    suffix = day_suffix + _maybe_reset_for_expiry(state, expiry_label)
    state.calls_cum += d_calls
    state.puts_cum += d_puts
    state.calls_cum_f += d_calls_f
    state.puts_cum_f += d_puts_f
    state.net_vol_cum += d_netvol
    state.net_vol_cum_f += d_netvol_f
    sample = {"ts": time.time(), "calls": state.calls_cum, "puts": state.puts_cum,
              "calls_f": state.calls_cum_f, "puts_f": state.puts_cum_f,
              "net_vol": state.net_vol_cum, "net_vol_f": state.net_vol_cum_f,
              "spot": spot, "vol": interval_vol, "expiry": expiry_label}
    state.history.append(sample)
    append_log(symbol, sample)
    state.have_baseline = True
    state.spot = spot
    state.status = live_status + suffix
    state.last_poll = time.time()


def _empty_venue_result(cursor, expiry_label, rolled):
    """First-poll (baseline-only) or fully-empty-batch result shape,
    shared by all three venue pollers — see poll_venue_deribit for the
    full field meanings."""
    return {"d_calls": 0.0, "d_puts": 0.0, "d_calls_f": 0.0, "d_puts_f": 0.0,
            "d_netvol": 0.0, "d_netvol_f": 0.0, "interval_vol": 0.0,
            "cursor": cursor, "expiry_label": expiry_label, "rolled": rolled}


def poll_venue_deribit(symbol, cursor_prev, expiry_prev, min_trade_usd, fallback_spot):
    """Pure function — reads the venue's prior cursor/expiry as plain
    arguments and returns its contribution without touching any shared
    state, so three of these can run concurrently (one per venue, see
    poll_once_crypto) without racing on the same dict. Caller applies the
    result to state.venues["deribit"] under state.lock afterward.

    Returns a dict: d_calls/d_puts (unfiltered $ premium, signed),
    d_calls_f/d_puts_f (same, but only trades passing [F]'s OTM filter —
    0DTE has no teeth for crypto since only the nearest expiry is ever
    tracked to begin with, see module docstring), d_netvol/d_netvol_f
    (signed CONTRACT count, calls+puts combined, for the Net Volume
    panel), interval_vol, cursor, expiry_label, rolled.

    True historical range query — cursor_prev being an earlier timestamp
    (e.g. seeded from a resumed log's last sample, see
    _seed_state_from_samples) naturally backfills that whole gap.

    min_trade_usd ([M]): trades below this $ premium are skipped entirely
    — not counted toward interval_vol either, since the point is to drop
    them from the picture altogether, not just leave them unsigned (that's
    what confidence_deadzone/classify_sign does instead, equities only)."""
    now_ms = int(time.time() * 1000)
    first_poll = cursor_prev is None
    instrument_set, expiry_label = fetch_deribit_nearest_expiry(symbol)
    rolled = expiry_prev is not None and expiry_label != expiry_prev
    if first_poll:
        return _empty_venue_result(now_ms, expiry_label, rolled)
    trades = fetch_deribit_trades(symbol, cursor_prev, now_ms, instrument_set)
    d_calls = d_puts = d_calls_f = d_puts_f = interval_vol = d_netvol = d_netvol_f = 0.0
    for t in trades:
        contracts = float(t["amount"])
        trade_spot = float(t["index_price"])
        usd = float(t["price"]) * contracts * trade_spot
        if usd < min_trade_usd:
            continue
        sign = 1.0 if t.get("direction") == "buy" else -1.0
        strike, otype = _parse_strike_otype(t["instrument_name"])
        otm = _is_otm(otype, strike, trade_spot)
        interval_vol += usd
        d_netvol += sign * contracts
        if otm:
            d_netvol_f += sign * contracts
        if otype == "call":
            d_calls += usd * sign
            if otm:
                d_calls_f += usd * sign
        else:
            d_puts += usd * sign
            if otm:
                d_puts_f += usd * sign
    return {"d_calls": d_calls, "d_puts": d_puts, "d_calls_f": d_calls_f, "d_puts_f": d_puts_f,
            "d_netvol": d_netvol, "d_netvol_f": d_netvol_f, "interval_vol": interval_vol,
            "cursor": now_ms, "expiry_label": expiry_label, "rolled": rolled}


def poll_venue_okx(symbol, cursor_prev, expiry_prev, min_trade_usd, fallback_spot):
    """Same pure-function shape and return-dict fields as
    poll_venue_deribit (see there for what each field means). cursor_prev/
    new cursor are sets of already-seen (instId, tradeId) pairs (no
    time-range query exists here — see fetch_okx_option_trades), so unlike
    Deribit this can only dedup against the current recent-trades
    snapshot, never backfill a gap wider than that snapshot. IMPORTANT:
    keyed on (instId, tradeId), NOT tradeId alone — confirmed live against
    the real endpoint that OKX's tradeId is scoped per-instrument, not
    globally unique across the whole option-trades response (~10% of a
    real batch had the same tradeId reused on a completely different
    instId); a tradeId-only key would silently drop genuine new trades
    whenever they collided with an old ID from an unrelated contract."""
    instrument_set, ct_val, ct_mult, expiry_label = fetch_okx_nearest_expiry(symbol)
    rolled = expiry_prev is not None and expiry_label != expiry_prev
    batch = fetch_okx_option_trades(symbol)
    new_cursor = {(t.get("instId"), t.get("tradeId")) for t in batch}
    if cursor_prev is None:
        return _empty_venue_result(new_cursor, expiry_label, rolled)
    d_calls = d_puts = d_calls_f = d_puts_f = interval_vol = d_netvol = d_netvol_f = 0.0
    for t in batch:
        key = (t.get("instId"), t.get("tradeId"))
        if t.get("tradeId") is None or key in cursor_prev or t.get("instId") not in instrument_set:
            continue
        contracts = float(t["sz"])
        trade_spot = float(t["idxPx"])
        usd = float(t["px"]) * ct_val * ct_mult * contracts * trade_spot
        if usd < min_trade_usd:
            continue
        sign = 1.0 if t.get("side") == "buy" else -1.0
        strike, otype = _parse_strike_otype(t["instId"])
        otm = _is_otm(otype, strike, trade_spot)
        interval_vol += usd
        d_netvol += sign * contracts
        if otm:
            d_netvol_f += sign * contracts
        if otype == "call":
            d_calls += usd * sign
            if otm:
                d_calls_f += usd * sign
        else:
            d_puts += usd * sign
            if otm:
                d_puts_f += usd * sign
    return {"d_calls": d_calls, "d_puts": d_puts, "d_calls_f": d_calls_f, "d_puts_f": d_puts_f,
            "d_netvol": d_netvol, "d_netvol_f": d_netvol_f, "interval_vol": interval_vol,
            "cursor": new_cursor, "expiry_label": expiry_label, "rolled": rolled}


def poll_venue_bybit(symbol, cursor_prev, expiry_prev, min_trade_usd, fallback_spot):
    """Same shape/limitation as poll_venue_okx (recent-N snapshot, no
    backfill, same return-dict fields). Bybit options are USDC-settled per
    Bybit's own docs, so price is already ~USD — no index-price multiply
    needed. This specific venue could not be verified against a live
    response (see fetch_bybit_option_trades docstring), including whether
    execId is globally unique or per-symbol like OKX's tradeId turned out
    to be (confirmed live — see poll_venue_okx) — so this dedups on
    (symbol, execId) defensively rather than assuming execId alone is
    safe. Per-trade index price (`iP`, for OTM classification) is also
    docs-only/unverified — falls back to the poll's own fetched spot
    (fallback_spot) when a trade record doesn't carry it."""
    instrument_set, expiry_label = fetch_bybit_nearest_expiry(symbol)
    rolled = expiry_prev is not None and expiry_label != expiry_prev
    batch = fetch_bybit_option_trades(symbol)
    new_cursor = {(t.get("symbol"), t.get("execId")) for t in batch}
    if cursor_prev is None:
        return _empty_venue_result(new_cursor, expiry_label, rolled)
    d_calls = d_puts = d_calls_f = d_puts_f = interval_vol = d_netvol = d_netvol_f = 0.0
    for t in batch:
        key = (t.get("symbol"), t.get("execId"))
        if t.get("execId") is None or key in cursor_prev or t.get("symbol") not in instrument_set:
            continue
        contracts = float(t["size"])
        usd = float(t["price"]) * contracts
        if usd < min_trade_usd:
            continue
        sign = 1.0 if t.get("side") == "Buy" else -1.0
        strike, otype = _parse_strike_otype(t["symbol"])
        trade_spot = float(t["iP"]) if t.get("iP") else fallback_spot
        otm = _is_otm(otype, strike, trade_spot)
        interval_vol += usd
        d_netvol += sign * contracts
        if otm:
            d_netvol_f += sign * contracts
        if otype == "call":
            d_calls += usd * sign
            if otm:
                d_calls_f += usd * sign
        else:
            d_puts += usd * sign
            if otm:
                d_puts_f += usd * sign
    return {"d_calls": d_calls, "d_puts": d_puts, "d_calls_f": d_calls_f, "d_puts_f": d_puts_f,
            "d_netvol": d_netvol, "d_netvol_f": d_netvol_f, "interval_vol": interval_vol,
            "cursor": new_cursor, "expiry_label": expiry_label, "rolled": rolled}


VENUE_POLLERS = {
    "deribit": poll_venue_deribit,
    "okx": poll_venue_okx,
    "bybit": poll_venue_bybit,
}


def _sync_totals(state):
    """Aggregate totals (unfiltered and [F] filtered, plus Net Volume) are
    always recomputed fresh from the per-venue sub-totals rather than
    maintained redundantly — there's no way for the displayed total to
    drift out of sync with its parts."""
    state.calls_cum = sum(v["calls_cum"] for v in state.venues.values())
    state.puts_cum = sum(v["puts_cum"] for v in state.venues.values())
    state.calls_cum_f = sum(v["calls_cum_f"] for v in state.venues.values())
    state.puts_cum_f = sum(v["puts_cum_f"] for v in state.venues.values())
    state.net_vol_cum = sum(v["net_vol_cum"] for v in state.venues.values())
    state.net_vol_cum_f = sum(v["net_vol_cum_f"] for v in state.venues.values())


def poll_once_crypto(symbol, state, min_trade_usd):
    """Fans out to all three venues in parallel, each isolated by its own
    try/except — one venue erroring (e.g. Bybit's unverified geo-block
    risk) contributes $0 that interval and is reflected in
    state.venue_status, but never stops the other two venues from
    recording or crashes the poll loop. See module docstring for the
    per-venue methodology and RESETS section for why each venue's own
    sub-total resets independently rather than one global reset."""
    with state.lock:
        first_poll = not state.have_baseline
        prior = {name: (state.venues[name]["cursor"], state.venues[name]["expiry_label"])
                  for name in VENUE_POLLERS}
        prior_spot = state.spot

    # fetched BEFORE dispatching the venue pollers (not after, like before
    # this feature) so it's available as fallback_spot for Bybit's OTM
    # classification (see poll_venue_bybit) — Deribit/OKX always carry
    # their own per-trade spot and don't need it.
    spot = None
    errors = {}
    try:
        spot = fetch_deribit_index(symbol)
    except Exception as e:
        errors["spot"] = str(e)
    fallback_spot = spot if spot is not None else prior_spot

    results = {}
    with ThreadPoolExecutor(max_workers=len(VENUE_POLLERS)) as ex:
        futs = {ex.submit(fn, symbol, *prior[name], min_trade_usd, fallback_spot): name
                for name, fn in VENUE_POLLERS.items()}
        for fut, name in futs.items():
            try:
                results[name] = fut.result()
            except Exception as e:
                errors[name] = str(e)

    with state.lock:
        day_rolled = _maybe_reset_for_new_day(state)
        if day_rolled:
            # top-level fields already zeroed by _maybe_reset_for_new_day
            # (it has no venue concept) — reset each venue's own cum
            # fields too, same as this loop already does per-venue on an
            # individual expiry rollover
            for v in state.venues.values():
                v["calls_cum"] = 0.0
                v["puts_cum"] = 0.0
                v["calls_cum_f"] = 0.0
                v["puts_cum_f"] = 0.0
                v["net_vol_cum"] = 0.0
                v["net_vol_cum_f"] = 0.0

        interval_vol = 0.0
        for name in VENUE_POLLERS:
            if name not in results:
                continue
            r = results[name]
            venue = state.venues[name]
            if r["rolled"]:
                venue["calls_cum"] = 0.0
                venue["puts_cum"] = 0.0
                venue["calls_cum_f"] = 0.0
                venue["puts_cum_f"] = 0.0
                venue["net_vol_cum"] = 0.0
                venue["net_vol_cum_f"] = 0.0
            venue["calls_cum"] += r["d_calls"]
            venue["puts_cum"] += r["d_puts"]
            venue["calls_cum_f"] += r["d_calls_f"]
            venue["puts_cum_f"] += r["d_puts_f"]
            venue["net_vol_cum"] += r["d_netvol"]
            venue["net_vol_cum_f"] += r["d_netvol_f"]
            venue["expiry_label"] = r["expiry_label"]
            venue["cursor"] = r["cursor"]
            interval_vol += r["interval_vol"]
        _sync_totals(state)
        state.venue_status = {name: ("live" if name not in errors else f"error: {errors[name]}")
                               for name in VENUE_POLLERS}
        new_spot = spot if spot is not None else state.spot
        day_suffix = " (new day — drift reset)" if day_rolled else ""

        if first_poll:
            state.have_baseline = True
            state.spot = new_spot
            state.status = "baseline set — first sample next poll" + day_suffix
            state.last_poll = time.time()
        else:
            sample = {
                "ts": time.time(), "calls": state.calls_cum, "puts": state.puts_cum,
                "calls_f": state.calls_cum_f, "puts_f": state.puts_cum_f,
                "net_vol": state.net_vol_cum, "net_vol_f": state.net_vol_cum_f,
                "spot": new_spot, "vol": interval_vol,
                "venues": {name: {"calls": v["calls_cum"], "puts": v["puts_cum"],
                                   "calls_f": v["calls_cum_f"], "puts_f": v["puts_cum_f"],
                                   "net_vol": v["net_vol_cum"], "net_vol_f": v["net_vol_cum_f"],
                                   "expiry": v["expiry_label"]}
                           for name, v in state.venues.items()},
            }
            state.history.append(sample)
            append_log(symbol, sample)
            state.spot = new_spot
            state.status = "live" + day_suffix
            state.last_poll = time.time()


def poll_once_cboe(symbol, state, min_trade_usd, confidence_deadzone):
    """Snapshot-diff aggressor-proxy path — see module docstring.

    min_trade_usd ([M]) here filters a whole POLL INTERVAL's aggregate
    volume delta for a contract, not a single trade's size — CBOE's feed
    has no per-trade granularity, only periodic per-contract snapshots, so
    "one trade" isn't a thing this path can see. Still serves the same
    purpose (drop small dribbles of volume rather than counting them).

    [F] filtered mode here is BOTH real filters (unlike crypto, where 0DTE
    is a no-op — see module docstring): is_0dte gates the whole poll's
    filtered contribution to zero on days fetch_cboe_chain had to fall
    back off today's date, and OTM is checked per contract using this
    poll's one chain["spot"] (CBOE gives one snapshot spot per poll, not
    per-contract, so there's no finer granularity to use here)."""
    chain = fetch_cboe_chain(symbol)
    contracts = chain["contracts"]
    is_0dte = chain["is_0dte"]
    spot = chain["spot"]
    with state.lock:
        first_poll = not state.have_baseline
        d_calls = d_puts = d_calls_f = d_puts_f = interval_vol = d_netvol = d_netvol_f = 0.0
        for c in contracts:
            key = c["key"]
            prev = state.baseline.get(key)
            cur = c["baseline_value"]
            state.baseline[key] = cur
            if prev is None or cur <= prev:
                continue
            delta_contracts = cur - prev
            usd = delta_contracts * c["multiplier"]
            if usd <= 0:
                continue
            if usd < min_trade_usd:
                continue
            sign = classify_sign(c["last"], c["bid"], c["ask"], confidence_deadzone)
            interval_vol += usd
            d_netvol += sign * delta_contracts
            otm = is_0dte and _is_otm(c["otype"], c["strike"], spot)
            if otm:
                d_netvol_f += sign * delta_contracts
            if c["otype"] == "call":
                d_calls += usd * sign
                if otm:
                    d_calls_f += usd * sign
            else:
                d_puts += usd * sign
                if otm:
                    d_puts_f += usd * sign

        if first_poll:
            state.have_baseline = True
            state.spot = chain["spot"]
            day_suffix = " (new day — drift reset)" if _maybe_reset_for_new_day(state) else ""
            suffix = _maybe_reset_for_expiry(state, chain["expiry_label"])
            state.status = "baseline set — first sample next poll" + day_suffix + suffix
            state.last_poll = time.time()
        else:
            _record_sample(symbol, state, d_calls, d_puts, d_calls_f, d_puts_f, d_netvol, d_netvol_f,
                            chain["spot"], chain["expiry_label"], interval_vol, "live")


def poll_once(symbol, is_crypto, state, min_trade_usd, confidence_deadzone):
    if is_crypto:
        poll_once_crypto(symbol, state, min_trade_usd)
    else:
        poll_once_cboe(symbol, state, min_trade_usd, confidence_deadzone)


def poll_loop(ctrl):
    """Polls into ctrl.live_state for whatever the current symbol is —
    independent of ctrl.state/ctrl.live, so this keeps running while the
    UI browses a historical date via [H] — BUT ONLY when the focused
    symbol is NOT one of BG_SYMBOLS. ETH/QQQ each have their own dedicated
    background_poll_loop that owns their State continuously regardless of
    focus (see BG_SYMBOLS' comment on why there must never be two pollers
    on the same symbol); when focus is on one of them, this loop just
    idles — there's nothing for it to do that the dedicated one isn't
    already doing."""
    while not ctrl.stop:
        with ctrl.lock:
            symbol, is_crypto, state, interval = ctrl.symbol, ctrl.is_crypto, ctrl.live_state, ctrl.interval
            min_trade_usd, confidence_deadzone = ctrl.min_trade_usd, ctrl.confidence_deadzone
            is_background = symbol in ctrl.background
        if not is_background:
            try:
                poll_once(symbol, is_crypto, state, min_trade_usd, confidence_deadzone)
            except Exception as e:
                with state.lock:
                    state.status = f"error: {e}"
            with ctrl.lock:
                ctrl.next_poll_ts = time.time() + interval
        ctrl.refresh_event.wait(timeout=interval)
        ctrl.refresh_event.clear()


def background_poll_loop(ctrl, tracker):
    """Runs for the program's whole lifetime, independent of what's
    displayed — see BG_SYMBOLS' comment for why this exists and why it's
    not a literal port of charm.py's own bg_tick. QQQ specifically only
    actually polls during is_market_open_et() (already timezone-correct
    via ZoneInfo, reused as-is rather than porting charm.py's own naive-CT
    asset_active_now); outside that window it idles and rechecks
    periodically instead of hammering CBOE for nothing new. ETH has no
    such gate — crypto trades 24/7."""
    while not ctrl.stop:
        if not tracker.is_crypto and not is_market_open_et():
            with tracker.state.lock:
                tracker.state.status = "market closed — waiting for next session"
            tracker.next_poll_ts = time.time() + 60
            tracker.refresh_event.wait(timeout=60)
            tracker.refresh_event.clear()
            continue
        try:
            poll_once(tracker.symbol, tracker.is_crypto, tracker.state,
                      tracker.min_trade_usd, tracker.confidence_deadzone)
        except Exception as e:
            with tracker.state.lock:
                tracker.state.status = f"error: {e}"
        tracker.next_poll_ts = time.time() + tracker.interval
        tracker.refresh_event.wait(timeout=tracker.interval)
        tracker.refresh_event.clear()


def switch_symbol(ctrl, raw):
    sym = raw.strip().upper()
    if not sym:
        return
    is_crypto = sym in ("ETH", "BTC")
    is_background = sym in ctrl.background
    if is_background:
        # already being tracked continuously — point straight at that
        # tracker's own State (same object, not a copy: see BG_SYMBOLS'
        # comment on why there must only ever be ONE poller touching a
        # symbol's cumulative totals). No seed_today needed, it never
        # stopped being logged.
        new_state = ctrl.background[sym].state
    else:
        new_state = State()
        try:
            seed_today(new_state, sym, is_crypto)
        except Exception:
            pass
    with ctrl.lock:
        ctrl.symbol = sym
        ctrl.is_crypto = is_crypto
        ctrl.live_state = new_state
        ctrl.state = new_state
        ctrl.live = True
        ctrl.view_date = None
        ctrl.view_offset = 0   # new symbol's data — start back at its live edge
    ctrl.refresh_event.set()   # wake poll_loop immediately — a no-op if the new
    # symbol is background-tracked (poll_loop just idles for those), but
    # harmless either way
    if is_background:
        ctrl.background[sym].refresh_event.set()   # nudge its OWN dedicated loop too


def view_historical(ctrl, date_str):
    """[H]: load a past day's log for the current symbol into a standalone
    snapshot and display it, read-only. Does NOT touch live_state — the
    background poll thread keeps tracking the live session the whole time,
    so [H] again with 'live' snaps straight back to an up-to-date chart."""
    with ctrl.lock:
        symbol, is_crypto = ctrl.symbol, ctrl.is_crypto
    samples = load_log(symbol, date_str)
    hist_state = State()
    hist_state.have_baseline = True
    if samples:
        _seed_state_from_samples(hist_state, samples, is_crypto)
        hist_state.status = f"historical — {date_str}"
    else:
        hist_state.status = f"no log found for {date_str}"
    with ctrl.lock:
        ctrl.state = hist_state
        ctrl.live = False
        ctrl.view_date = date_str
        ctrl.view_offset = 0   # new day's data — start at ITS edge (most recent logged sample)


def return_to_live(ctrl):
    with ctrl.lock:
        ctrl.state = ctrl.live_state
        ctrl.live = True
        ctrl.view_date = None
        ctrl.view_offset = 0


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


def fmt_axis_count(v):
    """Net Volume panel's axis — CONTRACT count, not $, so no '$' prefix
    (matches the QuantData-style reference: '5 K', '-35 K', no dollar
    sign, distinct from the premium panel's $-denominated axis above)."""
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}{v / 1_000_000:.1f}M"
    if v >= 1000:
        return f"{sign}{v / 1000:.0f}K"
    return f"{sign}{v:.0f}"


def fmt_price(p):
    if p is None:
        return "—"
    return f"${p:,.2f}" if p >= 1 else f"${p:.5f}"


def fmt_duration(seconds):
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


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


VENUE_ABBREV = {"deribit": "D", "okx": "OKX", "bybit": "Bybit"}


def _short_expiry(expiry_label):
    # "09 Aug 2026" -> "09Aug" — all three venues format expiry_label the
    # same way (see fetch_*_nearest_expiry), so this is safe across venues.
    if not expiry_label:
        return "—"
    parts = expiry_label.split()
    return parts[0] + parts[1] if len(parts) >= 2 else expiry_label


def fmt_venue_line(state):
    """Compact per-venue health/expiry readout for the info line, e.g.
    'D:09Aug✓ OKX:09Aug✓ Bybit:✗' — makes it visible at a glance if a venue
    (most likely Bybit, unverified from the dev sandbox — see module
    docstring) has silently stopped contributing to the aggregate."""
    parts = []
    for name in CRYPTO_VENUES:
        v = state.venues[name]
        status = state.venue_status.get(name, "")
        mark = "✓" if status == "live" else ("✗" if status else "…")
        parts.append(f"{VENUE_ABBREV[name]}:{_short_expiry(v['expiry_label'])}{mark}")
    return " ".join(parts)


def fmt_venue_breakdown(state, filtered_mode=False):
    """[V] toggle's extra line: each venue's own calls/puts sub-total, so
    you can see which venue is actually driving the aggregate. Shows the
    [F] filtered sub-totals when filtered_mode is on, matching whatever
    the rest of the chart is currently displaying."""
    calls_key = "calls_cum_f" if filtered_mode else "calls_cum"
    puts_key = "puts_cum_f" if filtered_mode else "puts_cum"
    parts = []
    for name in CRYPTO_VENUES:
        v = state.venues[name]
        parts.append(f"{VENUE_ABBREV[name]} {fmt_money(v[calls_key])}/{fmt_money(v[puts_key])}")
    return "   ".join(parts)


def chart_x_end(history, live, view_offset, bar_interval):
    """Right edge of the chart's fixed bar_interval-wide window (see
    build_columns), shifted back by view_offset bars — pan/scroll, ported
    from charthacker.py's view_offset scheme (same step sizes/keys — see
    the arrow/bracket key handling in curses_main). Returns
    (x_end, clamped_view_offset).

    The anchor view_offset counts back FROM is "now" while live, so a
    scrolled-back view keeps its distance from the live edge as new data
    keeps arriving (exactly like charthacker's index-based
    `n_all - view_offset`: pan 50 bars back, and you STAY 50 bars back as
    new bars land, rather than staying pinned to a fixed clock time) — or
    the last logged sample when browsing a finished historical day via
    [H], which is a fixed anchor since that day never gains new samples.

    view_offset is clamped so panning can't go further back than the
    earliest available sample, the same role as charthacker's
    `min(n_all - 1, view_offset)` clamp."""
    anchor = time.time() if live else (history[-1]["ts"] if history else time.time())
    if history:
        max_offset = max(0, int((anchor - history[0]["ts"]) // bar_interval))
    else:
        max_offset = 0
    clamped = max(0, min(max_offset, view_offset))
    return anchor - clamped * bar_interval, clamped


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


def plot_area(win, cols, vmin, vmax, row_top, row_bot, left_x):
    """Cumulative-signed area fill for the Net Volume panel — green above
    zero, red below, filled solid from the zero row (or the nearest panel
    edge, if zero itself isn't in [vmin, vmax]) to each column's own row.
    Matches the QuantData-style reference screenshot's filled net-volume
    area, as opposed to plot_line's thin connected-dot line used for the
    premium panel above."""
    zero_row = to_row(0.0, vmin, vmax, row_top, row_bot) if vmin <= 0.0 <= vmax else None
    for i, v in enumerate(cols):
        if v is None:
            continue
        r = to_row(v, vmin, vmax, row_top, row_bot)
        attr = cp(P_GREEN, dim=True) if v >= 0 else cp(P_RED, dim=True)
        anchor = zero_row if zero_row is not None else (row_bot if v >= 0 else row_top)
        r0, r1 = sorted((anchor, r))
        for rr in range(r0, r1 + 1):
            safe_add(win, rr, left_x + i, "█", attr)


def build_columns(history, x_end, bar_interval, plot_w, filtered=False):
    """Each column is a FIXED bar_interval-second bucket — a real "chart
    interval" control (like picking 1m/5m/15m bars in any other charting
    tool), not the previous behavior of proportionally squeezing however
    much history exists into plot_w columns. Column plot_w-1 (rightmost)
    is the bucket ending at x_end; each column to the left is one more
    bar_interval further back. Samples older than plot_w*bar_interval
    (col < 0) are simply outside the visible window — same as any
    charting tool, seeing further back means picking a larger bar
    interval, not proportionally rescaling what's already on screen.
    Multiple samples landing in the same bucket keep the LAST one —
    calls/puts/spot/net_vol are all cumulative running totals (a step
    chart, consistent with how a single poll's value already worked for
    calls/puts), so "last in the bucket" is correct for all four, not just
    the first three.

    filtered ([F]): pulls the "_f"-suffixed calls/puts/net_vol fields
    (OTM-filtered — see module docstring) instead of the unfiltered ones.
    Spot is never filtered, same either way.

    Also returns cols_vol — the OLD unsigned per-bucket $ premium
    activity (calls+puts combined, SUMMED per bucket rather than
    step-charted, since it's interval activity, not a running total) —
    [N] toggles the bottom panel between this and cols_netvol; both are
    always computed, the toggle is pure display, no re-poll needed, same
    pattern as [F]/[V]."""
    calls_key = "calls_f" if filtered else "calls"
    puts_key = "puts_f" if filtered else "puts"
    netvol_key = "net_vol_f" if filtered else "net_vol"
    cols_calls = [None] * plot_w
    cols_puts = [None] * plot_w
    cols_spot = [None] * plot_w
    cols_netvol = [None] * plot_w
    cols_vol = [0.0] * plot_w
    for s in history:
        c = plot_w - 1 - int((x_end - s["ts"]) // bar_interval)
        if c < 0 or c >= plot_w:
            continue
        cols_calls[c] = s[calls_key]
        cols_puts[c] = s[puts_key]
        cols_spot[c] = s["spot"]
        cols_netvol[c] = s.get(netvol_key, 0.0)
        cols_vol[c] += s.get("vol") or 0.0

    def ffill(col):
        last = None
        for i in range(len(col)):
            if col[i] is None:
                col[i] = last
            else:
                last = col[i]
        return col

    return ffill(cols_calls), ffill(cols_puts), ffill(cols_spot), ffill(cols_netvol), cols_vol


def draw(stdscr, ctrl, show_venues=False):
    with ctrl.lock:
        symbol, is_crypto, state, live = ctrl.symbol, ctrl.is_crypto, ctrl.state, ctrl.live
        view_date, bar_interval = ctrl.view_date, ctrl.bar_interval
        raw_view_offset = ctrl.view_offset
        filtered_mode = ctrl.filtered_mode
        net_volume_mode = ctrl.net_volume_mode
        # interval/min_trade_usd/confidence_deadzone/next_poll_ts come from
        # whatever's actually polling the focused symbol — its own
        # BackgroundTracker for ETH/QQQ, ctrl itself otherwise (see
        # _focused_tuning) — so the footer/countdown reflect reality
        # regardless of which of the three poll loops owns this symbol
        tuning = _focused_tuning(ctrl, symbol)
        interval, next_poll_ts = tuning.interval, tuning.next_poll_ts
        min_trade_usd, confidence_deadzone = tuning.min_trade_usd, tuning.confidence_deadzone
    with state.lock:
        history = list(state.history)
        calls_cum = state.calls_cum_f if filtered_mode else state.calls_cum
        puts_cum = state.puts_cum_f if filtered_mode else state.puts_cum
        spot, status, expiry = state.spot, state.status, state.expiry_label
        last_poll = state.last_poll
        # computed while the lock is held — state.venues/venue_status are
        # mutated by the poll thread, so format the strings now rather
        # than reading them again after releasing the lock
        venue_info = fmt_venue_line(state) if is_crypto else None
        venue_breakdown = fmt_venue_breakdown(state, filtered_mode) if (is_crypto and show_venues) else None

    x_end, view_offset = chart_x_end(history, live, raw_view_offset, bar_interval)
    if view_offset != raw_view_offset:
        with ctrl.lock:
            ctrl.view_offset = view_offset

    h, w = stdscr.getmaxyx()
    stdscr.erase()
    if h < 14 or w < 50:
        safe_add(stdscr, 0, 0, "terminal too small — resize", cp(P_YELLOW))
        stdscr.refresh()
        return

    title = f" Net Drift (Premium) — {symbol} "
    if filtered_mode:
        title += "(filtered — OTM only) "
    if not live:
        title += f"(historical — {view_date}) "
    if view_offset > 0:
        title += f"[← {fmt_duration(view_offset * bar_interval)}] "
    safe_add(stdscr, 0, 0, title.ljust(w), cp(P_STATUS, bold=True))

    calls_str, puts_str, spot_str = fmt_money(calls_cum), fmt_money(puts_cum), fmt_price(spot)
    draw_segments(stdscr, 1, 1, [
        ("● ", cp(P_GREEN, bold=True)), (f"Calls ({calls_str})   ", cp(P_DEFAULT)),
        ("● ", cp(P_RED, bold=True)), (f"Puts ({puts_str})   ", cp(P_DEFAULT)),
        ("● ", cp(P_BLUE, bold=True)), (f"{symbol} ({spot_str})", cp(P_DEFAULT)),
    ])
    if is_crypto:
        info = f"{venue_info}  {status}"
    else:
        info = (f"exp {expiry}  " if expiry else "") + status
        if not is_market_open_et():
            info = "market closed — " + info
    safe_add(stdscr, 1, max(0, w - len(info) - 1), info, cp(P_DIM))

    LEFT_W, RIGHT_W = 8, 10
    FOOTER_ROWS, TIME_ROWS = 1, 1
    top = 3
    if venue_breakdown is not None:
        safe_add(stdscr, 2, 1, venue_breakdown, cp(P_DIM))
        top = 4
    available = h - top - FOOTER_ROWS - TIME_ROWS
    if available < 8:
        stdscr.refresh()
        return
    main_h = max(5, int(available * 0.68))
    vol_h = max(3, available - main_h - 1)
    main_top = top
    vol_top = main_top + main_h + 1
    plot_w = max(1, w - LEFT_W - RIGHT_W - 1)

    cols_calls, cols_puts, cols_spot, cols_netvol, cols_vol = build_columns(history, x_end, bar_interval, plot_w, filtered_mode)

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

    # Live price line — ported from athena.py's own chart (its "live_g"/
    # live_row_y block): a dashed reference line at the CURRENT price
    # (state.spot, not just whatever the rightmost column happens to
    # show — matters while scrolled back via [<-], same as cvd.py's own
    # live_price adaptation of this already documents), plus a solid
    # reverse-video price-axis badge, TradingView-style, per the exact
    # request athena.py's own version was built for. Athena's axis is on
    # the LEFT; drift's price axis is on the RIGHT, same side adjustment
    # cvd.py already made for its own port of this feature. Drawn BEFORE
    # plot_line's real calls/puts/spot glyphs below, so it only shows
    # through in the gaps — non-destructive, same z-order athena's own
    # has_live_cell skip-list achieves a different way.
    if spot is not None and price_vals and smin <= spot <= smax:
        live_row = to_row(spot, smin, smax, main_top, main_bot)
        safe_add(stdscr, live_row, LEFT_W, "─" * plot_w, cp(P_YELLOW))
        badge = fmt_price(spot).rjust(max(0, RIGHT_W - 1))
        safe_add(stdscr, live_row, LEFT_W + plot_w + 1, badge, cp(P_YELLOW, bold=True) | curses.A_REVERSE)

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

    # Bottom panel — [N] toggles between two views of the same underlying
    # data, both always computed by build_columns:
    #   Net Volume (default) — cumulative signed CONTRACT count (buy -
    #     sell, calls+puts combined), filled from zero to each column's
    #     value and colored by that column's own sign, QuantData-style.
    #   Volume (the original panel) — unsigned per-bucket $ premium
    #     activity, plain green bars from the bottom up, no sign/zero-line.
    vol_bot = vol_top + vol_h - 1
    if net_volume_mode:
        netvol_vals = [v for v in cols_netvol if v is not None] + [0.0]
        nvmin, nvmax = min(netvol_vals), max(netvol_vals)
        if nvmin == nvmax:
            nvmin, nvmax = nvmin - 1, nvmax + 1
        nvpad = (nvmax - nvmin) * 0.08
        nvmin, nvmax = nvmin - nvpad, nvmax + nvpad

        N_VOL_TICKS = max(2, min(4, vol_h // 2))
        for k in range(N_VOL_TICKS):
            row = vol_top + int(k * (vol_h - 1) / max(1, N_VOL_TICKS - 1))
            frac = (row - vol_top) / max(1, vol_h - 1)
            nval = nvmax - frac * (nvmax - nvmin)
            lbl = fmt_axis_count(nval)
            safe_add(stdscr, row, max(0, LEFT_W - 1 - len(lbl)), lbl, cp(P_DIM))

        if nvmin <= 0.0 <= nvmax:
            zero_row_v = to_row(0.0, nvmin, nvmax, vol_top, vol_bot)
            safe_add(stdscr, zero_row_v, LEFT_W, "·" * plot_w, cp(P_DIM))

        plot_area(stdscr, cols_netvol, nvmin, nvmax, vol_top, vol_bot, LEFT_W)
    else:
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
        ts = x_end - (plot_w - 1 - i) * bar_interval
        lbl = fmt_time(ts, is_crypto)
        cx = LEFT_W + max(0, i - len(lbl) // 2)
        safe_add(stdscr, time_row, cx, lbl, cp(P_DIM))

    last_str = datetime.fromtimestamp(last_poll).strftime("%H:%M:%S") if last_poll else "—"
    if view_offset > 0:
        countdown = f"scrolled -{fmt_duration(view_offset * bar_interval)} — [→]/[Esc] to live"
    elif live:
        countdown = f"next refresh in {max(0, int(round(next_poll_ts - time.time())))}s"
    else:
        countdown = "live tracking continues in background"
    v_hint = "   [V] venues" if is_crypto else "   [C] deadzone"
    min_str = f"min ${min_trade_usd:g}"
    dz_str = f"   dz {confidence_deadzone * 100:g}%" if not is_crypto else ""
    filt_str = "   FILTERED (OTM)" if filtered_mode else ""
    vol_str = "   bottom: Net Vol" if net_volume_mode else "   bottom: Volume"
    footer = (f" [←/→] pan   [S] switch   [H] history   [I] refresh   [B] bars   [M] min$   [F] filtered   [N] vol mode{v_hint}   [R] refresh now   [Q] quit    "
              f"poll {interval}s   {fmt_duration(bar_interval)} bars   {min_str}{dz_str}{filt_str}{vol_str}    {countdown}    last update {last_str} ")
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
    show_venues = False
    while True:
        try:
            draw(stdscr, ctrl, show_venues)
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
                cur_interval = _focused_tuning(ctrl, ctrl.symbol).interval
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
                        tuning = _focused_tuning(ctrl, ctrl.symbol)
                        tuning.interval = new_interval
                    tuning.refresh_event.set()   # apply it immediately instead of finishing the old wait —
                    # ETH/QQQ's OWN dedicated loop if focused there, ctrl's ad-hoc loop otherwise
        elif ch in (ord('b'), ord('B')):
            with ctrl.lock:
                cur_bar = ctrl.bar_interval
            raw = prompt_text(
                stdscr,
                f" New chart bar size in seconds, min {MIN_BAR_INTERVAL} (currently {cur_bar}, "
                f"e.g. 60=1m, 300=5m, 900=15m) — Enter=confirm, Esc=cancel: ")
            if raw:
                try:
                    new_bar = max(MIN_BAR_INTERVAL, int(raw))
                except ValueError:
                    new_bar = None
                if new_bar is not None:
                    with ctrl.lock:
                        ctrl.bar_interval = new_bar
                        ctrl.view_offset = 0   # same view_offset now spans a
                    # different amount of real time — reset to live rather
                    # than silently jump to a confusing spot. Purely a
                    # display bucket size otherwise — no refresh_event wake
                    # needed, it doesn't touch polling cadence at all
        elif ch in (ord('m'), ord('M')):
            with ctrl.lock:
                cur_min = _focused_tuning(ctrl, ctrl.symbol).min_trade_usd
            raw = prompt_text(
                stdscr,
                f" Minimum trade premium in $ to count at all, 0=off (currently {cur_min:g}) — "
                f"Enter=confirm, Esc=cancel: ")
            if raw:
                try:
                    new_min = max(0.0, float(raw))
                except ValueError:
                    new_min = None
                if new_min is not None:
                    with ctrl.lock:
                        _focused_tuning(ctrl, ctrl.symbol).min_trade_usd = new_min
                    # applies to the NEXT poll onward only — already-logged
                    # history keeps whatever was computed under the old
                    # threshold, same convention as [I]/[B]
        elif ch in (ord('c'), ord('C')):
            with ctrl.lock:
                cur_is_crypto = ctrl.is_crypto
                cur_dz = _focused_tuning(ctrl, ctrl.symbol).confidence_deadzone
            if not cur_is_crypto:
                # crypto has real trade-side data — no aggressor guessing
                # happens there, so there's nothing for this to tune
                raw = prompt_text(
                    stdscr,
                    f" Equities aggressor deadzone, % of bid-ask spread around mid to leave "
                    f"unclassified, 0-100 (currently {cur_dz * 100:g}) — Enter=confirm, Esc=cancel: ")
                if raw:
                    try:
                        new_dz = max(0.0, min(100.0, float(raw))) / 100.0
                    except ValueError:
                        new_dz = None
                    if new_dz is not None:
                        with ctrl.lock:
                            _focused_tuning(ctrl, ctrl.symbol).confidence_deadzone = new_dz
        elif ch in (ord('r'), ord('R')):
            with ctrl.lock:
                tuning = _focused_tuning(ctrl, ctrl.symbol)
            tuning.refresh_event.set()
        elif ch in (ord('v'), ord('V')):
            show_venues = not show_venues
        elif ch in (ord('f'), ord('F')):
            with ctrl.lock:
                ctrl.filtered_mode = not ctrl.filtered_mode
            # pure display toggle — both ledgers are always tracked every
            # poll regardless of which one is currently shown, so this
            # takes effect immediately with no re-poll needed
        elif ch in (ord('n'), ord('N')):
            with ctrl.lock:
                ctrl.net_volume_mode = not ctrl.net_volume_mode
            # same as [F]/[V] — both cols_netvol and cols_vol are always
            # computed by build_columns, this just picks which is drawn
        elif ch in (curses.KEY_LEFT, curses.KEY_SLEFT, 541, 545, ord('['), ord('{')):
            # pan back in time — ported from charthacker.py's view_offset
            # scroll scheme: plain arrow = 1 bar, Shift+Left or '[' = 10
            # bars, Ctrl+Left (raw codes 541/545, terminal-dependent) or
            # '{' = 50 bars. Unclamped here on purpose — draw()/chart_x_end
            # clamps against the actual available history every frame,
            # same deferred-clamp pattern charthacker.py uses.
            if ch in (curses.KEY_SLEFT, ord('[')):
                step = 10
            elif ch in (541, 545, ord('{')):
                step = 50
            else:
                step = 1
            with ctrl.lock:
                ctrl.view_offset += step
        elif ch in (curses.KEY_RIGHT, curses.KEY_SRIGHT, 560, 564, ord(']'), ord('}')):
            # pan toward the live edge — same step sizes as KEY_LEFT.
            # Reaching 0 here (or Esc, below) IS "snapped back to live":
            # chart_x_end's anchor becomes "now" again with no offset.
            if ch in (curses.KEY_SRIGHT, ord(']')):
                step = 10
            elif ch in (560, 564, ord('}')):
                step = 50
            else:
                step = 1
            with ctrl.lock:
                ctrl.view_offset = max(0, ctrl.view_offset - step)
        elif ch == 27:   # Esc — snap straight back to the live edge
            with ctrl.lock:
                ctrl.view_offset = 0


USAGE = """Usage: python drift.py SYMBOL [--interval SEC] [--bar-interval SEC] [--date MM_DD_YYYY]

  SYMBOL               ETH or BTC (aggregated across Deribit+OKX+Bybit), or
                        any other ticker tried as a CBOE-listed equity/ETF
                        (QQQ, SPY, ...)
  --interval SEC        how often to POLL for new data, in seconds
                         (default 15, floor 5)
  --bar-interval SEC     chart bucket size — how much wall-clock time each
                         plotted point/bar represents, e.g. 60=1m, 300=5m,
                         900=15m (default 60, floor 5). Independent of
                         --interval: this only changes how already-polled
                         history is DISPLAYED, not how often new data comes
                         in. A longer bar interval shows more history at
                         once (plot width * bar interval); use [<-/->] to
                         pan back through more than that at a given size.
  --min-trade-usd USD     drop trades (crypto) / a poll's contract volume
                         delta (equities) below this $ premium entirely —
                         cuts small/dust noise (default 100, 0=off).
                         Changeable live with [M]; applies going forward
                         only, doesn't retroactively touch logged history.
  --confidence-deadzone PCT  equities only (0-100, default 20) — % of the
                         bid-ask spread around the midpoint left
                         unclassified instead of forced into a call/put
                         guess (see classify_sign). Changeable live with
                         [C]; crypto has real trade-side data and ignores
                         this entirely.
  --filtered              start in [F] filtered mode (0DTE + OTM only —
                         see the module docstring for exactly what that
                         means and, just as importantly, what it doesn't:
                         only 2 of the 6 filters this was originally asked
                         for are actually possible with free data).
  --volume                start with the bottom panel showing the old-style
                         Volume (unsigned per-bucket $ premium bars)
                         instead of the default Net Volume (cumulative
                         signed contract-count area). Changeable live with
                         [N]; both are always computed, so toggling is
                         instant either direction.
  --date MM_DD_YYYY      open straight into a previously logged day for
                         SYMBOL (read-only) — live tracking still starts in
                         the background; press [H] then 'live' to jump to it

Keys while running: [←/→] pan through history (1 bar; Shift+arrow or [/]=10;
                    Ctrl+arrow or {/}=50 bars), [Esc] snap back to live
                    [S] switch symbol   [H] browse a past day   [I] refresh interval
                    [B] chart bar size   [M] min trade $   [C] equities deadzone
                    [F] filtered mode (0DTE+OTM)   [N] bottom panel: Net Volume/Volume
                    [V] per-venue breakdown (crypto only)   [R] refresh now   [Q] quit
"""


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0 if args else 1)

    symbol = args[0].upper()
    rest = args[1:]
    interval = DEFAULT_INTERVAL
    bar_interval = DEFAULT_BAR_INTERVAL
    min_trade_usd = DEFAULT_MIN_TRADE_USD
    confidence_deadzone = DEFAULT_CONFIDENCE_DEADZONE
    filtered_mode = False
    net_volume_mode = True
    date_str = None
    i = 0
    while i < len(rest):
        if rest[i] == "--interval" and i + 1 < len(rest):
            try:
                interval = max(MIN_INTERVAL, int(rest[i + 1]))
            except ValueError:
                pass
            i += 2
        elif rest[i] == "--bar-interval" and i + 1 < len(rest):
            try:
                bar_interval = max(MIN_BAR_INTERVAL, int(rest[i + 1]))
            except ValueError:
                pass
            i += 2
        elif rest[i] == "--min-trade-usd" and i + 1 < len(rest):
            try:
                min_trade_usd = max(0.0, float(rest[i + 1]))
            except ValueError:
                pass
            i += 2
        elif rest[i] == "--confidence-deadzone" and i + 1 < len(rest):
            try:
                confidence_deadzone = max(0.0, min(100.0, float(rest[i + 1]))) / 100.0
            except ValueError:
                pass
            i += 2
        elif rest[i] == "--filtered":
            filtered_mode = True
            i += 1
        elif rest[i] == "--volume":
            net_volume_mode = False
            i += 1
        elif rest[i] == "--date" and i + 1 < len(rest):
            date_str = rest[i + 1]
            i += 2
        else:
            i += 1

    is_crypto = symbol in ("ETH", "BTC")
    ctrl = Controller(symbol, is_crypto, interval, bar_interval, min_trade_usd, confidence_deadzone)
    ctrl.filtered_mode = filtered_mode
    ctrl.net_volume_mode = net_volume_mode

    # ETH + QQQ resume their own logs regardless of the launch symbol —
    # see BG_SYMBOLS. If the launch symbol IS one of them, ctrl.live_state
    # already points at that same tracker's State (set in
    # Controller.__init__), so seeding the tracker here seeds the display
    # too; only seed separately when launching on something else.
    for bg_sym, tracker in ctrl.background.items():
        seed_today(tracker.state, bg_sym, tracker.is_crypto)
    if symbol not in ctrl.background:
        seed_today(ctrl.live_state, symbol, is_crypto)
    if date_str:
        view_historical(ctrl, date_str)   # --date just changes what's DISPLAYED at startup

    threads = [threading.Thread(target=poll_loop, args=(ctrl,), daemon=True)]
    for tracker in ctrl.background.values():
        threads.append(threading.Thread(target=background_poll_loop, args=(ctrl, tracker), daemon=True))
    for t in threads:
        t.start()

    try:
        curses.wrapper(curses_main, ctrl)
    finally:
        ctrl.stop = True
        ctrl.refresh_event.set()   # wake poll_loop out of its wait so it can see ctrl.stop and exit promptly
        for tracker in ctrl.background.values():
            tracker.refresh_event.set()   # same, for each background loop
        for t in threads:
            t.join(timeout=2)


if __name__ == "__main__":
    main()
