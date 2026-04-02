#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# deribit_dashboard.py — Real-time Deribit Put/Call Volume Monitor
#
# Displays 24h Put Volume, Call Volume, and Put/Call Ratio for ETH and BTC
# across ALL expiries. Refreshes every 30 seconds.
#
# Usage:
#   python deribit_dashboard.py
#   python deribit_dashboard.py --interval 60    # refresh every 60s
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import json
import os
import sys
import time
import argparse
from datetime import datetime, timezone

try:
    import websockets
except ImportError:
    print("websockets not installed — run: pip install websockets")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
WS_URL     = 'wss://www.deribit.com/ws/api/v2'
CURRENCIES = ['ETH', 'BTC']

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
    if ratio >= 1.02:   return RED
    elif ratio <= 0.98: return GRN
    else:               return YLW

# ── Layout ────────────────────────────────────────────────────────────────────
WIDTH = 62
ROWS  = {}  # dynamic field row positions, populated in draw_static()

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
    mark('ts'); p()
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


def update_values(results, errors, fetch_time, remaining):
    bar_width = 30
    ts = fetch_time.strftime('%Y-%m-%d  %H:%M:%S UTC')

    # Timestamp + countdown
    move(ROWS['ts'])
    erase_line()
    sys.stdout.write(f"  {DIM}{ts}    refreshing in {remaining}s{RST}")

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


# ── Deribit fetch loop ────────────────────────────────────────────────────────
async def fetch_all(interval):
    msg_id = 0
    first  = True

    async with websockets.connect(WS_URL, ping_interval=20) as ws:
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
                        raw  = await asyncio.wait_for(ws.recv(), timeout=20)
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
                    errors[ccy] = "Timeout waiting for response"
                except Exception as e:
                    errors[ccy] = str(e)

            if first:
                draw_static()
                first = False

            for remaining in range(interval, 0, -1):
                update_values(results, errors, fetch_time, remaining)
                await asyncio.sleep(1)


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
        show_cursor()
        # Move cursor below dashboard before exiting
        move(50)
        print(f"\n{DIM}Exited.{RST}")


if __name__ == '__main__':
    main()
