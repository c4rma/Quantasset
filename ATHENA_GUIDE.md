# ATHENA_GUIDE.md

Complete reference for `athena.py` — the automated Blackjack/CCCCWDE execution
engine for ETH and QQQ (via Phemex). This document is maintained alongside the
code and updated after every functional change.

---

## 1. What Athena Is

Athena is a single Python process that:

- Runs a background **trading engine** (asyncio, on its own thread) that
  continuously evaluates the CCCCWDE framework's automated conditions for
  both ETH and QQQ, places real or paper trades when every condition lines
  up, and manages the resulting position (stop-loss, take-profit legs,
  moving-target tracking, emergency closes) until it's flat again.
- Renders a **curses terminal UI** (main thread) showing that engine's live
  state — dashboard, live footprint/order-flow chart, a GEX (gamma exposure)
  screen, a Status screen, and a Data view with full trade history and
  statistics.
- Originally depended on three sibling scripts (`status.py`, `gex.py`,
  `footprint.py`) running as separate processes and exporting data to disk.
  All three have since been folded into Athena itself as internal engines —
  see §3. The original scripts still exist and still run standalone if
  wanted, but Athena no longer needs any of them running alongside it.

Everything in this document describes the CURRENT behavior of `athena.py` in
this repository. It is not a design spec — where the code and this document
ever disagree, the code is authoritative and this file is stale and should be
corrected.

---

## 2. The CCCCWDE Framework

CCCCWDE (formerly CCCCWIDE — the "I" for **I**dentify divergence was dropped
once divergence confirmation stopped being a requirement) is the trader's own
manual methodology, in order:

| Step | Meaning |
|---|---|
| **C** | **C**heck session — is a valid trading session currently open? |
| **C** | **C**heck high-impact news — nothing market-moving imminent/just released |
| **C** | **C**heck DVOL (Layer 1 & 2) — is realized/implied volatility in the right regime |
| **C** | **C**heck PCVRs — is the put/call volume ratio in an extreme (actionable) zone |
| **W** | **W**ait for HPL(s) — price must actually be AT a High-Probability Level |
| **D** | **D**etect order-flow reversal — a closed footprint bar confirms direction |
| **E** | **E**xecute the trade |

**What Athena automates vs. what stays manual:** Athena's dashboard shows six
lights per instrument — **Session**, **Volatility**, **PCVR**, **HPLs**,
**Targets**, **Order Flow** — which is the automatable subset of CCCCWDE:

- Session -> the **C**heck session step.
- Volatility -> the **C**heck DVOL step (no green/red rule of its own — the
  light is a presence check; Layer 1/Layer 2 drive actual ETH position
  sizing — see §8/§9).
- PCVR -> the **C**heck PCVRs step.
- HPLs -> the **W**ait for HPL step.
- Order Flow -> the **D**etect order-flow reversal step.
- **Targets** is not one of the CCCCWDE letters — it's an Athena-specific
  gate added on top: even once all 5 CCCCWDE conditions line up, Athena
  additionally requires a valid profit target at least 1R away before it
  will place an order (see §10). This exists to prevent entries with
  nowhere profitable to go, not to replace any CCCCWDE step.
- **High-impact news** ("Check high-impact news") is **not automated
  anywhere in Athena** — there is no news feed, calendar, or filter wired
  in. This remains a fully manual check the trader must still do themselves
  before trusting an Athena signal, or before leaving Athena running
  unattended around a scheduled news event.

Once all 5 automatable lights are green (or 4 in `--no-session`/`[N]` 24-hour
mode — see §9) AND a valid Target exists, Athena arms and waits for the next
closed footprint bar to confirm direction before executing.

---

## 3. Architecture

