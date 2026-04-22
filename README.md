# Copycat — Blackjack Trade Copier
### Confidential — Fund Intellectual Property

A CLI tool that simultaneously executes ETH and BTC perpetual futures trades across
Phemex (personal account) and XLTRADE (Chimera MT5 prop firm) from a single
command. Supports market and limit orders, live position monitoring, account
balances, and full flatten across both brokers.

---

## How It Works

```
copycat.py  ──────────────────────────▶  Phemex API (USDT Perp)
                │
                └── file bridge ──▶  BlackjackCopier.mq5 ──▶  XLTRADE (MT5)
```

When you run a trade command, `copycat.py` fires both brokers simultaneously:
- **Phemex** — called directly via the Phemex REST API
- **XLTRADE** — signalled via a text file that `BlackjackCopier.mq5` (an MT5
  Expert Advisor) polls every 50ms and executes on your behalf

When running from **Termux (Android)**, a third piece called `bridge.py` runs
on your Windows PC and relays signals from your phone to the EA over HTTP:

```
copycat.py (phone)  ──HTTP──▶  bridge.py (PC)  ──files──▶  MT5 EA
                           \──HTTPS─────────────────────▶  Phemex API
```

All Phemex API calls are also proxied through `bridge.py` when running remotely,
so your phone's IP never touches Phemex directly (important if your API key has
an IP whitelist set to your home network).

---

## Files

| File | Purpose |
|---|---|
| `copycat.py` | Main CLI — run this to trade |
| `BlackjackCopier.mq5` | MT5 Expert Advisor — compile and run in MT5 |
| `bridge.py` | Network bridge — run on Windows PC when using Termux |
| `.env` | Your credentials (create from `.env.example`) |
| `requirements.txt` | Python dependencies |

---

## Prerequisites

**Windows (primary setup):**
- Python 3.10+
- MetaTrader 5 open and logged into XLTRADE (Chimera terminal)
- Phemex account in **Hedge Mode** with a USDT perpetual API key

**Termux / Android (remote setup):**
- Termux app installed
- Python and pip installed in Termux
- Windows PC running `bridge.py` on the same network (or via Tailscale)

---

## Setup — Windows

### 1. Create and activate a virtual environment

```bash
cd C:\path\to\trade-copier

python -m venv venv
venv\Scripts\activate
```

You should see `(venv)` appear at the start of your prompt.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure credentials

```bash
copy .env.example .env
```

Edit `.env` and fill in:

```
PHEMEX_API_KEY=your_phemex_api_key
PHEMEX_API_SECRET=your_phemex_api_secret
MT5_FILES_PATH=C:\Users\YourName\AppData\Roaming\MetaQuotes\Terminal\Common\Files
```

> **Finding MT5_FILES_PATH:** In MT5, go to File → Open Data Folder,
> navigate up one level, then open the `Common` folder, then `Files`.
> Copy that full path.

### 4. Install the MT5 Expert Advisor

1. Copy `BlackjackCopier.mq5` into your MT5 `Experts` folder
   (File → Open Data Folder → MQL5 → Experts)
2. Open MetaEditor (press F4 in MT5)
3. Open `BlackjackCopier.mq5` and press F7 to compile — should show 0 errors
4. In MT5, drag `BlackjackCopier` from the Navigator onto your ETHUSD.nx M1 chart
5. Enable **Allow automated trading** in the EA settings

The EA is now running. It polls for signals every 50ms and writes a heartbeat
file every 2 seconds so `copycat.py` knows it is alive.

### 5. Test the connection

```bash
python copycat.py balance
```

You should see your Phemex balance and XLTRADE account data.

---

## Running bridge.py (required for Termux)

`bridge.py` must be running on your Windows PC whenever you want to trade from
Termux. It proxies both MT5 signals and Phemex API calls from your phone through
the PC.

### Starting the bridge

