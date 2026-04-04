#!/usr/bin/env python3
"""
Quantasset — Deribit 0DTE Options Chain
Curses-based terminal viewer with in-place cell updates (no flicker).
Usage: python deribit_chain.py [ETH|BTC] [-s]
  -s   Simple mode: Strike, Vol, OI, Mark only
"""

import sys
import time
import threading

# Windows compatibility — install windows-curses if _curses is missing
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
    import requests
except ModuleNotFoundError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"], stdout=subprocess.DEVNULL)
    import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── ARG PARSING ───────────────────────────────────────────────────────────────
args     = sys.argv[1:]
SIMPLE   = "-s" in args
args     = [a for a in args if a != "-s"]
CURRENCY = args[0].upper() if args else "ETH"

import os
import hmac
import hashlib

BASE_URL    = "https://www.deribit.com/api/v2"
REFRESH_SEC = 10
MAX_STRIKES = 36   # rows centred around ATM

# ── PHEMEX CONFIG ─────────────────────────────────────────────────────────────
# Reads from environment variables or a .env file in the same directory.
# Set PHEMEX_API_KEY and PHEMEX_API_SECRET in your environment,
# or create a .env file alongside this script with those two lines.
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

_load_env()
PHEMEX_API_KEY    = os.environ.get("PHEMEX_API_KEY", "")
PHEMEX_API_SECRET = os.environ.get("PHEMEX_API_SECRET", "")
PHEMEX_BASE_URL   = "https://api.phemex.com"
BRIDGE_URL        = os.environ.get("BRIDGE_URL", "").rstrip("/")
BRIDGE_TOKEN      = os.environ.get("BRIDGE_TOKEN", "")
USE_BRIDGE        = bool(BRIDGE_URL) and sys.platform != "win32"

# ── COLUMN LAYOUT ─────────────────────────────────────────────────────────────
# (label, width, justify)  justify: L=left R=right C=center
if SIMPLE:
    CALL_COLS = [
        ("Chg",     8, "R"),
        ("Mark",   11, "R"),
        ("OI",     10, "R"),
        ("Vol",    10, "R"),
    ]
    STRIKE_COL = ("Strike", 14, "C")
    PUT_COLS   = [
        ("Vol",    10, "L"),
        ("OI",     10, "L"),
        ("Mark",   11, "L"),
        ("Chg",     8, "L"),
    ]
else:
    CALL_COLS = [
        ("Theta",  11, "R"),
        ("Gamma",  10, "R"),
        ("Delta",   9, "R"),
        ("Chg",     8, "R"),
        ("Mark",   11, "R"),
        ("OI",     10, "R"),
        ("Vol",    10, "R"),
    ]
    STRIKE_COL = ("Strike", 14, "C")
    PUT_COLS   = [
        ("Vol",    10, "L"),
        ("OI",     10, "L"),
        ("Mark",   11, "L"),
        ("Chg",     8, "L"),
        ("Delta",   9, "L"),
        ("Gamma",  10, "L"),
        ("Theta",  11, "L"),
    ]

ALL_COLS = CALL_COLS + [STRIKE_COL] + PUT_COLS
TOTAL_W  = sum(w for _, w, _ in ALL_COLS)

# Column index helpers (derived so they're always correct for both modes)
N_CALL        = len(CALL_COLS)
IDX_STRIKE    = N_CALL
IDX_CALL_MARK = next(i for i, (l,_,_) in enumerate(CALL_COLS) if l == "Mark")
IDX_CALL_CHG  = next(i for i, (l,_,_) in enumerate(CALL_COLS) if l == "Chg")
IDX_CALL_OI   = next(i for i, (l,_,_) in enumerate(CALL_COLS) if l == "OI")
IDX_CALL_VOL  = next(i for i, (l,_,_) in enumerate(CALL_COLS) if l == "Vol")
IDX_PUT_VOL   = IDX_STRIKE + 1 + next(i for i, (l,_,_) in enumerate(PUT_COLS) if l == "Vol")
IDX_PUT_OI    = IDX_STRIKE + 1 + next(i for i, (l,_,_) in enumerate(PUT_COLS) if l == "OI")
IDX_PUT_MARK  = IDX_STRIKE + 1 + next(i for i, (l,_,_) in enumerate(PUT_COLS) if l == "Mark")
IDX_PUT_CHG   = IDX_STRIKE + 1 + next(i for i, (l,_,_) in enumerate(PUT_COLS) if l == "Chg")
IDX_CALL_GREEKS = [i for i, (l,_,_) in enumerate(CALL_COLS) if l in ("Theta","Gamma","Delta")]
IDX_PUT_GREEKS  = [IDX_STRIKE + 1 + i for i, (l,_,_) in enumerate(PUT_COLS) if l in ("Theta","Gamma","Delta")]

