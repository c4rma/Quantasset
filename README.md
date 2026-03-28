# Blackjack Trade Copier
### Confidential — Fund Intellectual Property

---

## Prerequisites

- Windows PC (required for MT5 Python library)
- Python 3.10+
- Node.js 18+
- MetaTrader 5 terminal installed and running with XLTRADE (Chimera) logged in
- Phemex API key with trading permissions enabled

---

## Setup

### 1. Clone / copy the project folder

### 2. Backend setup

```bash
cd trade-copier
pip install -r requirements.txt
```

Copy the env template and fill in your credentials:

```bash
copy .env.example .env
```

Edit `.env`:
```
PHEMEX_API_KEY=your_key_here
PHEMEX_API_SECRET=your_secret_here
MT5_LOGIN=your_account_number
MT5_PASSWORD=your_password
MT5_SERVER=Chimera-Live
```

### 3. Frontend setup

```bash
cd frontend
npm install
```

---

## Running

**Terminal 1 — Backend:**
```bash
cd trade-copier/backend
python main.py
```
You should see:
```
[MT5] Connected — Account: XXXXXX | Balance: XXXX.XX
INFO:     Uvicorn running on http://127.0.0.1:8000
```

**Terminal 2 — Frontend:**
```bash
cd trade-copier/frontend
npm run dev
```

Open your browser at: **http://localhost:3000**

---

## How It Works

### Placing a Trade
1. Select LONG or SHORT
2. Select the current sequence step (1R, 1R+W, 2R, etc.)
3. Enter the current market price as Entry Price
4. Enter your Take Profit price
5. Enter your Phemex position size in ETH
6. Click EXECUTE

The system will simultaneously:
- Send a market order to **Phemex** with your exact size, SL at entry ± $15.00
- Send a market order to **XLTRADE** with size × 0.829, SL at entry ± $18.10, TP + $3.10

### XLTRADE Adjustments (automatic)
| Parameter  | Rule |
|------------|------|
| Size       | Phemex size × 0.829 |
| SL (Long)  | Entry − $18.10 |
| SL (Short) | Entry + $18.10 |
| TP (Long)  | Phemex TP + $3.10 |
| TP (Short) | Phemex TP − $3.10 |

### Flatten
Click **FLATTEN ALL** once → confirm by clicking again within 4 seconds.
All positions across all brokers are closed at market simultaneously.

### Position Tracker
Updates every 500ms via WebSocket. Shows per-broker positions with
live mark price, SL, TP, and unrealised PnL.

---

## Adding Quantasset

When ready to add Quantasset:
1. Obtain a separate Phemex API key for the Quantasset sub-account
2. Add to `.env`:
   ```
   QUANTASSET_ENABLED=true
   QUANTASSET_API_KEY=your_key
   QUANTASSET_API_SECRET=your_secret
   ```
3. Restart the backend — Quantasset will appear automatically in the dashboard

---

## Troubleshooting

**MT5 not connecting:**
- Ensure MT5 terminal is open and logged into XLTRADE (Chimera)
- Confirm `MT5_SERVER` matches exactly what appears in MT5 (case-sensitive)
- Run `python -c "import MetaTrader5 as mt5; print(mt5.initialize())"` to test

**Phemex orders failing:**
- Check API key has `Trading` permissions enabled on Phemex
- Ensure IP whitelist includes `127.0.0.1` if you set one
- Check the symbol `ETHUSD` matches your Phemex contract name

**Frontend can't connect:**
- Confirm backend is running on port 8000
- Check browser console for WebSocket errors
