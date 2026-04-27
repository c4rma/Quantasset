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

# Pull the model (one-time setup):
ollama pull tinyllama        # 638 MB — default, always fits, fast on ARM
# OR for better quality if RAM allows:
ollama pull phi3:mini        # ~2.3 GB
ollama pull qwen2.5:3b       # ~2 GB

# DO NOT run 'ollama serve' manually — InfoHunter manages it:
export INFOHUNTER_AI=ollama
export OLLAMA_MODEL=tinyllama   # or phi3:mini, qwen2.5:3b etc.
python infohunter.py
# InfoHunter will kill any existing Ollama, relaunch it silently (no log bleed),
# verify the model is loaded, then start the UI.

# Optional: use a larger model if you have RAM to spare
export OLLAMA_MODEL=qwen2.5:7b
```

**Recommended models by RAM budget:**

| Model | RAM | Notes |
|-------|-----|-------|
| `tinyllama` | ~1 GB | **Default** — always fits, fast on ARM, good for classification |
| `qwen2.5:0.5b` | ~500 MB | Smallest Qwen, extremely fast |
| `phi3:mini` | ~2.3 GB | Much better reasoning, recommended upgrade |
| `qwen2.5:3b` | ~2 GB | Good quality if you have RAM headroom |
| `qwen2.5:7b` | ~5 GB | Best quality, only if 8+ GB free |

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

### v1.10 — Current
- **Fixed**: Complete AI scoring rewrite — abandoned batch JSON approach entirely for Ollama; now scores **one headline at a time** asking for a single word (HIGH/MEDIUM/LOW); this solves every previous failure mode simultaneously:
  - No more 90s timeouts (each call asks for ≤8 tokens, returns in a few seconds)
  - No more JSON parsing failures or model copying the example
  - No more context window overflow
  - Works on tinyllama, phi3:mini, qwen2.5 — any model
- **Changed**: `num_predict=8` (was 512) — model only needs to output one word; stops immediately
- **Changed**: Timeout raised to 120s per headline to handle slow ARM inference
- **Added**: Debug log now shows one compact line per headline: `HH:MM:SS [HIGH  ] raw='HIGH' title=...` making it easy to monitor scoring in real time with `tail -f ~/infohunter_ai_debug.log`
- **Changed**: Anthropic backend still uses batch JSON (it's fast and reliable); Ollama always uses one-at-a-time

### v1.9
- **Fixed**: AI scoring silently doing nothing — root causes were (1) `exclusive=True` on the worker meant any call while a previous pass was running got silently dropped, especially on slow mobile hardware; replaced with a manual `_ai_running` mutex flag so calls queue correctly; (2) `last_ai_error` was never cleared between passes, so a single transient error permanently stopped all future scoring; now cleared at the start of each pass
- **Fixed**: `tinyllama` ignoring the system prompt — tinyllama doesn't reliably honour the `system` role in `/api/chat`; switched to `/api/generate` with the system instructions embedded directly in the prompt, which works on all Ollama models including the smallest ones
- **Added**: AI debug log at `~/infohunter_ai_debug.log` — logs raw Ollama prompt/response and any exceptions so failures are always visible; set `AI_DEBUG_LOG = ""` to disable
- **Changed**: `num_predict` reduced to 512 (enough for 8 headlines), added `stop` tokens to prevent runaway generation

### v1.8
- **Fixed**: Ollama GIN logs still bleeding into terminal — `dup2()` on Python's file descriptors has no effect on a *separate* Ollama process that was already started; the only real fix is for InfoHunter to **own** the Ollama process by killing the existing one and relaunching it with `stdout`/`stderr` captured via `subprocess.Popen`. No more log pollution regardless of how Ollama was originally started
- **Fixed**: HTTP 500 on every `/api/chat` request — `qwen2.5:3b` (~2 GB model weights + Android OS overhead) was exceeding available memory mid-inference; changed default model to **`tinyllama`** (638 MB, ~1 GB RAM total), which comfortably fits on any Android device
- **Fixed**: App never loads when Ollama returns 500s — the AI worker was blocking app startup; now fully non-blocking: startup sequence is (1) restart Ollama silently, (2) verify model, (3) launch UI immediately, (4) score headlines in background
- **Changed**: Ollama startup is now managed by InfoHunter — you no longer need to run `ollama serve` manually; InfoHunter kills any existing instance and relaunches it with logs captured
- **Changed**: Default Ollama model changed from `qwen2.5:3b` → `tinyllama` (much smaller, always fits in RAM)

### v1.7
- **Fixed**: App not loading at all — parallel Ollama requests (3 concurrent threads) caused HTTP 500 crashes because Ollama on mobile is single-threaded and cannot handle concurrent inference; replaced `ThreadPoolExecutor` with strict sequential processing (one batch at a time)
- **Fixed**: Ollama log output still bleeding into terminal on Android/Termux — Ollama on Android writes to **stdout** (fd 1), not just stderr; now redirects both fd 1 and fd 2 to `/dev/null` before Textual launches (safe because Textual renders via `/dev/tty` directly, not fd 1)
- **Fixed**: Script hangs on startup when Ollama isn't running — added pre-launch health check that hits `/api/tags`, verifies the model is loaded, and prints a clear warning + continues with rule-based scoring if Ollama is unreachable (instead of hanging)
- **Added**: AI scoring pass now stops early if Ollama returns errors, rather than hammering it with more requests
- **Added**: 6-hour headline window when AI is enabled (vs 12h normally) — reduces the number of headlines that need scoring on startup, making AI mode viable on mobile hardware
- **Changed**: Max batches per AI pass capped at 5 (40 headlines) to prevent the worker from blocking the app for too long on a slow mobile CPU

### v1.6
- **Fixed**: Ollama GIN HTTP server logs bleeding into the terminal UI — stderr is now silenced at the OS file-descriptor level before Textual launches, so log lines can no longer corrupt the display
- **Fixed**: AI response parsing failing for `qwen2.5:3b` and similar small models that add prose preamble, trailing text, or markdown fences around JSON — replaced greedy regex with bracket-counting parser that reliably extracts the array regardless of surrounding text
- **Fixed**: Prompt overflowing small model context windows — batch size reduced from 20 → 8 headlines per call; summary truncated to 120 chars; `source`/`category` fields removed from payload (title + summary is sufficient for classification)
- **Fixed**: System prompt too verbose for instruction-following on 3B models — rewritten to be terse and JSON-first with a concrete output example
- **Added**: Start Ollama silently in Termux with `ollama serve 2>/dev/null &` (shown in startup tip)

### v1.5
- **Fixed**: AI scoring never actually ran on most headlines — `get_unscored_batch` only returned 12 ambiguous headlines per 60s pass, meaning hundreds of headlines would take many minutes to score (if ever). Replaced with `get_unscored()` which returns ALL unscored headlines, processed in parallel batches via `ThreadPoolExecutor` (3 concurrent calls)
- **Fixed**: AI worker silently swallowed all exceptions — errors are now surfaced in the status bar in red so you can see if Ollama isn't responding
- **Fixed**: Payload sent to AI did not include summary — now includes up to 300 chars of summary per headline, giving the model full context
- **Fixed**: AI only ran on "ambiguous" rule scores (2–10) — now scores every headline when `INFOHUNTER_AI` is set, so every entry gets `✦`
- **Fixed**: AI worker only fired on a timer — now also triggers immediately after each fetch cycle via `on_worker_state_changed`, so new headlines are scored within seconds of arrival
- **Changed**: `AI_RESCORE_INTERVAL` lowered from 60s → 20s; `AI_BATCH_SIZE` raised from 12 → 20
- **Added**: Status bar now shows count of AI-scored headlines (`✦`) and pending count in yellow while scoring is in progress

### v1.4
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