- **One process, one curses UI, four internal engines** running on
  background threads/tasks, each mirroring what used to be a separate script:
  - **Trading engine** (`engine_loop`, asyncio, its own thread) — the
    `AthenaInstrument` state machines (WATCHING -> ARMED -> PENDING_FILL ->
    IN_POSITION -> WATCHING), one per asset (ETH, QQQ).
  - **Status engine** (`_status_full_refresh_loop` 30s / `_status_live_price_loop`
    2s / `_status_snapshot_loop` 3s, plain threads) — ported from `status.py`:
    Session/Volatility/PCVR/HPLs/Targets computation, BT/ST, gamma-cluster
    targets. Publishes `STATUS_SNAPSHOT` (in-memory, preferred) and also
    still writes `status_logs/.../status_MM_DD_YYYY.jsonl` for compatibility.
  - **GEX engine** (`_gex_engine_loop`, one plain thread per asset) — ported
    from `gex.py`: Deribit (ETH) / CBOE (QQQ) options-chain fetch, gamma
    exposure, GEX Flip (Black-Scholes zero-crossing), gamma clusters.
    Publishes `GEX_EXPORT[asset]` in-memory and still writes
    `status_<ASSET>_gex.json`.
  - **CH (chart) engine** (`_ch_engine_eth` / `_ch_engine_qqq`, one thread
    per asset, + `_ch_export_loop`) — ported from `charthacker.py`'s live-WS
    VAH/VAL/POC/VWAP/SD-band math (session value area, volume profile).
    Publishes `CH_EXPORT[asset]`, gated by `CH_CANDLE_STALE_SECS = 180`
    freshness check so a silently-stalled feed can't keep re-publishing
    frozen numbers under a fresh timestamp.
  - **Live tape** (`_phemex_trade_ws` for ETH, `_alpaca_trade_ws` for QQQ,
    plus a `_fast_publish_loop` at 0.25s) — Athena's own direct trade-print
    WebSocket ingestion for the live-forming footprint bar (not read from
    `footprint.py`). QQQ specifically uses a **dedicated Alpaca account**
    (real "QQQ" equity tape) rather than the QQQUSDT perp, since the perp
    trades far less often than the real ETF — only ORDER EXECUTION uses
    QQQUSDT.
  - Closed footprint bars (used by the confirmation logic) are written by
    Athena itself now too (`LiveTape`'s own persist-on-rollover, matching
    `footprint.py`'s exact on-disk format/backfill behavior) — no
    `footprint.py` process is required.
- **UI thread** never awaits anything — it only reads a lock-protected
  `AppState` snapshot published by the engine thread and draws via a
  `DoubleBuffer` (flicker-free diff-against-previous-frame redraw, ported
  from `charthacker.py`).
- `status.py` / `gex.py` / `footprint.py` / `charthacker.py` themselves are
  untouched and still independently runnable — Athena just no longer needs
  them running alongside it.

---

## 4. Instruments & Core Constants

| | ETH | QQQ |
|---|---|---|
| Execution symbol (Phemex) | `ETHUSDT` | `QQQUSDT` |
| Confirmation/chart feed | Phemex WS (`ETHUSDT` trade prints) | **Alpaca** real "QQQ" equity tape |
| SL distance ("R"), user-adjustable via `[W]` | **$10.00** default | **$1.00** default |
| Tick size | $0.10 | $0.05 |
| Leverage | 100x | 10x |
| BT/ST qualifying run length | 5 consecutive strikes | 5 consecutive strikes |
| BT/ST band | 20% of spot | 12% of spot |

SL distance is stored in `ASSETS[asset]["sl"]` and is a **live reference**
shared everywhere it's read (bracket placement, position sizing, target
viability, dashboard display) — changing it via `[W]` takes effect on the
very next trade with no restart needed, but never retroactively resizes an
already-open position's own resting stop.

Fees: **flat 0.0006 (0.06%)** of notional (`price * qty`) on every leg —
entry, every TP exit, and SL — no separate maker/taker split.

---

## 5. Main Dashboard

Top to bottom:

- **Title bar**: clock (ET + CT), poll interval, snapshot age, `[DRY RUN]` /
  `[24H MODE]` tags.
