# Athena

A single-file, terminal-based automated trading dashboard for ETH and QQQ on Phemex. Athena combines a readiness-gated execution engine, real-time footprint charting, options flow analytics, and a multi-mode data visualization suite into one curses application.

Athena implements the **CCCCWIDE** framework — a rules-based system that requires five independent market conditions (Session, Volatility, PCVR, High-Probability Levels, Targets) to align before order-flow confirmation triggers an entry. Positions are sized via a configurable risk model (Standard or Aggressive), protected by a drawdown de-risking ladder, and managed with automatic stop-loss and split take-profit brackets.

---

## Table of Contents

- [Requirements](#requirements)
- [Configuration](#configuration)
- [Usage](#usage)
- [Startup Flow](#startup-flow)
- [Modes](#modes)
- [Trading Engine](#trading-engine)
- [Sizing Modes](#sizing-modes)
- [Drawdown De-Risking Ladder](#drawdown-de-risking-ladder)
- [DVOL Layer](#dvol-layer)
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

Athena has 8 full-screen modes, cycled with `[M]` (forward) and `[K]` (backward). Two additional overlays (`[D]` Data View, `[L]` Activity Log) are available from any mode. Every mode also has its own `[H]` full-key-reference overlay (scrollable, closes with `[H]`/`Esc`) listing everything that mode's own footer can't fit.

**2026-08-27: Chart mode and the standalone Markets mode were both removed.** Chart mode's own free-symbol candlestick tool (VP, VWAP, BT/ST/GEX-Flip, Expected Range, Big Trade Detector, crosshair, historical trade markers) was fully ported into the Trading dashboard's own embedded OHLC panel (`[V]`-cycled) over the course of this project, at which point Chart mode's separate, independent WebSocket feeds became the sole remaining reason its own signals could ever disagree with the dashboard's — removing it removed that class of bug at the root. Markets (an 8-asset macro overview: BTC/ETH/XAUUSD/USDJPY/USOIL/SPX500/NAS100/DXY) is no longer its own mode either — it's a `[Y]` toggle on the QQQ pane specifically of the Trading dashboard's OHLC view, swapping the candle chart for the overview and back.

### Trading (Default)

Split-pane ETH/QQQ footprint chart with a status dashboard. Shows:
- Real-time footprint bars (volume, delta, OHLC profile modes via `[V]`)
- The OHLC profile mode includes Volume Profile, a developing VWAP+SD band, Big Trade Detector signals, a crosshair (`[X]`), and historical trade markers — full parity with the former standalone Chart mode. Draw order (2026-08-27 user-reported fix): VAH/VAL/POC/target/position level-line labels on the price axis now hold their ground against the generic ±σ band labels when both round to the same row (previously the σ labels always won, silently hiding the more specific one); session boundary markers (NDO/Morn/Lunch/PWR/EOD/etc.) now draw *after* candles so they're never painted over by price action, and their vertical left/right edges are now drawn unconditionally too (they used a blank-cell check that made sense back when this overlay drew *before* candles, but once moved to draw last — same fix — almost every cell was already occupied, so the sides were silently dropping out almost every time; only the horizontal top/bottom dash had already been unconditional). The time axis (2026-08-27 user request, ported from charthacker.py's own SESSIONS block) now also shows a solid reverse-video strip in each active session's own color spanning its column range, with the plain `HH:MM` tick labels punched through on top — same layering charthacker.py uses. Vertical scale (2026-08-27 user-reported fix): the chart still auto-extends its price range to keep Entry/SL/every other open TP leg in view, but no longer for an ER 100%/150% leg specifically — an IV-projected daily move routinely sits tens of dollars from price, and letting it dictate the y-axis crushed the actual recent candles into an unreadable sliver; an ER-type TP still exists and is tracked normally, it just scrolls off-screen like anything else out of the visible window instead of forcing the whole chart to zoom out to reach it
- `[Y]` on the QQQ pane swaps to the Markets macro overview and back
- 6-light readiness meter per instrument
- Position PnL, SL/TP levels, margin usage
- Sizing mode status (Standard/Aggressive) and drawdown de-risking tracker
- Real-time bid/ask/spread next to each asset's price
- DVOL layer indicator
- Funding rate and next accrual countdown
- Compact activity log

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

Large-block institutional options flow. Always-on refresh loop with alert muting via `[U]`. (The Markets macro overview that used to be its own mode after this one is now a `[Y]` toggle on the Trading dashboard's own QQQ pane instead — see Trading, above.)

### Status

Full-screen CCCCWIDE framework readiness display. Scrollable sections:

1. **Session** — Current session state
2. **Volatility** — DVOL (ETH) / VXN (QQQ) with layer classification
3. **PCVR** — Put/Call Volume Ratio with regime determination
4. **High-Probability Levels** — VAH, VAL, POC, VWAP, SD bands, Expected Range, BT/ST, gamma clusters
5. **Targets** — Price-sorted target list (BT/ST, GEX Flip, clusters, ER 100%/150%, Margin Recovered when in position)

### Data View (`[D]` overlay)

Equity curve and detailed trade table. Toggle between sim and real trade sources with `[S]`. Shows entry/exit prices, PnL, TP types, sizing-mode sequence labels (Standard/Aggressive), and per-trade fee attribution.

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

## Sizing Modes

**2026-08-27: replaces the old Blackjack loss-progression ladder entirely.** Two user-selectable sizing modes, cycled with `[0]`, both built on the same base risk-per-trade amount (`[P]`, see Risk Sizing below):

- **Standard** — every trade risks exactly the base amount. No progression.
- **Aggressive ("1R+W")** — a trade taken at base risk that nets a profit arms a **one-shot boost** for the very next trade: that next trade risks base + the previous winner's own dollar PnL. The boosted trade's own result — win or lose — is never itself examined for a further boost; the trade after a boosted trade always reverts to bare base risk, unconditionally. A new boost only ever arms again from some later trade taken at base risk that wins.

**Safety Limits** (independent of sizing mode):
- **Daily Loss Limit** — 5 consecutive losses blocks the asset until the next 19:30 CT rollover or a PCVR regime switch.
- **Max Win Limit** — 5 consecutive wins blocks the asset similarly.
- **Drawdown De-Risking Ladder** — see below; account-wide, not per-asset.

State is persisted to `sizing_state.json` and survives restarts. (This file replaces `blackjack_state.json`, which is migrated once on first run after upgrading — only the persisted trading-mode choice is carried over, since it happened to live in the same reserved-key file.)

---

## Drawdown De-Risking Ladder

Protects the equity curve independent of whichever sizing mode is active — tracks each account's own **all-time equity high-water mark** (never resets) and scales every new trade's own risk-per-trade dollar amount down as the account's current equity falls further from that peak:

| Drawdown from Peak | Action |
|---|---|
| 0–5% | Normal — full base risk (100%) |
| 5–10% | Reduce base risk 25% (75% sizing) |
| 10–15% | Reduce base risk 50% (50% sizing) |
| 15–20% | Reduce base risk 75% ("skeleton size", 25% sizing) |
| 20%+ | **Full stop** — no new entries, manual review required |

Sim and real accounts are tracked completely independently (own peak, own block state), persisted to `equity_peak_state.json`. The 20%+ tier deliberately does **not** auto-clear the way the Daily Loss Limit does — it's cleared manually with `[1]` once you've reviewed the situation. The dashboard's own ACCOUNT line shows the current drawdown % and tier in real time.

---

## DVOL Layer

Volatility-adjusted risk scaling (ETH only, based on Deribit DVOL):

| DVOL Range | Layer 1 (Base $ Multiplier) |
|---|---|
| ≤ 60 | 100% |
| ≤ 75 | 75% |
| ≤ 90 | 50% |
| > 90 | 25% |

Layer 1 scales the base risk-per-trade dollar amount, applied before sizing. (The old Layer 2 — an R-multiple cap on Blackjack's own escalating ladder — was removed alongside Blackjack itself; it had no meaning without an escalating sequence to cap.)

---

## Target System

Targets are ordered by type priority in `reconstruct_targets`:

| Priority | Type | Source | Direction | Top-Tier | Moves Mid-Trade |
|---|---|---|---|---|---|
| 1 | BT / ST | Live options chain (status.py) | Either | Yes | Yes |
| 2 | VWAP | Session volume profile (CH_STATE, kline_p-fed) | Either | Yes | No (static) |
| 3 | POC | Session volume profile (CH_STATE, kline_p-fed) | Either | Yes | No (static) |
| 4 | VAH | Session volume profile (CH_STATE, kline_p-fed) | Long only | Yes | No (static) |
| 5 | VAL | Session volume profile (CH_STATE, kline_p-fed) | Short only | Yes | No (static) |
| 6 | Large Cluster | Live gamma (gex.py) | Either | Yes | Yes |
| 7 | Medium Cluster | Live gamma (gex.py) | Either | No (fallback) | Yes |
| 8 | Margin Recovered | Computed from fill price + leverage | Either | Yes | No (static) |
| 9 | ER 100% | Session open + IV | Either | No (fallback) | No (static) |
| 10 | ER 150% | Session open + IV | Either | No (fallback) | No (static) |
| 11 | GEX Flip | Live gamma (gex.py) | Either | No (fallback) | Yes |

**VWAP, POC, VAH, VAL** (2026-08-27, added as top-tier targets) are read from the same live `CH_STATE[asset].indicator_levels` numbers the Status screen's own HPL display already shows, so a target line here is always numerically consistent with what's on screen. All four still need to sit on the profit side of current price for the trade's own regime (a VWAP behind price isn't a target) — VAH and VAL carry an *additional* type-based restriction on top of that: VAH (the value area's own ceiling) is only ever a target in a **long**, VAL (the floor) only ever in a **short**, regardless of which side of price it happens to sit on. VWAP/POC have no such restriction — either regime can target either. Unlike BT/ST/Cluster/GEX Flip, none of the four get resynced mid-trade — session VAH/VAL/POC/VWAP drift continuously rather than snapping to a new options-driven level the way the others do, so a TP leg tracking one is left where it was placed at entry (same treatment ER 100%/150% already get).

**Margin Recovered** is the price level where the trade's gross PnL equals the margin used to open the position (`fill_price * (1 + 1/leverage)` for longs, `fill_price * (1 - 1/leverage)` for shorts). Only exists while a position is open.

A **fallback** target may justify entry only when no top-tier target exists anywhere in the target list.

When a position is opened, the two *nearest* valid targets (at least 1R from fill price, sorted by distance to fill price regardless of the priority order above) receive TP legs, with qty split 50/50. GEX Flip TPs are offset $3 toward price. Moving targets (GEX Flip, Cluster, BT/ST) are resynced every engine cycle.

---

## Risk Sizing

Base risk-per-trade, selectable via `[P]`:

**Percentage Mode** (`pct`): `base_risk = balance * (pct / 100)`

**Dollar Mode** (`dollars`): `base_risk = fixed_dollar_amount`

The active [Sizing Mode](#sizing-modes) (`[0]`) then determines the actual `trade_risk`:

- **Standard**: `trade_risk = base_risk`
- **Aggressive**: `trade_risk = base_risk` (+ the previous winning trade's own PnL, exactly once, per that mode's one-shot boost rule)

DVOL Layer 1 (ETH only) scales `base_risk` before either mode sees it. The [Drawdown De-Risking Ladder](#drawdown-de-risking-ladder) applies a third, independent multiplier last, based on the account's own current drawdown from its all-time equity peak.

Position size: `qty = trade_risk / SL_distance`

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
- Position/PnL data, sizing mode state
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
| `sizing_state.json` | Sizing mode, Aggressive mode's pending win-boost per asset, and the persisted trading mode (CCCCWIDE/BTD) |
| `equity_peak_state.json` | All-time equity high-water mark + drawdown block state, per sim/real account |
| `daily_loss_state.json` | Consecutive loss tracker per asset |
| `max_win_state.json` | Consecutive win tracker per asset |
| `closed_pnl_state.json` | Per-day realized PnL |
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
| `X` | Toggle crosshair (also works in the OHLC profile) |
| `S` | Hide/show dashboard |
| `Home` | Snap to live edge |
| `Left/Right` | Scroll footprint bars |
| `[` / `]` | Scroll by 10 bars |
| `{` / `}` | Scroll by 50 bars |
| `P` | Set risk per trade (% or $) |
| `W` | Set SL distance |
| `E` | Set fee per unit |
| `T` | Set imbalance ratio |
| `N` | Toggle 24H/session mode |
| `0` | Cycle sizing mode (Standard ↔ Aggressive) — candle/line style toggle instead, while viewing the OHLC profile |
| `1` | Clear an active Drawdown Full Stop block (manual review) |
| `9` | Cycle trading mode (CCCCWIDE ↔ BTD) |
| `Y` | OHLC profile, QQQ pane only — toggle the Markets macro overview |
| `4` | OHLC profile — toggle VAH/VAL/POC Historical Mode |
| `F` | Flatten position |
| `G` | Toggle live/sim mode |
| `H` | Full key-reference overlay for this mode |
| `R` (double) | Reset paper account (dry-run only) |

### GEX Mode

| Key | Action |
|---|---|
| `G` | Toggle dot-map / by-strike |
| `N` | Toggle net/gross |
| `Tab` | Switch asset |
| `Arrows` | Pan |
| `[` / `]` | Zoom |
| `H` | Full key-reference overlay for this mode |

### Net Drift / CVD / Volatility Drift

| Key | Action |
|---|---|
| `Tab` | Switch asset |
| `Y` | Historical browsing (Net Drift / Volatility Drift only) |
| `I` | Interval |
| `B` | Bar interval / BTD toggle |
| `F` | Filtered/raw toggle |
| `N` | Net volume toggle |
| `C` | Color scheme |
| `X` | Crosshair |
| `H` | Full key-reference overlay for this mode |
| `Arrows` | Pan / cursor move |

### Chain Mode

| Key | Action |
|---|---|
| `Y` | Simple mode toggle |
| `Tab` | Switch asset |
| `H` | Full key-reference overlay for this mode |

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
