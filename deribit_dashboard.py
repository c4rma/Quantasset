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

# Auto-refresh once per weekday when the clock reaches this CT time (market open
# data settles). Mirrors a manual restart so the new session's PCVR/IV/DVOL show
# up without intervention.
MARKET_OPEN_REFRESH = (8, 45)   # (hour, minute) in CT

# ── Shared equities state (prefetched in background, promoted at cycle boundary) ─
eq_results = {}            # symbol -> (put_vol, call_vol, ratio, iv30)
eq_errors  = {}            # symbol -> short error string
eq_status  = 'starting…'

# ── Interactive controls (set by the keyboard thread, polled by the loop) ──────
_quit_evt    = threading.Event()
_refresh_evt = threading.Event()
_last_open_refresh = None   # date of the most recent market-open auto-refresh

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
async def _cboe_symbol_volume(client, symbol):
    """
    Return (put_vol, call_vol, iv30) for one symbol — a single GET.

    put_vol/call_vol are summed across the entire CBOE chain (all strikes,
    all expiries). iv30 is CBOE's 30-day constant-maturity implied vol (VIX-style,
    in %) — stable intraday and matches IBKR's IV index, which makes it the right
    input for expected-range and vol-stop levels. (The raw 0DTE ATM IV is higher
    and lurches as time-to-expiry → 0, so it distorts fixed ER/stop formulas.)

    Each contract's OCC symbol ends with <type><8-digit-strike>, e.g.
    'GLD260618P00388000', so the option type is reliably char[-9].
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

    put_vol = call_vol = 0.0
    for o in options:
        name = o.get('option') or ''
        if len(name) < 9:
            continue
        cp  = name[-9]                       # 'C' or 'P'
        try:
            vol = float(o.get('volume') or 0)
        except (TypeError, ValueError):
            vol = 0.0
        if cp == 'P':
            put_vol  += vol
        elif cp == 'C':
            call_vol += vol

    return put_vol, call_vol, iv30


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
                    put_vol, call_vol, iv30 = await _cboe_symbol_volume(client, symbol)
                    ratio = put_vol / call_vol if call_vol > 0 else 0.0
                    results[symbol] = (put_vol, call_vol, ratio, iv30)
                except Exception as e:
                    errors[symbol] = str(e).replace('\n', ' ')[:36]

            await asyncio.gather(*(_one(s) for s in EQUITY_SYMBOLS))
        status = f'OK ({len(results)}/{len(EQUITY_SYMBOLS)})'

    except asyncio.CancelledError:
        raise
    except Exception as e:
        status = ('err: ' + str(e).replace('\n', ' '))[:40]

    return results, errors, status


# ── Layout ────────────────────────────────────────────────────────────────────
WIDTH = 62
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
    p(f"{BLD}{CYN}  DERIBIT  PUT/CALL VOLUME MONITOR  —  ALL EXPIRIES{RST}")
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
    p(f"    {BLD}{'Symbol':<8}{'Put Vol':>11}{'Call Vol':>11}{'P/C':>7}{'IV':>8}  Sentiment{RST}")
    for sym in EQUITY_SYMBOLS:
        mark(f'eq_{sym}'); p(f"    {sym:<8}")
    p()

    p(f"{BLD}{CYN}{'─'*WIDTH}{RST}")
    p(f"  {DIM}Equities P/C = total volume (all strikes/expiries){RST}")
    p(f"  {DIM}IV = 30-day (matches IBKR's IV index)  |  ~15m delayed{RST}")
    p(f"  {DIM}P/C >= 1.02 = BEARISH  |  P/C <= 0.98 = BULLISH{RST}")
    p(f"{BLD}{CYN}{'─'*WIDTH}{RST}")
    mark('exit'); p(f"  {BLD}[Q]{RST}{DIM}uit   {RST}{BLD}[R]{RST}{DIM}efresh{RST}")

    sys.stdout.flush()


def update_values(results, errors, dvol, fetch_time, remaining, eth_price):
    bar_width = 30
    ts = fetch_time.strftime('%A  %Y-%m-%d  %H:%M:%S UTC')

    move(ROWS['ts'])
    erase_line()
    sys.stdout.write(f"  {DIM}{ts}    refreshing in {remaining}s{RST}")

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

        put_vol, call_vol, ratio, iv30 = eq_results[sym]
        rc = ratio_colour(ratio)
        if ratio >= 1.02:
            eq_sent = f"{RED}{BLD}BEARISH{RST}"
        elif 0 < ratio <= 0.98:
            eq_sent = f"{GRN}{BLD}BULLISH{RST}"
        else:
            eq_sent = f"{YLW}{BLD}NEUTRAL{RST}"
        iv_str = f"{iv30:.1f}%" if iv30 else "n/a"

        sys.stdout.write(
            f"    {sym:<8}{RED}{put_vol:>11,.0f}{RST}{GRN}{call_vol:>11,.0f}{RST}"
            f"{rc}{BLD}{ratio:>7.2f}{RST}{CYN}{iv_str:>8}{RST}  {eq_sent}"
        )

        # Sentiment-change alert (shared with crypto)
        label = _get_sentiment(ratio)
        if _prev_sentiment.get(sym) is not None and _prev_sentiment[sym] != label:
            _play_alert()
        _prev_sentiment[sym] = label

    move(ROWS['exit'])
    erase_line()
    sys.stdout.write(f"  {BLD}[Q]{RST}{DIM}uit   {RST}{BLD}[R]{RST}{DIM}efresh{RST}")

    sys.stdout.flush()


# ── Keyboard input thread ([Q]uit / [R]efresh) ────────────────────────────────
def _input_thread():
    """Daemon thread: read single keypresses and signal the loop via events.
    Q quits, R forces an immediate refresh. No Enter needed."""
    try:
        if sys.platform == 'win32':
            import msvcrt
            while not _quit_evt.is_set():
                ch = msvcrt.getwch()
                if ch in ('q', 'Q'):
                    _quit_evt.set();    break
                elif ch in ('r', 'R'):
                    _refresh_evt.set()
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
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass


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
    global _last_open_refresh

    parser = argparse.ArgumentParser(description='Deribit + CBOE Put/Call Volume Dashboard')
    parser.add_argument('--interval', type=int, default=30,
                        help='Refresh interval in seconds (default: 30)')
    args = parser.parse_args()

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