```bash
# Navigate to your copycat folder
cd C:\path\to\trade-copier

# Activate the virtual environment
venv\Scripts\activate

# Start the bridge
python bridge.py
```

You should see output like:

```
──────────────────────────────────────────────────────
  COPYCAT BRIDGE — active
──────────────────────────────────────────────────────
  Local URL   http://192.168.1.42:7373
  MT5 path    C:\Users\...\Common\Files
  Auth        enabled
  ...
```

Note the **Local URL** — you'll use this IP in your phone's `.env` file.
Leave this terminal window open for as long as you want to trade remotely.

### bridge.py .env settings (Windows side)

Add these to your `.env` alongside your existing credentials:

```
BRIDGE_TOKEN=pick_any_random_string
BRIDGE_PORT=7373
```

`BRIDGE_TOKEN` is a shared secret — it must match on both your PC and phone.

---

## Setup — Termux (Android)

### 1. Install Termux dependencies

```bash
pkg install python git
pip install httpx
```

### 2. Clone the repo

```bash
git clone https://YOUR_GITHUB_TOKEN@github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
```

### 3. Configure credentials

```bash
cp .env.example .env
nano .env
```

Fill in your Phemex credentials plus the bridge settings:

```
PHEMEX_API_KEY=your_phemex_api_key
PHEMEX_API_SECRET=your_phemex_api_secret
BRIDGE_URL=http://192.168.1.42:7373
BRIDGE_TOKEN=pick_any_random_string
```

`BRIDGE_URL` uses the IP printed by `bridge.py` on your PC.
`BRIDGE_TOKEN` must be identical on both devices.

Save with `Ctrl+X` → `Y` → `Enter`.

### 4. Set up the copycat alias (optional but recommended)

Add to your `~/.bashrc` or `~/.zshrc`:

```bash
alias copycat='python ~/YOUR_REPO/copycat.py'
```

Then reload: `source ~/.bashrc`

