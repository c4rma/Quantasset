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
WS_URL   = 'wss://www.deribit.com/ws/api/v2'
CURRENCIES = ['ETH', 'BTC']

# ── Terminal colours ──────────────────────────────────────────────────────────
if sys.platform == 'win32':
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)

GRN  = '\033[92m'
RED  = '\033[91m'
YLW  = '\033[93m'
CYN  = '\033[96m'
MAG  = '\033[95m'
BLD  = '\033[1m'
DIM  = '\033[2m'
RST  = '\033[0m'

def clr():
    """Clear terminal screen."""
    os.system('cls' if sys.platform == 'win32' else 'clear')

def ratio_colour(ratio):
    """Colour the P/C ratio: green = puts dominant (bearish), red = calls dominant (bullish)."""
    if ratio > 1.1:
        return RED    # put-heavy = bearish sentiment
    elif ratio < 0.9:
        return GRN    # call-heavy = bullish sentiment
    else:
        return YLW    # neutral

# ── Deribit API ───────────────────────────────────────────────────────────────
async def fetch_options_volume(ws, currency):
    """
    Fetch all option book summaries for a currency and aggregate
    put/call 24h volumes across ALL expiries.
    """
    req = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "public/get_book_summary_by_currency",
        "params":  {"currency": currency, "kind": "option"}
    }
    await ws.send(json.dumps(req))

    # Read responses until we get the one matching our request id
    while True:
        raw  = await asyncio.wait_for(ws.recv(), timeout=15)
        data = json.loads(raw)
        if data.get('id') == 1:
            break

    result = data.get('result', [])

    put_vol  = 0.0
    call_vol = 0.0

    for inst in result:
        name   = inst.get('instrument_name', '')
        volume = float(inst.get('volume') or 0)
        if volume == 0:
            continue
        # Instrument name format: ETH-27MAR26-2000-P or ETH-27MAR26-2000-C
        suffix = name.split('-')[-1]
        if suffix == 'P':
            put_vol  += volume
        elif suffix == 'C':
            call_vol += volume

    ratio = (put_vol / call_vol) if call_vol > 0 else 0.0
    return put_vol, call_vol, ratio

async def fetch_all(interval):
    """Main loop: connect once, poll every interval seconds."""
    msg_id = 0

    async with websockets.connect(WS_URL, ping_interval=20) as ws:
        while True:
            msg_id += 1
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
                    put_vol  = 0.0
                    call_vol = 0.0

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

            # ── Render dashboard then countdown ───────────────────────────────
            for remaining in range(interval, 0, -1):
                render(results, errors, fetch_time, remaining)
                await asyncio.sleep(1)


def render(results, errors, fetch_time, interval):
    clr()
    width = 62
    ts    = fetch_time.strftime('%Y-%m-%d  %H:%M:%S UTC')
    next_refresh = f"refreshing in {interval}s"

    # Header
    print(f"{BLD}{CYN}{'─'*width}{RST}")
    print(f"{BLD}{CYN}  DERIBIT  PUT/CALL VOLUME MONITOR  —  ALL EXPIRIES{RST}")
    print(f"{BLD}{CYN}{'─'*width}{RST}")
    print(f"  {DIM}{ts}    {next_refresh}{RST}")
    print()

    for ccy in CURRENCIES:
        ccy_col = MAG if ccy == 'BTC' else CYN
        print(f"  {BLD}{ccy_col}{'─'*4} {ccy} {'─'*(width-7-len(ccy))}{RST}")

        if ccy in errors:
            print(f"    {RED}✗ Error: {errors[ccy]}{RST}")
            print()
            continue

        put_vol, call_vol, ratio = results[ccy]
        total  = put_vol + call_vol
        rc     = ratio_colour(ratio)

        # Bar chart: put vs call proportion
        bar_width = 30
        if total > 0:
            put_pct  = put_vol / total
            call_pct = call_vol / total
            put_bars  = int(round(put_pct  * bar_width))
            call_bars = int(round(call_pct * bar_width))
        else:
            put_bars = call_bars = 0

        put_bar  = f"{RED}{'█' * put_bars}{RST}"
        call_bar = f"{GRN}{'█' * call_bars}{RST}"

        print(f"    {'24h Put Volume':<20} {RED}{put_vol:>12,.2f}{RST}  {ccy}")
        print(f"    {'24h Call Volume':<20} {GRN}{call_vol:>12,.2f}{RST}  {ccy}")
        print(f"    {'24h Total Volume':<20} {DIM}{total:>12,.2f}{RST}  {ccy}")
        print()
        print(f"    {'Put/Call Ratio':<20} {rc}{BLD}{ratio:>12.4f}{RST}")
        print()

        # Sentiment label
        if ratio > 1.2:
            sentiment = f"{RED}▲ PUT HEAVY  (bearish lean){RST}"
        elif ratio > 1.05:
            sentiment = f"{YLW}▲ SLIGHT PUT BIAS{RST}"
        elif ratio < 0.8:
            sentiment = f"{GRN}▼ CALL HEAVY (bullish lean){RST}"
        elif ratio < 0.95:
            sentiment = f"{YLW}▼ SLIGHT CALL BIAS{RST}"
        else:
            sentiment = f"{YLW}  NEUTRAL{RST}"

        print(f"    Sentiment     {sentiment}")
        print()

        # Volume bar
        print(f"    {DIM}Put  {RST}{put_bar}{call_bar}{DIM}  Call{RST}")
        print(f"    {DIM}     {put_pct*100:>5.1f}%{' '*(bar_width-1)}{call_pct*100:>5.1f}%{RST}")
        print()

    print(f"{BLD}{CYN}{'─'*width}{RST}")
    print(f"  {DIM}Source: Deribit API  |  Covers all strikes & expiries{RST}")
    print(f"  {DIM}Red bar = Puts  |  Green bar = Calls{RST}")
    print(f"  {DIM}P/C > 1.0 = more puts traded  |  P/C < 1.0 = more calls{RST}")
    print(f"{BLD}{CYN}{'─'*width}{RST}")
    print(f"\n  {DIM}Press Ctrl+C to exit{RST}")


def main():
    parser = argparse.ArgumentParser(description='Deribit Put/Call Volume Dashboard')
    parser.add_argument('--interval', type=int, default=30,
                        help='Refresh interval in seconds (default: 30)')
    args = parser.parse_args()

    print(f"Connecting to Deribit...")
    try:
        asyncio.run(fetch_all(args.interval))
    except KeyboardInterrupt:
        print(f"\n{DIM}Exited.{RST}")


if __name__ == '__main__':
    main()
