#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# deribit_dashboard.py — Real-time Deribit + CBOE Put/Call Volume Monitor
#
# Usage:
#   python deribit_dashboard.py
#   python deribit_dashboard.py --interval 60
#
# Equities (TLT/GLD/QQQ) put/call volume comes from CBOE's free delayed-quote
# feed (exchange-sourced, ~15 min delay). Needs only httpx — no extra package.
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import csv
import json
import os
import sys
import time
import argparse
import threading
from datetime import datetime, timezone, timedelta

import subprocess

try:
    import websockets
except ImportError:
    print("websockets not installed — run: pip install websockets")
    sys.exit(1)

try:
    import httpx
except ImportError:
    print("httpx not installed — run: pip install httpx")
    sys.exit(1)

try:
    from PIL import ImageGrab            # optional — only needed for [S]creenshot
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────
WS_URL         = 'wss://www.deribit.com/ws/api/v2'
CURRENCIES     = ['ETH', 'BTC']
PHEMEX_TICKER  = 'https://api.phemex.com/md/v3/ticker/24hr?symbol=ETHUSDT'
CT_OFFSET      = timedelta(hours=-5)   # CT = UTC-5 (CDT); use -6 for CST

# ── Equities options config (CBOE delayed quotes — exchange source, ~15m delay) ─
# Webull's free API was inconsistent and its volumes didn't match IBKR. CBOE's
# delayed-quote feed returns the full consolidated chain and tracks IBKR's
# put/call direction (e.g. GLD/TLT correctly read < 1.00). Needs only httpx.
EQUITY_SYMBOLS = ['TLT', 'GLD', 'QQQ']
CBOE_URL       = 'https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json'
# Exp Move = ATM straddle of the 0DTE (or nearest) expiry × this factor.
# 1.0 = raw straddle (≈ market-implied expected absolute move to expiry);
# set 0.85 for the common 1-SD "expected move" convention.
EXP_MOVE_FACTOR = 1.0

# Auto-refresh once per weekday when the clock reaches this CT time (market open
# data settles). Mirrors a manual restart so the new session's PCVR/IV/DVOL show
# up without intervention.
MARKET_OPEN_REFRESH = (8, 45)   # (hour, minute) in CT

# ── CSV logging — one wide row appended after every refresh ────────────────────
# A separate file per CT day, named opt_data_MM_DD_YYYY.csv. On startup the
# current day's file is resumed if it exists (else created); when the CT clock
# crosses 00:00 the next refresh rolls over to a fresh file automatically.
_csv_dir      = None          # log directory; None disables logging (set in main)
_csv_cur_path = None          # path of the file currently being written
_csv_rows     = 0             # rows written this session to the current file
_csv_err      = None          # last write error (shown on the status line)

# ── Shared equities state (prefetched in background, promoted at cycle boundary) ─
eq_results = {}            # symbol -> (put_vol, call_vol, ratio, iv30, exp_move, em_is_0dte)
eq_errors  = {}            # symbol -> short error string
eq_status  = 'starting…'

# ── Interactive controls (set by the keyboard thread, polled by the loop) ──────
_quit_evt       = threading.Event()
_refresh_evt    = threading.Event()
_screenshot_evt = threading.Event()
_last_open_refresh = None   # date of the most recent market-open auto-refresh

# ── Screenshot state ([S] key saves a PNG of the console window) ───────────────
_shot_dir    = None         # resolved in main() -> <script dir>/screenshots
_shot_status = None         # last screenshot result (shown on the Shot line)

# ── Kill zones (CT, minutes since midnight) ───────────────────────────────────
KILL_ZONES = [
    ('NDO',         0,    210,  'CYN'),   # 12:00am – 3:30am
    ('Morning',     510,  630,  'YLW'),   # 8:30am  – 10:30am
    ('Lunchtime',   690,  810,  'YLW'),   # 11:30am – 1:30pm
    ('Power Hour',  840,  900,  'YLW'),   # 2:00pm  – 3:00pm
    ('EOD',         960,  1080, 'YLW'),   # 4:00pm  – 6:00pm
    ('EEOD',        1110, 1440, 'YLW'),   # 6:30pm  – 12:00am
]
EXCL_DAYS_09 = {2, 3}      # Wed=2, Thu=3 — 09:00-10:00 exclusion
EXCL_START   = 540          # 09:00
EXCL_END     = 600          # 10:00
EXCL_SUN     = 6            # Sunday — no trading
EXCL_EEOD_START = 1110      # 18:30 CT — EEOD begins, no trading

# ── Terminal colours ──────────────────────────────────────────────────────────
if sys.platform == 'win32':
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    # DPI-aware so GetWindowRect returns physical pixels that line up with the
    # screenshot grab (otherwise the [S] capture is cropped wrong on scaled displays).
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Force UTF-8 so the box-drawing/± glyphs always encode — otherwise a non-UTF-8
# stdout (e.g. piped to a file under cp1252) raises UnicodeEncodeError on every
# draw and the dashboard silently loops on the reconnect handler.
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

