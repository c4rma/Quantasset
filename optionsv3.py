import requests
import time
import csv
import os
from datetime import datetime, timezone

BASE_URL = "https://www.deribit.com/api/v2"
CURRENCY = "ETH"

# Format filename based on current date (mm-dd-yyyy for file safety)
date_str = datetime.now().strftime('%m-%d-%Y')
csv_filename = f"{date_str}.csv"

# Check if file exists; if not, write header
if not os.path.exists(csv_filename):
    with open(csv_filename, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            "Timestamp (UTC)",
            "Expiry",
            "ETH Spot Price",
            "Call OI",
            "Put OI",
            "Put/Call Ratio",
            "Call Notional",
            "Put Notional",
            "Total Notional"
        ])

def get_active_expiry_and_strikes():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    instruments = requests.get(f"{BASE_URL}/public/get_instruments?currency=ETH&kind=option").json()["result"]
    
    future_expiries = sorted(set(i["expiration_timestamp"] for i in instruments if i["expiration_timestamp"] > now_ms))
    if not future_expiries:
        return None, [], {}

    active_expiry_ts = future_expiries[0]
    expiry_str = datetime.utcfromtimestamp(active_expiry_ts / 1000).strftime('%d%b%y').upper()
    
    instrument_meta = {
        i["instrument_name"]: {
            "strike": i["strike"],
            "option_type": i["option_type"]
        }
        for i in instruments
        if i["expiration_timestamp"] == active_expiry_ts
    }

    return expiry_str, list(instrument_meta.keys()), instrument_meta

def fetch_bulk_summary():
    response = requests.get(f"{BASE_URL}/public/get_book_summary_by_currency", params={
        "currency": "ETH",
        "kind": "option"
    })
    return response.json()["result"]

def fetch_eth_spot():
    response = requests.get(f"{BASE_URL}/public/ticker", params={
        "instrument_name": "ETH-PERPETUAL"
    })
    return response.json()["result"]["last_price"]

def process_oi_and_notional(expiry_str, valid_instruments, instrument_meta, summaries, spot_price):
    call_oi = put_oi = call_notional = put_notional = 0.0

    for item in summaries:
        name = item["instrument_name"]
        if name not in valid_instruments:
            continue

        oi = item["open_interest"]
        if oi == 0:
            continue

        strike = instrument_meta[name]["strike"]
        option_type = instrument_meta[name]["option_type"]
        notional = oi * strike

        if option_type == "call":
            call_oi += oi
            call_notional += notional
        elif option_type == "put":
            put_oi += oi
            put_notional += notional

    put_call_ratio = put_oi / call_oi if call_oi > 0 else float('inf')
    total_notional = call_notional + put_notional

    return {
        "Timestamp": datetime.utcnow().strftime('%H:%M:%S'),
        "Expiry": expiry_str,
        "Spot": spot_price,
        "Call OI": call_oi,
        "Put OI": put_oi,
        "Put/Call Ratio": put_call_ratio,
        "Call Notional": call_notional,
        "Put Notional": put_notional,
        "Total Notional": total_notional
    }

# Continuous loop that logs to CSV
try:
    while True:
        try:
            expiry_str, valid_instruments, instrument_meta = get_active_expiry_and_strikes()
            if expiry_str and valid_instruments:
                summaries = fetch_bulk_summary()
                spot_price = fetch_eth_spot()
                result = process_oi_and_notional(expiry_str, valid_instruments, instrument_meta, summaries, spot_price)

                print(f"\n🕒 {result['Timestamp']} UTC | Expiry: {result['Expiry']}")
                print(f"   💎 Spot Price: ${result['Spot']:.2f}")
                print(f"   🔹 Call OI: {result['Call OI']:,.2f} | Notional: ${result['Call Notional']:,.2f}")
                print(f"   🔸 Put OI:  {result['Put OI']:,.2f} | Notional: ${result['Put Notional']:,.2f}")
                print(f"   📊 Put/Call OI Ratio: {result['Put/Call Ratio']:.4f}")
                print(f"   💰 Total Notional: ${result['Total Notional']:,.2f}")

                with open(csv_filename, mode='a', newline='') as file:
                    writer = csv.writer(file)
                    writer.writerow([
                        result["Timestamp"],
                        result["Expiry"],
                        result["Spot"],
                        result["Call OI"],
                        result["Put OI"],
                        result["Put/Call Ratio"],
                        result["Call Notional"],
                        result["Put Notional"],
                        result["Total Notional"]
                    ])
            else:
                print("⚠️ No valid expiry instruments found.")
        except Exception as e:
            print(f"❌ Script error: {e}")

        time.sleep(2)

except KeyboardInterrupt:
    print("\n🛑 Script terminated by user.")

