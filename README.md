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
- [Risk Structures (Fixed / VE)](#risk-structures-fixed--ve)
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

**2026-08-27: Chart mode and the standalone Markets mode were both removed.** Chart mode's own free-symbol candlestick tool (VP, VWAP, BT/ST/GEX-Flip, Expected Range, Big Trade Detector, crosshair, historical trade markers) was fully ported into the Trading dashboard's own embedded OHLC panel (`[V]`-cycled) over the course of this project, at which point Chart mode's separate, independent WebSocket feeds became the sole remaining reason its own signals could ever disagree with the dashboard's — removing it removed that class of bug at the root. Markets (a 9-asset macro overview: BTC/ETH/XAUUSD/USDJPY/USOIL/SPX500/NAS100/DXY/TLT — TLT added 2026-09-01 user request, Yahoo-only since a bond ETF has no Phemex/Kraken crypto equivalent) is no longer its own mode either — it's part of a `[Y]` cycle on the QQQ pane specifically of the Trading dashboard's OHLC view (candle chart → Markets overview → NDX candlestick chart → back — NDX added 2026-09-02, see Trading mode's own note below).

### Trading (Default)

**QQQ trading permanently disabled (2026-09-02 explicit user request — "You can remove QQQUSDT entirely from trading - I will not be trading that instrument anymore. The only trading functionality going forward is solely for ETH."):** unlike `[A]`'s per-asset pause (reversible, a live toggle), QQQ can no longer arm under **any** trading mode, full stop — the WATCHING-state gate returns unconditionally for `self.asset == "QQQ"` before any of the usual gate/blackout/loss-limit checks even run. This is a trading-only change: QQQ's display/footprint/OHLC panel, GEX engine, CVD, Volatility Drift, and the dashboard's own split-pane/`[Tab]` focus switching are all untouched and continue to show live QQQ data exactly as before — only order placement is gone. (Net Drift's own QQQ tracking was briefly swapped out for NDX the same day, then explicitly restored moments later — "keep tracking QQQ in everything like you were before... I still want & need that data in order to compare with the others" — see Net Drift, below.)

Split-pane ETH/QQQ footprint chart with a status dashboard. Shows:
- Real-time footprint bars (volume, delta, OHLC profile modes via `[V]`)
- The OHLC profile mode includes Volume Profile, a developing VWAP+SD band, Big Trade Detector signals, a crosshair (`[X]`), and historical trade markers — full parity with the former standalone Chart mode. Draw order (2026-08-27 user-reported fix): VAH/VAL/POC/target/position level-line labels on the price axis now hold their ground against the generic ±σ band labels when both round to the same row (previously the σ labels always won, silently hiding the more specific one); session boundary markers (NDO/Morn/Lunch/PWR/EOD/etc.) now draw *after* candles so they're never painted over by price action, and their vertical left/right edges are now drawn unconditionally too (they used a blank-cell check that made sense back when this overlay drew *before* candles, but once moved to draw last — same fix — almost every cell was already occupied, so the sides were silently dropping out almost every time; only the horizontal top/bottom dash had already been unconditional). The time axis (2026-08-27 user request, ported from charthacker.py's own SESSIONS block) now also shows a solid reverse-video strip in each active session's own color spanning its column range, with the plain `HH:MM` tick labels punched through on top — same layering charthacker.py uses. Vertical scale (2026-08-27 user-reported fix): the chart still auto-extends its price range to keep Entry/SL/every other open TP leg in view, but no longer for an ER 100%/150% leg specifically — an IV-projected daily move routinely sits tens of dollars from price, and letting it dictate the y-axis crushed the actual recent candles into an unreadable sliver; an ER-type TP still exists and is tracked normally, it just scrolls off-screen like anything else out of the visible window instead of forcing the whole chart to zoom out to reach it. Startup backlog (2026-08-27 user-reported — "all the previous candles & data disappeared" right after a relaunch): ETH's OHLC panel/BTD reads candles from `CH_STATE`'s own kline_p-fed deque (not `LIVE_TAPE`), which had no startup backfill of its own — a fresh launch showed only the handful of minutes accrued since restart until the live feed slowly refilled it, unlike QQQ's own panel (still reads the already-seeded `LIVE_TAPE`) which never had this gap. `_ch_seed_candles` now backfills `CH_STATE["ETH"]`'s deque from the exact same disk+REST history `_seed_ohlc_1m` already fetches for `LIVE_TAPE` — merged with, not overwriting, anything the live WS feed already delivered by the time it runs, since startup ordering between the two isn't guaranteed. That fix then exposed a second gap (2026-08-27 user-reported — "Previous day is missing the VAL/VAH/POC values & the VP"): with more than one session's candles now actually visible at once, the VP histogram and VAH/VAL/POC lines had only ever been computed for the CURRENT session — the previous session's own portion of the chart had candles and a dimmed VWAP+SD band, but no volume profile of its own at all. The previous session now gets its own VP histogram + VAH/VAL/POC, dimmed and scoped to its own column range (capped where the current session begins so its bars can't bleed into it), with no separate axis label — the same "dimmed, in-chart only" treatment the VWAP+SD band already gave the previous session. Follow-up (2026-08-27 user-reported, "previous day's VAH/VAL/POC/VWAP carrying over into the current day, should stop at the session break like TradingView" + "when Historical Mode is on, the previous day should show the historical data, not a static line"): the previous session's own VAH/VAL/POC — while `[4]` Historical Mode is on — is now a developing STEPPED TRACE (`_historical_vp_map`, the same mechanism the current session's own Historical Mode already uses) instead of one flat repeated value. A flat line for the previous session could sit at nearly the same row as the current session's own value whenever the two happen to be close (common for a continuously-traded asset like ETH), reading as one uninterrupted line spanning both days even though it was mechanically two separately-scoped segments; a stepped trace necessarily starts fresh the instant the current session's own columns begin, so the day boundary is visibly obvious rather than a coincidence of values. VWAP's own previous-session rendering was already a developing trace from the start (`_draw_vwap_session` never had this gap) — only VAH/VAL/POC needed the fix. Normal Mode (`[4]` off) keeps the flat-line fallback for the previous session, already correctly scoped to stop at the boundary. SL/TP axis labels (2026-08-27 user request, "should be highlighted in their respective colors just like the ER levels") now render in reverse-video, matching ER's own label treatment (`_er_line`'s `attrs | REVERSE`) — only the axis label itself is highlighted; the in-chart dotted reference line is unchanged. ENTRY label visibility (2026-08-27 user-reported — "does not appear on the chart until after the trade is closed"): the live-price marker's own axis label (drawn last, unconditionally) used to silently replace ENTRY's label whenever fill price and live price landed on the same row — which, right at a fresh fill, they usually do (a market order fills AT roughly the current price) — so ENTRY often stayed invisible until price had drifted far enough away, which for a ranging or losing trade could mean never until the position closed. The live-price label now yields to any already-bold label sharing its row (same "don't clobber a more specific label" guard used for the earlier VAH/VAL/POC-vs-σ-band axis collision). Target labels (2026-08-28 user request, "TP labels in OHLC should be labeled either 'TP1' or 'TP2'"): while a position is open, the chart used to ALSO keep drawing every OTHER candidate target from the full target list (e.g. a second Cluster that wasn't actually chosen as either TP leg), labeled by its raw type name right alongside the real "TP1"/"TP2" legs — visually indistinguishable from an actual live take-profit level despite not being one. Those unselected candidates are now suppressed entirely while a position is open (only the real TP1/TP2 legs show); they still show normally while only PENDING (no real TP legs exist yet to replace them with — these candidates ARE what TP1/TP2 will become once it fills).
- **QQQ pre-market volume (2026-08-28 user-reported — "VWAP+SD bands/VAH/VAL/POC start at 08:30 instead of 03:00 CT"):** not actually a session-boundary bug — real price data does exist back to 03:00 CT, but Yahoo's free intraday API reports **zero volume for every pre-market bar** (confirmed live: 08:30:00 CT, exactly the regular open, is the first minute with any nonzero volume at all). VWAP/Volume Profile are inherently volume-weighted, so a `v=0` bar can't move them no matter what window it's scoped into. The actual fix: `_run_footprint_backfill` already replays Alpaca's own real historical QQQ trades (same credentials/source as the footprint chart) through `LiveTape.ingest()` moments before `_seed_ohlc_1m` runs — which already updates this same 1-minute buffer with genuine executed-trade volume, including pre-market (IEX trades extended hours). The old "REST always wins on overlap" rule was then unconditionally clobbering those already-correct, already-real-volume bars with Yahoo's own (frequently zero) figure for the same timestamps a moment later. Fixed for QQQ specifically: a Yahoo candle is only used at a timestamp where nothing already-ingested (from disk or from this run's own Alpaca-fed backfill) has real volume of its own to lose — filling genuine gaps, never overwriting real trade volume with Yahoo's unreliable pre-market figure. ETH's own "REST wins" rule (Phemex, never the problem here) is untouched. **Known remaining gap, by design, not a bug (2026-08-28 user-confirmed):** IEX (Alpaca's free-tier feed, the only one Athena has access to) itself sees almost no QQQ trades before roughly 07:00–07:15 CT most days — confirmed live: 1 trade total in the 03:00–07:14 CT window on a normal morning. That's a real gap in this specific venue's own tape, not something the merge fix above can close — the full consolidated tape (SIP, all exchanges) would show more, but that's a paid Alpaca market-data tier upgrade, not a code change. Explicit user decision: leave this honest — candles/price still render correctly for 03:00 onward (Yahoo's price data is fine, only its volume is unreliable), but VWAP/VAH/VAL/POC simply don't show until real volume actually exists, rather than fabricating a number for a stretch with no genuine trading activity on the venue Athena reads from.
- **VolEffort (`[6]`, 2026-08-29 user request)** — ported from the standalone `vol-effort.py` tool, replacing the bottom VOL histogram with a z-score histogram: `log(volume / max(range, tick))` per bar, z-scored against a trailing baseline of the last `window` eligible (closed, non-dead, session-appropriate) bars strictly before it — a bar never contributes to its own score. High z (above a per-asset `hi_z`) reads as **absorption** (magenta) — heavy volume for a small range, effort without result; low z (below `lo_z`) reads as a **clean** move (cyan) — an ordinary-volume bar covering an outsized range, unresisted. Anything in between is a plain neutral bar, colored the same bull/bear (default/blue) convention the candles and the regular volume histogram already use. `window`/`hi_z`/`lo_z` are calibrated per asset (ETH: 45/1.9/-1.75, near-symmetric; QQQ: 60/2.5/-1.10, right-skewed — see `vol-effort_calibration.md`), and QQQ's own baseline eligibility is restricted to 8:30 AM–3:00 PM CT (regular trading hours), matching that calibration. `vol-effort.py`'s own Big Trade Detector (`compute_btd_signals`) was deliberately **not** ported — the OHLC panel already has its own BTD overlay, and it's the same math either way (vol-effort.py's own BTD was itself ported *from* this panel). Started off by default; the pane header shows `VolEffort:On` while active (see the 2026-08-30 default-on change below).

Also ported (2026-08-29 follow-up, "you did not include the bar outlines and markers"): vol-effort.py's own `_flag_box` + `bias_marker` overlay on the candlestick chart itself, for any visible bar classified absorption/clean. The original draws a 3-column-wide rectangle around the flagged bar (room its own per-bar column spacing has) plus a directional bias glyph above it (▲ green bullish lean / ▼ red bearish lean / ◆ dim no lean — the tool's own directional *read*, not the candle's own up/down). This panel packs one column per bar with no gap, so a literal 3-wide box would overwrite the neighboring bar's own wick/body on both sides — adapted instead to a single-column treatment: the bar's own wick recolors to the class color (magenta/cyan) so the whole bar reads as flagged, plus a small cap (`▔`/`▁`) just outside the high/low and the bias glyph one row further out. Same outline-plus-marker purpose, fit to this panel's own tighter layout.

Two same-day follow-ups from a later user pass over the initial port: **(1)** "Remove the bar outlines but leave the markers" — the wick-recolor and `▔`/`▁` cap glyphs from the paragraph above are gone; only the bias glyph (▲/▼/◆) remains, placed one row above the bar's own high, with no other change to how the candle itself renders. **(2)** "Make the z-score window big enough to fit the whole chart" — the panel's fixed 4-row `OHLC_VOL_H` (shared with the plain volume histogram) was nowhere near enough room for a genuinely readable *signed* histogram; when VolEffort is active the bottom band takes a dynamic `_vol_h` instead, capped so the candle area always keeps a usable minimum of its own — and the whole geometry cascade (`chart_bot_excl`/`chart_h`/`time_row`/`vol_top`/`vol_bot_excl`) derives from it instead of the fixed constant. With real vertical room to work with, the panel's rendering was rewritten to match vol-effort.py's own `draw_zpane` properly: a dotted zero-line at the vertical middle of the band, absorption (+z) bars growing *up* toward the top, clean (-z) bars growing *down* toward the bottom, plus dashed `hi_z`/`lo_z` guide lines with axis labels — replacing the original 4-row version's top-anchored, downward-only compromise. Sized at 35% of the pane's own total height at first; **2026-09-01 user-reported ("too big and there is more empty space than what's needed")** — z-scores rarely swing past ±3 even at the hi_z/lo_z guide lines, so most of that 35% sat empty above/below the actual bars every render — condensed to 20%, roughly half the previous band, still well past the original 4-row sliver.

**OHLC crosshair scroll bug fixed (2026-08-29 user-reported):** "the crosshair does not go all the way to the left - it locks after a few candles from the right end and then scrolls the whole chart back... should be just like charthacker.py." The `[X]` crosshair's `KEY_LEFT`/`KEY_RIGHT` handlers estimate how many bars are currently visible (`n_est`) to decide when to start panning `chart_scroll` to follow the crosshair off-screen; that estimate was always computed from `CHART_AXIS_W`/`CHART_COL_W` — footprint.py's own per-bar geometry, sized for its wide multi-character price/volume cell (`CHART_COL_W = 1 + CELL_TXT_W + 1 = 17` columns per bar). The OHLC panel packs exactly **one** column per 1-minute bar with no per-bar cell at all, so for a typical pane width that formula underestimated the true visible window by roughly an order of magnitude (e.g. `n_est=9` when ~169 bars actually fit) — the scroll clamp thought the visible edge was 9 bars away and started dragging the whole chart after only a handful of crosshair moves. `n_est` for `profile_mode == "ohlc"` now mirrors `_draw_ohlc_chart_panel`'s own `chart_w` formula (`pane_cols - OHLC_PRICE_W - 1`) instead; footprint mode's own formula is unchanged. Verified with a synthetic harness driving `_footprint_crosshair_clamp` directly (the clamp function itself was already correct — only its callers' `n_est` was wrong): the crosshair now travels the full true visible width before scroll engages in either direction, and reaches/returns from the oldest bar cleanly at both full and split-pane widths.

**OHLC crosshair now spans the VOL/VolEffort band too (2026-08-30 user-reported, with a screenshot circling where the line stopped short):** the vertical crosshair line used to be drawn right after the level-lines/candles section, spanning only `chart_top..chart_bot_excl` — the candle area — well BEFORE the VOLUME/VOLEFFORT section beneath it ever ran. That section's own zero-line/guide-line/bar draws are unconditional (no "blank cells only" check, unlike the crosshair's own), so even extending the old range down would have gotten silently painted over the instant VolEffort's own zero-line swept across that column. The crosshair draw is now the true last thing the panel does — moved to after the TIME AXIS section — and its range extends down through `vol_top..vol_bot_excl`, so it now runs the full height of the chart through the VolEffort z-score band (or the plain volume histogram, whichever is active) exactly like it always did through the candles above. Verified with a synthetic render: the crosshair column now lights up in 57 of 58 rows top-to-bottom, versus stopping roughly in the middle before this fix.

**VolEffort now on by default (2026-08-30 user request):** `ohlc_vol_effort` started `False` when first ported (a brand-new indicator, deliberately conservative) — now defaults to `True`, same default-on treatment `[4]`'s own Historical Mode already got on 2026-08-27. `[6]` still toggles it off per-session as before.

**EEOD boundary moved to 19:30 CT (2026-08-30 user-reported — "EEOD should begin at 19:30 CT, not 18:30 CT"):** EEOD's start was defined in three separate places that all needed the same change: `STATUS_KILL_ZONES`/`STATUS_EXCL_EEOD_START` (the Data view's own session-status readout, `status_get_session_status`), `OPTFLOW_KILL_ZONES`/`OPTFLOW_EXCL_EEOD_START` (the same session logic duplicated for the options-flow view, `_optflow_session_status`), and the OHLC panel's own in-chart `OHLC_SESSIONS` boundary-marker list. All three had EEOD starting at minute 1110 (18:30 CT) / `(18,30)` — all three now start it at minute 1170 (19:30 CT) / `(19,30)`. EOD's own boundary (16:00–18:00 CT) is unchanged, so there's now an unlabeled 18:00–19:30 CT gap between EOD and EEOD in all three views, same as the pre-existing 18:00–18:30 gap before this change — just wider.