> **For access outside your home network:** Install [Tailscale](https://tailscale.com)
> (free) on both your PC and phone. Use the Tailscale IP instead of your
> local IP in `BRIDGE_URL` — works from anywhere.

---

## Daily Workflow

**On your Windows PC:**
1. Open MT5 with `BlackjackCopier` running on the ETHUSD.nx M1 chart
2. Open a terminal in the `trade-copier` folder
3. Activate the venv: `venv\Scripts\activate`
4. Start the bridge: `python bridge.py`

**On Termux (phone):**
1. Run any copycat command — e.g. `python copycat.py balance` to confirm connection

---

## Usage

```
python copycat.py [eth|btc] buy   market -sp <phemex_size> -sx <xltrade_size> -tp <tp>
python copycat.py [eth|btc] buy   market -os -sp <phemex_size> -sx <xltrade_size>
python copycat.py [eth|btc] buy   limit  -p <price> -sp <phemex_size> -sx <xltrade_size> -tp <tp>
python copycat.py [eth|btc] sell  market -sp <phemex_size> -sx <xltrade_size> -tp <tp>
python copycat.py [eth|btc] sell  market -os -sp <phemex_size> -sx <xltrade_size>
python copycat.py [eth|btc] sell  limit  -p <price> -sp <phemex_size> -sx <xltrade_size> -tp <tp>
python copycat.py [eth|btc] positions
python copycat.py [eth|btc] positions --refresh
python copycat.py [eth|btc] orders
python copycat.py [eth|btc] balance
python copycat.py -f                    (closes ALL positions across ALL assets)
```

### Asset prefix

| Prefix | Phemex symbol | XLTRADE symbol |
|---|---|---|
| `eth` | ETHUSDT | ETHUSD.nx |
| `btc` | BTCUSDT | BTCUSD.nx |

The asset prefix is optional for non-trade commands — it defaults to ETH if omitted.
`-f` (flatten) requires no prefix and closes everything across all assets.

### Flags

| Flag | Description |
|---|---|
| `-p` | Limit price (limit orders only) |
| `-sp` | Position size on Phemex (ETH or BTC) |
| `-sx` | Position size on XLTRADE (lots) |
| `-tp` | Take profit price (Phemex) — XLTRADE TP auto-adjusted |
| `-os` | On-sides: fixed 1:2 R:R market order, no -tp needed |

### Commands

| Command | Description |
|---|---|
| `buy / sell` | Place a market or limit order on both brokers simultaneously |
| `positions` | Show all open positions with SL, TP, and unrealised PnL |
| `positions --refresh` | Live auto-refreshing positions view (updates every 5s) |
| `orders` | Show all pending and conditional orders on both brokers |
| `balance` | Show account balance, margin, open PnL, and today's closed PnL |
| `-f` | Close all positions and cancel all orders across all assets on both brokers |

### Examples

```bash
# ETH market orders
python copycat.py eth buy market -sp 3.41 -sx 2.72 -tp 2400.00
python copycat.py eth sell market -sp 3.41 -sx 2.72 -tp 2100.00

# ETH on-sides (fixed 1:2 R:R)
python copycat.py eth buy market -os -sp 3.41 -sx 2.72

# BTC limit order
python copycat.py btc buy limit -p 77000.00 -sp 0.01 -sx 0.01 -tp 77300.00

# Info
python copycat.py eth positions --refresh
python copycat.py balance
python copycat.py -f
```

---

## XLTRADE Adjustments

All XLTRADE parameters are automatically derived. You only ever specify the
Phemex values in your command:

| Parameter | Rule |
|---|---|
| Size | Specified separately with `-sx` flag |
| SL (Long) | Fill/limit price − $17.60 |
| SL (Short) | Fill/limit price + $17.60 |
| TP (Long) | Phemex TP + $2.60 |
| TP (Short) | Phemex TP − $2.60 |

For **market orders**, SL is set after fill using the actual fill price on
both brokers — not an estimate. The SL placement retries up to 3 times if
the first attempt fails.

**On-sides (-os):**

| | Phemex | XLTRADE |
|---|---|---|
| SL distance | ±$15.00 | ±$17.60 |
| TP distance | ±$30.00 | ±$32.60 |

---

## Troubleshooting

**`EA heartbeat not found`**
- The MT5 EA is not running. Make sure `BlackjackCopier` is on the chart with
  automated trading enabled and the MT5 terminal is open.

**`EA heartbeat stale`**
- The EA stopped responding. Remove it from the chart and re-add it.

**Phemex `API Signature verification failed`**
- Check that `PHEMEX_API_KEY` and `PHEMEX_API_SECRET` are correct in `.env`
- Ensure your system clock is accurate (signature includes a timestamp)

**`XLTRADE: OK|POSITIONS|NONE` when placing an order**
- A stale response file was left from a previous `positions` poll.
  This is automatically handled via pre-clearing, but if it persists,
  check that MT5 has write access to the files folder.

**XLTRADE `Timeout — EA did not respond`**
- On Windows: check the heartbeat file age and that MT5 is not frozen
- On Termux: check that `bridge.py` is running on your PC and that
  `BRIDGE_URL` and `BRIDGE_TOKEN` match on both devices

**`Bridge error: ...` on Termux**
- Confirm your PC and phone are on the same network (or both on Tailscale)
- Confirm `bridge.py` is running and printed `active` on startup
- Check that `BRIDGE_URL` uses the correct IP and port (default 7373)
- Check that `BRIDGE_TOKEN` is identical in both `.env` files
- Confirm the venv is activated on the PC before running `bridge.py`

**Phemex calls failing from Termux (IP whitelist error)**
- All Phemex calls from Termux are proxied through `bridge.py` on the PC,
  so your phone's IP never reaches Phemex. If you're getting IP errors,
  confirm `BRIDGE_URL` is set correctly in your phone's `.env` —
  if it's blank, copycat will attempt to call Phemex directly from the phone.
