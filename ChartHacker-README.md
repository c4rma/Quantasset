# ChartHacker — Quantasset Terminal Chart

A TradingView-style terminal charting tool for ETH and BTC perpetual futures, built in Python with flicker-free curses rendering. Supports Phemex (authenticated) and Kraken (public) feeds with live WebSocket updates.

---

## Quick Start

```bash
# Install dependencies (auto-installs on first run)
python chart.py
```

Create a `.env` file in the same directory for Phemex authentication:

```
PHEMEX_API_KEY=your_key_here
PHEMEX_API_SECRET=your_secret_here
```

Without credentials, the chart runs in public/unauthenticated mode (rate limits may apply).

---

## Assets & Feeds

| Key | Action |
|-----|--------|
| `E` | Switch to ETH/USDT |
| `B` | Switch to BTC/USDT |
| `F` | Cycle feed: PHEMEX ↔ KRAKEN |
| `I` | Cycle interval: 1m → 3m → 15m → 1H → 4H → 1D |

**Phemex** is the default feed and requires IP whitelisting in your Phemex API settings. **Kraken** is fully public and works from any IP (useful for Termux/mobile setups).

---

## Navigation

| Key | Action |
|-----|--------|
| `←` / `→` | Move cursor 1 candle |
| `[` / `]` | Move cursor 10 candles |
| `{` / `}` | Move cursor 50 candles |
| `G` | Jump to date/time (enter `YYYY-MM-DD HH:MM` or `HH:MM`) |
| `Esc` | Snap back to live edge / close help overlay |

When the cursor is active, the header shows OHLCV data for the selected candle. Panning left near the oldest loaded candle automatically fetches more history (up to 3,000 candles).

---

## Chart Display

| Key | Action |
|-----|--------|
| `L` | Toggle chart mode: CANDLE ↔ LINE |
| `C` | Toggle color scheme: B/W (white bull, blue bear) ↔ R/G (red/green) |
| `P` | Screenshot — saves to `screenshots/quantasset_YYYYMMDD_HHMMSS.txt` |
| `H` / `?` | Toggle help overlay |
| `Q` | Quit |

---

## Indicators

### VWAP — `W`

Session-anchored Volume Weighted Average Price with standard deviation bands.

- Anchor adapts to interval: **daily** (19:00 CT) for 1m/3m/15m/1H, **weekly** (Sunday 19:00 CT) for 4H, **monthly** (1st of month) for 1D
- **White line** — VWAP
- **Cyan shading** — ±0.5σ band
- **Yellow lines** — ±2σ
- **Dim yellow lines** — ±2.5σ
- Previous session shown dim; current session full brightness
- Right axis labels: `VW`, `.5s`, `2s`, `2.5s`

### Volume Profile — `V`

Per-session volume distribution histogram drawn on the left side of the chart.

- **POC** (Point of Control) — brightest level, yellow label
- **VAH/VAL** (Value Area High/Low) — magenta lines with virgin extensions into the current session until touched
- Historical sessions shown as dim dots; previous session as dim lines; current session full brightness
- Anchor follows the same weekly/monthly logic as VWAP on higher timeframes

### Big Trade Detector — `T`

Detects anomalous buy/sell volume using intrabar intensity z-scores (matches TV "Big Trades Detector" settings: lookback 10, sigma 3.0).

- **Buy intensity** = `(close - low) / range × volume`
- **Sell intensity** = `(high - close) / range × volume`
- Signals placed outside the candle wick (buys below low, sells above high)
- **Cyan blocks** = buy anomalies, **Magenta blocks** = sell anomalies

| Tier | Threshold | Display |
|------|-----------|---------|
| T1 | > 3σ | 1×1 reversed block |
| T2 | > 4.5σ | 1×2 reversed block |
| T3 | > 6σ | 3×2 reversed block |

A 1-candle cooldown prevents duplicate signals on consecutive candles.

### Sessions Indicator — `S`

Draws colored border rectangles around named trading sessions (all times CT):

| Session | Time | Color |
|---------|------|-------|
| NDO | 00:00 – 03:30 | Blue |
| Morning | 08:30 – 10:30 | Cyan |
| Exclusion | 09:00 – 10:00 (Wed/Thu only) | Red |
| Lunchtime | 11:30 – 13:30 | Yellow |
| Power Hour | 14:00 – 15:00 | Magenta |
| EOD/EEOD | 18:30 – 23:59 | Green |

Each rectangle is labeled at its top-left corner with the session name.

```
Example — Morning session border:
  Morn─────────────────────
  │   candles here        │
  └───────────────────────┘
```

---

## Alerts — `A`

Opens the non-blocking alert list overlay (chart stays live behind it).

### Creating an Alert — `N` (when list is open)

1. Select condition with `↑` / `↓`, confirm with `Enter`
2. Enter price value (defaults to current live price)
3. Enter alert name
4. Enter alert message
5. Toggle sound on/off with `←` / `→`

**Conditions:**
- Price crosses UP through value
- Price crosses DOWN through value

Alerts evaluate every 100ms against `last_price` (live WebSocket tick) — they fire the instant the threshold is crossed, not on candle close.

**Alerts fire once and then delete themselves.** The trigger remains in the history log.