- **ACCOUNT line**: `Balance` (realized only) · **`Equity`** (`Balance + Open
  PnL` — falls back to Balance itself when there's no open position, not
  "n/a") · `Available` · `Open PnL` · `Closed PnL Today`.
- **Margin Used** line: real Phemex `totalUsedBalanceRv` (or its SimAccount
  equivalent), $ and % of balance.
- **Per-instrument block** (ETH, then QQQ), each showing:
  - Meter: `X/5` or `X/6` green squares (Session/Volatility/PCVR/HPLs/Targets
    [/Order Flow]) — 5 in `--no-session`/`[N]` 24H mode, 6 otherwise.
  - Regime (long/short/none), state (WATCHING/ARMED/PENDING_FILL/
    IN_POSITION/CLOSED for a QQQ-market-closed override), live price.
  - `[PAUSED]` tag if that asset was paused via `[A]`.
  - `[LOSS LIMIT]` tag if the Daily Loss Limit is currently blocking new
    entries for that asset (see §11).
  - `BJ: <sequence label>` if Blackjack mode is on (e.g. `1R`, `2R`, `Win
    (1R+$42.00)`).
  - Pending order line (limit price, target) while PENDING_FILL.
  - Position line while IN_POSITION: side, qty, entry, **SL price + distance
    to live price**, TP1/TP2 (level, target type, distance — green text).