**OHLC chart scale bug fixed (2026-09-01 user-reported, with a screenshot showing candles crushed into the bottom half of the pane — "should scale to the highest high and lowest low of all of the candles visible... the scaling is too small here"):** the 2026-08-27 fix that excludes an ER-type TP leg from extending the chart's own visible price range only ever listed `"ER 100%"`/`"ER 150%"` — it predates `ER 40%`/`ER 80%` joining the target list as their own TP types (added earlier the same day as this report). A TP leg landing on either of those two newer tiers was still blowing the visible range out exactly like ER 100%/150% used to, exactly as seen in the reported screenshot (TP2 at `ER 80%`, 46 points from price). All four ER tiers are excluded from the range-extension now — the chart scales to the candles' own high/low (plus Entry/SL/BT/ST/Cluster/GEX Flip/VWAP/POC/VAH/VAL, which stay close enough to price not to cost readability) exactly as originally intended. Verified with a synthetic render: a TP2 leg 46 points outside the candle range no longer pulls the top axis label anywhere near it.

**VolEffort bias marker vs. BTD signal collision fixed (2026-09-01 user-reported, screenshot circling a bar where the marker had vanished — "the VolEffort marker is hidden underneath the BTD signal"):** VolEffort's own bias glyph (▲/▼/◆) was drawn immediately, one row above the bar's own high, during the candle-drawing loop — but the OHLC panel's BTD overlay draws its SELL marker at that exact same cell, unconditionally, much later in the render (after candles, sessions, and the live-price marker all finish), silently painting over any VolEffort glyph already sitting there whenever both fire on the same bar. Fixed by deferring VolEffort's own placement instead of drawing it inline: each candidate marker is collected during the candle loop, then actually placed *after* the BTD block runs — preferring the original one-row-above-the-high spot when it's still free, falling back to one row below the bar's own low (mirroring where BTD's own buy marker sits) when it isn't. Verified with a synthetic bar crafted to trigger both a "large" BTD sell signal and a VolEffort absorption reading simultaneously: the glyph now lands below the bar's low, where the old code would have silently dropped it under the BTD marker instead.
- `[Y]` on the QQQ pane cycles candle chart → Markets macro overview → NDX candlestick chart → back (NDX added 2026-09-02, see its own note below)
- 6-light readiness meter per instrument
- Position PnL, SL/TP levels, margin usage
- Sizing mode status (Standard/Aggressive/Aggressive-1R+0.33W) and drawdown de-risking tracker
- Trading mode, risk structure (ETH, NV/NV-Auto only), and profit-ratchet status, shown inline next to each other on the readiness-row line (2026-09-02 user-reported — risk structure previously only ever appeared in the log, never on the dashboard itself; profit ratchet's own display line was already wired but the `[2]` key that activates it was silently broken while viewing the OHLC profile, so it never had anything to show)
- Real-time bid/ask/spread next to each asset's price
- DVOL layer indicator
- Funding rate and next accrual countdown
- Compact activity log

**NDX candlestick chart added to `[Y]` (2026-09-02 user request — "In the QQQ OHLC chart, add NDX to the [Y] function"):** a genuine bare candlestick + volume chart, not a repeat of the Markets overview's own normalized-%-since-open line (which already carries NDX's own data under the label "NAS100", but as a single overlay line, not real OHLC). Sourced from its own small always-on poller (`_ndx_ohlc_poll_loop`, `NDX_OHLC_POLL_SECS=20`) hitting Yahoo's intraday chart endpoint directly for `^NDX` — the same endpoint Markets itself already uses, just keeping the full `open`/`high`/`low`/`close`/`volume` Yahoo returns instead of only `close`. Deliberately minimal: no VWAP/VP/session overlays, BTD signals, or position levels — none of those have real tick-level trade data behind them for NDX the way they do for ETH/QQQ, so `_draw_ndx_ohlc_panel` is its own small, dedicated function rather than a heavily-conditional fork of the full OHLC panel. One known, structural limitation: Yahoo reports `0` volume for `^NDX` itself (an index has no traded volume of its own, unlike a stock or ETF) — the volume band renders but stays empty; this is a real gap in the data source, not a bug.

**Scroll, crosshair, and the chart title fixed (2026-09-02 user-reported, screenshot showing "QQQ FOOTPRINT" as the title and an embedded "NDX ..." sub-line):** the first version was scroll/crosshair-less and always showed the tail — both now work exactly like every other OHLC view, sourced from NDX's own dedicated scroll/crosshair state (`_effective_chart_asset`, a new "NDX" entry alongside ETH/QQQ in `chart_scroll`/`chart_crosshair_active`/`chart_crosshair_idx`/`ohlc_vert_offset` — kept separate from QQQ's own real candle view's state so the two can't fight over one scroll position). The outer title bar is still drawn by `draw_footprint_panel` itself, which always receives `asset="QQQ"` (it's still fundamentally the QQQ pane) — it now overrides the DISPLAYED name to "NDX" and suppresses QQQ's own VP:Historical/VolEffort tags (meaningless for NDX's bare view) whenever `ohlc_ui["show_ndx"]` is set, rather than showing "QQQ" unconditionally.

### GEX

Gamma Exposure visualization with two sub-modes:
- **Dot Map** — Heatmap-style GEX by strike and expiry
- **By Strike** — Bar chart of net gamma per strike

Toggle between views with `[G]`, net/gross with `[N]`.

**TLT, NDX and VIX added (2026-09-02 user request):** `[Tab]` now cycles `ETH → QQQ → NDX → TLT → VIX → ETH` — the ETH↔QQQ leg still toggles `chart_focus` itself (unchanged, same convention the footprint charts use elsewhere), the three extras live in their own `gex_extra_focus` overlay instead so they never disturb it (same pattern Net Drift's own `drift_extra_focus` already established). All three are monitoring-only, same as everywhere else in the app. NDX/VIX (CBOE index roots) reuse Net Drift's own `DRIFT_CBOE_SYMBOL` underscore-prefix mapping for the fetch — one source of truth, not a second copy of it.

### Net Drift

Options net premium flow chart. Tracks whether money is flowing into calls or puts across multiple exchanges (Deribit, OKX, Bybit for crypto; CBOE for equity-style indices).

- Per-asset historical browsing `[H]`
- Filtered/raw toggle `[F]`
- Net volume toggle `[N]`
- Crosshair with OHLC readout `[X]`

**Tracked assets — NDX, TLT and VIX added alongside QQQ (2026-09-02 user request):** `DRIFT_ASSETS` is now `(ETH, QQQ, NDX, TLT, VIX)`. NDX briefly *replaced* QQQ here the same day the user decided to stop trading QQQ entirely, then the user changed their mind moments later — "keep tracking QQQ in everything like you were before. I still want & need that data in order to compare with the others" — so QQQ's own Net Drift/Net Volume/Options tracking is back, alongside NDX rather than instead of it; only QQQ's **trading** stays permanently disabled (a separate, still-standing instruction, unaffected by this). ETH tracks continuously; QQQ/NDX/TLT/VIX are windowed to 08:30–15:00 CT weekdays. QQQ/NDX/TLT/VIX are all **monitoring-only** for trading purposes — "No trading decisions are to be made from TLT yet - I want to see how the flow affects ETH/QQQ" (TLT, 2026-09-01) and "NDX & VIX/VXN will be for my own monitoring, no trades will be taken with Athena for NDX" (2026-09-02) — none of the four is ever passed to `instrument_lights` or any entry-decision code (ETH is the only asset Net Volume can ever influence a trade for, regardless of whether QQQ's own status happens to read Positive/Negative). The mode's own `[Tab]`-equivalent asset cycle is `ETH → QQQ → NDX → TLT → VIX → ETH`, entirely separate from `chart_focus`'s own ETH/QQQ meaning used everywhere else in the app — ETH is simply this cycle's own `None`/default state.

**VXN has no options chain (2026-09-01/02 finding, not a code limitation):** the user initially asked for VXN (Nasdaq-100 volatility) alongside VIX; confirmed live via CBOE's own delayed-quotes endpoint that neither `_VXN` nor `VXN` resolves to a real options chain (both 403, vs. `_NDX`/`_VIX` returning 200 with live chains as a control) — Nasdaq-100 volatility simply has no listed, tradable CBOE options chain to source premium flow from. VIX is tracked instead, per explicit user decision. Index-root tickers (VIX, NDX) need a CBOE-specific underscore prefix in the fetch URL (`_VIX`/`_NDX`) — `DRIFT_CBOE_SYMBOL` maps this per-asset; the option contract names CBOE returns are unaffected by the prefix (parsed by fixed suffix offset, not by name prefix), so nothing downstream needed to change.

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

Large-block institutional options flow. Always-on refresh loop with alert muting via `[U]`. (The Markets macro overview that used to be its own mode after this one is now part of the `[Y]` cycle on the Trading dashboard's own QQQ pane instead, alongside an NDX candlestick chart — see Trading, above.)

### Status

Full-screen CCCCWIDE framework readiness display. Scrollable sections:

1. **Session** — Current session state
2. **Volatility** — DVOL (ETH) / VXN (QQQ) with layer classification
3. **Options** (renamed from "PCVR" 2026-09-01) — Put/Call Volume Ratio with regime determination, plus (same-day user request) a **Net Volume** readout per tracked Net Drift asset (ETH, QQQ, NDX, TLT, VIX — see Net Drift, above) shown above the PCVR value: status (`Positive`/`Negative`/`Neutral`/`n/a`, color-coded) and the raw filtered net-volume value it was classified from, e.g. `Positive (+142.30)`, refreshed on the same 15s cadence the trading engine itself reads (see Net Volume, below) so the display and any live trading decision never disagree mid-tick.
4. **High-Probability Levels** — VAH, VAL, POC, VWAP, SD bands, Expected Range, BT/ST, gamma clusters. BT/ST and (2026-08-28, explicit user request) POC are shown here for information but never gate an entry on their own — `hpl_any_active` excludes them the same way, since they're directional profit-take TARGETS ("price is near POC" isn't itself a "key level" condition that should arm a trade), not a standalone HPL condition. VAH/VAL/VWAP are unaffected — still both HPL-gating conditions and top-tier targets.
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

### BTD Confirmation

In **BTD** trading mode (`[9]` toggle), entries fire off `btd_confirmation` instead of footprint confirmation: the most recently closed 1m bar's own buy-imbalance-volume (for a long) or sell-imbalance-volume (for a short) — `(close-low)/(high-low) * volume` on the buy side, mirrored for sell — must exceed a rolling `mean + sigma * stdev` computed over the trailing `BTD_ENTRY_LOOKBACK` (10) bars. Lower `sigma` = lower bar to clear = more frequent signals.

`BTD_ENTRY_SIGMA` is **per-asset** (2026-08-31 user-reported — "Reduce QQQ's BTD sensitivity to 2.5 from 3.0. There are too few signals and they often miss the big moves"): `{"ETH": 3.0, "QQQ": 2.5}`. ETH's threshold is unchanged; QQQ's own is now easier to clear, so it should fire more often — including on moves that a flat 3.0 bar was missing entirely.

### NV Confirmation

**NV** (2026-09-01 user request) is a third trading mode, `[9]`-cycled alongside Order Flow and BTD: `Order Flow → BTD → NV → Order Flow`. It shares BTD's *exact* candle-close confirmation mechanism (same `btd_confirmation` call, same entry/target/sizing/risk-management pipeline downstream — none of that changed) — the only thing NV changes is where `regime` (the long/short directional bias) comes from. Every other mode derives it from PCVR (a single TLT-based ratio shared by both ETH and QQQ); NV derives it **per-asset**, independently, from that asset's own **Net Volume** status (see below) — ETH always reads ETH's own Net Volume, QQQ always reads QQQ's own, with no cross-asset or TLT influence at all. Entry-placement events log the actual mode (`"trigger": "BTD"` or `"trigger": "NV"`) instead of always saying "BTD", so trade-log/backtest analysis can tell the two apart.

**Immediate close on a Net Volume flip (2026-09-01 same-day follow-up):** needed no new code. The existing PCVR-flip emergency-close (`_manage_position`'s own `flipped` check, and the analogous `pending_flipped` check for a still-resting entry) already just compares the open position's side against whatever `regime` the CURRENT cycle's `instrument_lights` call produced — and under NV that's already Net-Volume-derived. A Positive→Negative (or Negative→Positive) flip trips it exactly like a PCVR flip always has, market-closes the position, and logs `pcvr_flip_close` (event name unchanged — it's the same mechanism, just fed by a different regime source under NV). Merely moving to Neutral does **not** trigger this — `regime` becomes `None`, which matches neither side of the `flipped` check, same as PCVR's own dead-zone never force-closing a position on its own.

The compact per-instrument readiness row on the Trading dashboard relabels two of its six lights based on the active mode — **independently**, not as one shared substitution: the `Order Flow` light reads **BTD** whenever BTD, NV, *or* NV-Auto is active (all three share the identical candle-close-derived trigger downstream of the confirmation check itself), while the `PCVR` light reads **NV** only when NV or NV-Auto is active (its data source is what actually changed) and stays `PCVR` under BTD. An NV/NV-Auto row therefore shows `NV` and `BTD` side by side, never `NV` twice.

### NV-Auto Confirmation

**NV-Auto** (2026-09-02 user request) is a fourth trading mode, `[9]`-cycled after NV: `Order Flow → BTD → NV → NV-Auto → Order Flow`. It shares every rule NV has (Net-Volume-derived per-asset regime, same target/sizing/risk-management pipeline) with one change to *when* an entry fires: instead of waiting for the next BTD-style candle-close confirmation, NV-Auto enters **immediately** the instant Net Volume goes Positive or Negative — the ARMED→confirmation dispatch skips the "once per new bar" dedup entirely and re-checks every cycle while armed, which is safe because becoming ARMED at all under NV/NV-Auto already requires a decisive (non-Neutral) regime, and a successful order placement moves the instrument out of WATCHING/ARMED immediately, naturally stopping further attempts.

**ETH day-rollover flatten (00:00 CT):** since Net Volume's own cumulative counter resets to 0 at CT midnight, any open ETH position is force-flattened at that instant (`reason="nv_auto_midnight"`) — a clock-time trigger, not a status-derived one, guarded to fire exactly once per calendar day so it can't also flatten a brand-new position NV-Auto opens later that same hour once Net Volume goes decisive again. QQQ's own analogous "flatten at 15:00 CT" rule needs no separate implementation — QQQ can never hold a position under any mode any more (see Trading mode's own QQQ-removal note, above).

**Win progression — "1R+0.33W"** (2026-09-02 user request, NV-Auto only, not a `[0]`-selectable Sizing Mode): a winning trade arms a boost for the next entry worth **33%** of that win's own net PnL, conserving the other 66% rather than rolling the whole profit forward the way Aggressive's "1R+W" does — "I'd like to conserve 66% of the profits and just have a portion used for compounding... keep more of the profits instead of losing all of it in one or two trades." Tracked in its own `nv_auto_pending_boost_dollars` state, independent of Aggressive's own `pending_boost_dollars` — the two can never interact since NV-Auto's progression applies unconditionally, regardless of which `[0]` Sizing Mode is active.

### Net Volume

**Net Volume** (2026-09-01 user request) is a lightweight directional-bias readout, sampled every 15s (`NET_VOLUME_POLL_SECS`) off Net Drift (Premium)'s own already-live engine — specifically its **filtered** (OTM-only) cumulative net volume (`net_vol_cum_f`, the same field `[F]` toggles into view in Net Drift mode), not the standard one. Reads as **Positive** (`net_vol_cum_f > 0`), **Negative** (`< 0`), or **Neutral** (exactly `0` — added same-day follow-up, "if Net Volume is 0, its status should be 'Neutral'. When Neutral, no trades are to be taken"; a bare `> 0`/`<= 0` split had silently folded an exact-zero reading into Negative). Neutral (like the `None`/not-yet-available state) maps to `regime = None` under NV/NV-Auto, the same "can't arm" value PCVR's own 0.98–1.02 dead-zone already produces — no separate gating needed. ETH samples continuously; QQQ/NDX/TLT/VIX only within their own 08:30–15:00 CT window (a dedicated window, not a reuse of the 08:45–15:00 CT gate used elsewhere). Displayed in the Data view's own Status screen, section **3. Options** (renamed from "PCVR" the same request), each status followed by the raw value it was classified from (e.g. `Positive (+142.30)`) sourced from the exact same 15s snapshot, never a second, possibly-inconsistent read — and consumed directly by NV/NV-Auto's own regime derivation, so the display and the trading decision never disagree mid-tick. **Only ETH is ever traded from this data** (2026-09-02) — QQQ/NDX/TLT/VIX are tracked for monitoring/comparison only ("I still want & need that data in order to compare with the others"), per the QQQ trading-removal decision below; the "each asset traded independently" framing from when this was first built (ETH from ETH's own Net Volume, QQQ from QQQ's own) no longer applies now that QQQ can't trade at all — its Net Volume is display data only now, same as NDX/TLT/VIX.

**TLT and VIX's own filtered figure now uses the nearest available expiry, not raw (2026-09-02 user-reported — "the Net Volume has stayed 0 all morning long for TLT & VIX. Should I use the filtered version for these two assets, or the non-filtered version?"):** confirmed live against CBOE's own chain data — the filtered metric's OTM classification was gated behind same-day (`is_0dte`) availability (see `_drift_poll_once_cboe`: `otm = is_0dte and _drift_is_otm(...)`), which QQQ/NDX both have every trading day, but TLT and VIX structurally never do — TLT's nearest listed expiry was a full day out, VIX's a full week out (VIX only ever expires weekly, on Wednesdays) at the moment this was checked. That permanently zeroed the filtered figure for these two by construction, regardless of how much real options flow was happening. Fixed at the source rather than falling back to raw: `DRIFT_FILTER_NEAREST_EXPIRY_ASSETS = (TLT, VIX)` relaxes the `is_0dte` requirement for just these two, so their own OTM filter now applies to whichever expiry `_drift_fetch_cboe_chain` actually fetched (nearest available — the SAME chain the raw figure already reads), restoring a genuine "speculative OTM positioning" signal instead of raw's own ITM-diluted one — closer in kind to what ETH/QQQ/NDX's own filtered figure already means, better for the stated cross-asset comparison goal. QQQ/NDX keep the stricter same-day-only rule unchanged (and satisfy it most days anyway).

### Entry Blackout

No new entries are placed between 19:00–19:30 CT (the daily session boundary).

---

## Sizing Modes

**2026-08-27: replaces the old Blackjack loss-progression ladder entirely.** Three user-selectable sizing modes, cycled with `[0]`, all built on the same base risk-per-trade amount (`[P]`, see Risk Sizing below):

- **Standard** — every trade risks exactly the base amount. No progression.
- **Aggressive ("1R+W")** — a trade taken at base risk that nets a profit arms a **one-shot boost** for the very next trade: that next trade risks base + the previous winner's own dollar PnL. The boosted trade's own result — win or lose — is never itself examined for a further boost; the trade after a boosted trade always reverts to bare base risk, unconditionally. A new boost only ever arms again from some later trade taken at base risk that wins.
- **Aggressive/1R+0.33W** (2026-09-02 user request) — identical mechanic to Aggressive, except the one-shot boost only ever carries **33%** of the winning trade's own dollar PnL forward (conserving the other 67%), not the full amount. Same one-shot/never-re-examined/reverts-to-base rule otherwise.

A fourth, separate "1R+0.33W" progression exists but is **not** one of these three `[0]`-selectable modes — it's inherent to **NV-Auto** trading mode specifically (see Trading Engine's own NV-Auto Confirmation section), applies regardless of which Sizing Mode is active, and uses its own isolated boost slot so switching between NV-Auto and Aggressive/1R+0.33W elsewhere can never cross-arm a boost the other one actually produced.

**Safety Limits** (independent of sizing mode):
- **Daily Loss Limit** — 5 consecutive losses blocks the asset until the next 19:30 CT rollover or a PCVR regime switch.
- **Max Win Limit** — 5 consecutive wins blocks the asset similarly.
- **Drawdown De-Risking Ladder** — see below; account-wide, not per-asset.

State is persisted to `sizing_state.json` and survives restarts. (This file replaces `blackjack_state.json`, which is migrated once on first run after upgrading — only the persisted trading-mode choice is carried over, since it happened to live in the same reserved-key file.)

---

## Risk Structures (Fixed / VE)

**2026-09-02 user request** — two SL/TP management structures, `[8]`-cycled, that apply **only to ETH, only under NV or NV-Auto trading mode** (every other asset/mode combination is unaffected and keeps the normal [Target System](#target-system)-driven TP1/TP2 selection):

- **Fixed** — TP1 is set exactly $20 from entry, TP2 exactly $30 (`NV_FIXED_TP1_DISTANCE`/`NV_FIXED_TP2_DISTANCE`), split 50/50 same as the standard target system. SL begins static at $10. Once TP1 fills, SL moves to **true breakeven** — "the level where the open position's entry & exit trading costs are recovered," i.e. exactly $0.00 of net profit beyond fees, not the $1.00/unit cushion every other mode's own TP1-breakeven-lock gives (`_apply_tp1_breakeven_lock`'s `net_per_unit` parameter is `0.0` here, `1.00` everywhere else).
- **VE** — no TP1/TP2 are ever placed (`tp_legs` stays empty). Instead, **1/3 of the position closes each time a VolEffort signal (`[6]` in the OHLC panel) shows absorption in the direction OPPOSING the trade** — a bullish absorption bar while short, a bearish absorption bar while long — up to 3 signals, at which point the 3rd signal closes whatever remains (never leaves dust from the `/3` rounding). SL begins at $10; once price has moved **2R** into profit (`NV_VE_BREAKEVEN_R_MULTIPLE`), SL moves to the same true-breakeven level Fixed's own TP1 lock uses. Since VE has no TP legs for the generic TP1-partial-fill-detection code to react to, its own breakeven move and partial closes are both handled directly in `_manage_position`, and the trade is pre-marked as if its TP1 lock had already fired at entry time so the generic code never tries.

Persisted as `_risk_structure` in `sizing_state.json`, same reserved-key pattern as `_trading_mode`/`_sizing_mode`.

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
| 2 | VWAP | Session volume profile (CH_STATE, kline_p-fed; REST fallback) | Either | Yes | No (static) |
| 3 | POC | Session volume profile (CH_STATE, kline_p-fed; REST fallback) | Either | Yes | No (static) |
| 4 | VAH | Session volume profile (CH_STATE, kline_p-fed; REST fallback) | Long only | Yes | No (static) |
| 5 | VAL | Session volume profile (CH_STATE, kline_p-fed; REST fallback) | Short only | Yes | No (static) |
| 6 | Large Cluster | Live gamma (gex.py) | Either | Yes | Yes |
| 7 | Medium Cluster | Live gamma (gex.py) | Either | **No (the only fallback-tier target)** | Yes |
| 8 | ER 40% | Session open + IV | Either | Yes | No (static) |
| 9 | ER 80% | Session open + IV | Either | Yes | No (static) |
| 10 | ER 100% | Session open + IV | Either | Yes | No (static) |
| 11 | ER 150% | Session open + IV | Either | Yes | No (static) |
| 12 | GEX Flip | Live gamma (gex.py) | Either | Yes | Yes |

**Top-tier gating changed 2026-08-31 (explicit user request — "Add ER 40/80% levels as top-tier targets. Cluster (Medium) is the only target that is not a top-tier target" + "ER 100/150% levels should also be considered top-tier targets"):** `_is_top_tier` used to be an inclusion list (BT/ST/VWAP/POC/VAH/VAL/Large Cluster); it's now an exclusion — everything is top-tier except a Medium cluster. ER 40/80/100/150% and GEX Flip (previously fallback-only alongside Medium clusters) are promoted as a direct consequence of that wording. ER 40%/80% didn't exist as targets at all before this — they were OHLC chart fill-overlay boundaries only; `reconstruct_targets` now loops over all four ER tiers (was just 100/150%) the same way, nearest-to-farthest, same side-matching-regime rule. GEX Flip's *priority order* is unchanged (still placed last, still lowest priority among equally-valid candidates) — only its top-tier/fallback *gating* status changed; it can now justify entry on its own with nothing else present, same as every other type except Medium clusters.

**VWAP, POC, VAH, VAL** (2026-08-27, added as top-tier targets) are preferentially read from the same live `CH_STATE[asset].indicator_levels` numbers the Status screen's own HPL display already shows, so a target line here is normally numerically consistent with what's on screen. All four still need to sit on the profit side of current price for the trade's own regime (a VWAP behind price isn't a target) — VAH and VAL carry an *additional* type-based restriction on top of that: VAH (the value area's own ceiling) is only ever a target in a **long**, VAL (the floor) only ever in a **short**, regardless of which side of price it happens to sit on. VWAP/POC have no such restriction — either regime can target either. Unlike BT/ST/Cluster/GEX Flip, none of the four get resynced mid-trade — session VAH/VAL/POC/VWAP drift continuously rather than snapping to a new options-driven level the way the others do, so a TP leg tracking one is left where it was placed at entry (same treatment ER 100%/150% already get).

**Reliability fix (2026-08-31 user-reported — "Athena is currently in a long position and VAH never appeared as a target"):** `CH_STATE[asset].indicator_levels` is live-WS-fed and starts empty on every restart, repopulating only once that feed has accrued enough fresh data — a gap that can outlast a single entry decision. Checking the trade logs (`athena_logs/`) back to 2026-08-27 confirmed this wasn't a one-off: roughly half of all logged entries since the feature launched were missing VWAP/POC/VAH/VAL entirely, including the trade that prompted this report (`athena_logs/2026/08/31/athena_08_31_2026.jsonl`, `btd_entry_placed` @ 01:30:05 — `targets: [BT, ER 100%, ER 150%, GEX Flip]`, no VAH). `evaluate_hpls` (the Data view's own HPL evaluator) computes the exact same four numbers a second, independent way — off REST-polled session candles, already proven reliable since it's what that Data view always shows correctly — and now returns them (`inst["vp_levels"]`, set in `compute_dashboard_snapshot`). `instrument_lights` still prefers `CH_STATE`'s number when it has one, but now falls back to `vp_levels` when it doesn't, instead of silently treating the target as unavailable. Verified with a synthetic test: with `CH_STATE[asset].indicator_levels` cleared (simulating the exact post-restart gap seen in the logs), VAH now correctly appears in `targets_full`; with `CH_STATE` populated, its number is still the one used, not the fallback's.

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
- **PCVR Flip Close** — If PCVR flips to the opposite extreme (long position + PCVR ≥ 1.02, or short + PCVR ≤ 0.98), the position is market-closed immediately. Under NV/NV-Auto, the same mechanism reacts to a Net Volume flip (Positive↔Negative) instead — no separate code path, `regime` is just sourced differently.
- **EOD Flatten** — QQQ positions used to close at market close (15:00 CT); moot since 2026-09-02 — QQQ can no longer hold a position under any mode (see Trading mode's own QQQ-removal note). ETH under NV-Auto instead flattens at **00:00 CT** (see NV-Auto Confirmation) since Net Volume's own counter resets there.
- **Risk Structures (Fixed / VE)** — ETH-only, NV/NV-Auto-only alternate SL/TP management; see [Risk Structures](#risk-structures-fixed--ve) above.
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
| `sizing_state.json` | Sizing mode, Aggressive's pending win-boost per asset, NV-Auto's own "1R+0.33W" pending boost per asset, the persisted trading mode (Order Flow/BTD/NV/NV-Auto), and the persisted risk structure (Fixed/VE) |
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
| `0` | Cycle sizing mode (Standard → Aggressive → Aggressive/1R+0.33W) — candle/line style toggle instead, while viewing the OHLC profile. Aggressive/1R+0.33W added 2026-09-02, see Sizing Modes |
| `1` | Clear an active Drawdown Full Stop block (manual review) |
| `2` | Toggle the profit ratchet (trail an open stop to lock +(N-1)R at each +NR) — 2026-09-02 bugfix: used to silently do nothing while viewing the OHLC profile (a stray copy-paste of `0`'s own guard, with no actual OHLC-mode meaning of its own to justify it); now works in every view |
| `8` | Cycle risk structure (Fixed ↔ VE) — ETH only, under NV/NV-Auto (2026-09-02, see Risk Structures) |
| `9` | Cycle trading mode (Order Flow → BTD → NV → NV-Auto → Order Flow) — renamed 2026-08-27, was "CCCCWIDE"; NV added 2026-09-01, NV-Auto added 2026-09-02 (see Trading Engine's own NV/NV-Auto Confirmation sections) |
| `Y` | OHLC profile, QQQ pane only — cycle candle chart → Markets macro overview → NDX candlestick chart → back (NDX added 2026-09-02) |
| `4` | OHLC profile — toggle VAH/VAL/POC Historical Mode (on by default as of 2026-08-27). Current state (`VP:Normal`/`VP:Historical`) shows right in the pane's own header (2026-08-28) |
| `6` | OHLC profile — toggle **VolEffort** (2026-08-29), ported from `vol-effort.py`. Replaces the bottom VOL histogram with a z-score histogram of volume-per-range "effort" — off by default. Header shows `VolEffort:On` when active |
| `F` | Flatten position |
| `G` | Toggle live/sim mode |
| `H` | Full key-reference overlay for this mode |
| `R` (double) | Reset paper account (dry-run only) |

### GEX Mode

| Key | Action |
|---|---|
| `G` | Toggle dot-map / by-strike |
| `N` | Toggle net/gross |
| `X` | Toggle crosshair (by-strike view only, 2026-08-28) — `←`/`→` move it while on, otherwise they pan the strike window as usual. Readout shows the selected strike's own Net GEX (or Call/Put, in non-net mode) in the info line. Confined to strikes that actually have a nonzero value in either direction (2026-08-29 fix) — the visible pan window routinely extends into deep-OTM strikes with $0 gamma either side, and activating/moving into that dead space put the crosshair nowhere near an actual bar. Activates at the strike nearest the live spot price, not the rightmost strike in view (2026-08-30 user-reported — "still starts at the farthest end of the graph. Have it begin where the current price is."): `crosshair_idx` is stored as "N candidates back from the rightmost nonzero strike," and activation used to always set it to 0 — with `vert_follow` on, spot sits near the MIDDLE of the visible window, not its right edge, so that landed the crosshair far from price. `[X]` now resolves the candidate nearest spot (`_gex_by_strike_spot_ch_idx`, replicating the render's own visible-window math) and stores ITS position in that same convention instead — `←`/`→` still walk away from/back toward the edge exactly as before, just starting from price instead of the edge |
| `Tab` | Switch asset — cycles `ETH → QQQ → NDX → TLT → VIX → ETH` (2026-09-02; NDX/TLT/VIX are monitoring-only) |
| `Arrows` | Pan |
| `[` / `]` | Zoom |
| `H` | Full key-reference overlay for this mode |

### Net Drift / CVD / Volatility Drift

| Key | Action |
|---|---|
| `Tab` | Switch asset — Net Drift's own cycle is `ETH → QQQ → NDX → TLT → VIX → ETH` (2026-09-02; QQQ/NDX/TLT/VIX are all monitoring-only here, see Net Drift above). CVD/Volatility Drift are unaffected — still plain ETH/QQQ `chart_focus` switching, since QQQ's own market data keeps flowing even though it can no longer trade. |
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