GRN = '\033[92m'
RED = '\033[91m'
YLW = '\033[93m'
CYN = '\033[96m'
MAG = '\033[95m'
BLD = '\033[1m'
DIM = '\033[2m'
RST = '\033[0m'
COLS = {'GRN': GRN, 'RED': RED, 'YLW': YLW, 'CYN': CYN, 'MAG': MAG}

# ── Cursor helpers ────────────────────────────────────────────────────────────
def move(row, col=1):
    sys.stdout.write(f'\033[{row};{col}H')

def erase_line():
    sys.stdout.write('\033[K')

def hide_cursor():
    sys.stdout.write('\033[?25l')

def show_cursor():
    sys.stdout.write('\033[?25h')

def clr():
    os.system('cls' if sys.platform == 'win32' else 'clear')

def ratio_colour(ratio):
    if ratio >= 1.02:    return RED
    elif ratio <= 0.98:  return GRN
    else:                return YLW

# ── Kill zone logic ───────────────────────────────────────────────────────────
def get_session_status():
    now_ct  = datetime.now(timezone.utc) + CT_OFFSET
    t_mins  = now_ct.hour * 60 + now_ct.minute
    dow     = now_ct.weekday()  # Mon=0 ... Sun=6

    # Determine exclusion reason (priority order)
    excl_reason = None
    if dow == EXCL_SUN:
        excl_reason = 'Sunday — no trading'
    elif dow in EXCL_DAYS_09 and EXCL_START <= t_mins < EXCL_END:
        excl_reason = 'Excluded (09:00–10:00)'
    elif t_mins >= EXCL_EEOD_START:
        excl_reason = 'EEOD — no trading'

    for name, start, end, col in KILL_ZONES:
        if start <= t_mins < end:
            return name, COLS[col], excl_reason
    return None, None, excl_reason

# ── Phemex price fetch ────────────────────────────────────────────────────────
async def fetch_eth_price():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(PHEMEX_TICKER)
            data = r.json()
            result = data.get('result', {})
            price = result.get('lastRp') or result.get('lastPrice') or result.get('last')
            if price:
                return float(price)
    except Exception:
        pass
    return None

# ── Sentiment alert ───────────────────────────────────────────────────────────
_prev_sentiment = {}  # {'ETH': 'NEUTRAL', 'BTC': 'BULLISH', ...}

def _get_sentiment(ratio):
    if ratio >= 1.02:    return 'BEARISH'
    elif ratio <= 0.98:  return 'BULLISH'
    else:                return 'NEUTRAL'

