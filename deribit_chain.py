#!/usr/bin/env python3
"""
Quantasset — Deribit 0DTE Options Chain
Curses-based terminal viewer with in-place cell updates (no flicker).
Usage: python deribit_chain.py [ETH|BTC]
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
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── CONFIG ────────────────────────────────────────────────────────────────────
CURRENCY    = sys.argv[1].upper() if len(sys.argv) > 1 else "ETH"
BASE_URL    = "https://www.deribit.com/api/v2"
REFRESH_SEC = 10
MAX_STRIKES = 36   # rows centred around ATM

# ── COLUMN LAYOUT ─────────────────────────────────────────────────────────────
# (label, width, justify)  justify: L=left R=right C=center
CALL_COLS = [
    ("Theta",  11, "R"),
    ("Gamma",  10, "R"),
    ("Delta",   9, "R"),
    ("Mark",   16, "R"),
    ("OI",     10, "R"),
    ("Vol",    10, "R"),
]
STRIKE_COL = ("Strike", 14, "C")
PUT_COLS   = [
    ("Vol",    10, "L"),
    ("OI",     10, "L"),
    ("Mark",   16, "L"),
    ("Delta",   9, "L"),
    ("Gamma",  10, "L"),
    ("Theta",  11, "L"),
]

ALL_COLS = CALL_COLS + [STRIKE_COL] + PUT_COLS
TOTAL_W  = sum(w for _, w, _ in ALL_COLS)

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

def fetch_phemex_price():
    """Fetch ETH/USDT perp mark price from Phemex public API."""
    try:
        r = requests.get(
            "https://api.phemex.com/md/v3/ticker/24hr",
            params={"symbol": "ETHUSD"},
            timeout=6,
        )
        j = r.json()
        # Phemex returns markPrice scaled by 10000 for USD-settled perps
        result = j.get("result", {})
        mark = result.get("markPrice")
        if mark:
            return float(mark) / 10000
    except Exception:
        pass
    # Fallback: try the USDT perp endpoint
    try:
        r = requests.get(
            "https://api.phemex.com/md/v3/ticker/24hr",
            params={"symbol": "ETHUSDTPERP"},
            timeout=6,
        )
        j = r.json()
        result = j.get("result", {})
        mark = result.get("markPrice")
        if mark:
            return float(mark) / 10000
    except Exception:
        pass
    return None

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
        fut_phemex = ex.submit(fetch_phemex_price) if CURRENCY == "ETH" else None
        ticker_futs = {ex.submit(fetch_ticker, ins["instrument_name"]): ins for ins in chain_ins}

        idx_data    = fut_index.result()
        index_price = idx_data["index_price"]
        phemex_price = fut_phemex.result() if fut_phemex else None
        atm_ref_price = phemex_price if phemex_price else index_price

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
        "phemex_price":  phemex_price,
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
    phemex_price = data.get("phemex_price")
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
    x = 1
    pieces = [
        ("QUANTASSET",             cp(P_CYAN, bold=True)),
        ("  │  ",                  cp(P_DIM, dim=True)),
        ("Instrument ",            cp(P_DIM, dim=True)),
        (f"{CURRENCY}-0DTE",       cp(P_DEFAULT, bold=True)),
        ("  Phemex ",              cp(P_DIM, dim=True)),
        (f"${phemex_price:,.2f}" if phemex_price else "—",  cp(P_YELLOW, bold=True)),
        ("  Deribit Index ",       cp(P_DIM, dim=True)),
        (f"${index_price:,.2f}",   cp(P_DEFAULT)),
        ("  ATM IV ",              cp(P_DIM, dim=True)),
        (f"{atm_iv:.1f}%",         cp(P_YELLOW, bold=True)),
        ("  Expiry ",              cp(P_DIM, dim=True)),
        (exp_str,                  cp(P_DEFAULT)),
        ("  Time Left ",           cp(P_DIM, dim=True)),
        (ttl,                      cp(P_YELLOW, bold=True)),
        (f"  Updated {fetched_at}",cp(P_DIM, dim=True)),
    ]
    for text, attr in pieces:
        safe_add(win, 0, x, text, attr)
        x += len(text)

    # ── Row 1: spacer ─────────────────────────────────────────────────────────
    win.move(1, 0)
    win.clrtoeol()

    # ── Row 2: column headers ─────────────────────────────────────────────────
    start_x = max(0, (w - TOTAL_W) // 2)
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

        # Raw vol values for comparison
        c_vol_raw = c.get("stats", {}).get("volume") if c else None
        p_vol_raw = p.get("stats", {}).get("volume") if p else None

        cells = [
            fv(cg.get("theta"), 4),           # 0  call theta
            fv(cg.get("gamma"), 5),           # 1  call gamma
            fv(cg.get("delta"), 3, sign=True),# 2  call delta
            c_mark_full,                       # 3  call mark
            foi(c.get("open_interest") if c else None),  # 4 call OI
            fvol(c_vol_raw),                   # 5 call vol
            fstrike(strike, atm_strike),       # 6  strike
            fvol(p_vol_raw),                   # 7 put vol
            foi(p.get("open_interest") if p else None),  # 8 put OI
            p_mark_full,                       # 9  put mark
            fv(pg.get("delta"), 3, sign=True), # 10 put delta
            fv(pg.get("gamma"), 5),            # 11 put gamma
            fv(pg.get("theta"), 4),            # 12 put theta
        ]

        # Vol colour: green = higher side, red = lower side
        cv_raw = c_vol_raw or 0
        pv_raw = p_vol_raw or 0
        if cv_raw > 0 and pv_raw > 0:
            call_vol_attr = cp(P_GREEN) if cv_raw >= pv_raw else cp(P_RED)
            put_vol_attr  = cp(P_GREEN) if pv_raw >= cv_raw else cp(P_RED)
        elif cv_raw > 0:
            call_vol_attr = cp(P_DEFAULT)
            put_vol_attr  = cp(P_DIM, dim=True)
        elif pv_raw > 0:
            call_vol_attr = cp(P_DIM, dim=True)
            put_vol_attr  = cp(P_DEFAULT)
        else:
            call_vol_attr = cp(P_DIM, dim=True)
            put_vol_attr  = cp(P_DIM, dim=True)

        cx = start_x
        for col_i, ((label, width, just), cell) in enumerate(zip(ALL_COLS, cells)):
            text = pad(cell, width, just)

            # ── colour logic ──────────────────────────────────────────────
            if col_i == 6:   # strike
                attr = cp(P_ATM, bold=True) if is_atm else cp(P_STRIKE)
                safe_add(win, row, cx, text, attr)

            elif col_i == 3:   # call mark — price in yellow, pct coloured
                mark_text = pad(c_mark_str, width, just)
                safe_add(win, row, cx, mark_text, cp(P_MARK))
                if c_pct_str:
                    pct_x = cx + len(mark_text.rstrip()) + 1
                    pct_attr = cp(P_GREEN) if c_dir > 0 else cp(P_RED)
                    safe_add(win, row, pct_x, c_pct_str, pct_attr)

            elif col_i == 9:   # put mark
                mark_text = pad(p_mark_str, width, just)
                safe_add(win, row, cx, mark_text, cp(P_MARK))
                if p_pct_str:
                    pct_x = cx + len(mark_text) - len(mark_text.lstrip()) + len(p_mark_str.strip()) + 1
                    pct_attr = cp(P_GREEN) if p_dir > 0 else cp(P_RED)
                    safe_add(win, row, pct_x, p_pct_str, pct_attr)

            elif col_i in (0, 1, 2):   # call greeks
                attr = cp(P_DIM, dim=True) if cell == "—" else cp(P_CALL)
                safe_add(win, row, cx, text, attr)

            elif col_i in (10, 11, 12): # put greeks
                attr = cp(P_DIM, dim=True) if cell == "—" else cp(P_PUT)
                safe_add(win, row, cx, text, attr)

            elif col_i in (4, 8): # OI
                attr = cp(P_DIM, dim=True) if cell == "—" else cp(P_DEFAULT)
                safe_add(win, row, cx, text, attr)

            elif col_i == 5:  # call vol
                safe_add(win, row, cx, text, call_vol_attr)

            elif col_i == 7:  # put vol
                safe_add(win, row, cx, text, put_vol_attr)

            else:
                safe_add(win, row, cx, text, cp(P_DEFAULT))

            cx += width

    # ── Bottom status bar ─────────────────────────────────────────────────────
    bot = h - 1
    hint = f" q=quit  r=refresh  [{CURRENCY}]  {status}"
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
    try:
        curses.wrapper(curses_main)
    except KeyboardInterrupt:
        pass
    print("Quantasset — exited.")

if __name__ == "__main__":
    main()
