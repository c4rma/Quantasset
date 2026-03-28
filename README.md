# Copycat — Blackjack Trade Copier
### Confidential — Fund Intellectual Property

A CLI tool that simultaneously executes ETH perpetual futures trades across
Phemex (personal account) and XLTRADE (Chimera MT5 prop firm) from a single
command. Supports market and limit orders, live position monitoring, account
balances, and full flatten across both brokers.

---

## How It Works

```
copycat.py  ──────────────────────▶  Phemex API (USDT Perp)
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
```

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

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

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

### 3. Install the MT5 Expert Advisor

1. Copy `BlackjackCopier.mq5` into your MT5 `Experts` folder
   (File → Open Data Folder → MQL5 → Experts)
2. Open MetaEditor (press F4 in MT5)
3. Open `BlackjackCopier.mq5` and press F7 to compile — should show 0 errors
4. In MT5, drag `BlackjackCopier` from the Navigator onto your ETHUSD.nx M1 chart
5. Enable **Allow automated trading** in the EA settings

The EA is now running. It polls for signals every 50ms and writes a heartbeat
file every 2 seconds so `copycat.py` knows it is alive.

### 4. Test the connection

```bash
python copycat.py balance
```

You should see your Phemex balance and XLTRADE account data.

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
BRIDGE_URL=http://YOUR_PC_LOCAL_IP:7373
BRIDGE_TOKEN=your_shared_secret
```

### 4. Start the bridge on your Windows PC

On your PC, in the `copycat` folder:

```bash
python bridge.py
```

It will print the URL and local IP to use in `BRIDGE_URL`.

> **For access outside your home network:** Install [Tailscale](https://tailscale.com)
> (free) on both your PC and phone. Use the Tailscale IP instead of your
> local IP in `BRIDGE_URL` — works from anywhere.

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
both brokers — not an estimate.

---

## Running `copycat` from Anywhere

By default you need to navigate to the folder and type `python copycat.py ...`.
Follow the steps below for your platform to shorten this to just `copycat ...`
from any directory.

### Windows

**1. Create a scripts folder** (if you don't have one):
```
mkdir C:\scripts
```

**2. Create `C:\scripts\copycat.bat`** with this content:
```batch
@echo off
python C:\path\to\your\copycat\copycat.py %*
```
Replace `C:\path\to\your\copycat` with the actual path to your folder.

**3. Add `C:\scripts` to your PATH:**
- Search **"environment variables"** in the Windows start menu
- Click **Environment Variables**
- Under **System Variables**, select **Path** → **Edit** → **New**
- Add `C:\scripts` and click OK
- Open a new terminal — `copycat balance` should now work from anywhere

---

### Termux

**Option 1 — Alias (simpler):**

Find your copycat folder path first:
```bash
cd /path/to/copycat && pwd
```

Then add the alias to your shell config:
```bash
echo "alias copycat='python /path/to/your/copycat/copycat.py'" >> ~/.bashrc
source ~/.bashrc
```

**Option 2 — Shell script (more permanent, survives reboots):**
```bash
cat > $PREFIX/bin/copycat << 'EOF'
#!/data/data/com.termux/files/usr/bin/sh
exec python /path/to/your/copycat/copycat.py "$@"
EOF
chmod +x $PREFIX/bin/copycat
```

Replace `/path/to/your/copycat` with your actual path in both options.
After either approach, `copycat balance` works from any directory.

---

## Usage

```
python copycat.py buy   market -sp <phemex_size> -sx <xltrade_size> -tp <tp>
python copycat.py buy   limit  -p <price> -sp <phemex_size> -sx <xltrade_size> -tp <tp>
python copycat.py sell  market -sp <phemex_size> -sx <xltrade_size> -tp <tp>
python copycat.py sell  limit  -p <price> -sp <phemex_size> -sx <xltrade_size> -tp <tp>
python copycat.py positions
python copycat.py positions --refresh
python copycat.py orders
python copycat.py balance
python copycat.py flatten
```

### Arguments

| Flag | Description |
|---|---|
| `-p` | Limit price (limit orders only) |
| `-sp` | Position size on Phemex in ETH |
| `-sx` | Position size on XLTRADE in lots |
| `-tp` | Take profit price (Phemex price — XLTRADE TP auto-adjusted) |

### Commands

| Command | Description |
|---|---|
| `buy / sell` | Place a market or limit order on both brokers simultaneously |
| `positions` | Show all open positions with SL, TP, and unrealised PnL |
| `positions --refresh` | Live auto-refreshing positions view (updates every 5s) |
| `orders` | Show all pending and conditional orders on both brokers |
| `balance` | Show account balance, margin, open PnL, and today's closed PnL |
| `flatten` | Close all positions and cancel all orders on both brokers |

### Examples

```bash
# Market orders
python copycat.py buy market -sp 0.33 -sx 0.27 -tp 2400.00
python copycat.py sell market -sp 0.33 -sx 0.27 -tp 2100.00

# Limit orders
python copycat.py buy limit -p 2130.00 -sp 0.33 -sx 0.27 -tp 2400.00
python copycat.py sell limit -p 2150.00 -sp 0.33 -sx 0.27 -tp 2100.00

# Info
python copycat.py positions --refresh
python copycat.py balance
python copycat.py orders
python copycat.py flatten
```

---

## Daily Workflow

1. Open MT5 with `BlackjackCopier` running on the ETHUSD.nx M1 chart
2. *(If using Termux)* Run `python bridge.py` on your PC
3. Open a terminal (Windows or Termux) in the `copycat` folder
4. Activate your venv if using one: `venv\Scripts\activate`
5. Run any command — `balance` is a good first check each session

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

**Phemex `TE_PRICE_TOO_SMALL` or similar**
- Your limit price is too far from the current market price. Phemex rejects
  limit orders that are more than ~20% from the mark price.

**XLTRADE `Timeout — EA did not respond`**
- On Windows: check the heartbeat file age and that MT5 is not frozen
- On Termux: check that `bridge.py` is running on your PC and that
  `BRIDGE_URL` and `BRIDGE_TOKEN` match on both devices

**`Bridge error: ...` on Termux**
- Confirm your PC and phone are on the same network (or both on Tailscale)
- Confirm `bridge.py` is running and printed `active` on startup
- Check that `BRIDGE_URL` uses the correct IP and port (default 7373)
- Check that `BRIDGE_TOKEN` is identical in both `.env` files