def _play_alert():
    """Play sentiment.wav non-blocking from the same folder as this script."""
    wav = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sentiment.wav')
    if not os.path.exists(wav):
        return
    try:
        if sys.platform == 'win32':
            import winsound
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            subprocess.Popen(['aplay', wav],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

# ── Equities options fetch (CBOE delayed quotes) ──────────────────────────────
def _opt_mid(o):
    """Mid price of a contract; falls back to last trade if no two-sided quote."""
    try:
        bid = float(o.get('bid') or 0)
        ask = float(o.get('ask') or 0)
    except (TypeError, ValueError):
        bid = ask = 0.0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    try:
        return float(o.get('last_trade_price') or 0)
    except (TypeError, ValueError):
        return 0.0


async def _cboe_symbol_volume(client, symbol):
    """
    Return (put_vol, call_vol, iv30, exp_move, em_is_0dte) for one symbol — one GET.

    put_vol/call_vol are summed across the entire CBOE chain (all strikes/expiries).
    iv30 is CBOE's 30-day constant-maturity implied vol (VIX-style, %) — stable and
    IBKR-matching, the right input for vol-stop levels.
    exp_move is the ATM straddle (call mid + put mid at the strike nearest spot) of
    the 0DTE expiry × EXP_MOVE_FACTOR — the market-implied expected move to expiry.
    If the symbol has no 0DTE today (e.g. GLD/TLT), it uses the nearest expiry and
    em_is_0dte is False.

    OCC symbol ends with <YYMMDD><type><8-digit-strike>, e.g. 'GLD260618P00388000':
    type = char[-9], expiry = chars[-15:-9], strike = int(chars[-8:]) / 1000.
    """
    r = await client.get(CBOE_URL.format(symbol))
    r.raise_for_status()
    data    = r.json().get('data') or {}
    options = data.get('options') or []
    if not options:
        raise RuntimeError('empty chain')

    try:
        iv30 = float(data.get('iv30'))
    except (TypeError, ValueError):
        iv30 = 0.0
    try:
        price = float(data.get('current_price'))
    except (TypeError, ValueError):
        price = 0.0

    today   = datetime.now().strftime('%y%m%d')
    put_vol = call_vol = 0.0
    legs    = {}   # expiry -> {strike: {'C': mid, 'P': mid}}
    for o in options:
        name = o.get('option') or ''
        if len(name) < 15:
            continue
        cp  = name[-9]                       # 'C' or 'P'
        exp = name[-15:-9]                   # YYMMDD
        try:
            strike = int(name[-8:]) / 1000.0
        except ValueError:
            continue
        try:
            vol = float(o.get('volume') or 0)
        except (TypeError, ValueError):
            vol = 0.0
        if cp == 'P':
            put_vol  += vol
        elif cp == 'C':
            call_vol += vol
        else:
            continue
        legs.setdefault(exp, {}).setdefault(strike, {})[cp] = _opt_mid(o)

    # Expected move: ATM straddle of the 0DTE expiry, else nearest expiry
    em_is_0dte = today in legs
    target_exp = today if em_is_0dte else min((e for e in legs if e >= today), default=None)
    exp_move = 0.0
    if target_exp and price > 0:
        strikes = legs[target_exp]
        atm = [s for s, lg in strikes.items() if lg.get('C', 0) > 0 and lg.get('P', 0) > 0]
        if atm:
            k = min(atm, key=lambda s: abs(s - price))
            exp_move = (strikes[k]['C'] + strikes[k]['P']) * EXP_MOVE_FACTOR

    return put_vol, call_vol, iv30, exp_move, em_is_0dte


async def refresh_equities():
    """
    Fetch all equity symbols concurrently from CBOE and return a completed
    snapshot: (results, errors, status). Does NOT touch the displayed globals —
    the caller promotes the snapshot at the crypto cycle boundary so equities
    update in lockstep with the counter. ~9s total for all three.
    """
    results, errors = {}, {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            async def _one(symbol):
                try:
                    put_vol, call_vol, iv30, exp_move, em_is_0dte = await _cboe_symbol_volume(client, symbol)
                    ratio = put_vol / call_vol if call_vol > 0 else 0.0
                    results[symbol] = (put_vol, call_vol, ratio, iv30, exp_move, em_is_0dte)
                except Exception as e:
                    errors[symbol] = str(e).replace('\n', ' ')[:36]

            await asyncio.gather(*(_one(s) for s in EQUITY_SYMBOLS))
        status = f'OK ({len(results)}/{len(EQUITY_SYMBOLS)})'

    except asyncio.CancelledError:
        raise
    except Exception as e:
        status = ('err: ' + str(e).replace('\n', ' '))[:40]

    return results, errors, status


# ── CSV logging ───────────────────────────────────────────────────────────────
def _csv_columns():
    """Ordered column names — one group per crypto currency and equity symbol."""
    cols = ['timestamp_utc', 'timestamp_ct', 'session', 'eth_price']
    for ccy in CURRENCIES:
        cols += [f'{ccy}_put_vol', f'{ccy}_call_vol', f'{ccy}_total_vol',
                 f'{ccy}_pc_ratio', f'{ccy}_dvol', f'{ccy}_sentiment']
    for sym in EQUITY_SYMBOLS:
        cols += [f'{sym}_put_vol', f'{sym}_call_vol', f'{sym}_total_vol',
                 f'{sym}_pc_ratio', f'{sym}_iv30', f'{sym}_exp_move',
                 f'{sym}_exp_move_0dte', f'{sym}_sentiment']
    return cols


def _daily_csv_path(ct_dt):
    """Path of the CSV for a given CT datetime: opt_data_MM_DD_YYYY.csv."""
    return os.path.join(_csv_dir, f"opt_data_{ct_dt.strftime('%m_%d_%Y')}.csv")


def append_csv(fetch_time, results, dvol, eth_price):
    """Append one wide row capturing every displayed field for this refresh.
    Writes to the current CT day's file, rolling over to a new file at 00:00 CT.
    Reads the equities from the shared eq_results snapshot (already promoted)."""
    global _csv_rows, _csv_err, _csv_cur_path

    ct = fetch_time + CT_OFFSET

    # Resume today's file or roll over to a new day's file (also covers startup)
    path = _daily_csv_path(ct)
    if path != _csv_cur_path:
        _csv_cur_path = path
        _csv_rows = 0

    session_name, _, excl_reason = get_session_status()
    row = {c: '' for c in _csv_columns()}
    row['timestamp_utc'] = fetch_time.strftime('%Y-%m-%d %H:%M:%S')
    row['timestamp_ct']  = ct.strftime('%Y-%m-%d %H:%M:%S')
    row['session']       = excl_reason or session_name or 'No active session'
    row['eth_price']     = f'{eth_price:.2f}' if eth_price else ''

    for ccy in CURRENCIES:
        if ccy in results:
            pv, cv, ratio = results[ccy]
            row[f'{ccy}_put_vol']   = f'{pv:.2f}'
            row[f'{ccy}_call_vol']  = f'{cv:.2f}'
            row[f'{ccy}_total_vol'] = f'{pv + cv:.2f}'
            row[f'{ccy}_pc_ratio']  = f'{ratio:.4f}'
            row[f'{ccy}_sentiment'] = _get_sentiment(ratio)
        if ccy in dvol:
            row[f'{ccy}_dvol'] = f'{dvol[ccy]:.2f}'

    for sym in EQUITY_SYMBOLS:
        if sym in eq_results:
            pv, cv, ratio, iv30, em, z = eq_results[sym]
            row[f'{sym}_put_vol']      = f'{pv:.0f}'
            row[f'{sym}_call_vol']     = f'{cv:.0f}'
            row[f'{sym}_total_vol']    = f'{pv + cv:.0f}'
            row[f'{sym}_pc_ratio']     = f'{ratio:.4f}'
            row[f'{sym}_iv30']         = f'{iv30:.2f}'
            row[f'{sym}_exp_move']     = f'{em:.2f}'
            row[f'{sym}_exp_move_0dte'] = 'True' if z else 'False'
            row[f'{sym}_sentiment']    = _get_sentiment(ratio)

    try:
        cols     = _csv_columns()
        new_file = (not os.path.exists(path)) or os.path.getsize(path) == 0
        with open(path, 'a', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=cols)
            if new_file:
                w.writeheader()
            w.writerow(row)
        _csv_rows += 1
        _csv_err = None
    except Exception as e:
        _csv_err = str(e).replace('\n', ' ')[:40]


def _resume_csv_row_count():
    """On startup, seed the row counter from today's existing file (if any) so
    the status line reflects rows already logged rather than just this session."""
    global _csv_cur_path, _csv_rows
    if _csv_dir is None:
        return
    ct = datetime.now(timezone.utc) + CT_OFFSET
    path = _daily_csv_path(ct)
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                n = sum(1 for _ in f)
            _csv_cur_path = path
            _csv_rows = max(0, n - 1)   # minus the header line
        except Exception:
            pass


# ── Layout ────────────────────────────────────────────────────────────────────
WIDTH = 70
ROWS  = {}

def draw_static():
    ROWS.clear()
    clr()
    # After cls the cursor position is unreliable — force it to row 1
    sys.stdout.write('\033[H')
    sys.stdout.flush()
    hide_cursor()
    row = [1]

    def p(text=''):
        print(text)
        row[0] += 1

    def mark(key):
        ROWS[key] = row[0]

    p(f"{BLD}{CYN}{'─'*WIDTH}{RST}")
    p(f"{BLD}{CYN}  Options Flow Dashboard - Crypto & Equities{RST}")
    p(f"{BLD}{CYN}{'─'*WIDTH}{RST}")
    mark('ts');        p()
    p()

    p(f"  {BLD}{'─'*4} MARKET {'─'*(WIDTH-10)}{RST}")
    mark('eth_price'); p(f"    {'ETH Perp (Phemex)':<24}")
    mark('session');   p(f"    {'Session':<24}")
    p()

    for ccy in CURRENCIES:
        ccy_col = MAG if ccy == 'BTC' else CYN
        p(f"  {BLD}{ccy_col}{'─'*4} {ccy} {'─'*(WIDTH-7-len(ccy))}{RST}")
        mark(f'{ccy}_put');       p(f"    {'24h Put Volume':<20}")
        mark(f'{ccy}_call');      p(f"    {'24h Call Volume':<20}")
        mark(f'{ccy}_total');     p(f"    {'24h Total Volume':<20}")
        mark(f'{ccy}_dvol');      p(f"    {'DVOL (30d IV)':<20}")
        p()
        mark(f'{ccy}_ratio');     p(f"    {'Put/Call Ratio':<20}")
        p()
        mark(f'{ccy}_sentiment'); p(f"    {'Sentiment':<20}")
        p()
        mark(f'{ccy}_bar');       p(f"    {DIM}Put  {RST}{'':30}{DIM}  Call{RST}")
        mark(f'{ccy}_pct');       p(f"    {DIM}     {'':35}{RST}")
        p()

    # ── Equities (CBOE) — compact table ──────────────────────────────────────
    p(f"  {BLD}{YLW}{'─'*4} EQUITIES (CBOE) {'─'*(WIDTH-18)}{RST}")
    mark('eq_status'); p(f"    {'Status':<8}")
    p(f"    {BLD}{'Symbol':<7}{'Put Vol':>11}{'Call Vol':>11}{'P/C':>7}{'IV':>8}{'Exp Move':>10}  Sentiment{RST}")
    for sym in EQUITY_SYMBOLS:
        mark(f'eq_{sym}'); p(f"    {sym:<7}")
    p()

    p(f"{BLD}{CYN}{'─'*WIDTH}{RST}")
    p(f"  {DIM}Equities P/C = total volume  |  IV = 30-day (matches IBKR){RST}")
    p(f"  {DIM}Exp Move = ±ATM straddle to expiry (0DTE, *=nearest)  |  ~15m delay{RST}")
    p(f"  {DIM}P/C >= 1.02 = BEARISH  |  P/C <= 0.98 = BULLISH{RST}")
    p(f"{BLD}{CYN}{'─'*WIDTH}{RST}")
    mark('csv');  p(f"    {'CSV':<8}")
    mark('shot'); p(f"    {'Shot':<8}")
    mark('exit'); p(f"  {BLD}[Q]{RST}{DIM}uit   {RST}{BLD}[R]{RST}{DIM}efresh   {RST}{BLD}[S]{RST}{DIM}creenshot{RST}")

    sys.stdout.flush()


def update_values(results, errors, dvol, fetch_time, remaining, eth_price):
    bar_width = 30
    ct = fetch_time + CT_OFFSET
    ts = f"{ct.strftime('%A %Y-%m-%d  %H:%M:%S')} CT  /  {fetch_time.strftime('%H:%M:%S')} UTC"

    move(ROWS['ts'])
    erase_line()
    sys.stdout.write(f"  {DIM}{ts}   refresh in {remaining}s{RST}")

    move(ROWS['eth_price'])
    erase_line()
    if eth_price:
        sys.stdout.write(f"    {'ETH Perp (Phemex)':<24} {BLD}${eth_price:,.2f}{RST}")
    else:
        sys.stdout.write(f"    {'ETH Perp (Phemex)':<24} {DIM}unavailable{RST}")

    session_name, session_col, excl_reason = get_session_status()
    move(ROWS['session'])
    erase_line()
    if excl_reason:
        sys.stdout.write(f"    {'Session':<24} {RED}{BLD}{excl_reason}{RST}")
    elif session_name:
        sys.stdout.write(f"    {'Session':<24} {GRN}{BLD}{session_name}{RST}")
    else:
        sys.stdout.write(f"    {'Session':<24} {RED}{BLD}No active session{RST}")

    for ccy in CURRENCIES:
        if ccy in errors:
            move(ROWS[f'{ccy}_put'])
            erase_line()
            sys.stdout.write(f"    {RED}✗ Error: {errors[ccy]}{RST}")
            for key in ('call', 'total', 'dvol', 'ratio', 'sentiment', 'bar', 'pct'):
                move(ROWS[f'{ccy}_{key}'])
                erase_line()
            continue

        put_vol, call_vol, ratio = results[ccy]
        total    = put_vol + call_vol
        rc       = ratio_colour(ratio)
        put_pct  = put_vol  / total if total > 0 else 0
        call_pct = call_vol / total if total > 0 else 0
        put_bars  = int(round(put_pct  * bar_width))
        call_bars = int(round(call_pct * bar_width))

        move(ROWS[f'{ccy}_put'])
        erase_line()
        sys.stdout.write(f"    {'24h Put Volume':<20} {RED}{put_vol:>12,.2f}{RST}  {ccy}")

        move(ROWS[f'{ccy}_call'])
        erase_line()
        sys.stdout.write(f"    {'24h Call Volume':<20} {GRN}{call_vol:>12,.2f}{RST}  {ccy}")

        move(ROWS[f'{ccy}_total'])
        erase_line()
        sys.stdout.write(f"    {'24h Total Volume':<20} {DIM}{total:>12,.2f}{RST}  {ccy}")

        move(ROWS[f'{ccy}_dvol'])
        erase_line()
        dv = dvol.get(ccy)
        if dv is not None:
            sys.stdout.write(f"    {'DVOL (30d IV)':<20} {CYN}{BLD}{dv:>12.2f}{RST}  %")
        else:
            sys.stdout.write(f"    {'DVOL (30d IV)':<20} {DIM}{'unavailable':>12}{RST}")

        move(ROWS[f'{ccy}_ratio'])
        erase_line()
        sys.stdout.write(f"    {'Put/Call Ratio':<20} {rc}{BLD}{ratio:>12.2f}{RST}")

        if ratio >= 1.02:
            sentiment = f"{RED}{BLD}BEARISH{RST}"
        elif ratio <= 0.98:
            sentiment = f"{GRN}{BLD}BULLISH{RST}"
        else:
            sentiment = f"{YLW}{BLD}NEUTRAL{RST}"
        move(ROWS[f'{ccy}_sentiment'])
        erase_line()
        sys.stdout.write(f"    {'Sentiment':<20} {sentiment}")

        # Check for sentiment change and play alert
        label = _get_sentiment(ratio)
        if _prev_sentiment.get(ccy) is not None and _prev_sentiment[ccy] != label:
            _play_alert()
        _prev_sentiment[ccy] = label

        put_bar  = f"{RED}{'█' * put_bars}{RST}"
        call_bar = f"{GRN}{'█' * call_bars}{RST}"
        move(ROWS[f'{ccy}_bar'])
        erase_line()
        sys.stdout.write(f"    {DIM}Put  {RST}{put_bar}{call_bar}{DIM}  Call{RST}")

        move(ROWS[f'{ccy}_pct'])
        erase_line()
        sys.stdout.write(f"    {DIM}     {put_pct*100:>5.1f}%{' '*(bar_width-1)}{call_pct*100:>5.1f}%{RST}")

    # ── Equities (CBOE) ───────────────────────────────────────────────────────
    move(ROWS['eq_status'])
    erase_line()
    if eq_status.startswith('OK'):
        st_col = GRN
    elif eq_status.startswith('err'):
        st_col = RED
    else:
        st_col = YLW
    sys.stdout.write(f"    {'Status':<8} {st_col}{eq_status}{RST}")

    for sym in EQUITY_SYMBOLS:
        move(ROWS[f'eq_{sym}'])
        erase_line()

        if sym in eq_errors:
            sys.stdout.write(f"    {sym:<8} {RED}✗ {eq_errors[sym]}{RST}")
            continue
        if sym not in eq_results:
            sys.stdout.write(f"    {sym:<8} {DIM}waiting…{RST}")
            continue

        put_vol, call_vol, ratio, iv30, exp_move, em_is_0dte = eq_results[sym]
        rc = ratio_colour(ratio)
        if ratio >= 1.02:
            eq_sent = f"{RED}{BLD}BEARISH{RST}"
        elif 0 < ratio <= 0.98:
            eq_sent = f"{GRN}{BLD}BULLISH{RST}"
        else:
            eq_sent = f"{YLW}{BLD}NEUTRAL{RST}"
        iv_str = f"{iv30:.1f}%" if iv30 else "n/a"
        # '*' marks expected move taken from the nearest expiry (no 0DTE today)
        em_str = (f"±{exp_move:.2f}" + ("" if em_is_0dte else "*")) if exp_move else "n/a"

        sys.stdout.write(
            f"    {sym:<7}{RED}{put_vol:>11,.0f}{RST}{GRN}{call_vol:>11,.0f}{RST}"
            f"{rc}{BLD}{ratio:>7.2f}{RST}{CYN}{iv_str:>8}{RST}{MAG}{em_str:>10}{RST}  {eq_sent}"
        )

        # Sentiment-change alert (shared with crypto)
        label = _get_sentiment(ratio)
        if _prev_sentiment.get(sym) is not None and _prev_sentiment[sym] != label:
            _play_alert()
        _prev_sentiment[sym] = label

    # CSV logging status
    if 'csv' in ROWS:
        move(ROWS['csv'])
        erase_line()
        if _csv_dir is None:
            sys.stdout.write(f"    {'CSV':<8} {DIM}disabled{RST}")
        elif _csv_err:
            sys.stdout.write(f"    {'CSV':<8} {RED}error: {_csv_err}{RST}")
        elif _csv_cur_path:
            sys.stdout.write(f"    {'CSV':<8} {GRN}{_csv_rows}{RST} {DIM}rows → {os.path.basename(_csv_cur_path)}{RST}")
        else:
            sys.stdout.write(f"    {'CSV':<8} {DIM}starting…{RST}")

    # Screenshot status
    if 'shot' in ROWS:
        move(ROWS['shot'])
        erase_line()
        if not PIL_AVAILABLE:
            sys.stdout.write(f"    {'Shot':<8} {DIM}[S] needs Pillow (pip install pillow){RST}")
        elif _shot_status:
            col = RED if _shot_status.startswith('error') else GRN
            sys.stdout.write(f"    {'Shot':<8} {col}{_shot_status}{RST}")
        else:
            sys.stdout.write(f"    {'Shot':<8} {DIM}press [S] to capture{RST}")

    move(ROWS['exit'])
    erase_line()
    sys.stdout.write(f"  {BLD}[Q]{RST}{DIM}uit   {RST}{BLD}[R]{RST}{DIM}efresh   {RST}{BLD}[S]{RST}{DIM}creenshot{RST}")

    sys.stdout.flush()


# ── Keyboard input thread ([Q]uit / [R]efresh / [S]creenshot) ─────────────────
def _input_thread():
    """Daemon thread: read single keypresses and signal the loop via events.
    Q quits, R forces an immediate refresh, S saves a screenshot. No Enter needed."""
    try:
        if sys.platform == 'win32':
            import msvcrt
            while not _quit_evt.is_set():
                ch = msvcrt.getwch()
                if ch in ('q', 'Q'):
                    _quit_evt.set();    break
                elif ch in ('r', 'R'):
                    _refresh_evt.set()
                elif ch in ('s', 'S'):
                    _screenshot_evt.set()
                elif ch == '\x03':                 # Ctrl+C
                    _quit_evt.set();    break
        else:
            import termios, tty, select
            fd  = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while not _quit_evt.is_set():
                    if select.select([sys.stdin], [], [], 0.2)[0]:
                        ch = sys.stdin.read(1)
                        if ch in ('q', 'Q'):
                            _quit_evt.set();    break
                        elif ch in ('r', 'R'):
                            _refresh_evt.set()
                        elif ch in ('s', 'S'):
                            _screenshot_evt.set()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass


def take_screenshot():
    """Save a PNG of the console window (fallback: full screen) to the shots dir.
    Sets _shot_status for the on-screen Shot line."""
    global _shot_status
    if not PIL_AVAILABLE:
        _shot_status = 'Pillow not installed — pip install pillow'
        return
    try:
        os.makedirs(_shot_dir, exist_ok=True)
        ct    = datetime.now(timezone.utc) + CT_OFFSET
        fname = f"opt_dashboard_{ct.strftime('%Y-%m-%d_%H-%M-%S')}.png"
        path  = os.path.join(_shot_dir, fname)

        # Try to grab just the console window; fall back to the full screen.
        bbox = None
        if sys.platform == 'win32':
            try:
                from ctypes import wintypes
                hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                if hwnd:
                    r = wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
                    if r.right > r.left and r.bottom > r.top:
                        bbox = (r.left, r.top, r.right, r.bottom)
            except Exception:
                bbox = None
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
        else:
            img = ImageGrab.grab(bbox=bbox)

        img.save(path)
        _shot_status = f'saved → {fname}'
    except Exception as e:
        _shot_status = 'error: ' + str(e).replace('\n', ' ')[:32]


def _market_open_refresh_due():
    """True once per weekday when the CT clock first reaches MARKET_OPEN_REFRESH."""
    global _last_open_refresh
    now_ct = datetime.now(timezone.utc) + CT_OFFSET
    if now_ct.weekday() >= 5:                       # Sat/Sun — market closed
        return False
    h, m = MARKET_OPEN_REFRESH
    if (now_ct.hour * 60 + now_ct.minute) >= (h * 60 + m) and _last_open_refresh != now_ct.date():
        _last_open_refresh = now_ct.date()
        return True
    return False


# ── Terminal cleanup ──────────────────────────────────────────────────────────
def cleanup_terminal():
    show_cursor()
    move(60)
    sys.stdout.write('\n')
    sys.stdout.flush()

# ── Main fetch loop ───────────────────────────────────────────────────────────
async def fetch_all(interval):
    global eq_status
    msg_id    = 0
    eth_price = None
    backoff   = 5   # seconds, doubles on each failed attempt up to 60s

    # Equities prefetch pipeline: a fetch is kicked off at the start of each
    # countdown and runs (~9s) in the background. Its completed snapshot is
    # promoted to the display at the next cycle boundary, so equities refresh in
    # lockstep with the crypto counter instead of at random times.
    eq_job   = None
    force_eq = False   # set after a manual/market-open refresh to pull fresh data

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval = 30,
                ping_timeout  = 20,
                close_timeout = 10,
            ) as ws:
                backoff = 5  # reset backoff on successful connect

                # Redraw static layout on every (re)connect so screen is clean
                draw_static()

                while True:
                    msg_id    += 1
                    fetch_time = datetime.now(timezone.utc)
                    results    = {}
                    errors     = {}

                    # ── Equities: promote the finished prefetch, then start the
                    #    next one so it's ready by the time this cycle ends.
                    #    A forced refresh discards any in-flight fetch. ──
                    if force_eq and eq_job is not None and not eq_job.done():
                        eq_job.cancel()
                        eq_job = None
                    force_eq = False
                    if eq_job is not None and eq_job.done():
                        res, errs, status = eq_job.result()
                        eq_results.clear(); eq_results.update(res)
                        eq_errors.clear();  eq_errors.update(errs)
                        eq_status = status
                        eq_job = None
                    if eq_job is None:
                        eq_status = 'fetching…'
                        eq_job = asyncio.ensure_future(refresh_equities())

                    for ccy in CURRENCIES:
                        try:
                            req = {
                                "jsonrpc": "2.0",
                                "id":      msg_id,
                                "method":  "public/get_book_summary_by_currency",
                                "params":  {"currency": ccy, "kind": "option"}
                            }
                            await ws.send(json.dumps(req))

                            while True:
                                raw  = await asyncio.wait_for(ws.recv(), timeout=25)
                                resp = json.loads(raw)
                                if resp.get('id') == msg_id:
                                    break

                            if 'error' in resp:
                                errors[ccy] = resp['error'].get('message', 'Unknown error')
                                continue

                            instruments = resp.get('result', [])
                            put_vol = call_vol = 0.0
                            for inst in instruments:
                                name   = inst.get('instrument_name', '')
                                volume = float(inst.get('volume') or 0)
                                if volume == 0:
                                    continue
                                suffix = name.split('-')[-1]
                                if suffix == 'P':
                                    put_vol  += volume
                                elif suffix == 'C':
                                    call_vol += volume

                            ratio = (put_vol / call_vol) if call_vol > 0 else 0.0
                            results[ccy] = (put_vol, call_vol, ratio)
                            msg_id += 1

                        except asyncio.TimeoutError:
                            errors[ccy] = "Timeout"
                        except Exception as e:
                            # If it's a WebSocket connection error, propagate to reconnect loop
                            if 'websockets' in type(e).__module__ or 'close frame' in str(e).lower() or 'connection' in str(e).lower():
                                raise
                            errors[ccy] = str(e)

                    # ── Deribit DVOL (30-day volatility index) per currency ──
                    dvol    = {}
                    now_ms  = int(time.time() * 1000)
                    for ccy in CURRENCIES:
                        try:
                            msg_id += 1
                            req = {
                                "jsonrpc": "2.0",
                                "id":      msg_id,
                                "method":  "public/get_volatility_index_data",
                                "params":  {
                                    "currency":        ccy,
                                    "start_timestamp": now_ms - 7200000,   # last 2h
                                    "end_timestamp":   now_ms,
                                    "resolution":      "3600",
                                },
                            }
                            await ws.send(json.dumps(req))
                            while True:
                                raw  = await asyncio.wait_for(ws.recv(), timeout=25)
                                resp = json.loads(raw)
                                if resp.get('id') == msg_id:
                                    break
                            candles = (resp.get('result') or {}).get('data') or []
                            if candles:
                                dvol[ccy] = float(candles[-1][4])   # last close
                        except asyncio.TimeoutError:
                            pass
                        except Exception as e:
                            if 'websockets' in type(e).__module__ or 'close frame' in str(e).lower() or 'connection' in str(e).lower():
                                raise

                    eth_price = await fetch_eth_price()

                    # ── Auto-save this refresh's full snapshot to CSV ──
                    if _csv_dir is not None:
                        append_csv(fetch_time, results, dvol, eth_price)

                    # ── Countdown — polls keys/market-open ~10x/sec so [R] and
                    #    [Q] feel instant and the 08:45 refresh fires on time ──
                    interrupted = False
                    for remaining in range(interval, 0, -1):
                        update_values(results, errors, dvol, fetch_time, remaining, eth_price)
                        for _ in range(10):
                            if _quit_evt.is_set():
                                if eq_job is not None:
                                    eq_job.cancel()
                                return
                            if _screenshot_evt.is_set():
                                _screenshot_evt.clear()
                                take_screenshot()
                            if _refresh_evt.is_set() or _market_open_refresh_due():
                                _refresh_evt.clear()
                                force_eq    = True   # pull fresh equities, don't reuse in-flight
                                interrupted = True
                                break
                            await asyncio.sleep(0.1)
                        if interrupted:
                            break   # restart the cycle now → refetch everything

        except asyncio.CancelledError:
            if eq_job is not None:
                eq_job.cancel()
            raise
        except Exception as e:
            # Show error and wait with exponential backoff before reconnecting
            if ROWS:
                move(ROWS.get('exit', 50))
                erase_line()
                sys.stdout.write(f"  {RED}Disconnected — retrying in {backoff}s...{RST}")
                sys.stdout.flush()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)  # double up to 60s max