- **Recent Closed Trades** (last 2): time, symbol, side, qty, entry -> exit,
  PnL, and the close reason. In DRY_RUN this is exact (`tp`/`sl`/`manual`/
  etc., straight from the paper ledger). In real mode it's `{reason}
  approx` for a bracket-order fill Athena inferred (`SL`/`TP`/`MANUAL` — see
  §12) or an explicit `eod approx`/`flip approx`/`manual` for a flatten
  Athena itself triggered (no fill-history API is wired up in real mode, so
  the PRICE is always an approximation there — the reason may or may not
  be, depending which path produced it).
- **Recent Activity** (last 2 lines) — `[L]` opens the full scrollable
  popup (up to 500 in-memory events this session).
- **Chart region** — live footprint/order-flow panel(s), split ETH|QQQ
  (always split after 15:00 CT / when both are visible), full-width via
  `[H]`.

---

## 6. Other Screens

`[M]` cycles **Trading -> GEX -> Status -> Trading**. `[H]` (dashboard
hide/full-chart) is independent of this cycle. `Esc` fast-exits GEX/Status
back to Trading from either.

- **GEX mode**: gamma-exposure dot-map or GEX-by-strike bar chart (`[G]`
  toggles), net/separate call-put view (`[N]`), asset switch (`[Tab]`),
  vertical/horizontal pan (arrows, `z`/`Z`/`End` reset to live).
- **Status mode**: the full ported `status.py` render — Session, Volatility,
  PCVR, HPLs (grouped by category), Targets (sorted low-to-high price),
  Final Status. Scrollable (`↑/↓`, `PgUp/PgDn`, `Home`/`End`).
- **Data view** (`[D]`): permanent split screen — top half a connected
  cumulative-PnL equity curve, bottom half the full all-time trades table.
  `[S]` switches sim/real source. `←/→` scroll table columns
  horizontally (`TRADES_TABLE_COLS`, in order): ENTRY, EXIT, SYM, DIR, QTY,
  SEQUENCE, GROSS, R:R, ENTRY$, EXIT$, REASON, FEES, NET PNL, BALANCE, DD,
  R$, TP1, TP1 TYPE, TP2, TP2 TYPE, DUR. Stats header: count/win-rate/total
  PnL/avg win-loss/largest win-loss/profit factor/**Max Drawdown** (account-
  level, see §13)/Sharpe/**Avg R:R** (mean `rr` across **profitable trades
  only** — losses are excluded, not just averaged in; shows `n/a` in real
  mode, which has no per-trade `rr` data).

---

## 7. Key Bindings Reference

**Main dashboard:**

| Key | Action |
|---|---|
| `Q` | Quit |
| `V` | Cycle footprint chart profile mode (volume -> delta -> ohlc -> off) |
| `R` | Reset paper account (DRY_RUN only) — arm, `R` again to confirm/enter new balance |
| `F` | Flatten All — closes every position, cancels every order (any asset, any state) |
| `D` | Open Data view |
| `L` | Open Recent Activity log popup |
| `C` | Screenshot (dumps current `DoubleBuffer` to a file) |
| `H` | Hide/show the dashboard block (chart gets the full terminal when hidden) |
| `N` | Toggle 24-hour mode (drops the Session requirement live) |
| `A` | Pause/resume new entries for the `[Tab]`-focused asset (open positions still managed) |
| `B` | Toggle Blackjack sizing mode on/off — prompts for the 1R $ value when turning ON |
| `1` | Reset the focused asset's Blackjack progression back to 1R, any time |
| `W` | Set the focused asset's SL distance in points |
| `P` | Set risk per trade (value + $/% unit, one combined prompt) |
| `G` | Go live / Go sim (toggle real vs. paper trading, with a confirmation dialog) |
| `Tab` | Switch which asset's pane is focused (only matters with both panes visible) |
| `Z` | Toggle zoomed/split chart view (both panes visible only) |
| `X` | Toggle crosshair on the focused chart |
| `←/→` | Scroll the focused chart |
| `Home` / `Esc` | Reset the focused chart to live |
| `M` | Cycle Trading -> GEX -> Status -> Trading |

**Data view (`[D]`):** `D`/`Esc` back to dashboard, `S` switch sim/real
source, `↑/↓/PgUp/PgDn` scroll table, `←/→` scroll table columns.

**Activity log popup (`[L]`):** `↑/↓/PgUp/PgDn` scroll, `L`/`Esc`/`Q` close.

**GEX mode:** `G` dot-map/by-strike toggle, `N` net/separate toggle, `Tab`
switch asset, arrows pan, `z`/`Z`/`End` reset to live, `M` -> Status, `Esc` ->
Trading.

**Status mode:** `↑/↓/PgUp/PgDn/Home/End` scroll, `M`/`Esc` -> Trading.

---

## 8. Position Sizing

Two independent systems, never combined:

- **Flat mode** (default): `[P]` sets either a **%** of balance or a flat
  **$** amount per trade. Quantity = `trade_risk / sl_distance`, where
  `sl_distance` is that asset's own `ASSETS[asset]["sl"]` (§4) — **not** a
  single hardcoded divisor shared across assets.
- **Blackjack mode** (`[B]`): a **1R,1R,2R,3R,5R** loss progression
  (`BLACKJACK_STEPS = [1.0, 1.0, 2.0, 3.0, 5.0]` — note there are genuinely
  **two** distinct 1R steps before the first escalation, not one) plus a
  2-trade win-progression that steps back down. A win is determined purely
  by **net PnL** (fees already deducted) — any amount of net profit, no
  matter how small, starts the win progression; a net loss (even with a
  hypothetically positive gross) advances the loss ladder, never the
  reverse. `[B]` prompts for the 1R dollar value the moment it's turned
  on; `[1]` resets the focused asset's progression back to step 0 (1R) at
  any time, independent of a full paper-account reset (which also resets
  BJ to 1R for both assets automatically). Each asset's progression is
  tracked completely independently (`BLACKJACK_STATE["ETH"]` /
  `["QQQ"]`), persisted to `blackjack_state.json` — **including whether
  Blackjack mode itself is on/off and the current 1R dollar value**, so a
  restart doesn't silently revert to flat sizing.
  **Daily reset**: each asset's ladder automatically resets to 1R once per
  day, independently — **ETH at 19:30 CT**, **QQQ at 15:00 CT** (the same
  moment its EOD flatten fires). This happens every cycle regardless of
  whether Blackjack mode is currently on, so the ladder is always correct
  whenever it's next enabled.
- **Layer 1 / Layer 2 (DVOL-based, ETH only)**: every trade's sizing is
  further adjusted by ETH's own current DVOL (`dvol_layer_values`), on top
  of flat or Blackjack sizing above:
  - **Layer 1** scales the *effective 1R dollar value* itself (Blackjack's
    1R, or flat mode's own risk-per-trade $/%) — `100%` at DVOL ≤ 60,
    `75%` at ≤ 75, `50%` at ≤ 90, `25%` above that.
  - **Layer 2** caps the *R-multiple actually risked at this entry* —
    `5R` cap at DVOL ≤ 60 (no-op), `3R` at ≤ 75, `2R` at ≤ 90, `1R`
    above that. The Blackjack ladder's own `loss_step`/win-progression
    still advance normally underneath — only the dollar amount *this*
    trade risks is capped, not the ladder's own position.
  - Both are **ETH-only** — QQQ has its own separate VXN volatility
    measure with no analogous layer table. If DVOL is momentarily
    unavailable, both are a no-op (`100%`, no cap) rather than blocking
    entries.
  - A capped entry logs a console warning (`Layer 2 cap — {R}R risk
    capped to {cap}R`); the "filled" event and dashboard SEQUENCE label
    both reflect the post-cap R actually risked.

---

## 9. Volatility, PCVR, and BT/ST

- **Volatility** (5th light) is green whenever its data (DVOL for ETH / VXN
  for QQQ) is simply **present** this cycle — there's no green/red
  threshold rule; it's a presence check. DVOL additionally drives the
  "Layer 1 / Layer 2" position-sizing rules (§8) — no longer just display
  tags.
- **PCVR** extreme zones: ratio `< 0.98` (call-dominant, bullish -> BT
  target) or `> 1.02` (put-dominant, bearish -> ST target); `0.98–1.02` is
  neutral (no targets computed).
- **BT/ST** (Breakout/Breakdown target strikes): the first strike, scanning
  outward from spot, where **5 consecutive strikes** (`STATUS_BT_ST_RUN_LEN`,
  both assets) all have put volume > call volume (for ST) or vice versa (for
  BT), within a spot-relative band (20% crypto / 12% equity). BT/ST **move**
  as PCVR-weighted volume shifts intraday — Athena's TP-tracking logic
  treats them as moving targets, same as a gamma cluster (see §10).

---

## 10. Entry Logic — the Confirmation Gate

Once all required lights are green and a footprint bar closes confirming
direction (POC + net delta both moved the same way as the PCVR regime),
`_check_confirmation` runs a further **target-viability check** before
placing any order:

- **Top-tier targets**: BT/ST, and **Large** gamma clusters.
- **Fallback-only targets**: **Medium** gamma clusters, GEX Flip.
- **Rule**: a fallback target may justify an entry **only when no top-tier
  target exists in the list at all**. If a top-tier target exists but is
  too close (< R away), the fallback list is **not** consulted as a
  substitute — the entry is rejected outright.
- Distance is always measured to the target actually being traded toward
  (the SAME tier the position ultimately targets), never to "whichever
  target happens to be nearest" regardless of tier.
- **R** (the minimum required distance) is `ASSETS[asset]["sl"]` — the same
  user-adjustable value from `[W]` (§4). So: *"a trade is still valid to
  enter if another target is in close proximity... as long as BT is above
  price with at least 1R distance for longs / ST is below price with at
  least 1R distance for shorts"* is exactly this rule — a nearby fallback
  target (say, a Medium cluster $0.80 away) never invalidates an entry as
  long as a top-tier target (BT/ST or a Large cluster) clears R.

Worked examples (from the test suite):

1. BT is $2 away (too close), a Large cluster is $15 away -> **ENTER**
   against the Large cluster.
2. BT AND the only Large cluster are both $2 away, a Medium cluster is $27
   away -> **REJECT** (a top-tier target exists but clears nothing; the
   Medium cluster cannot substitute).
3. No BT/ST and no Large cluster at all today, only a Medium cluster with
   room -> **ENTER** against the Medium cluster (nothing stronger exists to
   defer to).
4. One Large cluster too close, another Large cluster far enough -> **ENTER**
   against the farther one (same-tier skip is fine).

If the setup is valid but price hasn't reached VAH/VAL yet, Athena places a
resting **limit** order instead of a market order. An unfilled limit entry
is automatically cancelled (back to WATCHING) if it sits unfilled for
**`ENTRY_ORDER_EXPIRY_BARS = 4`** footprint bars.

**Blackout window**: no new entries are placed **19:00:00–19:29:59 CT**
(`_in_entry_blackout`) — any resting entry order still unfilled when the
blackout starts is cancelled immediately.

---

## 11. Bracket Management

- **Placement** (`_check_fill`, right after fill): SL at `fill_price ± R`.
  TP legs split across the first 1–2 valid targets (targets within R of
  fill are dropped, quantity redistributed). Valid candidates are always
  sorted by distance from the actual fill price before the TP1/TP2 split
  (fixed 2026-07-29 — they used to be assigned in `targets`/`targets_full`'s
  own tier/priority order, which could put a farther target in TP1 and a
  nearer one in TP2, so TP2 would fill before TP1 as price moved
  favorably) — **TP1 is always the nearer target, TP2 the farther one.**
  GEX Flip TP legs sit `GEX_FLIP_TP_BUFFER = $3.00` on the near side of
  the flip level, never exactly on it; BT/ST and Cluster legs sit exactly
  at their own level.
- **Moving-target tracking** (`_sync_moving_tps`, every cycle while
  IN_POSITION): a TP leg tracking **GEX Flip**, a **Cluster**, or **BT/ST**
  is refreshed to the current live level whenever it moves by more than
  half a cent, as long as the new level still clears R from fill (if not,
  the refresh is skipped that cycle and logged once, not spammed). Multiple
  Cluster-type legs are ranked independently (TP1 gets the nearest/Large
  cluster, TP2 gets the next one) — never collapsed onto the same level.
  A refresh does a full bracket replace (cancel everything, re-place SL
  unchanged + every TP leg at its current level) — there is a brief window
  with no resting SL between cancel and re-place; no verified
  single-order-amend endpoint is wired up, so this is the accepted
  trade-off.
- **TP1 breakeven+$1 lock** (`_apply_tp1_breakeven_lock`, fires exactly once
  per trade, right when a partial TP fill is detected): moves the
  remaining position's SL to the exact price such that if the remainder
  later stops out there, the **combined** trade (both legs, all 3 legs' fees
  included) nets exactly `original_qty * $1.00`, never less. Only ever
  tightens the SL — if the already-realized TP1 gain alone already exceeds
  that target by more than the existing SL would give up, the existing SL
  is left alone rather than loosened. In real mode, TP1's own fill price is
  approximated off its resting order's own level (no fill-history API); in
  DRY_RUN it's read back exactly from the paper ledger.
- **PCVR-flip emergency close**: if PCVR flips to the opposite extreme zone
  while a position is open, that position is market-closed immediately,
  regardless of TP/SL. The flip is just this trade's **close reason** —
  the Blackjack ladder still updates normally off this trade's own net
  pnl (win progression starts on a net win, loss ladder advances one step
  on a net loss), exactly like an SL/TP/manual close (clarified
  2026-07-29 after an initial, incorrect reading of the same user request
  briefly had this skip `_update_blackjack` entirely — reverted same
  day). The only PCVR-flip-specific behavior is Daily Loss Limit
  reactivation: if the Loss Limit was already active on this asset, the
  ladder was already forced to 1R the moment the limit was hit (not when
  it's later cleared), so a flip that reactivates trading finds it
  already sitting at 1R with no extra handling needed.
- **QQQ EOD flatten**: any open QQQ position/order is force-closed at
  15:00 CT (real equity market close), independent of PCVR.

---

## 12. Safety Systems

- **Daily Loss Limit** (`DAILY_LOSS_LIMIT = 5`): blocks new entries for an
  asset after **5 consecutive losses within the same trading day**. Trading
  days run **19:00 CT to 19:00 CT the next day** (`_trading_day_key`) — a
  loss streak spanning that boundary, even with zero intervening win,
  resets rather than combining into one trigger. A win, or the streak
  crossing into a new trading day, resets the counter to 0. Also resets the
  Blackjack progression to 1R when it fires. Clears automatically when
  either (a) PCVR's regime switches away from whatever it was when the
  limit hit, or (b) 19:30 CT passes since the block was set — whichever
  comes first. Shown as a `[LOSS LIMIT]` tag on the dashboard.
- **Entry blackout**: 19:00:00–19:29:59 CT, no new entries (§10).
- **Stale entry expiry**: an unfilled limit entry is cancelled after
  `ENTRY_ORDER_EXPIRY_BARS = 4` bars (§10).
- **`[F]` Flatten All**: force-closes every position and cancels every
  order, any asset, any state, on demand.
- **Real-account safety pause**: switching real<->sim via `[G]` shows an
  explicit confirmation dialog before taking effect.
- **`_sl_missing` retry-every-cycle safety net**: if a real SL placement
  ever fails (and every immediate retry too), Athena keeps retrying it
  every single engine cycle for as long as it takes, rather than treating
  it as a one-shot attempt — a naked real position is treated as the worst
  failure mode in the app.

---

## 13. Trade Log & Statistics Semantics

- **A trade only appears once fully closed** (2026-07-29, explicit user
  rule): "closed" means ALL of its relevant orders are done — both TP
  legs and/or the SL, whichever combination actually fired. A trade with
  TP1 filled but TP2/SL still resting produces **no row at all** yet, not
  a partial/"OPEN" one — its net pnl/DD/R:R aren't final until the
  remainder's own exit lands too. This reverses a 2026-07-25 feature that
  briefly surfaced a partially-open trade as its own "OPEN"-tagged row;
  removed since it read as premature, not-yet-final trade data.
  `scan_all_trades_detailed` only promotes a trade out of its internal
  `open_trades` bookkeeping once every exit's qty sums to the full entry
  qty. The Blackjack win/loss progression decision (`_update_blackjack`)
  is likewise only ever made at this same fully-flat moment — it's driven
  by the actual exchange/sim position size going to zero (`_manage_
  position`'s "position flat" branch, the PCVR-flip-close branch, and
  `_flatten_now`), never by this table or any partial fill.
- **DD (trades table column)** — **per-trade isolated** drawdown: the TRUE
  intra-trade max adverse excursion (MAE) in dollars, i.e. the worst the
  trade was actually underwater at any point during its life, computed by
  scanning the real 90s footprint bars between its own entry and exit
  timestamps (`_trade_worst_adverse_dollars`) using the position size
  ACTUALLY open at each bar's own time (a TP1 partial fill reduces the
  size a later dip is scaled against). Falls back to `max(0, -net_pnl)`
  only when no footprint bars exist for that window (predates footprint
  persistence, or the window is narrower than one bar). A winning trade
  can still show a nonzero DD if price dipped against it before recovering
  into profit; it never carries over another trade's decline (the
  pre-2026-07-28 bug this replaced).
  Performance note (2026-07-29): this reads only the calendar-day
  footprint file(s) each trade's own entry->exit window actually spans
  (via `_footprint_log_path_for_day`, almost always 1 file, occasionally
  2 across a local-midnight crossing) — not the entire footprint history
  tree. An earlier version globbed and re-read the WHOLE `data/footprint/**`
  archive for every trade, every 2 seconds while the Data view was open,
  which was the root cause of a reported "long delay loading the Data
  page" / "laggy scrolling in the trading log."
- **Max Drawdown (Data view stats header)** — **account-level** cumulative
  peak-to-trough over the whole equity curve. Distinct from the per-trade
  column above by design; this one is unchanged from its original
  correct behavior.
- **TP1/TP1 TYPE/TP2/TP2 TYPE** — shown for every trade regardless of
  outcome (planned target logged at bracket-placement time via a
  `tp_targets_set` event), not just trades that actually hit a TP leg. An
  actually-hit leg's real fill price takes priority over the merely-planned
  one when both exist.
- **SEQUENCE** — which Blackjack ladder step this trade risked (e.g. `1R`,
  `2R`, `Win (1R+$42.00)`, or `—` in flat mode).
- **REASON / close classification** — DRY_RUN is always exact, straight
  from the paper ledger's own `tp`/`sl`/`manual`/`flip`/`eod` reason. Real
  mode infers `SL`/`TP`/`MANUAL` by comparing the polled exit price against
  the last-known resting SL/TP levels (§6/§11), tagged `approx` since no
  fill-history API confirms it outright.
- **Fees** — flat `0.0006 * notional` per leg (§4), shown before Net PnL,
  which is Gross PnL minus all fees on that trade.

---

## 14. Real vs. Sim Trading, Known Approximation Gaps

- **`--dry-run`** is a full paper-trading account (`SimAccount`), not a log
  line — persisted balance/positions/resting orders, fills matched against
  Phemex's real public ticker so paper fills happen at real market prices.
  `[R]` resets it in-app (prompts for a new starting balance).
- **Real mode** has no wired-up "list my fills"/fill-history endpoint on
  Phemex. Consequences, all explicitly documented in-code and above:
  - Exit price on a discovered-already-flat position is an approximation
    off a live price poll, not the actual fill.
  - Close reason (TP/SL/manual) is inferred, not certain (§13).
  - `tp_legs`/`sl_price` display is reconciled from `fetch_resting_orders`
    every cycle (added 2026-07-25) so it doesn't go stale after a partial
    fill, but still isn't the authoritative fill ledger DRY_RUN has.
- **Concurrency hazard (known, not hardened)**: running two `--dry-run`
  instances against the same `sim_account.json` at once is unsafe — no file
  locking, last save wins. Don't run two instances against the same paper
  account intentionally.
- **`--no-session`/`[N]`**: drops Session from the arm/disarm gate while
  still computing and displaying its real value (tagged "(bypassed)").

---

## 15. Quick Reference — Key Constants

| Constant | Value | Meaning |
|---|---|---|
| `DAILY_LOSS_LIMIT` | 5 | Consecutive same-trading-day losses to trigger the block |
| `ENTRY_ORDER_EXPIRY_BARS` | 4 | Bars before an unfilled limit entry is cancelled |
| `GEX_FLIP_TP_BUFFER` | $3.00 | Buffer a GEX-Flip-tracking TP sits off the flip level |
| `PHEMEX_FEE_RATE` | 0.0006 | Flat fee rate on every leg's notional |
| `BLACKJACK_STEPS` | `[1.0, 1.0, 2.0, 3.0, 5.0]` | Loss-progression R-multiples |
| `STATUS_BT_ST_RUN_LEN` | 5 (both assets) | Consecutive strikes required for BT/ST |
| `CH_CANDLE_STALE_SECS` | 180 | Freshness gate for the CH engine's export |
| `SIM_DEFAULT_BALANCE` | $10,000 | Default paper-account starting balance |
| `ASSETS["ETH"]["sl"]` | $10.00 (default, user-adjustable) | ETH's SL distance / "R" |
| `ASSETS["QQQ"]["sl"]` | $1.00 (default, user-adjustable) | QQQ's SL distance / "R" |
| Entry blackout | 19:00:00–19:29:59 CT | No new entries |
| QQQ EOD flatten | 15:00 CT | Force-close any open QQQ position/order |
| Trading day boundary | 19:00 CT | Anchors the Daily Loss Limit's own day key |