### Alert List Controls

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate alert list |
| `N` | Create new alert |
| `D` | Delete selected alert |
| `E` | Edit selected alert (modify condition, price, message) |
| `A` | Close alert list |

### Alert Popup

When an alert fires, a banner appears on the chart:
```
 *** ALERT: My Alert @ 14:32:01 ***
 Price crosses DOWN through value 2350.00
 [A] Alert list  [.] Dismiss
```

Press `.` to dismiss the popup. The triggered alert stays in the history log inside `[A]`.

---

## Economic Calendar — `K`

Fetches today's US-only economic events from Investing.com. Times displayed in CT.

```
 Economic Calendar — US Events  [14:30 CT]  [K] close  [R] refresh  [P] screenshot
 Impact filter:  [x] *  [1]    [x] **  [2]    [x] ***  [3]    (toggle with number keys)
 Time    Imp   Event                                  Actual     Fcst       Prev
 ▶ Next: Fed Chair Powell Speaks  in 01:28:14
 07:30   *     NY Empire State Manufacturing Index    11.00      0.30       -0.20
 09:30   ***   Crude Oil Inventories                  -0.913M    2.100M     3.081M
 13:00   **    Beige Book
```

### Controls

| Key | Action |
|-----|--------|
| `1` / `2` / `3` | Toggle * / ** / *** impact filter |
| `R` | Manual refresh |
| `P` | Screenshot |
| `K` or `Esc` | Close calendar |

**Color coding:** `***` events in red, `**` in yellow, `*` in white. Past events dimmed. Next upcoming event highlighted in gold with a live countdown.

Data auto-refreshes every 60 seconds while the calendar is open.

---

## Global / Macro Mode — `M`

Comparative performance chart showing all 8 assets normalized to 0% from market open (00:00 CT). Each asset is expressed as `(price / open_price - 1) × 100%`.

```
  ETH   +8.64% ($2,299.30)
  BTC   +5.31% ($84,200.00)
  NAS100 +1.27% ($18,234.56)
  ...
```

### Assets

| Asset | Source | Color |
|-------|--------|-------|
| BTC | Phemex / Kraken | Yellow |
| ETH | Phemex / Kraken | Cyan |
| XAUUSD (Gold) | Yahoo Finance `GC=F` | Gold |
| USDJPY | Yahoo Finance `JPY=X` | Red |
| USOIL (Crude) | Yahoo Finance `CL=F` | Magenta |
| SPX500 | Yahoo Finance `^GSPC` | Green |
| NAS100 | Yahoo Finance `^NDX` | Blue |
| DXY | Yahoo Finance `DX-Y.NYB` | White |

The Asia session (00:00–02:00 CT) is shaded. Asset labels are placed on the right axis at the position of each line's current value, with collision resolution so labels don't overlap.

### Controls

| Key | Action |
|-----|--------|
| `M` | Enter / exit Global mode |
| `R` | Manual refresh |
| `P` | Screenshot |

Data auto-refreshes every 30 seconds. All 8 assets fetch in parallel (~5–10 seconds on entry).

---

## Key Bindings — Full Reference

```
ASSETS & FEED
  E         Switch to ETH/USDT
  B         Switch to BTC/USDT
  F         Cycle feed (PHEMEX ↔ KRAKEN)
  I         Cycle interval (1m → 3m → 15m → 1H → 4H → 1D)

NAVIGATION
  ← →       Cursor ±1 candle
  [ ]       Cursor ±10 candles
  { }       Cursor ±50 candles
  G         Jump to date/time
  Esc       Snap to live edge / close help

CHART DISPLAY
  L         Toggle CANDLE ↔ LINE
  C         Toggle B/W ↔ R/G color scheme
  W         Toggle VWAP + SD bands
  V         Toggle Volume Profile
  T         Toggle Big Trade Detector
  S         Toggle Sessions indicator
  P         Screenshot
  H / ?     Help overlay
  Q         Quit

ALERTS
  A         Open/close alert list overlay
  N         New alert (when list open)
  D         Delete selected alert (when list open)
  E         Edit selected alert (when list open)
  ↑ ↓       Navigate alert list
  .         Dismiss alert popup

ECONOMIC CALENDAR
  K         Open/close calendar
  R         Refresh calendar (when open)
  P         Screenshot (when open)
  1 / 2 / 3 Toggle * / ** / *** impact filter
  Esc       Close calendar

GLOBAL / MACRO MODE
  M         Toggle global mode
  R         Refresh data (when in global mode)
  P         Screenshot
```

---

## Architecture Notes

- **Double-buffer renderer** — `DoubleBuffer` computes a character-level diff between the previous and current frame, only writing changed cells to the terminal. This eliminates flicker entirely.
- **WebSocket feeds** — live candle updates arrive via WS and update `state.live` in-place. The draw loop runs at ~25fps independent of the WS thread.
- **Alert monitor** — a dedicated daemon thread polls `state.last_price` every 100ms for sub-second alert latency.
- **Bridge compatibility** — all Phemex API calls can be proxied through a Windows `bridge.py` if running on Termux (Android) where Phemex IP whitelisting prevents direct API access.

---

## File Layout

```
chart.py          Main script — run this
.env              PHEMEX_API_KEY and PHEMEX_API_SECRET
screenshots/      Auto-created; stores [P] screenshots as .txt files
```
