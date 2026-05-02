# InfoHunter
**Quantasset Terminal News Aggregator**

Real-time financial headlines from 26 free RSS sources, ranked by market impact using a rule-based scoring engine, displayed in a scrollable terminal UI.

---

## Features

- **26 free RSS sources** across 5 categories: Central Bank, Macro, Forex, Markets, Crypto
- **Rule-based impact scoring**: 80+ compiled regex rules covering all major market-moving event types
- **12-hour rolling window** — up to 2000 headlines retained at all times
- **15-second auto-refresh** — as close to real-time as RSS allows
- **Fully scrollable** terminal UI built on [Textual](https://textual.textualize.io/)
- **Filter by impact** (ALL / HIGH / MEDIUM / LOW) and **category**
- **Freetext search** across titles, sources, and categories
- **Detail view** with summary, tags, and full article URL

---

## Installation

```bash
pip install feedparser textual requests
```

---

## Usage

```bash
python infohunter.py
```

---

## Keybindings

| Key | Action |
|-----|--------|
| `↑` / `↓` | Scroll headlines |
| `PgUp` / `PgDn` | Fast scroll |
| `Enter` | Open detail view |
| `R` | Force refresh now |
| `F` | Cycle impact filter: ALL → HIGH → MEDIUM → LOW |
| `C` | Cycle category filter: ALL → CB → MACRO → FOREX → MARKETS → CRYPTO |
| `S` | Open search |
| `ESC` | Clear all filters |
| `Q` | Quit |
| `H` / `?` | Help screen |

---

## Impact Levels

| Level | Color | Examples |
|-------|-------|---------|
| **HIGH** | 🔴 Bold Red | FOMC decisions, NFP/CPI/GDP prints, war/invasion, strait blockades, tanker/pipeline attacks, OPEC surprises, bank failures, stablecoin depegs, major crypto hacks |
| **MEDIUM** | 🟡 Yellow | Fed-speak, PMI data, earnings beats/misses, M&A, geopolitical tension, regulatory proposals |
| **LOW** | ⬜ Dim | Routine company news, analyst ratings, recaps, lifestyle |

---

## Categories

| Tag | Color | Sources |
|-----|-------|---------|
| `CB` | Magenta | Fed Reserve, ECB, IMF, BIS |
| `MACRO` | Cyan | Reuters, WSJ, MarketWatch, CNBC, FT, AP, Investing.com |
| `FOREX` | Blue | ForexLive, FXStreet |
| `MARKETS` | Green | MarketWatch, CNBC, Bloomberg, Yahoo Finance |
| `CRYPTO` | Yellow | CoinDesk, Cointelegraph, The Block, Decrypt, Bitcoin Magazine |

---

## Scoring Engine

80+ compiled regex rules covering:

- **Central bank / monetary policy** — Fed, ECB, BoJ, BoE, RBA, PBOC rate decisions, QT/QE, yield curve
- **Macro data releases** — NFP, CPI, PCE, GDP, ISM/PMI, JOLTS, jobless claims, retail sales
- **Market extremes** — crashes, circuit breakers, bank failures, systemic risk, VIX spikes
- **Geopolitical / energy shocks** — wars, strait blockades (Hormuz, Suez, Bab el-Mandeb), tanker/pipeline attacks, Houthi/Iran proxy actions, Red Sea disruptions, OPEC decisions, energy crises, sanctions
- **Crypto** — ETF approvals, exchange hacks, stablecoin depegs, regulatory actions, BTC halving
- **Corporate** — bankruptcy, earnings, major M&A, credit downgrades
- **Scale boosts** — billion/trillion-dollar events score higher
- **Noise suppressors** — sports, entertainment, lifestyle, routine analyst notes score lower

Category base boosts: CB +4, MACRO +1, FOREX +1.
Thresholds: **HIGH** ≥ 7 | **MEDIUM** ≥ 3 | **LOW** < 3

---

## News Sources (26 total)

**Central Bank:** Federal Reserve, ECB, IMF, BIS

**Macro:** Reuters Business, Reuters Finance, Reuters Top News, AP Business, MarketWatch (Top + Economy), WSJ (Markets + World), CNBC (Finance + Economy), Yahoo Finance, FT, Bloomberg Markets, Investing.com Economy

**Forex:** ForexLive, FXStreet

**Crypto:** CoinDesk, Cointelegraph, The Block, Decrypt, Bitcoin Magazine, Investing.com Crypto

---

## Configuration

Key constants at the top of `infohunter.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `REFRESH_INTERVAL_SECONDS` | `15` | How often all feeds are polled |
| `WINDOW_HOURS` | `12` | Rolling headline retention window |
| `MAX_HEADLINES` | `2000` | Hard cap on stored headlines |

---

## Changelog

### v1.15 — Current
- **Fixed**: Scroll position resetting after the user scrolls between refreshes — replaced snapshot-based restore with continuous tracking via `on_scroll_changed`; `_user_scroll_y` is updated every time the user moves the viewport (mouse wheel, scrollbar, keyboard), so the restore always uses the latest position rather than a stale pre-rebuild snapshot

### v1.14
- **Fixed**: Scroll position not preserved on refresh when using mouse wheel or scrollbar — `move_cursor` was triggering an auto-scroll-into-view that overrode the restored position; now uses `call_after_refresh` + `set_scroll` to restore the exact scroll offset after the layout pass settles

### v1.13
- **Fixed**: Scroll position jumping to top on every feed refresh — cursor row and scroll offset are now saved before the table is cleared and restored after it's rebuilt

### v1.12
- **Fixed**: `ScreenStackError` crash from accidental command palette activation on mobile — disabled with `COMMANDS = set()` and `ENABLE_COMMAND_PALETTE = False`
- **Simplified**: Startup is instant, no external dependencies

### v1.4
- Fixed search freeze/deadlock using `push_screen(callback=...)` pattern

### v1.3
- Fixed search crash (`NoActiveWorker`) with `@work(thread=True)`

### v1.2
- Fixed detail view crash on URLs with special characters
- Fixed geopolitical scoring (Houthi, Red Sea, pipeline, OPEC)
- Lowered refresh rate to 15s

### v1.1
- Fixed NFP, GDP, Fed rate, and stablecoin depeg scoring patterns

### v1.0
- Initial release: 26 RSS sources, rule-based scoring, Textual UI
