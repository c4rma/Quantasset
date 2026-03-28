#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# copycat.py — Blackjack Trade Copier CLI
# Confidential — Fund Intellectual Property
#
# Usage:
#   python copycat.py buy   market <size> -tp <tp>
#   python copycat.py buy   limit  <price> <size> -tp <tp>
#   python copycat.py sell  market <size> -tp <tp>
#   python copycat.py sell  limit  <price> <size> -tp <tp>
#   python copycat.py positions
#   python copycat.py balance
#   python copycat.py flatten
#
# Examples:
#   python copycat.py buy market 0.33 -tp 2400.00
#   python copycat.py sell market 0.33 -tp 2100.00
#   python copycat.py buy limit 2130.00 0.33 -tp 2400.00
#   python copycat.py sell limit 2150.00 0.33 -tp 2100.00
# ─────────────────────────────────────────────────────────────────────────────

import sys
import os
import time
import json
import hmac
import hashlib
import asyncio
from datetime import datetime

# ── Load .env ─────────────────────────────────────────────────────────────────
def load_env():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if not os.path.exists(env_path):
        print('\033[91m✗ .env file not found. Copy .env.example to .env.\033[0m')
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()

# ── Config ────────────────────────────────────────────────────────────────────
PHEMEX_API_KEY    = os.environ.get('PHEMEX_API_KEY', '')
PHEMEX_API_SECRET = os.environ.get('PHEMEX_API_SECRET', '')
PHEMEX_BASE_URL   = 'https://api.phemex.com'
PHEMEX_SYMBOL     = 'ETHUSDT'

MT5_FILES_PATH = os.environ.get('MT5_FILES_PATH', '')
MT5_SYMBOL     = 'ETHUSD.nx'
BRIDGE_URL     = os.environ.get('BRIDGE_URL', '').rstrip('/')
BRIDGE_TOKEN   = os.environ.get('BRIDGE_TOKEN', '')

# Auto-detect mode: use network bridge if on non-Windows or BRIDGE_URL is set
USE_BRIDGE = bool(BRIDGE_URL) or sys.platform != 'win32'

PHEMEX_SL         = 15.00
XLTRADE_SL        = 17.60
XLTRADE_TP_OFFSET = 2.60
XLTRADE_SIZE_RATIO= 0.829

# ── Terminal colours ──────────────────────────────────────────────────────────
if sys.platform == 'win32':
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)

GRN = '\033[92m'
RED = '\033[91m'
YLW = '\033[93m'
CYN = '\033[96m'
BLD = '\033[1m'
DIM = '\033[2m'
RST = '\033[0m'
LINE = '─' * 60

def die(msg):
    print(f"{RED}✗ {msg}{RST}")
    sys.exit(1)

def ok(msg):   print(f"{GRN}✓ {msg}{RST}")
def warn(msg): print(f"{YLW}⚠ {msg}{RST}")

def header(title):
    pad = max(0, 54 - len(title))
    print(f"\n{BLD}{CYN}{'─'*4} {title} {'─'*pad}{RST}")