# ── API ───────────────────────────────────────────────────────────────────────
def api(path, **params):
    r = requests.get(BASE_URL + path, params=params, timeout=12)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"]["message"])
    return j["result"]

def fetch_ticker(name):
    return name, api("/public/ticker", instrument_name=name)

def _phemex_get(path, query):
    """Signed GET to Phemex — routes through bridge when USE_BRIDGE is set."""
    expiry = str(int(time.time()) + 60)
    msg    = path + query + expiry
    sig    = hmac.new(PHEMEX_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    hdrs   = {
        "x-phemex-access-token":      PHEMEX_API_KEY,
        "x-phemex-request-expiry":    expiry,
        "x-phemex-request-signature": sig,
        "Content-Type":               "application/json",
    }
    if USE_BRIDGE:
        import json as _json, urllib.request as _req
        payload = _json.dumps({
            "method": "GET", "path": path,
            "query": query, "body": "", "headers": hdrs,
        }).encode()
        req = _req.Request(
            f"{BRIDGE_URL}/phemex", data=payload,
            headers={"Content-Type": "application/json",
                     "X-Bridge-Token": BRIDGE_TOKEN},
            method="POST",
        )
        with _req.urlopen(req, timeout=8) as resp:
            return __import__("json").loads(resp.read())
    else:
        r = requests.get(f"{PHEMEX_BASE_URL}{path}?{query}", headers=hdrs, timeout=6)
        return r.json()

def fetch_perp_mark_price():
    """Fetch current perp mark price from Phemex.
    Tries /md/v3/ticker/24hr first (always has markPrice, no open position needed),
    then falls back to accountPositions. Routes through bridge.py on Termux.
    """
    if not PHEMEX_API_KEY or not PHEMEX_API_SECRET:
        return None, None

    symbol = f"{CURRENCY}USDT"

    # Primary: public ticker — use lastRp (last trade price, always live)
    try:
        data   = _phemex_get("/md/v3/ticker/24hr", f"symbol={symbol}")
        result = data.get("result") or {}
        for field in ("lastRp", "lastPrice", "markPriceRp", "indexPriceRp", "closeRp"):
            val = result.get(field)
            if val:
                price = float(val)
                if 100 < price < 1_000_000:
                    return price, "Phemex"
    except Exception:
        pass

    # Fallback: authenticated positions endpoint
    try:
        data = _phemex_get("/g-accounts/accountPositions", "currency=USDT")
        for pos in (data.get("data") or {}).get("positions", []):
            if pos.get("symbol") != symbol:
                continue
            mp = pos.get("markPriceRp")
            if mp:
                price = float(mp)
                if 100 < price < 1_000_000:
                    return price, "Phemex"
    except Exception:
        pass

    return None, None

# ── FORMAT HELPERS ────────────────────────────────────────────────────────────
def fv(v, d=2, sign=False):
    if v is None:
        return "—"
    fmt = f"{'+' if sign else ''}.{d}f"
    return f"{v:{fmt}}"

def fvol(v):
    if not v:
        return "—"
    return f"{int(v):,}"

def foi(v):
    if not v:
        return "—"
    return f"{int(v):,}"

def fmark(mark_usd):
    if mark_usd is None:
        return "—"
    return f"{mark_usd:,.2f}"

def fpct(mark, last):
    """Return (pct_str, direction) where direction: 1=up -1=down 0=flat"""
    if not mark or not last or last == 0:
        return "", 0
    pct = (mark - last) / last * 100
    arrow = "▲" if pct >= 0 else "▼"
    return f"{arrow}{abs(pct):.1f}%", (1 if pct >= 0 else -1)

def fstrike(strike, atm):
    s = f"${strike:,.0f}"
    if strike == atm:
        return s + " ◆"
    return s

def pad(s, width, just):
    s = str(s)
    if len(s) > width - 1:
        s = s[:width - 1]
    if just == "R":
        return s.rjust(width)
    elif just == "L":
        return s.ljust(width)
    else:
        return s.center(width)

def countdown(ts_ms):
    ms = ts_ms - int(time.time() * 1000)
    if ms <= 0:
        return "EXPIRED"
    h, rem = divmod(ms // 1000, 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

# ── DATA FETCH ────────────────────────────────────────────────────────────────
def fetch_all():
    # ── Step 1: instruments list (needed to know what to fetch) ──────────────
    instruments = api("/public/get_instruments", currency=CURRENCY, kind="option", expired="false")

    now_ms = int(time.time() * 1000)
    by_exp = {}
    for ins in instruments:
        by_exp.setdefault(ins["expiration_timestamp"], []).append(ins)

    target_exp = min((e for e in by_exp if e > now_ms), default=None)
    if not target_exp:
        raise RuntimeError("No active expiry found")

    chain_ins = by_exp[target_exp]

    # ── Step 2: fire Phemex, Deribit index, and ALL tickers concurrently ─────
    with ThreadPoolExecutor(max_workers=40) as ex:
        fut_index  = ex.submit(api, "/public/get_index_price", index_name=f"{CURRENCY.lower()}_usd")
        fut_perp   = ex.submit(fetch_perp_mark_price)
        ticker_futs = {ex.submit(fetch_ticker, ins["instrument_name"]): ins for ins in chain_ins}

        idx_data    = fut_index.result()
        index_price = idx_data["index_price"]
        perp_price, perp_source = fut_perp.result() if fut_perp else (None, None)
        atm_ref_price = perp_price if perp_price else index_price

        tickers = {}
        for fut in as_completed(ticker_futs):
            try:
                name, t = fut.result()
                tickers[name] = t
            except Exception:
                pass

    # ── Step 3: organise by strike ───────────────────────────────────────────
    strikes_data = {}
    for ins in chain_ins:
        t = tickers.get(ins["instrument_name"])
        if not t:
            continue
        strikes_data.setdefault(ins["strike"], {})[ins["option_type"]] = t

    atm_strike = min(strikes_data, key=lambda s: abs(s - atm_ref_price))

    atm_iv = 0.0
    atm_call = strikes_data.get(atm_strike, {}).get("call")
    if atm_call:
        atm_iv = atm_call.get("mark_iv") or 0.0

    return {
        "index_price":   index_price,
        "perp_price":    perp_price,
        "perp_source":   perp_source,
        "atm_ref_price": atm_ref_price,
        "expiry_ts":     target_exp,
        "strikes_data":  strikes_data,
        "atm_strike":    atm_strike,
        "atm_iv":        atm_iv,
        "fetched_at":    datetime.now().strftime("%H:%M:%S"),
    }

# ── COLOUR PAIRS ──────────────────────────────────────────────────────────────
P_DEFAULT = 1
P_CALL    = 2
P_PUT     = 3
P_STRIKE  = 4
P_ATM     = 5
P_DIM     = 6
P_YELLOW  = 7
P_GREEN   = 8
P_RED     = 9
P_STATUS  = 10
P_CYAN    = 11
P_MARK    = 12

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    BG = -1
    curses.init_pair(P_DEFAULT, curses.COLOR_WHITE,   BG)
    curses.init_pair(P_CALL,    curses.COLOR_GREEN,   BG)
    curses.init_pair(P_PUT,     curses.COLOR_RED,     BG)
    curses.init_pair(P_STRIKE,  curses.COLOR_WHITE,   BG)
    curses.init_pair(P_ATM,     curses.COLOR_CYAN,    BG)
    curses.init_pair(P_DIM,     curses.COLOR_WHITE,   BG)
    curses.init_pair(P_YELLOW,  curses.COLOR_YELLOW,  BG)
    curses.init_pair(P_GREEN,   curses.COLOR_GREEN,   BG)
    curses.init_pair(P_RED,     curses.COLOR_RED,     BG)
    curses.init_pair(P_STATUS,  curses.COLOR_BLACK,   curses.COLOR_WHITE)
    curses.init_pair(P_CYAN,    curses.COLOR_CYAN,    BG)
    curses.init_pair(P_MARK,    curses.COLOR_YELLOW,  BG)

def cp(pair, bold=False, dim=False):
    a = curses.color_pair(pair)
    if bold: a |= curses.A_BOLD
    if dim:  a |= curses.A_DIM
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

# ── DRAW ──────────────────────────────────────────────────────────────────────
def draw(win, data, status=""):
    h, w = win.getmaxyx()

    index_price  = data["index_price"]
    perp_price   = data.get("perp_price")
    perp_source  = data.get("perp_source") or "Perp"
    atm_ref_price= data.get("atm_ref_price", index_price)
    expiry_ts    = data["expiry_ts"]
    strikes_data = data["strikes_data"]
    atm_strike   = data["atm_strike"]
    atm_iv       = data["atm_iv"]
    fetched_at   = data["fetched_at"]
    ttl          = countdown(expiry_ts)
    exp_dt       = datetime.fromtimestamp(expiry_ts / 1000, tz=timezone.utc)
    exp_str      = exp_dt.strftime("%d %b %Y %H:%M UTC")

    # ── Row 0: header bar ────────────────────────────────────────────────────
    win.move(0, 0)
    win.clrtoeol()
    start_x = max(0, (w - TOTAL_W) // 2)
    x = start_x

    if SIMPLE:
        pieces = [
            ("QUANTASSET",                                      cp(P_CYAN, bold=True)),
            ("  │  ",                                           cp(P_DIM, dim=True)),
            (f"{CURRENCY}-0DTE",                                cp(P_DEFAULT, bold=True)),
            ("  Perp Mk ",                                      cp(P_DIM, dim=True)),
            (f"${perp_price:,.2f}" if perp_price else "—",  cp(P_YELLOW, bold=True)),
            ("  IV ",                                           cp(P_DIM, dim=True)),
            (f"{atm_iv:.1f}%",                                  cp(P_YELLOW, bold=True)),
            ("  ",                                              cp(P_DIM, dim=True)),
            (ttl,                                               cp(P_YELLOW, bold=True)),
            (f"  {fetched_at}",                                 cp(P_DIM, dim=True)),
        ]
    else:
        pieces = [
            ("QUANTASSET",                                      cp(P_CYAN, bold=True)),
            ("  │  ",                                           cp(P_DIM, dim=True)),
            ("Instrument ",                                     cp(P_DIM, dim=True)),
            (f"{CURRENCY}-0DTE",                                cp(P_DEFAULT, bold=True)),
            ("  Perp Mk ",                                      cp(P_DIM, dim=True)),
            (f"${perp_price:,.2f}" if perp_price else "—",  cp(P_YELLOW, bold=True)),
            ("  Deribit Index ",                                cp(P_DIM, dim=True)),
            (f"${index_price:,.2f}",                            cp(P_DEFAULT)),
            ("  ATM IV ",                                       cp(P_DIM, dim=True)),
            (f"{atm_iv:.1f}%",                                  cp(P_YELLOW, bold=True)),
            ("  Expiry ",                                       cp(P_DIM, dim=True)),
            (exp_str,                                           cp(P_DEFAULT)),
            ("  Time Left ",                                    cp(P_DIM, dim=True)),
            (ttl,                                               cp(P_YELLOW, bold=True)),
            (f"  Updated {fetched_at}",                         cp(P_DIM, dim=True)),
        ]
    for text, attr in pieces:
        safe_add(win, 0, x, text, attr)
        x += len(text)

    # ── Row 1: spacer ─────────────────────────────────────────────────────────
    win.move(1, 0)
    win.clrtoeol()

    # ── Row 2: column headers ─────────────────────────────────────────────────
    win.move(2, 0)
    win.clrtoeol()
    cx = start_x
    for i, (label, width, just) in enumerate(ALL_COLS):
        is_call   = i < len(CALL_COLS)
        is_put    = i >= len(CALL_COLS) + 1
        attr = (cp(P_CALL, bold=True) if is_call
                else cp(P_PUT, bold=True) if is_put
                else cp(P_DEFAULT, bold=True))
        safe_add(win, 2, cx, pad(label, width, just), attr)
        cx += width

    # ── Row 3: section divider ────────────────────────────────────────────────
    call_w = sum(wd for _, wd, _ in CALL_COLS)
    str_w  = STRIKE_COL[1]
    put_w  = sum(wd for _, wd, _ in PUT_COLS)
    win.move(3, 0)
    win.clrtoeol()
    safe_add(win, 3, start_x,              "─" * call_w,                     cp(P_CALL))
    safe_add(win, 3, start_x + call_w,     "─" * str_w,                      cp(P_DIM, dim=True))
    safe_add(win, 3, start_x + call_w + str_w, "─" * put_w,                  cp(P_PUT))
    safe_add(win, 3, start_x,             "── CALLS ",                        cp(P_CALL, bold=True))
    puts_lbl = " PUTS ──"
    safe_add(win, 3, start_x + call_w + str_w + put_w - len(puts_lbl), puts_lbl, cp(P_PUT, bold=True))

    # ── Data rows ─────────────────────────────────────────────────────────────
    sorted_strikes = sorted(strikes_data.keys())
    if atm_strike in sorted_strikes:
        ai   = sorted_strikes.index(atm_strike)
        half = MAX_STRIKES // 2
        sorted_strikes = sorted_strikes[max(0, ai - half): min(len(sorted_strikes), ai + half)]

    data_row_start = 4
    # Erase any leftover rows from previous render
    for ri in range(MAX_STRIKES):
        row = data_row_start + ri
        if row < h - 1:
            win.move(row, 0)
            win.clrtoeol()

    for ri, strike in enumerate(sorted_strikes):
        row = data_row_start + ri
        if row >= h - 1:
            break

        d      = strikes_data[strike]
        c      = d.get("call")
        p      = d.get("put")
        is_atm = (strike == atm_strike)

        cg = c.get("greeks", {}) if c else {}
        pg = p.get("greeks", {}) if p else {}

        c_mark_usd = (c.get("mark_price") or 0) * index_price if c else None
        p_mark_usd = (p.get("mark_price") or 0) * index_price if p else None
        c_last_usd = (c.get("last_price") or 0) * index_price if c and c.get("last_price") else None
        p_last_usd = (p.get("last_price") or 0) * index_price if p and p.get("last_price") else None

        c_pct_str, c_dir = fpct(c_mark_usd, c_last_usd)
        p_pct_str, p_dir = fpct(p_mark_usd, p_last_usd)

        # Build cell strings
        # Mark columns get "price pct%" packed into the width
        c_mark_str = fmark(c_mark_usd)
        p_mark_str = fmark(p_mark_usd)
        c_mark_full = f"{c_mark_str} {c_pct_str}" if c_pct_str else c_mark_str
        p_mark_full = f"{p_mark_str} {p_pct_str}" if p_pct_str else p_mark_str

        # Build cell values in full-mode order, then select the ones we need
        c_vol_raw = c.get("stats", {}).get("volume") if c else None
        p_vol_raw = p.get("stats", {}).get("volume") if p else None

        # Full 15-cell list — Theta Gamma Delta Chg Mark OI Vol | Strike | Vol OI Mark Chg Delta Gamma Theta
        full_cells = [
            fv(cg.get("theta"), 4),            # 0  call theta
            fv(cg.get("gamma"), 5),            # 1  call gamma
            fv(cg.get("delta"), 3, sign=True), # 2  call delta
            c_pct_str,                          # 3  call chg
            fmark(c_mark_usd),                  # 4  call mark (price only)
            foi(c.get("open_interest") if c else None),  # 5  call OI
            fvol(c_vol_raw),                    # 6  call vol
            fstrike(strike, atm_strike),        # 7  strike
            fvol(p_vol_raw),                    # 8  put vol
            foi(p.get("open_interest") if p else None),  # 9  put OI
            fmark(p_mark_usd),                  # 10 put mark (price only)
            p_pct_str,                          # 11 put chg
            fv(pg.get("delta"), 3, sign=True),  # 12 put delta
            fv(pg.get("gamma"), 5),             # 13 put gamma
            fv(pg.get("theta"), 4),             # 14 put theta
        ]

        # Map label → full_cells index
        call_label_to_full = {"Theta":0,"Gamma":1,"Delta":2,"Chg":3,"Mark":4,"OI":5,"Vol":6}
        put_label_to_full  = {"Vol":8,"OI":9,"Mark":10,"Chg":11,"Delta":12,"Gamma":13,"Theta":14}

        cells = []
        for label, _, _ in CALL_COLS:
            cells.append(full_cells[call_label_to_full[label]])
        cells.append(full_cells[7])  # strike
        for label, _, _ in PUT_COLS:
            cells.append(full_cells[put_label_to_full[label]])

        # Vol colour: green = higher side, red = lower side
        # If only one side has volume, that side is green
        cv_raw = c_vol_raw or 0
        pv_raw = p_vol_raw or 0
        if cv_raw > 0 and pv_raw > 0:
            call_vol_attr = cp(P_GREEN) if cv_raw >= pv_raw else cp(P_RED)
            put_vol_attr  = cp(P_GREEN) if pv_raw >= cv_raw else cp(P_RED)
        elif cv_raw > 0:
            call_vol_attr = cp(P_GREEN)
            put_vol_attr  = cp(P_DIM, dim=True)
        elif pv_raw > 0:
            call_vol_attr = cp(P_DIM, dim=True)
            put_vol_attr  = cp(P_GREEN)
        else:
            call_vol_attr = cp(P_DIM, dim=True)
            put_vol_attr  = cp(P_DIM, dim=True)

        cx = start_x
        for col_i, ((label, width, just), cell) in enumerate(zip(ALL_COLS, cells)):
            text = pad(cell, width, just)

            if col_i == IDX_STRIKE:
                attr = cp(P_ATM, bold=True) if is_atm else cp(P_STRIKE)
                safe_add(win, row, cx, text, attr)

            elif col_i == IDX_CALL_MARK:
                safe_add(win, row, cx, pad(cell, width, just), cp(P_MARK))

            elif col_i == IDX_CALL_CHG:
                if cell:
                    attr = cp(P_GREEN) if c_dir > 0 else cp(P_RED)
                else:
                    attr = cp(P_DIM, dim=True)
                safe_add(win, row, cx, pad(cell or "—", width, just), attr)

            elif col_i == IDX_PUT_MARK:
                safe_add(win, row, cx, pad(cell, width, just), cp(P_MARK))

            elif col_i == IDX_PUT_CHG:
                if cell:
                    attr = cp(P_GREEN) if p_dir > 0 else cp(P_RED)
                else:
                    attr = cp(P_DIM, dim=True)
                safe_add(win, row, cx, pad(cell or "—", width, just), attr)

            elif col_i in IDX_CALL_GREEKS:
                attr = cp(P_DIM, dim=True) if cell == "—" else cp(P_CALL)
                safe_add(win, row, cx, text, attr)

            elif col_i in IDX_PUT_GREEKS:
                attr = cp(P_DIM, dim=True) if cell == "—" else cp(P_PUT)
                safe_add(win, row, cx, text, attr)

            elif col_i in (IDX_CALL_OI, IDX_PUT_OI):
                attr = cp(P_DIM, dim=True) if cell == "—" else cp(P_DEFAULT)
                safe_add(win, row, cx, text, attr)

            elif col_i == IDX_CALL_VOL:
                safe_add(win, row, cx, text, call_vol_attr)

            elif col_i == IDX_PUT_VOL:
                safe_add(win, row, cx, text, put_vol_attr)

            else:
                safe_add(win, row, cx, text, cp(P_DEFAULT))

            cx += width

    # ── Bottom status bar ─────────────────────────────────────────────────────
    bot = h - 1
    mode_tag = "SIMPLE" if SIMPLE else "FULL"
    hint = f" q=quit  r=refresh  [{CURRENCY}]  [{mode_tag}]  {status}"
    safe_add(win, bot, 0, hint.ljust(w - 1)[:w - 1], cp(P_STATUS))

    win.noutrefresh()

# ── CURSES MAIN ───────────────────────────────────────────────────────────────
def curses_main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(250)
    init_colors()

    data          = None
    error_msg     = ""
    last_fetch    = 0.0   # when the last fetch COMPLETED
    fetch_started = 0.0   # when the last fetch was TRIGGERED
    fetch_dur     = 5.0   # rolling estimate of how long a fetch takes (seconds)
    fetching      = False
    lock          = threading.Lock()

    def do_fetch():
        nonlocal data, error_msg, last_fetch, fetching, fetch_dur, fetch_started
        t0 = time.time()
        try:
            d = fetch_all()
            elapsed = time.time() - t0
            with lock:
                data       = d
                error_msg  = ""
                last_fetch = time.time()
                fetch_dur  = elapsed          # update rolling estimate
        except Exception as e:
            with lock:
                error_msg = str(e)
        finally:
            with lock:
                fetching = False

    def trigger_fetch():
        nonlocal fetching, fetch_started
        with lock:
            if fetching:
                return
            fetching      = True
            fetch_started = time.time()
        threading.Thread(target=do_fetch, daemon=True).start()

    trigger_fetch()

    # Loading splash
    h, w = stdscr.getmaxyx()
    msg  = f"Fetching {CURRENCY} 0DTE chain from Deribit…"
    safe_add(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg, cp(P_CYAN))
    stdscr.refresh()

    while True:
        key = stdscr.getch()
        if key in (ord('q'), ord('Q'), 27):
            break
        if key in (ord('r'), ord('R')):
            trigger_fetch()

        with lock:
            now_fetching  = fetching
            elapsed       = time.time() - last_fetch if last_fetch else 999
            cur_fetch_dur = fetch_dur

        # Pre-emptively trigger so fetch COMPLETES right at REFRESH_SEC.
        # Fire when: time_since_last_fetch >= REFRESH_SEC - estimated_fetch_time
        lead = max(1.0, cur_fetch_dur)
        if elapsed >= (REFRESH_SEC - lead) and not now_fetching and last_fetch > 0:
            trigger_fetch()

        with lock:
            cur_data  = data
            cur_error = error_msg

        if cur_data:
            # Counter shows time until next data lands (REFRESH_SEC from last completion)
            next_in = max(0, int(REFRESH_SEC - elapsed))
            if now_fetching:
                status = "↻ fetching…"
            else:
                status = f"↻ in {next_in}s"
            if cur_error:
                status += f"  ⚠ {cur_error}"
            draw(stdscr, cur_data, status)
        elif cur_error:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            safe_add(stdscr, h // 2,     2, f"Error: {cur_error}", cp(P_RED, bold=True))
            safe_add(stdscr, h // 2 + 1, 2, "Press r to retry, q to quit.", cp(P_DIM, dim=True))

        curses.doupdate()   # single atomic flip — zero flicker

def main():
    if "--debug-phemex" in sys.argv:
        print(f"API key loaded: {'yes' if PHEMEX_API_KEY else 'NO'}")
        print(f"Bridge:         {'yes — ' + BRIDGE_URL if USE_BRIDGE else 'no (direct)'}")
        symbol = f"{CURRENCY}USDT"
        for path, query in [
            ("/md/v3/ticker/24hr",           f"symbol={symbol}"),
            ("/g-accounts/accountPositions", "currency=USDT"),
        ]:
            try:
                data = _phemex_get(path, query)
                print(f"\n── {path} ──")
                if "result" in data:
                    r = data.get("result") or {}
                    for field in ("lastRp", "lastPrice", "markPriceRp", "indexPriceRp", "closeRp"):
                        if field in r:
                            print(f"  {field}: {r[field]}")
                elif "data" in data:
                    for pos in (data.get("data") or {}).get("positions", []):
                        if pos.get("symbol") == symbol:
                            print(f"  symbol={pos['symbol']}  markPriceRp={pos.get('markPriceRp')}")
                            break
            except Exception as e:
                print(f"\n── {path} — error: {e}")
        print(f"\nResolved price: {fetch_perp_mark_price()}")
        return
    try:
        curses.wrapper(curses_main)
    except KeyboardInterrupt:
        pass
    print("Quantasset — exited.")

if __name__ == "__main__":
    main()