def main():
    global _last_open_refresh, _csv_dir, _shot_dir

    parser = argparse.ArgumentParser(description='Options Flow Dashboard - Crypto & Equities')
    parser.add_argument('--interval', type=int, default=30,
                        help='Refresh interval in seconds (default: 30)')
    parser.add_argument('--csv-dir', default=None, metavar='DIR',
                        help='directory for daily CSV logs (default: next to this script)')
    parser.add_argument('--no-csv', action='store_true',
                        help='disable per-refresh CSV logging')
    parser.add_argument('--shots-dir', default=None, metavar='DIR',
                        help='directory for [S] screenshots (default: <script dir>/screenshots)')
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Resolve the CSV log directory (logging is on by default). Files are named
    # opt_data_MM_DD_YYYY.csv, one per CT day. Resume today's file if it exists.
    if not args.no_csv:
        _csv_dir = args.csv_dir or script_dir
        _resume_csv_row_count()

    # Resolve the screenshot directory ([S] key saves PNGs here).
    _shot_dir = args.shots_dir or os.path.join(script_dir, 'screenshots')

    # Pre-arm the market-open auto-refresh: if we launch after today's trigger
    # time, mark it done so it only fires when the clock *crosses* 08:45 during
    # a run (the unattended overnight case) — not redundantly at startup.
    now_ct = datetime.now(timezone.utc) + CT_OFFSET
    if (now_ct.hour * 60 + now_ct.minute) >= (MARKET_OPEN_REFRESH[0] * 60 + MARKET_OPEN_REFRESH[1]):
        _last_open_refresh = now_ct.date()

    # Start the keyboard listener ([Q]uit / [R]efresh) as a daemon thread.
    threading.Thread(target=_input_thread, daemon=True).start()

    try:
        asyncio.run(fetch_all(args.interval))
    except KeyboardInterrupt:
        pass
    finally:
        _quit_evt.set()
        cleanup_terminal()
        print(f"{DIM}Exited.{RST}")


if __name__ == '__main__':
    main()
