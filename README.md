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
- The OHLC profile mode includes Volume Profile, a developing VWAP+SD band, Big Trade Detector signals, a crosshair (`[X]`), and historical trade markers — full parity with the former standalone Chart mode. Draw order (2026-08-27 user-reported fix): VAH/VAL/POC/target/position level-line labels on the price axis now hold their ground against the generic ±σ band labels when both round to the same row (previously the σ labels always won, silently hiding the more specific one); session boundary markers (NDO/Morn/Lunch/PWR/EOD/etc.) now draw *after* candles so they're never painted over by price action, and their vertical left/right edges are now drawn unconditionally too (they used a blank-cell check that made sense back when this overlay drew *before* candles, but once moved to draw last — same fix — almost every cell was already occupied, so the sides were silently dropping out almost every time; only the horizontal top/bottom dash had already been unconditional). The time axis (2026-08-27 user request, ported from charthacker.py's own SESSIONS block) now also shows a solid reverse-video strip in each active session's own color spanning its column range, with the plain `HH:MM` tick labels punched through on top — same layering charthacker.py uses. Vertical scale (2026-08-27 user-reported fix): the chart still auto-extends its price range to keep Entry/SL/every other open TP leg in view, but no longer for an ER 100%/150% leg specifically — an IV-projected daily move routinely sits tens of dollars from price, and letting it dictate the y-axis crushed the actual recent candles into an unreadable sliver; an ER-type TP still exists and is tracked normally, it just scrolls off-screen like anything else out of the visible window instead of forcing the whole chart to zoom out to reach it. Startup backlog (2026-08-27 user-reported — "all the previous candles & data disappeared" right after a relaunch): ETH's OHLC panel/BTD reads candles from `CH_STATE`'s own kline_p-fed deque (not `LIVE_TAPE`), which had no startup backfill of its own — a fresh launch showed only the handful of minutes accrued since restart until the live feed slowly refilled it, unlike QQQ's own panel (still reads the already-seeded `LIVE_TAPE`) which never had this gap. `_ch_seed_candles` now backfills `CH_STATE["ETH"]`'s deque from the exact same disk+REST history `_seed_ohlc_1m` already fetches for `LIVE_TAPE` — merged with, not overwriting, anything the live WS feed already delivered by the time it runs, since startup ordering between the two isn't guaranteed. That fix then exposed a second gap (2026-08-27 user-reported — "Previous day is missing the VAL/VAH/POC values & the VP"): with more than one session's candles now actually visible at once, the VP histogram and VAH/VAL/POC lines had only ever been computed for the CURRENT session — the previous session's own portion of the chart had candles and a dimmed VWAP+SD band, but no volume profile of its own at all. The previous session now gets its own VP histogram + VAH/VAL/POC, dimmed and scoped to its own column range (capped where the current session begins so its bars can't bleed into it), with no separate axis label — the same "dimmed, in-chart only" treatment the VWAP+SD band already gave the previous session. Follow-up (2026-08-27 user-reported, "previous day's VAH/VAL/POC/VWAP carrying over into the current day, should stop at the session break like TradingView" + "when Historical Mode is on, the previous day should show the historical data, not a static line"): the previous session's own VAH/VAL/POC — while `[4]` Historical Mode is on — is now a developing STEPPED TRACE (`_historical_vp_map`, the same mechanism the current session's own Historical Mode already uses) instead of one flat repeated value. A flat line for the previous session could sit at nearly the same row as the current session's own value whenever the two happen to be close (common for a continuously-traded asset like ETH), reading as one uninterrupted line spanning both days even though it was mechanically two separately-scoped segments; a stepped trace necessarily starts fresh the instant the current session's own columns begin, so the day boundary is visibly obvious rather than a coincidence of values. VWAP's own previous-session rendering was already a developing trace from the start (`_draw_vwap_session` never had this gap) — only VAH/VAL/POC needed the fix. Normal Mode (`[4]` off) keeps the flat-line fallback for the previous session, already correctly scoped to stop at the boundary. SL/TP axis labels (2026-08-27 user request, "should be highlighted in their respective colors just like the ER levels") now render in reverse-video, matching ER's own label treatment (`_er_line`'s `attrs | REVERSE`) — only the axis label itself is highlighted; the in-chart dotted reference line is unchanged. ENTRY label visibility (2026-08-27 user-reported — "does not appear on the chart until after the trade is closed"): the live-price marker's own axis label (drawn last, unconditionally) used to silently replace ENTRY's label whenever fill price and live price landed on the same row — which, right at a fresh fill, they usually do (a market order fills AT roughly the current price) — so ENTRY often stayed invisible until price had drifted far enough away, which for a ranging or losing trade could mean never until the position closed. The live-price label now yields to any already-bold label sharing its row (same "don't clobber a more specific label" guard used for the earlier VAH/VAL/POC-vs-σ-band axis collision). Target labels (2026-08-28 user request, "TP labels in OHLC should be labeled either 'TP1' or 'TP2'"): while a position is open, the chart used to ALSO keep drawing every OTHER candidate target from the full target list (e.g. a second Cluster that wasn't actually chosen as either TP leg), labeled by its raw type name right alongside the real "TP1"/"TP2" legs — visually indistinguishable from an actual live take-profit level despite not being one. Those unselected candidates are now suppressed entirely while a position is open (only the real TP1/TP2 legs show); they still show normally while only PENDING (no real TP legs exist yet to replace them with — these candidates ARE what TP1/TP2 will become once it fills).
- **QQQ pre-market volume (2026-08-28 user-reported — "VWAP+SD bands/VAH/VAL/POC start at 08:30 instead of 03:00 CT"):** not actually a session-boundary bug — real price data does exist back to 03:00 CT, but Yahoo's free intraday API reports **zero volume for every pre-market bar** (confirmed live: 08:30:00 CT, exactly the regular open, is the first minute with any nonzero volume at all). VWAP/Volume Profile are inherently volume-weighted, so a `v=0` bar can't move them no matter what window it's scoped into. The actual fix: `_run_footprint_backfill` already replays Alpaca's own real historical QQQ trades (same credentials/source as the footprint chart) through `LiveTape.ingest()` moments before `_seed_ohlc_1m` runs — which already updates this same 1-minute buffer with genuine executed-trade volume, including pre-market (IEX trades extended hours). The old "REST always wins on overlap" rule was then unconditionally clobbering those already-correct, already-real-volume bars with Yahoo's own (frequently zero) figure for the same timestamps a moment later. Fixed for QQQ specifically: a Yahoo candle is only used at a timestamp where nothing already-ingested (from disk or from this run's own Alpaca-fed backfill) has real volume of its own to lose — filling genuine gaps, never overwriting real trade volume with Yahoo's unreliable pre-market figure. ETH's own "REST wins" rule (Phemex, never the problem here) is untouched. **Known remaining gap, by design, not a bug (2026-08-28 user-confirmed):** IEX (Alpaca's free-tier feed, the only one Athena has access to) itself sees almost no QQQ trades before roughly 07:00–07:15 CT most days — confirmed live: 1 trade total in the 03:00–07:14 CT window on a normal morning. That's a real gap in this specific venue's own tape, not something the merge fix above can close — the full consolidated tape (SIP, all exchanges) would show more, but that's a paid Alpaca market-data tier upgrade, not a code change. Explicit user decision: leave this honest — candles/price still render correctly for 03:00 onward (Yahoo's price data is fine, only its volume is unreliable), but VWAP/VAH/VAL/POC simply don't show until real volume actually exists, rather than fabricating a number for a stretch with no genuine trading activity on the venue Athena reads from.
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
5. **Targets** — Price-sorted target list (BT/ST, GEX Flip, clusters, ER 100%/150%)

### Data View (`[D]` overlay)

Equity curve and detailed trade table. Toggle between sim and real trade sources with `[S]`. Shows entry/exit prices, PnL, TP types, sizing-mode sequence labels (Standard/Aggressive), per-trade fee attribution, and a **Funding** column (2026-08-27) — net funding paid(-)/received(+) while each sim trade was open, correlated from `apply_funding_if_due`'s own logged events; shows `n/a` for real trades and for a sim trade that never crossed an 8h funding boundary (distinct from a genuine $0.00).

The stats header also shows **Avg Trade Duration** (mean time-in-trade across all closed trades) and **Fee Drag** (2026-08-27) — total trading fees plus net funding cost, as a percentage of total gross profit (winning trades' own gross PnL only, the standard gross-profit accounting definition) — answering "how much of what I actually made did fees/funding take back." Real trades' own Fee Drag reflects trading fees only, since funding isn't tracked locally for real mode.

**Sim trades are shown as a retroactive "1R+W" hypothetical (2026-08-27 explicit user request):** every sim trade's real entry/exit prices are untouched, but Qty/Sequence/Gross/Fees/Funding/Net PnL/Balance/DD/R$ and the PnL chart are all recomputed as if Aggressive's win-boost progression had been active for the entire trade history (it wasn't — sizing mode has been Standard this whole time), sized off today's live PCT/RISK_DOLLARS setting walked forward against the hypothetical balance. R:R comes back numerically identical to the real trade's own R:R — a uniform position-size rescale can't change a reward:risk ratio, so this is expected, not a bug. DVOL Layer 1 and the drawdown ladder are NOT replayed on top of this (no historical record of either to replay from). Real trades are unaffected — always the actual historical record.

A **"Live sizing mode: Standard/Aggressive"** readout (2026-08-27, right below the title) shows the REAL, currently-active sizing mode regardless of which source tab is open — added after a user report ("the win progression did not kick in") traced back to this exact table: the hypothetical always shows "Aggressive"/boosted sequence labels, which had been mistaken for confirmation that live Aggressive mode + boosting was actually running, when the live mode was genuinely Standard the whole time (Standard never applies any progression, by design). Toggle the real mode with `[0]`. The PnL chart and Net PnL figures ARE already fee-adjusted net, not gross — verified directly against the stats header's own "Total" figure, which is mathematically the chart's own final (not peak) point on the cumulative curve.

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

**Down-triggers are immediate and unconditional** — moving to a worse tier never waits:

| Drawdown from Peak | Size (down-trigger, unchanged) | Size restored on recovery to |
|---|---|---|
| 0–5% | 100% | — |
| 5–10% | 75% | Recover to <3% before restoring 100% |
| 10–15% | 50% | Recover to <8% before restoring 75% |
| 15–20% | 25% (skeleton) | Recover to <13% before restoring 50% |
| 20%+ | 0% (full stop) | Manual review required — never auto-resumes |

**Recovery hysteresis + 1-day confirmation (2026-08-27, explicit user request):** restoring a BETTER tier isn't just "cross back over the same line" — it requires dropping below a lower, tier-specific threshold (the middle column above), and that threshold must then hold for a full CT calendar day before the restoration actually takes effect. Example: drawdown reaches 18% (25%/skeleton sizing), then recovers to 12.5% the same day — sizing stays at 25% until the *next* CT day; if 12.5% (or better) is still true then, sizing restores to 50%. Popping back above the recovery threshold at any point before the day rolls over cancels the pending restoration — it has to re-qualify fresh from there, no partial credit. Restoration only ever advances one tier per satisfied wait, never skips ahead, even if the account recovers dramatically in one move. The dashboard's own ACCOUNT line shows a `→ {tier} pending` tag whenever a restoration is waiting on its day-confirmation.

The 20%+ Full Stop tier is the one exception to all of the above — it **never** auto-recovers regardless of how much drawdown improves; it's cleared manually with `[1]`. Clearing re-derives the tier fresh from wherever the account naturally stands at that moment (clamped to at least the skeleton tier — never straight back to a 0%-sized "unblocked" state) — any further improvement from there still has to earn its way up through the normal hysteresis + 1-day wait.

Sim and real accounts are tracked completely independently (own peak, own tier, own pending-restoration state, own block state), persisted to `equity_peak_state.json`. The dashboard's own ACCOUNT line shows the current drawdown % and tier in real time.

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
| 8 | ER 100% | Session open + IV | Either | No (fallback) | No (static) |
| 9 | ER 150% | Session open + IV | Either | No (fallback) | No (static) |
| 10 | GEX Flip | Live gamma (gex.py) | Either | No (fallback) | Yes |

**VWAP, POC, VAH, VAL** (2026-08-27, added as top-tier targets) are read from the same live `CH_STATE[asset].indicator_levels` numbers the Status screen's own HPL display already shows, so a target line here is always numerically consistent with what's on screen. All four still need to sit on the profit side of current price for the trade's own regime (a VWAP behind price isn't a target) — VAH and VAL carry an *additional* type-based restriction on top of that: VAH (the value area's own ceiling) is only ever a target in a **long**, VAL (the floor) only ever in a **short**, regardless of which side of price it happens to sit on. VWAP/POC have no such restriction — either regime can target either. Unlike BT/ST/Cluster/GEX Flip, none of the four get resynced mid-trade — session VAH/VAL/POC/VWAP drift continuously rather than snapping to a new options-driven level the way the others do, so a TP leg tracking one is left where it was placed at entry (same treatment ER 100%/150% already get).

**Margin Recovered — removed entirely (2026-08-27 explicit user request).** It used to be a synthetic fallback TP target (the price where gross PnL equals the margin used to open the position, `fill_price * (1 ± 1/leverage)`) inserted whenever no other valid target existed, or when it sat farther from fill than the nearest real one. That entire mechanism — the fallback insertion in `_check_fill`, the leg-type checks in `_sync_moving_tps` (including the "upgrade to a real BT/ST once one becomes available" behavior), and the TP-type code/display maps — was removed with no replacement; a position with no valid TP target now simply falls through to the existing "nearest dropped candidate anyway" fallback that already handled that case independently of Margin Recovered.

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
- **Duration** (2026-08-27) — shown on the dashboard's Position line next to Realized, `HH:MM:SS` elapsed since fill, recomputed live every render (started as `HH:MM`-only, switched to include seconds since a fresh position under a minute old otherwise reads as stuck at "00:00" with no visible movement). A reconciled position (one already open when Athena starts, DRY_RUN or real) used to always stamp `fill_time` as the moment of reconciliation, so Duration silently reset to `00:00:00` on every relaunch — fixed: `_last_fill_ts` recovers the TRUE original entry timestamp from `athena_logs`' own `filled` event (written on every fill regardless of mode), falling back to the reconciliation moment only if that log entry genuinely isn't there (rotated away, or a position that predates this fix).

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
| `sizing_state.json` | Sizing mode, Aggressive mode's pending win-boost per asset, and the persisted trading mode (Order Flow/BTD) |
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
| `9` | Cycle trading mode (Order Flow ↔ BTD) — renamed 2026-08-27, was "CCCCWIDE" |
| `Y` | OHLC profile, QQQ pane only — toggle the Markets macro overview |
| `4` | OHLC profile — toggle VAH/VAL/POC Historical Mode (on by default as of 2026-08-27) |
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