# ── Phemex ────────────────────────────────────────────────────────────────────
def _phemex_sign(path, query='', body=''):
    expiry = str(int(time.time()) + 60)
    msg    = path + query + expiry + body
    sig    = hmac.new(PHEMEX_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return expiry, sig

def _phemex_headers(path, query='', body=''):
    expiry, sig = _phemex_sign(path, query, body)
    return {
        'x-phemex-access-token':      PHEMEX_API_KEY,
        'x-phemex-request-expiry':    expiry,
        'x-phemex-request-signature': sig,
        'Content-Type':               'application/json',
    }

async def phemex_request(method, path, params=None, body=None):
    # When running remotely, proxy Phemex requests through bridge.py on the
    # PC so all traffic originates from the PC's whitelisted IP address
    if USE_BRIDGE and BRIDGE_URL:
        return await phemex_request_via_bridge(method, path, params, body)

    try:
        import httpx
    except ImportError:
        die("httpx not installed — run: pip install httpx")

    query    = '&'.join(f'{k}={v}' for k, v in params.items()) if params else ''
    body_str = json.dumps(body) if body else ''

    if method == 'PUT' and params and not body:
        headers = _phemex_headers(path, query, '')
    else:
        headers = _phemex_headers(path, query, body_str)

    url = f"{PHEMEX_BASE_URL}{path}" + (f"?{query}" if query else '')

    async with httpx.AsyncClient(timeout=10) as client:
        if method == 'GET':
            r = await client.get(url, headers=headers)
        elif method == 'PUT':
            if body:
                r = await client.put(url, headers=headers, content=body_str)
            else:
                r = await client.put(url, headers=headers)
        elif method == 'POST':
            r = await client.post(url, headers=headers, content=body_str)
        elif method == 'DELETE':
            r = await client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        return r.json()


async def phemex_request_via_bridge(method, path, params=None, body=None):
    """Proxy a Phemex API request through bridge.py on the PC."""
    import asyncio
    loop = asyncio.get_event_loop()

    def _send():
        import urllib.request
        payload = json.dumps({
            'method': method,
            'path':   path,
            'params': params,
            'body':   body,
        }).encode()
        req = urllib.request.Request(
            f"{BRIDGE_URL}/phemex",
            data    = payload,
            headers = {
                'Content-Type':   'application/json',
                'X-Bridge-Token': BRIDGE_TOKEN,
            },
            method = 'POST',
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    try:
        result = await loop.run_in_executor(None, _send)
        if 'error' in result:
            raise RuntimeError(result['error'])
        return result.get('resp', {})
    except Exception as e:
        raise RuntimeError(f"Bridge Phemex proxy error: {e}")

async def phemex_get_fill_price(cl_ord_id, side, retries=8, delay=0.3):
    """Get fill price — first try position avg entry, then order history."""
    # Try position first — fastest since market orders fill instantly
    for attempt in range(retries):
        await asyncio.sleep(delay if attempt > 0 else 0.2)
        try:
            data = await phemex_request('GET', '/g-accounts/accountPositions',
                                        params={'currency': 'USDT'})
            for p in (data.get('data', {}).get('positions', []) or []):
                if (p.get('symbol') == PHEMEX_SYMBOL
                        and p.get('posSide') == side
                        and float(p.get('size', 0)) != 0):
                    price = float(p.get('avgEntryPriceRp') or 0)
                    if price > 0:
                        return price
            # Fallback: check order history
            data2 = await phemex_request('GET', '/g-orders/hist',
                                         params={'symbol': PHEMEX_SYMBOL, 'limit': '10'})
            for o in (data2.get('data', {}).get('rows', []) or []):
                if o.get('clOrdID') == cl_ord_id:
                    price = float(o.get('avgTransactPriceRp') or o.get('execPriceRp') or 0)
                    if price > 0:
                        return price
        except Exception:
            pass
    return None

async def phemex_amend_sl(side, fill_price, tp):
    """Place a stop-market order to act as SL after a market fill."""
    sl = round(fill_price - PHEMEX_SL if side == 'Long' else fill_price + PHEMEX_SL, 2)
    close_side = 'Sell' if side == 'Long' else 'Buy'
    try:
        params = {
            'symbol':        PHEMEX_SYMBOL,
            'clOrdID':       f'cc_sl_{int(time.time()*1000)}',
            'side':          close_side,
            'posSide':       side,
            'orderQtyRq':    '0',
            'ordType':       'Stop',
            'stopPxRp':      str(sl),
            'triggerType':   'ByMarkPrice',
            'closeOnTrigger': 'true',
            'reduceOnly':    'true',
            'timeInForce':   'GoodTillCancel',
        }
        result = await phemex_request('PUT', '/g-orders/create', params=params)
        return sl, result
    except Exception as e:
        return sl, {'error': str(e)}

# ── MT5 bridge ────────────────────────────────────────────────────────────────
# On Windows with MT5_FILES_PATH set: uses local file bridge directly.
# On any other platform (Termux, Linux, Mac) or when BRIDGE_URL is set:
# sends an HTTP request to bridge.py running on the Windows PC.

def mt5_send_file(signal, timeout=20):
    """Local file bridge — Windows only."""
    if not MT5_FILES_PATH:
        return None, "MT5_FILES_PATH not set in .env"
    if not os.path.isdir(MT5_FILES_PATH):
        return None, f"MT5 files path not found: {MT5_FILES_PATH}"

    sig_path  = os.path.join(MT5_FILES_PATH, 'bj_signal.txt')
    resp_path = os.path.join(MT5_FILES_PATH, 'bj_response.txt')
    hb_path   = os.path.join(MT5_FILES_PATH, 'bj_heartbeat.txt')

    if not os.path.exists(hb_path):
        return None, "EA heartbeat not found — is BlackjackCopier running on the chart?"
    age = time.time() - os.path.getmtime(hb_path)
    if age > 8:
        return None, f"EA heartbeat stale ({age:.0f}s) — EA may have stopped"

    for _ in range(3):
        if os.path.exists(resp_path):
            try:
                os.remove(resp_path)
                break
            except Exception:
                time.sleep(0.05)

    with open(sig_path, 'w') as f:
        f.write(signal)

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.03)
        if not os.path.exists(resp_path):
            continue
        for _ in range(5):
            try:
                with open(resp_path, 'r') as f:
                    resp = f.read().strip()
                if resp:
                    try:
                        os.remove(resp_path)
                    except Exception:
                        pass
                    return resp, None
            except Exception:
                pass
            time.sleep(0.03)

    return None, "Timeout — EA did not respond"


def mt5_send_network(signal, timeout=20):
    """Network bridge — sends signal to bridge.py over HTTP."""
    if not BRIDGE_URL:
        return None, "BRIDGE_URL not set in .env (required when running outside Windows)"
    try:
        import urllib.request
        payload = json.dumps({'signal': signal, 'timeout': timeout}).encode()
        req = urllib.request.Request(
            f"{BRIDGE_URL}/signal",
            data    = payload,
            headers = {
                'Content-Type':    'application/json',
                'X-Bridge-Token':  BRIDGE_TOKEN,
            },
            method = 'POST',
        )
        with urllib.request.urlopen(req, timeout=timeout + 5) as r:
            data = json.loads(r.read())
        if 'error' in data:
            return None, data['error']
        return data.get('resp'), None
    except Exception as e:
        return None, f"Bridge error: {e}"


def mt5_send(signal, timeout=20):
    """Route to file bridge (Windows) or network bridge (everything else)."""
    if USE_BRIDGE:
        return mt5_send_network(signal, timeout)
    return mt5_send_file(signal, timeout)

def mt5_positions():
    resp, err = mt5_send("POSITIONS")
    if err:
        return [], err
    if not resp or not resp.startswith("OK|POSITIONS"):
        return [], f"Unexpected response: {resp}"
    result = []
    for p in resp.split('|')[2:]:
        if p in ('NONE', ''):
            continue
        f = p.split(',')
        if len(f) < 9:
            continue
        try:
            result.append({
                'broker':    'XLTRADE',
                'symbol':    f[0],
                'side':      f[1],
                'size':      float(f[2]),
                'avg_price': float(f[3]),
                'mark':      float(f[4]),
                'sl':        float(f[5]),
                'tp':        float(f[6]),
                'upnl':      float(f[7]),
                'ticket':    f[8],
            })
        except (ValueError, IndexError):
            continue
    return result, None

def mt5_account():
    """Request account info from EA. Returns dict or None + error string."""
    resp, err = mt5_send("ACCOUNT")
    if err:
        return None, err
    if not resp or not resp.startswith("OK|ACCOUNT"):
        return None, f"Unexpected response: {resp}"
    # Parse key=value pairs from the response
    parts = resp.split('|')[2:]  # everything after OK|ACCOUNT
    data  = {}
    for part in parts:
        for kv in part.split(','):
            if '=' in kv:
                k, v = kv.split('=', 1)
                data[k.strip()] = v.strip()
    return data, None

def mt5_orders():
    """Request pending/open orders from EA. Returns list + error string."""
    resp, err = mt5_send("ORDERS")
    if err:
        return [], err
    if not resp or not resp.startswith("OK|ORDERS"):
        return [], f"Unexpected response: {resp}"
    result = []
    for p in resp.split('|')[2:]:
        if p in ('NONE', ''):
            continue
        f = p.split(',')
        if len(f) < 8:
            continue
        try:
            result.append({
                'broker': 'XLTRADE',
                'symbol': f[0],
                'side':   f[1],
                'type':   f[2],
                'size':   float(f[3]),
                'price':  float(f[4]),
                'sl':     float(f[5]),
                'tp':     float(f[6]),
                'ticket': f[7],
            })
        except (ValueError, IndexError):
            continue
    return result, None

# ── Command: trade ────────────────────────────────────────────────────────────
async def cmd_trade(direction, order_type, limit_price, phemex_size, xltrade_size, tp):
    side        = 'Long'   if direction == 'buy'  else 'Short'
    mt5_side    = 'BUY'    if direction == 'buy'  else 'SELL'
    phemex_side = 'Buy'    if direction == 'buy'  else 'Sell'
    ord_type    = 'Market' if order_type == 'market' else 'Limit'
    xltrade_tp  = round(tp + XLTRADE_TP_OFFSET if side == 'Long' else tp - XLTRADE_TP_OFFSET, 2)

    # ── For limit orders: show full preview with known entry ──────────────────
    if ord_type == 'Limit':
        sl_phemex  = round(limit_price - PHEMEX_SL  if side == 'Long' else limit_price + PHEMEX_SL, 2)
        sl_xltrade = round(limit_price - XLTRADE_SL if side == 'Long' else limit_price + XLTRADE_SL, 2)
        rr_phemex  = round(abs(tp - limit_price) / PHEMEX_SL, 2)
        rr_xltrade = round((abs(tp - limit_price) - XLTRADE_TP_OFFSET) / XLTRADE_SL, 2)

        header(f"{side.upper()} LIMIT")
        print(f"  {'Limit Price':<20} {limit_price}")
        print(f"  {'TP (Phemex)':<20} {tp}")
        print(f"  {'Phemex SL':<20} {sl_phemex}  (limit ± $15.00)")
        print(f"  {'Phemex Size':<20} {phemex_size} ETH")
        print(f"  {'Phemex R:R':<20} {GRN}1:{rr_phemex}{RST}")
        print(f"  {LINE}")
        print(f"  {'XLTRADE SL':<20} {sl_xltrade}  (limit ± $17.60)")
        print(f"  {'XLTRADE TP':<20} {xltrade_tp}  (tp + $2.60)")
        print(f"  {'XLTRADE Size':<20} {xltrade_size} lots")
        print(f"  {'XLTRADE R:R':<20} {GRN}1:{rr_xltrade}{RST}")
        print()
    else:
        # Market: show what we know, note SL set post-fill
        header(f"{side.upper()} MARKET")
        print(f"  {'Size (Phemex)':<20} {phemex_size} ETH")
        print(f"  {'Size (XLTRADE)':<20} {xltrade_size} lots")
        print(f"  {'TP (Phemex)':<20} {tp}")
        print(f"  {'TP (XLTRADE)':<20} {xltrade_tp}")
        print(f"  {'Phemex SL':<20} fill price ± $15.00  {DIM}(set after fill){RST}")
        print(f"  {'XLTRADE SL':<20} fill price ± $17.60  {DIM}(set at execution){RST}")
        print()

    confirm = input(f"  {BLD}Confirm? (y/n): {RST}").strip().lower()
    if confirm != 'y':
        warn("Cancelled.")
        return

    print()
    errors    = []
    cl_ord_id = f'cc_{int(time.time()*1000)}'
    phemex_fill = None

    # Build Phemex params
    phemex_params = {
        'symbol':       PHEMEX_SYMBOL,
        'clOrdID':      cl_ord_id,
        'side':         phemex_side,
        'posSide':      side,
        'orderQtyRq':   str(phemex_size),
        'ordType':      ord_type,
        'reduceOnly':   'false',
        'takeProfitRp': str(tp),
        'tpTrigger':    'ByMarkPrice',
        'timeInForce':  'GoodTillCancel',
    }
    if ord_type == 'Limit':
        phemex_params['priceRp']    = str(limit_price)
        phemex_params['stopLossRp'] = str(sl_phemex)
        phemex_params['slTrigger']  = 'ByMarkPrice'

    # Build XLTRADE signal
    if ord_type == 'Limit':
        xltrade_signal = f"OPEN|{MT5_SYMBOL}|{mt5_side}|{xltrade_size:.2f}|{sl_xltrade:.2f}|{xltrade_tp:.2f}|CC-Limit|LIMIT|{limit_price:.2f}"
    else:
        xltrade_signal = f"OPEN|{MT5_SYMBOL}|{mt5_side}|{xltrade_size:.2f}|0.00|{xltrade_tp:.2f}|CC-Market|MARKET"

    # ── Fire both brokers simultaneously ──────────────────────────────────────
    if ord_type == 'Market':
        # Pre-clear the MT5 response file to avoid stale reads
        resp_path = os.path.join(MT5_FILES_PATH, 'bj_response.txt')
        if os.path.exists(resp_path):
            try:
                os.remove(resp_path)
            except Exception:
                pass

        # Run Phemex (async) and XLTRADE (sync in thread) at the same time
        loop = asyncio.get_event_loop()
        print(f"  Sending to {CYN}Phemex{RST} + {YLW}XLTRADE{RST} simultaneously...", flush=True)
        phemex_task  = phemex_request('PUT', '/g-orders/create', params=phemex_params)
        xltrade_task = loop.run_in_executor(None, lambda: mt5_send(xltrade_signal, timeout=20))
        phemex_result, xltrade_resp_tuple = await asyncio.gather(phemex_task, xltrade_task)
        xltrade_resp, xltrade_err = xltrade_resp_tuple

        # Process Phemex result
        if phemex_result.get('code') == 0:
            ok("Phemex market order placed")
            # Poll for fill price then set SL
            print(f"  Getting fill price...", end=' ', flush=True)
            phemex_fill = await phemex_get_fill_price(cl_ord_id, side)
            if phemex_fill and phemex_fill > 0:
                sl_phemex = round(phemex_fill - PHEMEX_SL if side == 'Long' else phemex_fill + PHEMEX_SL, 2)
                _, amend = await phemex_amend_sl(side, phemex_fill, tp)
                if isinstance(amend, dict) and amend.get('code') == 0:
                    ok(f"Fill @ {phemex_fill} | SL set @ {sl_phemex}")
                else:
                    warn(f"Fill @ {phemex_fill} | SL amend: {amend} — set SL manually")
            else:
                warn("Could not confirm fill price — set SL manually on Phemex")
        else:
            msg  = phemex_result.get('msg', str(phemex_result))
            code = phemex_result.get('code', '')
            print(f"{RED}✗ Phemex failed ({code}): {msg}{RST}")
            errors.append(f"Phemex: ({code}) {msg}")

        # Process XLTRADE result
        if xltrade_err:
            print(f"{RED}✗ XLTRADE: {xltrade_err}{RST}")
            errors.append(f"XLTRADE: {xltrade_err}")
        elif xltrade_resp and xltrade_resp.startswith('OK|OPEN'):
            parts  = xltrade_resp.split('|')
            ticket = parts[8] if len(parts) > 8 else '?'
            fill   = parts[5] if len(parts) > 5 else '?'
            try:
                fill_f = float(fill)
                xt_sl  = round(fill_f - XLTRADE_SL if side == 'Long' else fill_f + XLTRADE_SL, 2)
                ok(f"XLTRADE filled — ticket {ticket} @ {fill_f:.2f} | SL @ {xt_sl:.2f}")
            except (ValueError, TypeError):
                ok(f"XLTRADE order placed — ticket {ticket}")
        else:
            print(f"{RED}✗ XLTRADE: {xltrade_resp}{RST}")
            errors.append(f"XLTRADE: {xltrade_resp}")

    else:
        # ── Limit orders: fire sequentially (both are async-friendly) ──────────
        print(f"  Sending to {CYN}Phemex{RST}...", end=' ', flush=True)
        try:
            result = await phemex_request('PUT', '/g-orders/create', params=phemex_params)
            if result.get('code') == 0:
                ok(f"Phemex limit order placed @ {limit_price}")
            else:
                msg  = result.get('msg', str(result))
                code = result.get('code', '')
                print(f"{RED}✗ Failed ({code}): {msg}{RST}")
                print(f"  {DIM}Request: {json.dumps(phemex_params)}{RST}")
                errors.append(f"Phemex: ({code}) {msg}")
        except Exception as e:
            print(f"{RED}✗ Error: {e}{RST}")
            errors.append(f"Phemex: {e}")

        print(f"  Sending to {YLW}XLTRADE{RST}...", end=' ', flush=True)
        xltrade_resp, xltrade_err = mt5_send(xltrade_signal, timeout=20)
        if xltrade_err:
            print(f"{RED}✗ {xltrade_err}{RST}")
            errors.append(f"XLTRADE: {xltrade_err}")
        elif xltrade_resp and xltrade_resp.startswith('OK|OPEN'):
            parts  = xltrade_resp.split('|')
            ticket = parts[8] if len(parts) > 8 else '?'
            ok(f"XLTRADE limit order placed — ticket {ticket} @ {limit_price:.2f} | SL @ {sl_xltrade:.2f} | TP @ {xltrade_tp:.2f}")
        else:
            print(f"{RED}✗ {xltrade_resp}{RST}")
            errors.append(f"XLTRADE: {xltrade_resp}")

    print()
    if not errors:
        ok(f"{side.upper()} {phemex_size} ETH (Phemex) / {xltrade_size} lots (XLTRADE) — both brokers filled")
    else:
        warn("Completed with errors:")
        for e in errors:
            print(f"    {RED}{e}{RST}")

# ── Command: positions ────────────────────────────────────────────────────────
async def cmd_positions(refresh=False):
    while True:
        results = []
        errors  = []
        ts      = datetime.now().strftime('%H:%M:%S')

        try:
            data = await phemex_request('GET', '/g-accounts/accountPositions', params={'currency': 'USDT'})

            tp_map = {}
            sl_map = {}
            try:
                ord_data = await phemex_request('GET', '/g-orders/activeList',
                                                params={'symbol': PHEMEX_SYMBOL, 'currency': 'USDT'})
                if ord_data and ord_data.get('code') == 0:
                    for o in (ord_data.get('data', {}).get('rows', []) or []):
                        spx      = float(o.get('stopPxRp') or 0)
                        stop_dir = o.get('stopDirection', '')
                        side_o   = o.get('side', '')
                        if spx == 0:
                            continue
                        if stop_dir == 'Rising' and side_o == 'Sell':
                            tp_map['Long'] = spx
                        elif stop_dir == 'Falling' and side_o == 'Sell':
                            sl_map['Long'] = spx
                        elif stop_dir == 'Falling' and side_o == 'Buy':
                            tp_map['Short'] = spx
                        elif stop_dir == 'Rising' and side_o == 'Buy':
                            sl_map['Short'] = spx
            except Exception:
                pass

            for p in data.get('data', {}).get('positions', []):
                if p.get('symbol') == PHEMEX_SYMBOL and float(p.get('size', 0)) != 0:
                    pos_side = p.get('posSide', 'Long')
                    avg      = float(p.get('avgEntryPriceRp', 0))
                    mark     = float(p.get('markPriceRp', 0))
                    size     = abs(float(p.get('size', 0)))
                    sl       = sl_map.get(pos_side) or round(avg - PHEMEX_SL if pos_side == 'Long' else avg + PHEMEX_SL, 2)
                    tp_val   = tp_map.get(pos_side) or float(p.get('takeProfitRp') or 0)
                    upnl     = round((mark - avg) * size if pos_side == 'Long' else (avg - mark) * size, 4)
                    results.append({'broker': 'Phemex', 'symbol': PHEMEX_SYMBOL,
                                    'side': pos_side, 'size': size, 'avg': avg,
                                    'mark': mark, 'sl': sl, 'tp': tp_val, 'upnl': upnl})
        except Exception as e:
            errors.append(f"Phemex: {e}")

        mt5_pos, err = mt5_positions()
        if err:
            errors.append(f"XLTRADE: {err}")
        else:
            for p in mt5_pos:
                results.append({'broker': 'XLTRADE', 'symbol': p['symbol'],
                                 'side': p['side'], 'size': p['size'],
                                 'avg': p['avg_price'], 'mark': p['mark'],
                                 'sl': p['sl'], 'tp': p['tp'], 'upnl': p['upnl']})

        # ── Clear AFTER data is ready, so screen is blank for <1ms ──────────
        if refresh:
            os.system('cls' if sys.platform == 'win32' else 'clear')

        suffix = f"  {DIM}[auto-refresh  {ts}  Ctrl+C to exit]{RST}" if refresh else ""
        header(f"OPEN POSITIONS{suffix}")

        if not results:
            print(f"  No open positions")
        else:
            print(f"  {'Broker':<12}{'Symbol':<12}{'Side':<8}{'Size':<10}{'Avg':<10}{'Mark':<10}{'SL':<10}{'TP':<10}{'UPnL':>10}")
            print(f"  {'─'*90}")
            total = 0
            for p in results:
                pc = GRN if p['upnl'] >= 0 else RED
                sc = GRN if p['side'] == 'Long' else RED
                bc = CYN if p['broker'] == 'Phemex' else YLW
                print(f"  {bc}{p['broker']:<12}{RST}"
                      f"{p['symbol']:<12}"
                      f"{sc}{p['side']:<8}{RST}"
                      f"{p['size']:<10}"
                      f"{p['avg']:<10.2f}"
                      f"{p['mark']:<10.2f}"
                      f"{RED}{p['sl']:<10.2f}{RST}"
                      f"{GRN}{p['tp']:<10.2f}{RST}"
                      f"{pc}{p['upnl']:>+10.2f}{RST}")
                total += p['upnl']
            print(f"  {'─'*90}")
            tc = GRN if total >= 0 else RED
            print(f"  {'Total UPnL':<70}{tc}{total:>+10.2f}{RST}")

        if errors:
            print()
            for e in errors:
                warn(e)

        if not refresh:
            break

        for remaining in range(5, 0, -1):
            # Reprint the last line with countdown
            print(f"\r  {DIM}Refreshing in {remaining}s...  {RST}", end='', flush=True)
            await asyncio.sleep(1)
        print()



# ── Command: balance ──────────────────────────────────────────────────────────
async def cmd_balance():
    header("ACCOUNT BALANCES")
    try:
        # Balance
        data  = await phemex_request('GET', '/g-accounts/accountPositions', params={'currency': 'USDT'})
        acc   = data.get('data', {}).get('account', {})
        bal   = float(acc.get('accountBalanceRv') or 0)
        used  = float(acc.get('totalUsedBalanceRv') or 0)
        avail = round(bal - used, 4)
        bonus = float(acc.get('bonusBalanceRv') or 0)

        # Today's closed PnL — sum closedPnlRv from today's trade history
        closed_pnl = 0.0
        try:
            day_start_ms = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
            hist = await phemex_request('GET', '/g-orders/hist',
                                        params={'symbol': PHEMEX_SYMBOL,
                                                'limit':  '50',
                                                'start':  str(day_start_ms)})
            for o in (hist.get('data', {}).get('rows', []) or []):
                pnl = float(o.get('closedPnlRv') or 0)
                closed_pnl += pnl
        except Exception:
            pass

        # Open PnL — sum unrealized PnL across open positions
        open_pnl = 0.0
        try:
            for p in (data.get('data', {}).get('positions', []) or []):
                if p.get('symbol') == PHEMEX_SYMBOL and float(p.get('size', 0)) != 0:
                    pos_side = p.get('posSide', 'Long')
                    avg      = float(p.get('avgEntryPriceRp') or 0)
                    mark     = float(p.get('markPriceRp') or 0)
                    size     = abs(float(p.get('size') or 0))
                    upnl     = (mark - avg) * size if pos_side == 'Long' else (avg - mark) * size
                    open_pnl += upnl
        except Exception:
            pass

        pnl_col  = GRN if closed_pnl >= 0 else RED
        opnl_col = GRN if open_pnl   >= 0 else RED
        print(f"  {CYN}Phemex{RST}")
        print(f"    {'Balance':<18} ${bal:.4f}")
        print(f"    {'Available':<18} ${avail:.4f}")
        print(f"    {'Used Margin':<18} ${used:.4f}")
        print(f"    {'Open PnL':<18} {opnl_col}${open_pnl:+.4f}{RST}")
        print(f"    {'Today Closed PnL':<18} {pnl_col}${closed_pnl:+.4f}{RST}")
        if bonus > 0:
            print(f"    {'Bonus':<18} ${bonus:.4f}")
    except Exception as e:
        warn(f"Phemex balance error: {e}")

    print()
    acc, err = mt5_account()
    if err:
        warn(f"XLTRADE: {err}")
    else:
        bal       = float(acc.get('balance', 0))
        equity    = float(acc.get('equity', 0))
        margin    = float(acc.get('margin', 0))
        free      = float(acc.get('free', 0))
        profit    = float(acc.get('profit', 0))
        closedpnl = float(acc.get('closedpnl', 0))
        cur       = acc.get('currency', 'USD')
        lev       = acc.get('leverage', '?')
        login     = acc.get('login', '?')
        pnl_col   = GRN if profit    >= 0 else RED
        cpnl_col  = GRN if closedpnl >= 0 else RED
        print(f"  {YLW}XLTRADE{RST}  {DIM}(#{login} | 1:{lev}){RST}")
        print(f"    {'Balance':<18} {cur} {bal:.2f}")
        print(f"    {'Equity':<18} {cur} {equity:.2f}")
        print(f"    {'Free Margin':<18} {cur} {free:.2f}")
        print(f"    {'Used Margin':<18} {cur} {margin:.2f}")
        print(f"    {'Open PnL':<18} {pnl_col}{cur} {profit:+.2f}{RST}")
        print(f"    {'Today Closed PnL':<18} {cpnl_col}{cur} {closedpnl:+.2f}{RST}")

# ── Command: flatten ──────────────────────────────────────────────────────────
async def cmd_flatten():
    header("FLATTEN ALL POSITIONS + ORDERS")
    warn("This will close ALL positions and cancel ALL pending orders across ALL brokers.")
    confirm = input(f"\n  {BLD}Confirm? (y/n): {RST}").strip().lower()
    if confirm != 'y':
        warn("Cancelled.")
        return

    print()
    print(f"  Flattening {CYN}Phemex{RST}...", end=' ', flush=True)
    try:
        closed    = 0
        cancelled = 0

        # Close all open positions
        data = await phemex_request('GET', '/g-accounts/accountPositions', params={'currency': 'USDT'})
        for p in data.get('data', {}).get('positions', []):
            if p.get('symbol') == PHEMEX_SYMBOL and float(p.get('size', 0)) != 0:
                pos_side   = p.get('posSide', 'Long')
                close_side = 'Sell' if pos_side == 'Long' else 'Buy'
                params = {
                    'symbol':       PHEMEX_SYMBOL,
                    'clOrdID':      f'cc_flat_{int(time.time()*1000)}',
                    'side':         close_side,
                    'posSide':      pos_side,
                    'orderQtyRq':   str(abs(float(p.get('size', 0)))),
                    'ordType':      'Market',
                    'reduceOnly':   'true',
                    'timeInForce':  'GoodTillCancel',
                }
                result = await phemex_request('PUT', '/g-orders/create', params=params)
                if isinstance(result, dict) and result.get('code') == 0:
                    closed += 1

        # Cancel all pending orders using bulk cancel
        cancel_all = await phemex_request('DELETE', '/g-orders/all',
                                          params={'symbol': PHEMEX_SYMBOL,
                                                  'untriggered': 'false'})
        if isinstance(cancel_all, dict):
            code = cancel_all.get('code')
            if code == 0:
                rows = cancel_all.get('data', {})
                if isinstance(rows, dict):
                    cancelled = len(rows.get('rows', []) or [])
                elif isinstance(rows, list):
                    cancelled = len(rows)
                else:
                    cancelled = 1  # cancelled but can't count
            elif code in (10002, 10016):
                cancelled = 0  # no orders to cancel

        ok(f"Phemex — {closed} position(s) closed, {cancelled} order(s) cancelled")
    except Exception as e:
        print(f"{RED}✗ Error: {e}{RST}")

    print(f"  Flattening {YLW}XLTRADE{RST}...", end=' ', flush=True)
    resp, err = mt5_send(f"FLATTEN|{MT5_SYMBOL}", timeout=15)
    if err:
        print(f"{RED}✗ {err}{RST}")
    elif resp and resp.startswith('OK|FLATTEN'):
        parts     = resp.split('|')
        closed    = next((p.replace('closed:', '')    for p in parts if p.startswith('closed:')),    '?')
        cancelled = next((p.replace('cancelled:', '') for p in parts if p.startswith('cancelled:')), '0')
        errors    = next((p.replace('errors:', '')    for p in parts if p.startswith('errors:')),    '0')
        ok(f"XLTRADE — {closed} position(s) closed, {cancelled} order(s) cancelled, {errors} error(s)")
    else:
        print(f"{RED}✗ {resp}{RST}")

    print()
    ok("Flatten complete")

# ── Argument parser ───────────────────────────────────────────────────────────
USAGE = f"""
{BLD}{CYN}COPYCAT — Blackjack Trade Copier{RST}
{DIM}Confidential — Fund Intellectual Property{RST}

{BLD}Usage:{RST}
  python copycat.py buy   market -sp <phemex_size> -sx <xltrade_size> -tp <tp>
  python copycat.py buy   limit  -p <price> -sp <phemex_size> -sx <xltrade_size> -tp <tp>
  python copycat.py sell  market -sp <phemex_size> -sx <xltrade_size> -tp <tp>
  python copycat.py sell  limit  -p <price> -sp <phemex_size> -sx <xltrade_size> -tp <tp>
  python copycat.py positions
  python copycat.py positions --refresh
  python copycat.py orders
  python copycat.py balance
  python copycat.py flatten

{BLD}Examples:{RST}
  python copycat.py buy market -sp 0.33 -sx 0.27 -tp 2400.00
  python copycat.py sell market -sp 0.33 -sx 0.27 -tp 2100.00
  python copycat.py buy limit -p 2130.00 -sp 0.33 -sx 0.27 -tp 2400.00
  python copycat.py sell limit -p 2150.00 -sp 0.33 -sx 0.27 -tp 2100.00
"""

# ── Command: orders ───────────────────────────────────────────────────────────
async def cmd_orders():
    header("OPEN ORDERS")
    results = []
    errors  = []

    # Phemex — active orders
    try:
        data = await phemex_request('GET', '/g-orders/activeList',
                                    params={'symbol': PHEMEX_SYMBOL, 'currency': 'USDT'})
        if data is None:
            raise ValueError("No response from Phemex")
        code = data.get('code')
        if code == 10002:
            pass  # OM_ORDER_NOT_FOUND — no active orders, not an error
        elif code != 0:
            errors.append(f"Phemex orders: code={code} msg={data.get('msg')}")
        else:
            rows = data.get('data', {}).get('rows', []) or []
            for o in rows:
                side      = o.get('side', '')
                # ordType works for limit orders; orderType for conditional
                ord_type  = o.get('ordType') or o.get('orderType', '')
                # For stop/conditional orders price is stopPxRp, not priceRp
                stop_px   = float(o.get('stopPxRp') or 0)
                price     = float(o.get('priceRp') or 0) if stop_px == 0 else stop_px
                stop_dir  = o.get('stopDirection', '')
                side_o    = o.get('side', '')
                # Derive posSide since Phemex doesn't return it on conditional orders
                if stop_dir == 'Rising' and side_o == 'Sell':
                    pos_side = 'Long'
                elif stop_dir == 'Falling' and side_o == 'Sell':
                    pos_side = 'Long'
                elif stop_dir == 'Falling' and side_o == 'Buy':
                    pos_side = 'Short'
                elif stop_dir == 'Rising' and side_o == 'Buy':
                    pos_side = 'Short'
                else:
                    pos_side = ''
                close_on  = 'CloseOnTrigger' in (o.get('execInst', '') or '')
                raw_size  = float(o.get('orderQtyRq') or o.get('leavesQtyRq') or 0)
                size      = raw_size
                sl        = float(o.get('stopLossRp') or 0)
                tp_val    = float(o.get('takeProfitRp') or 0)
                cl_id     = o.get('clOrdID', '')
                status    = o.get('ordStatus', '')
                results.append({
                    'broker': 'Phemex',
                    'symbol': PHEMEX_SYMBOL,
                    'side':   side,
                    'type':   ord_type,
                    'size':   size,
                    'price':  price,
                    'sl':     sl,
                    'tp':     tp_val,
                    'id':     cl_id,
                    'status': status,
                    'pos_side': pos_side,
                })
    except Exception as e:
        errors.append(f"Phemex: {e}")

    # XLTRADE — pending orders from EA
    xt_orders, err = mt5_orders()
    if err:
        errors.append(f"XLTRADE: {err}")
    else:
        for o in xt_orders:
            results.append({
                'broker':   'XLTRADE',
                'symbol':   o['symbol'],
                'side':     o['side'],
                'type':     o['type'],
                'size':     o['size'],
                'price':    o['price'],
                'sl':       o['sl'],
                'tp':       o['tp'],
                'id':       o['ticket'],
                'status':   'Pending',
                'pos_side': '',
            })

    if not results:
        print(f"  No open orders")
    else:
        print(f"  {'Broker':<12}{'Symbol':<12}{'Side':<8}{'PosSide':<8}{'Type':<22}{'Size':<8}{'Trigger':<12}{'Status'}")
        print(f"  {'─'*90}")
        for o in results:
            sc  = GRN if o['side'] == 'Buy' else RED
            bc  = CYN if o['broker'] == 'Phemex' else YLW
            ps  = o.get('pos_side', '')
            psc = GRN if ps == 'Long' else (RED if ps == 'Short' else RST)
            close_on = 'CloseOnTrigger' in (o.get('execInst', '') if isinstance(o, dict) else '')
            size_str = 'Full' if (o['size'] == 0 and o.get('broker') == 'Phemex') else f"{o['size']:.2f}"
            trigger = o['price'] if o['price'] > 0 else 0
            print(f"  {bc}{o['broker']:<12}{RST}"
                  f"{o['symbol']:<12}"
                  f"{sc}{o['side']:<8}{RST}"
                  f"{psc}{ps:<8}{RST}"
                  f"{o['type']:<22}"
                  f"{size_str:<8}"
                  f"{trigger:<12.2f}"
                  f"{DIM}{o['status']}{RST}")

    if errors:
        print()
        for e in errors:
            warn(e)

def parse_args():
    args = sys.argv[1:]

    if not args:
        print(USAGE)
        sys.exit(0)

    cmd = args[0].lower()

    if cmd == 'positions':
        refresh = '--refresh' in args
        return ('positions', refresh)
    if cmd == 'orders':    return ('orders',)
    if cmd == 'balance':   return ('balance',)
    if cmd == 'flatten':   return ('flatten',)

    if cmd not in ('buy', 'sell'):
        die(f"Unknown command '{cmd}'. Run 'python copycat.py' for usage.")

    if len(args) < 2:
        die(f"Missing order type. Usage: python copycat.py {cmd} market|limit ...")

    order_type = args[1].lower()
    if order_type not in ('market', 'limit'):
        die(f"Order type must be 'market' or 'limit', got '{order_type}'")

    # Extract -tp
    if '-tp' not in args:
        die("Missing -tp <takeProfitPrice>")
    tp_idx = args.index('-tp')
    if tp_idx + 1 >= len(args):
        die("-tp requires a price value")
    try:
        tp = float(args[tp_idx + 1])
    except ValueError:
        die(f"Invalid TP price: '{args[tp_idx + 1]}'")

    # Args between order_type and -tp
    between = args[2:tp_idx]

    if order_type == 'market':
        # buy market -sp <phemex_size> -sx <xltrade_size> -tp <tp>
        if '-sp' not in args:
            die(f"Usage: python copycat.py {cmd} market -sp <phemex_size> -sx <xltrade_size> -tp <tp>")
        if '-sx' not in args:
            die(f"Usage: python copycat.py {cmd} market -sp <phemex_size> -sx <xltrade_size> -tp <tp>")
        sp_idx = args.index('-sp')
        sx_idx = args.index('-sx')
        if sp_idx + 1 >= len(args): die("-sp requires a size value")
        if sx_idx + 1 >= len(args): die("-sx requires a size value")
        try:
            phemex_size  = float(args[sp_idx + 1])
            xltrade_size = float(args[sx_idx + 1])
        except ValueError:
            die("Invalid -sp or -sx value")
        return ('trade', cmd, 'market', None, phemex_size, xltrade_size, tp)

    else:
        # buy limit -p <price> -sp <phemex_size> -sx <xltrade_size> -tp <tp>
        if '-p' not in args:
            die(f"Limit orders require -p <price>.")
        if '-sp' not in args:
            die(f"Limit orders require -sp <phemex_size>.")
        if '-sx' not in args:
            die(f"Limit orders require -sx <xltrade_size>.")
        p_idx  = args.index('-p')
        sp_idx = args.index('-sp')
        sx_idx = args.index('-sx')
        if p_idx  + 1 >= len(args): die("-p requires a price value")
        if sp_idx + 1 >= len(args): die("-sp requires a size value")
        if sx_idx + 1 >= len(args): die("-sx requires a size value")
        try:
            price        = float(args[p_idx  + 1])
            phemex_size  = float(args[sp_idx + 1])
            xltrade_size = float(args[sx_idx + 1])
        except ValueError:
            die("Invalid -p, -sp, or -sx value")
        return ('trade', cmd, 'limit', price, phemex_size, xltrade_size, tp)


def main():
    parsed = parse_args()
    cmd = parsed[0]

    if cmd == 'positions':
        _, refresh = parsed
        try:
            asyncio.run(cmd_positions(refresh=refresh))
        except KeyboardInterrupt:
            print(f"\n{DIM}Stopped.{RST}")
    elif cmd == 'orders':  asyncio.run(cmd_orders())
    elif cmd == 'balance': asyncio.run(cmd_balance())
    elif cmd == 'flatten': asyncio.run(cmd_flatten())
    elif cmd == 'trade':
        _, direction, order_type, price, phemex_size, xltrade_size, tp = parsed
        asyncio.run(cmd_trade(direction, order_type, price, phemex_size, xltrade_size, tp))

    print()

if __name__ == '__main__':
    main()
