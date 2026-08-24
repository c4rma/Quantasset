# Athena

A single-file, terminal-based automated trading dashboard for ETH and QQQ on Phemex. Athena combines a readiness-gated execution engine, real-time footprint charting, options flow analytics, and a multi-mode data visualization suite into one 23,500-line curses application.

Athena implements the **CCCCWIDE/Blackjack** framework — a rules-based system that requires five independent market conditions (Session, Volatility, PCVR, High-Probability Levels, Targets) to align before order-flow confirmation triggers an entry. Positions are sized via a configurable risk model and managed with automatic stop-loss and split take-profit brackets.

---

## Table of Contents

- [Requirements](#requirements)
- [Configuration](#configuration)
- [Usage](#usage)
- [Startup Flow](#startup-flow)
- [Modes](#modes)
- [Trading Engine](#trading-engine)
- [Blackjack Progression](#blackjack-progression)
- [DVOL Layers](#dvol-layers)
- [Target System](#target-system)
- [Risk Sizing](#risk-sizing)
- [Position Management](#position-management)
- [Server / Client Sync](#server--client-sync)
- [Data Sources](#data-sources)
- [Persistence](#persistence)
- [Key Bindings](#key-bindings)

---

## Requirements

**Python 3.10+** with the following third-party packages (auto-installed at startup if missing):

| Package | Purpose |
|---|---|
| `requests` | Synchronous REST API calls |
| `httpx` | Async HTTP for the trading engine |
| `websocket-client` | WebSocket connections to exchanges |

Standard library modules used: `curses`, `asyncio`, `threading`, `json`, `hmac`, `hashlib`, `socket`, `zoneinfo`, `collections`, `concurrent.futures`, among others.

No `requirements.txt` is provided — Athena self-installs its dependencies on first run.

---

## Configuration

### Environment Variables

Create a `.env` file in the same directory as `athena.py`:

```env
# Phemex API (required for trading)
PHEMEX_API_KEY=your_key
PHEMEX_API_SECRET=your_secret

# Alpaca API (required for QQQ live tape)
ALPACA_API_ATHENA_ID=your_alpaca_key
ALPACA_API_SECRET_KEY_ATHENA=your_alpaca_secret

# Login credentials (required)
ATHENA_LOGIN_USER=your_username
ATHENA_SYNC_SECRET=your_password

# WireGuard VPN address (optional, for remote sync)
ATHENA_VPN_IP=10.0.0.1
```

### Instrument Defaults

| Parameter | ETH | QQQ |
|---|---|---|
| Phemex symbol | `ETHUSDT` | `QQQUSDT` |
| Leverage | 100x | 10x |
| Stop-loss distance | $10.00 | $1.00 |
| Fee model | 0.06% of notional | $0.85 per unit |

SL distance and fee rate are adjustable at runtime via `[W]` and `[E]`.

---

## Usage

```
python3 athena.py [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--interval SEC` | `2` | Engine poll cadence in seconds (clamped to 1–3) |
| `--pct FLOAT` | `1.0` | % of balance to risk per trade |
| `--dry-run` | off | Paper-trade via SimAccount (no real orders) |
| `--no-session` | off | 24-hour mode — removes Session from the readiness gate |
| `--reset-sim` | off | Wipe paper account to starting balance on launch |
| `--sim-balance FLOAT` | `10000` | Paper-account balance to seed or reset to |
| `--backfill-hours FLOAT` | auto | Footprint backfill depth in hours |
| `--backfill-budget-secs FLOAT` | `30` | Max seconds to spend on initial REST backfill |

**Examples:**

```bash
# Paper trading, 24-hour mode
python3 athena.py --dry-run --no-session

# Live trading, 1% risk, 1-second interval
python3 athena.py --interval 1 --pct 1.0

# Reset paper account to $5,000
python3 athena.py --dry-run --reset-sim --sim-balance 5000
```

---

## Startup Flow

1. **Login** — Username and password prompt. Credentials are compared against `ATHENA_LOGIN_USER` and `ATHENA_SYNC_SECRET` via constant-time HMAC comparison.

2. **Mode Select** — Choose a runtime mode:
   - **Server** — Runs the full trading engine and streams state to connected Clients over the LAN and/or WireGuard.
   - **Client** — View-only mirror of a running Server. No exchange credentials needed, no trading actions permitted.

3. **Loading Screen** — Displays a checklist of all engine threads as they initialize (WebSocket feeds, GEX engine, CVD backfill, footprint recovery, etc.). Press any key to skip waiting.

4. **Trading Dashboard** — The main view. The engine begins its poll cycle.

---

## Modes

Athena has 10 full-screen modes, cycled with `[M]` (forward) and `[K]` (backward). Two additional overlays (`[D]` Data View, `[L]` Activity Log) are available from any mode.

### Trading (Default)

Split-pane ETH/QQQ footprint chart with a status dashboard. Shows:
- Real-time footprint bars (volume, delta, OHLC profile modes via `[V]`)
- 6-light readiness meter per instrument
- Position PnL, SL/TP levels, margin usage
- Blackjack ladder status
- DVOL layer indicators
- Funding rate and next accrual countdown
- Compact activity log

### Chart

Full-screen candlestick chart ported from charthacker.py. Standalone feed layer supporting any ticker (not just ETH/QQQ) via `[E]` symbol switching.

- Volume Profile, VWAP, session markers
- BT/ST/GEX Flip overlay
- Expected Range bands
- Big Trade Detector
- Horizontal drawing tools and price alerts
- Read-only position/order monitor (plots live Phemex positions on the chart)
- Econ calendar overlay with impact filtering
- Save/load chart state
- Phemex, Kraken, and Yahoo data feeds with interval switching

### GEX

Gamma Exposure visualization with two sub-modes:
- **Dot Map** — Heatmap-style GEX by strike and expiry
- **By Strike** — Bar chart of net gamma per strike

Toggle between views with `[G]`, net/gross with `[N]`.

### Net Drift

Options net premium flow chart. Tracks whether money is flowing into calls or puts across multiple exchanges (Deribit, OKX, Bybit for crypto; CBOE for QQQ).

- Per-asset historical browsing `[H]`
- Filtered/raw toggle `[F]`
- Net volume toggle `[N]`
- Crosshair with OHLC readout `[X]`

### CVD

Cumulative Volume Delta chart built from the existing footprint feed (no dedicated data thread).

- Interval switching `[I]`
- Big Trade Detector overlay `[B]`
- Color scheme toggle `[C]`
- Crosshair `[X]`

### Volatility Drift

DVOL/IV drift chart showing implied volatility trends over time. Same navigation pattern as Net Drift.

### Chain

Options chain viewer pulling from Deribit (ETH) and CBOE (QQQ).

- Simple mode `[Y]` — Strike, Vol, OI, Mark only
- On-demand (starts/stops with mode entry/exit)

### Macro Options Flow

Large-block institutional options flow. Always-on refresh loop with alert muting via `[U]`.

### Markets

Macro market overview dashboard (ported from charthacker.py's Global Mode). Asset prices, indices, sector performance. On-demand.

### Status

Full-screen CCCCWIDE framework readiness display. Scrollable sections:

1. **Session** — Current session state
2. **Volatility** — DVOL (ETH) / VXN (QQQ) with layer classification
3. **PCVR** — Put/Call Volume Ratio with regime determination
4. **High-Probability Levels** — VAH, VAL, POC, VWAP, SD bands, Expected Range, BT/ST, gamma clusters
5. **Targets** — Price-sorted target list (BT/ST, GEX Flip, clusters, ER 100%/150%, Margin Recovered when in position)

### Data View (`[D]` overlay)

Equity curve and detailed trade table. Toggle between sim and real trade sources with `[S]`. Shows entry/exit prices, PnL, TP types, Blackjack sequence labels, and per-trade fee attribution.

### Activity Log (`[L]` overlay)

Scrollable popup of the last 500 console events — entries, fills, closes, errors, SL/TP placements.

---

## Trading Engine

### State Machine

Each instrument (ETH, QQQ) runs an independent engine with four states:

```
WATCHING  →  ARMED  →  PENDING_FILL  →  IN_POSITION  →  (back to WATCHING)
```

- **WATCHING** — Polling the 6-light readiness gate every cycle.
- **ARMED** — All 5 non-flow lights are green. Waiting for footprint confirmation.
- **PENDING_FILL** — Entry order placed. Polling for fill. Limit orders expire after 10 unfilled bars.
- **IN_POSITION** — Managing SL/TP brackets, syncing moving targets, checking for exit conditions.

### Readiness Gate (6 Lights)

All five structural lights must be green before the engine arms:

| Light | Condition |
|---|---|
| **Session** | Inside a defined trading session (skipped with `--no-session`) |
| **Volatility** | DVOL (ETH) or VXN (QQQ) data available |
| **PCVR** | Put/Call Volume Ratio in a directional regime (≤0.98 = Long, ≥1.02 = Short) |
| **HPLs** | At least one high-probability level active (from status.py) |
| **Targets** | At least one valid take-profit target exists |

The sixth light, **Order Flow**, turns green when footprint confirmation fires.

### Footprint Confirmation

A bar confirms a regime when both conditions are met on the new bar vs. the previous bar:
- **POC** moved in the regime's direction (up for long, down for short)
- **Net delta** moved in the regime's direction

### Entry Blackout

No new entries are placed between 19:00–19:30 CT (the daily session boundary).

---

## Blackjack Progression

An optional position-sizing mode toggled with `[B]`. Instead of flat risk-per-trade, Blackjack uses a loss-recovery ladder:

**Loss Steps:** `1R → 1R → 2R → 3R → 5R`

On a loss, the ladder advances one step. At the end (5R), it wraps back to 1R.

On a win, a 2-trade win progression begins: the next trade risks the step-back R-multiple plus the dollar profit from the win. Two consecutive wins fully reset the ladder to 1R.

**Safety Limits:**
- **Daily Loss Limit** — 5 consecutive losses blocks the asset until the next 19:30 CT rollover or a PCVR regime switch.
- **Max Win Limit** — 5 consecutive wins blocks the asset similarly.
- **Home Run Rule** — A 5R trade that closes at 3R+ net profit triggers a full reset.

State is persisted to `blackjack_state.json` and survives restarts.

---

## DVOL Layers

Volatility-adjusted risk scaling (ETH only, based on Deribit DVOL):

| DVOL Range | Layer 1 (Base $ Multiplier) | Layer 2 (R-Multiple Cap) |
|---|---|---|
| ≤ 60 | 100% | 5R |
| ≤ 75 | 75% | 3R |
| ≤ 90 | 50% | 2R |
| > 90 | 25% | 1R |

Layer 1 scales the effective dollar value of 1R. Layer 2 caps the R-multiple actually risked at entry.

---

## Target System

Targets are ordered by type priority for TP leg allocation:

| Priority | Type | Source | Top-Tier | Moves Mid-Trade |
|---|---|---|---|---|
| 1 | BT / ST | Live options chain (status.py) | Yes | Yes |
| 2 | Large Cluster | Live gamma (gex.py) | Yes | Yes |
| 3 | Medium Cluster | Live gamma (gex.py) | No (fallback) | Yes |
| 4 | Margin Recovered | Computed from fill price + leverage | Yes | No (static) |
| 5 | ER 100% | Session open + IV | No (fallback) | No (static) |
| 6 | ER 150% | Session open + IV | No (fallback) | No (static) |
| 7 | GEX Flip | Live gamma (gex.py) | No (fallback) | Yes |

**Margin Recovered** is the price level where the trade's gross PnL equals the margin used to open the position (`fill_price * (1 + 1/leverage)` for longs, `fill_price * (1 - 1/leverage)` for shorts). Only exists while a position is open.

A **fallback** target may justify entry only when no top-tier target exists anywhere in the target list.

When a position is opened, the two nearest valid targets (at least 1R from fill price) receive TP legs, with qty split 50/50. GEX Flip TPs are offset $3 toward price. Moving targets (GEX Flip, Cluster, BT/ST) are resynced every engine cycle.

---

## Risk Sizing

Two sizing modes, selectable via `[P]`:

**Percentage Mode** (`pct`): `trade_risk = balance * (pct / 100)`

**Dollar Mode** (`dollars`): `trade_risk = fixed_dollar_amount`

In Blackjack mode, `trade_risk = risked_R * 1R_dollars` (plus win-progression profit if applicable).

Position size: `qty = trade_risk / SL_distance`

DVOL Layer 1 scales the base dollar value. Layer 2 caps the R-multiple.

---

## Position Management

- **Stop-Loss** — Fixed distance from fill price (ETH: $10, QQQ: $1). Placed immediately on fill. Retried every cycle if placement fails.
- **Take-Profit** — Up to 2 legs, split evenly. Nearest valid targets by distance from fill.
- **TP1 Breakeven Lock** — After TP1 fills, the SL moves to breakeven on the remaining position.
- **PCVR Flip Close** — If PCVR flips to the opposite extreme (long position + PCVR ≥ 1.02, or short + PCVR ≤ 0.98), the position is market-closed immediately.
- **EOD Flatten** — QQQ positions are closed at market close (15:00 CT).
- **Funding Rate** — Phemex funding rates are fetched and displayed. In `--dry-run` mode, funding is accrued to the SimAccount every 8 hours.

---

## Server / Client Sync

Athena supports a two-machine setup for monitoring trades away from the primary terminal.

### Server Mode

Binds to the LAN interface (auto-detected) and optionally a WireGuard VPN address (`ATHENA_VPN_IP`). Never binds `0.0.0.0`. Broadcasts the full application state over a WebSocket on port `8765`:

- Status lines, GEX state, footprint bars
- Position/PnL data, Blackjack state
- DRY_RUN/NO_SESSION/ENABLED flags
- Data view trade history

### Client Mode

Connects to one or more Server addresses (comma-separated, LAN tried first). Pure view-only mirror — no exchange credentials loaded, no trading engine started. All trading-related keys are blocked.

### Authentication

The sync handshake uses HMAC-SHA256 with a challenge-response protocol. The shared secret is `ATHENA_SYNC_SECRET`. Brute-force lockout is enforced after repeated failures.

---

## Data Sources

| Source | Protocol | Data |
|---|---|---|
| **Phemex** | REST + WebSocket | ETHUSDT/QQQUSDT trading, live tape, klines, account, positions, orders, funding rates |
| **Alpaca** | REST + WebSocket | QQQ live tape (IEX), historical bars |
| **Kraken** | REST + WebSocket | ETH/USD live tape, historical candles |
| **Coinbase** | REST + WebSocket | ETH-USD live tape, historical candles |
| **Deribit** | REST | DVOL, ETH options chain, GEX data, Net Drift options tape, PCVR |
| **CBOE** | REST | QQQ options chain, GEX data, VXN, IV |
| **Yahoo Finance** | REST | Spot prices, equity klines, macro data |
| **OKX** | REST | Net Drift options tape (crypto) |
| **Bybit** | REST | Net Drift options tape (crypto, USDC-settled) |

---

## Persistence

| File | Purpose |
|---|---|
| `sim_account.json` | Paper account state (balance, positions, orders) |
| `sim_logs/YYYY/MM/DD/*.jsonl` | Paper trade ledger (one file per day) |
| `athena_logs/YYYY/MM/DD/*.jsonl` | Event log (entries, fills, closes, errors) |
| `blackjack_state.json` | Blackjack ladder position and mode |
| `daily_loss_state.json` | Consecutive loss tracker per asset |
| `max_win_state.json` | Consecutive win tracker per asset |
| `closed_pnl_state.json` | Per-day realized PnL |
| `chart_state.json` | Saved chart mode state (symbol, interval, drawings) |
| `status_<ASSET>.json` | VP/VWAP export for status.py integration |
| `status_<ASSET>_gex.json` | GEX export per asset |
| `data/footprint/YYYY/MM/DD/*.jsonl` | Footprint bar data |
| `drift_logs/YYYY/MM/DD/*.jsonl` | Net Drift history |
| `screenshots/` | Text-based terminal screenshots (`[C]`) |

---

## Key Bindings

### Global (Available in All Modes)

| Key | Action |
|---|---|
| `Q` | Quit |
| `M` | Next mode |
| `K` | Previous mode |
| `D` | Toggle Data View overlay |
| `L` | Toggle Activity Log overlay |
| `C` | Capture screenshot |

### Trading Mode

| Key | Action |
|---|---|
| `A` | Arm/disarm focused instrument |
| `Tab` | Switch focus between ETH/QQQ |
| `Z` | Toggle full/split pane |
| `V` | Cycle footprint profile (volume/delta/OHLC/off) |
| `X` | Toggle crosshair |
| `H` | Hide/show dashboard |
| `Home` | Snap to live edge |
| `Left/Right` | Scroll footprint bars |
| `[` / `]` | Scroll by 10 bars |
| `{` / `}` | Scroll by 50 bars |
| `P` | Set risk per trade (% or $) |
| `W` | Set SL distance |
| `E` | Set fee per unit |
| `T` | Set imbalance ratio |
| `N` | Toggle 24H/session mode |
| `B` | Toggle Blackjack mode |
| `1` | Reset focused asset's Blackjack ladder to 1R |
| `F` | Flatten position |
| `G` | Toggle live/sim mode |
| `R` (double) | Reset paper account (dry-run only) |

### Chart Mode

| Key | Action |
|---|---|
| `E` | Switch symbol (any ticker) |
| `F` | Cycle data feed (Phemex/Kraken/Yahoo) |
| `I` | Change candle interval |
| `V` | Toggle Volume Profile |
| `W` | Toggle VWAP |
| `B` | Toggle Expected Range overlay |
| `;` | Toggle BT/ST/GEX Flip overlay |
| `T` | Toggle Big Trade Detector |
| `S` | Toggle session markers |
| `L` | Toggle candle/line chart mode |
| `\|` | Add horizontal line + alert |
| `\\` | Add position planning tool |
| `A` | Toggle alert list |
| `N` | Add hline/alert (context-dependent) |
| `O` | Edit selected hline/alert |
| `Shift+D` | Delete selected hline/alert |
| `X` | Toggle econ calendar |
| `Y` | Econ calendar yesterday range |
| `1/2/3` | Econ calendar impact filter |
| `U` | Save chart state |
| `J` | Reset vertical offset |
| `G` | Jump to date/time |
| `R` | Refresh data |
| `H` / `?` | Help overlay |

### GEX Mode

| Key | Action |
|---|---|
| `G` | Toggle dot-map / by-strike |
| `N` | Toggle net/gross |
| `Tab` | Switch asset |
| `Arrows` | Pan |
| `[` / `]` | Zoom |

### Net Drift / CVD / Volatility Drift

| Key | Action |
|---|---|
| `Tab` | Switch asset |
| `H` | Historical browsing |
| `I` | Interval |
| `B` | Bar interval / BTD toggle |
| `F` | Filtered/raw toggle |
| `N` | Net volume toggle |
| `C` | Color scheme |
| `X` | Crosshair |
| `Arrows` | Pan / cursor move |

### Chain Mode

| Key | Action |
|---|---|
| `Y` | Simple mode toggle |
| `Tab` | Switch asset |

---

## Architecture

Athena is intentionally a single file. Every mode, data layer, and engine thread lives in `athena.py`. This makes deployment trivial (`scp` one file) and eliminates import/version-mismatch issues across machines.

The application is structured internally as:
- **Data layer** — Fetchers, WebSocket feeds, state classes, and persistence for each mode
- **Engine layer** — `AthenaInstrument` class with async state machine, one instance per asset
- **UI layer** — `draw_*` functions rendering to a `DoubleBuffer` abstraction over curses
- **Sync layer** — WebSocket server/client for Server/Client mode communication

All exchange API calls, trading logic, and position management run in a single `asyncio` event loop on the main thread. Data feeds (WebSockets, REST polls) run in dedicated `threading.Thread` instances. The curses UI renders synchronously on its own refresh cycle.

---

## Disclaimer

This software is provided for educational and research purposes. Automated trading carries significant financial risk. Use at your own discretion and always verify the system's behavior in `--dry-run` mode before enabling live trading.
