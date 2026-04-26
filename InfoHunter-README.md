# InfoHunter
**Quantasset Terminal News Aggregator**

Real-time financial headlines aggregated from 26 free RSS sources, ranked by market impact using a hybrid rule-based + AI scoring engine, displayed in a scrollable terminal UI.

---

## Features

- **26 free RSS sources** across 5 categories: Central Bank, Macro, Forex, Markets, Crypto
- **Hybrid impact scoring**: 80+ compiled regex rules + optional Claude AI re-scoring for nuanced events
- **12-hour rolling window** — up to 2000 headlines retained at all times
- **15-second auto-refresh** — as close to real-time as RSS allows
- **Fully scrollable** terminal UI built on [Textual](https://textual.textualize.io/)
- **AI marker (✦)** on headlines that have been re-scored by Claude
- **Filter by impact** (ALL / HIGH / MEDIUM / LOW) and **category**
- **Freetext search** across titles, sources, and categories
- **Detail view** with summary, tags, AI reason, and full article URL

---

## Installation

```bash
pip install feedparser textual requests
```

### Optional: AI Scoring — Local (Ollama, free) or Cloud (Anthropic)

InfoHunter supports two AI backends for re-scoring ambiguous headlines.
Set the `INFOHUNTER_AI` environment variable to activate one:

#### Option A — Ollama (free, runs locally, recommended)

Ollama runs a local LLM entirely on your machine — no API costs, no internet required for scoring.
Works on Windows, Linux, macOS, and Android (via Termux).

**Windows setup:**
```powershell
# 1. Download and install Ollama from https://ollama.com
# 2. Pull a model (pick one based on your RAM):
ollama pull qwen2.5:3b      # ~2 GB RAM — fast, great quality, recommended
ollama pull qwen2.5:7b      # ~5 GB RAM — better reasoning
ollama pull llama3.2:3b     # ~2 GB RAM — good alternative
ollama pull phi3.5          # ~2 GB RAM — very fast on low-power hardware

# 3. Enable in InfoHunter:
$env:INFOHUNTER_AI = "ollama"
python infohunter.py
```

**Android / Termux setup (OnePlus 7T Pro, 11 GB RAM):**
```bash
pkg update && pkg install ollama
ollama serve &              # start server in background
ollama pull qwen2.5:3b     # ~2 GB — leaves plenty of headroom
export INFOHUNTER_AI=ollama
python infohunter.py

# Optional: use a larger model if you have RAM to spare
export OLLAMA_MODEL=qwen2.5:7b
```

**Recommended models by RAM budget:**

| Model | RAM | Notes |
|-------|-----|-------|
| `qwen2.5:3b` | ~2 GB | Best speed/quality ratio, default |
| `phi3.5` | ~2 GB | Microsoft, very fast on ARM |
| `llama3.2:3b` | ~2 GB | Meta, solid general reasoning |
| `qwen2.5:7b` | ~5 GB | Better nuance, fits in 11 GB |
| `mistral:7b` | ~5 GB | Strong reasoning, fits in 11 GB |

#### Option B — Anthropic Claude (paid, most accurate)
```powershell
$env:INFOHUNTER_AI = "anthropic"
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python infohunter.py
```

#### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `INFOHUNTER_AI` | `none` | `ollama` / `anthropic` / `none` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server address |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Model to use for scoring |
| `ANTHROPIC_API_KEY` | _(none)_ | Required if `INFOHUNTER_AI=anthropic` |

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
| `CB` | Magenta | Fed Reserve, ECB, BoJ, BoE, BIS, IMF |
| `MACRO` | Cyan | Reuters, WSJ, MarketWatch, CNBC, FT, AP, Investing.com |
| `FOREX` | Blue | ForexLive, FXStreet |
| `MARKETS` | Green | MarketWatch, CNBC, Bloomberg, Yahoo Finance |
| `CRYPTO` | Yellow | CoinDesk, Cointelegraph, The Block, Decrypt, Bitcoin Magazine |

---

## Scoring Architecture

### Rule Engine (always active)
80+ compiled regex rules covering:
- Central bank / monetary policy (Fed, ECB, BoJ, BoE, RBA, PBOC)
- Macro data releases (NFP, CPI, PCE, GDP, ISM/PMI, JOLTS, etc.)
- Market extremes (crashes, circuit breakers, bank failures, systemic risk)
- Geopolitical / energy shocks (wars, strait blockades, tanker/pipeline attacks, Houthi/Iran proxy actions, Red Sea disruptions, OPEC decisions, energy crises)
- Crypto events (ETF approvals, exchange hacks, stablecoin depegs, regulatory actions)
- Corporate (bankruptcy, earnings, major M&A, credit downgrades)
- Scale boosts for billion/trillion-dollar events
- Noise suppressors for sports, entertainment, lifestyle

Category base boosts: CB +4, MACRO +1, FOREX +1.
Thresholds: HIGH ≥ 7 | MEDIUM ≥ 3 | LOW < 3

### AI Re-Scoring (optional — Ollama or Anthropic)
- Runs every 60 seconds in a background thread
- Batches up to 12 un-scored headlines per pass, prioritising ambiguous rule scores (2–10)
- Re-scored headlines are marked with `✦` in the IMP column
- Understands context beyond keywords: geopolitical significance, supply chain impact, systemic risk
- **Ollama** (`INFOHUNTER_AI=ollama`): completely free, runs on your local machine or Android via Termux; recommended models: `qwen2.5:3b` (2 GB), `qwen2.5:7b` (5 GB)
- **Anthropic** (`INFOHUNTER_AI=anthropic`): Claude Haiku, most accurate, requires API key

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
| `AI_BATCH_SIZE` | `12` | Headlines sent to AI per scoring pass |
| `AI_RESCORE_INTERVAL` | `60` | Seconds between AI scoring passes |

---

## Changelog

### v1.4 — Current
- **Fixed**: Search (`S` key) caused full freeze/deadlock when closing — replaced `@work(thread=True)` + `push_screen_wait` pattern with `push_screen(callback=...)` which runs entirely on the main thread with no blocking; ESC and Enter both close correctly
- **Added**: Pluggable AI backend system — set `INFOHUNTER_AI=ollama` for free local scoring via Ollama (no API costs), or `INFOHUNTER_AI=anthropic` for Claude Haiku
- **Added**: Full Ollama support with configurable host (`OLLAMA_HOST`) and model (`OLLAMA_MODEL`); recommended models for 11 GB RAM: `qwen2.5:3b`, `phi3.5`, `llama3.2:3b`, `qwen2.5:7b`
- **Added**: `_parse_ai_response()` helper that robustly strips markdown fences and finds JSON arrays even if local models wrap output

### v1.3
- **Fixed**: Search (`S` key) crashed with `NoActiveWorker` on Python 3.14 / newer Textual builds — `action_search` is now decorated with `@work(thread=True)` so `push_screen_wait` always runs inside a worker thread as Textual requires

### v1.2
- **Fixed**: Crash when opening detail view on articles with URLs containing `[`, `]`, `&`, or other Rich markup special characters — all user content now fully escaped via `_esc()`
- **Fixed**: Search screen did not work — replaced broken `asyncio.create_task()` pattern with correct `await push_screen_wait()` in async action
- **Fixed**: Geopolitical events (Houthi attacks, tanker strikes, Red Sea disruptions, pipeline explosions, OPEC decisions, energy crises) were scoring LOW — added 15+ new geopolitical/energy rules covering chokepoints, proxy actors, vessel attacks, Red Sea, nat gas, supply shock
- **Added**: AI re-scoring layer via Claude Haiku API (optional, set `ANTHROPIC_API_KEY`)
  - Runs every 60 seconds in background thread
  - Re-scores ambiguous headlines for nuanced geopolitical and market context
  - AI-scored headlines marked with `✦` in IMP column
- **Changed**: Refresh rate lowered from 90s → **15s**
- **Added**: This README

### v1.1
- Added GDP contraction / miss / beat patterns
- Fixed NFP to match plural "payrolls"
- Fixed Tether/USDT depeg patterns
- Fixed Fed rate pattern to match "raises rates" (plural)

### v1.0
- Initial release: 26 RSS sources, rule-based scoring, Textual UI
