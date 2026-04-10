#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# deribit_dashboard.py — Real-time Deribit Put/Call Volume Monitor
#
# Usage:
#   python deribit_dashboard.py
#   python deribit_dashboard.py --interval 60
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import json
import os
import sys
import time
import argparse
from datetime import datetime, timezone, timedelta

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

# ── Kill zones (CT, minutes since midnight) ───────────────────────────────────
KILL_ZONES = [
    ('NDO',         0,    210,  'CYN'),   # 12:00am – 3:30am
    ('Morning',     510,  630,  'YLW'),   # 8:30am  – 10:30am
    ('Lunchtime',   690,  810,  'YLW'),   # 11:30am – 1:30pm
    ('Power Hour',  840,  900,  'YLW'),   # 2:00pm  – 3:00pm
    ('EOD',         960,  1080, 'YLW'),   # 4:00pm  – 6:00pm
    ('EEOD',        1110, 1440, 'YLW'),   # 6:30pm  – 12:00am
]
EXCL_DAYS_09 = {2, 3, 6}   # Wed=2, Thu=3, Sun=6
EXCL_START   = 540          # 09:00
EXCL_END     = 600          # 10:00

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
    dow     = now_ct.weekday()
    in_excl = (dow in EXCL_DAYS_09 and EXCL_START <= t_mins < EXCL_END)
    for name, start, end, col in KILL_ZONES:
        if start <= t_mins < end:
            return name, COLS[col], in_excl
    return None, None, in_excl

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

# ── Layout ────────────────────────────────────────────────────────────────────
WIDTH = 62
ROWS  = {}

def draw_static():
    clr()
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
        p()
        mark(f'{ccy}_ratio');     p(f"    {'Put/Call Ratio':<20}")
        p()
        mark(f'{ccy}_sentiment'); p(f"    {'Sentiment':<20}")
        p()
        mark(f'{ccy}_bar');       p(f"    {DIM}Put  {RST}{'':30}{DIM}  Call{RST}")
        mark(f'{ccy}_pct');       p(f"    {DIM}     {'':35}{RST}")
        p()

    p(f"{BLD}{CYN}{'─'*WIDTH}{RST}")
    p(f"  {DIM}Source: Deribit API  |  Covers all strikes & expiries{RST}")
    p(f"  {DIM}Red bar = Puts  |  Green bar = Calls{RST}")
    p(f"  {DIM}P/C >= 1.02 = BEARISH  |  P/C <= 0.98 = BULLISH{RST}")
    p(f"{BLD}{CYN}{'─'*WIDTH}{RST}")
    mark('exit'); p(f"  {DIM}Press Ctrl+C to exit{RST}")

    sys.stdout.flush()


def update_values(results, errors, fetch_time, remaining, eth_price):
    bar_width = 30
    ts = fetch_time.strftime('%Y-%m-%d  %H:%M:%S UTC')

    move(ROWS['ts'])
    erase_line()
    sys.stdout.write(f"  {DIM}{ts}    refreshing in {remaining}s{RST}")

    move(ROWS['eth_price'])
    erase_line()
    if eth_price:
        sys.stdout.write(f"    {'ETH Perp (Phemex)':<24} {BLD}${eth_price:,.2f}{RST}")
    else:
        sys.stdout.write(f"    {'ETH Perp (Phemex)':<24} {DIM}unavailable{RST}")

    session_name, session_col, in_excl = get_session_status()
    move(ROWS['session'])
    erase_line()
    if in_excl:
        sys.stdout.write(f"    {'Session':<24} {RED}{BLD}EXCLUDED (09:00-10:00){RST}")
    elif session_name:
        sys.stdout.write(f"    {'Session':<24} {GRN}{BLD}{session_name}{RST}")
    else:
        sys.stdout.write(f"    {'Session':<24} {RED}{BLD}No active session{RST}")

    for ccy in CURRENCIES:
        if ccy in errors:
            move(ROWS[f'{ccy}_put'])
            erase_line()
            sys.stdout.write(f"    {RED}✗ Error: {errors[ccy]}{RST}")
            for key in ('call', 'total', 'ratio', 'sentiment', 'bar', 'pct'):
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

        put_bar  = f"{RED}{'█' * put_bars}{RST}"
        call_bar = f"{GRN}{'█' * call_bars}{RST}"
        move(ROWS[f'{ccy}_bar'])
        erase_line()
        sys.stdout.write(f"    {DIM}Put  {RST}{put_bar}{call_bar}{DIM}  Call{RST}")

        move(ROWS[f'{ccy}_pct'])
        erase_line()
        sys.stdout.write(f"    {DIM}     {put_pct*100:>5.1f}%{' '*(bar_width-1)}{call_pct*100:>5.1f}%{RST}")

    move(ROWS['exit'])
    erase_line()
    sys.stdout.write(f"  {DIM}Press Ctrl+C to exit{RST}")

    sys.stdout.flush()


# ── Terminal cleanup ──────────────────────────────────────────────────────────
def cleanup_terminal():
    show_cursor()
    move(60)
    sys.stdout.write('\n')
    sys.stdout.flush()

# ── Main fetch loop ───────────────────────────────────────────────────────────
async def fetch_all(interval):
    msg_id    = 0
    eth_price = None
    backoff   = 5   # seconds, doubles on each failed attempt up to 60s

    while True:
        # ── Show reconnecting status on screen if layout already drawn ─────
        if ROWS:
            move(ROWS.get('exit', 50))
            erase_line()
            sys.stdout.write(f"  {YLW}Connecting to Deribit...{RST}")
            sys.stdout.flush()

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
                            errors[ccy] = "Timeout — retrying"
                        except Exception as e:
                            errors[ccy] = str(e)

                    eth_price = await fetch_eth_price()

                    for remaining in range(interval, 0, -1):
                        update_values(results, errors, fetch_time, remaining, eth_price)
                        await asyncio.sleep(1)

        except asyncio.CancelledError:
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
    parser = argparse.ArgumentParser(description='Deribit Put/Call Volume Dashboard')
    parser.add_argument('--interval', type=int, default=30,
                        help='Refresh interval in seconds (default: 30)')
    args = parser.parse_args()

    sys.stdout.write("Connecting to Deribit...\n")
    sys.stdout.flush()
    try:
        asyncio.run(fetch_all(args.interval))
    except KeyboardInterrupt:
        pass
    finally:
        cleanup_terminal()
        print(f"{DIM}Exited.{RST}")


if __name__ == '__main__':
    main()
