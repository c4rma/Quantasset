import sys
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import numpy as np

# --- Input: Current Spot Price from Command Line ---
if len(sys.argv) < 2:
    print("Usage: python3 mm_sim6.py <spot_price>")
    sys.exit(1)

try:
    current_spot = float(sys.argv[1])
except ValueError:
    print("Invalid spot price provided.")
    sys.exit(1)

# --- Simulation Parameter: Days to Expiry ---
# Adjust this value to simulate PnL at different times before expiration.
# 0.0 means at expiration (0DTE).
# 1.0 means 1 day before expiration.
# 0.5 means 12 hours before expiration.
days_to_expiry = 0.0 # Start with 0.0 for 0DTE, change as needed for pre-expiry PnL

# Load the CSV
df = pd.read_csv('optionchain.csv')

# Convert 'Open' and 'Mark' to numeric
df['Open'] = pd.to_numeric(df['Open'], errors='coerce')
df['Mark'] = pd.to_numeric(df['Mark'], errors='coerce')

# Filter for Open Interest >= 100
df_filtered = df[df['Open'] >= 100].copy()

# Extract strike and type
def parse_instrument(inst):
    try:
        parts = inst.split('-')
        return float(parts[2]), parts[3].lower()
    except:
        return None, None

df_filtered[['Strike', 'Type']] = df_filtered['Instrument'].apply(
    lambda x: pd.Series(parse_instrument(x))
)

# Clean the delta, gamma, theta, and Mark columns by coercing to numeric and dropping invalid rows
df_filtered["Δ|Delta"] = pd.to_numeric(df_filtered["Δ|Delta"], errors='coerce')
df_filtered["Gamma"] = pd.to_numeric(df_filtered["Gamma"], errors='coerce')
df_filtered["Theta"] = pd.to_numeric(df_filtered["Theta"], errors='coerce')

# Drop rows with NaNs in critical columns (including 'Mark' now)
df_filtered.dropna(subset=["Δ|Delta", "Gamma", "Theta", "Mark", "Strike", "Type"], inplace=True)

# Build the strike_data dictionary
strike_data = {}
for _, row in df_filtered.iterrows():
    strike = int(row["Strike"])
    strike_data[strike] = {
        "contracts": int(row["Open"]),
        "delta": row["Δ|Delta"],
        "gamma": row["Gamma"],
        "theta": row["Theta"],
        "type": row["Type"],
        "mark_price": row["Mark"]
    }

# Simulation parameters
spot_prices = np.linspace(2500, 5000, 300)
total_pnl = np.zeros_like(spot_prices)
pnl_matrix = {}

# Run the simulation
for strike, data in strike_data.items():
    contracts = data['contracts']
    gamma = data['gamma']
    theta = data['theta']
    opt_type = data['type']
    mark_price = data['mark_price']

    # Calculate intrinsic value for calls and puts
    if opt_type == 'c':
        intrinsic = np.maximum(spot_prices - strike, 0)
    elif opt_type == 'p':
        intrinsic = np.maximum(strike - spot_prices, 0)
    else:
        print(f"Warning: Unknown option type '{opt_type}' for strike {strike}. Skipping.")
        continue

    # Premium received is based on the actual Mark Price (converted to USD)
    premium_received = (mark_price * current_spot) * contracts

    # Option value at different spot prices (intrinsic value at expiration)
    option_value = intrinsic * contracts

    # Initial PnL from premium received minus current intrinsic value
    pnl = premium_received - option_value

    # Adjust gamma for USD denomination
    hedge_adjustment = (0.5 * gamma * (spot_prices - strike) ** 2 * contracts) / current_spot
    pnl -= hedge_adjustment

    # Incorporate Theta Decay
    theta_pnl = theta * days_to_expiry * contracts
    pnl += theta_pnl

    # Add this option's PnL to the total PnL profile
    total_pnl += pnl
    pnl_matrix[strike] = pnl

# Determine breakeven points (where total PnL crosses zero)
breakeven_indices = np.where(np.diff(np.sign(total_pnl)))[0]
breakeven_prices = spot_prices[breakeven_indices]

# Find the maximum dealer profit point
max_pnl_idx = np.argmax(total_pnl)
max_pnl_value = total_pnl[max_pnl_idx]
max_pnl_spot = spot_prices[max_pnl_idx]

# --- Plotting ---
plt.figure(figsize=(14, 6))

plt.plot(spot_prices, total_pnl, label='Total Dealer PnL', color='black')
plt.axhline(0, color='gray', linestyle='--')
plt.title(f'Dealer Net PnL Based on Strikes w/ 100+ OI ({days_to_expiry} Days to Expiry)')
plt.xlabel('ETH Spot Price')
plt.ylabel('Total PnL')
plt.fill_between(spot_prices, total_pnl, where=total_pnl < 0, color='red', alpha=0.3, label='Dealer Pain Zone')
plt.axvline(current_spot, color='orange', linestyle='-', linewidth=2, label=f'Spot Price: ${current_spot:,.2f}')

# Annotate breakeven points
for bp in breakeven_prices:
    plt.axvline(bp, color='blue', linestyle='--', alpha=0.6)
    plt.text(bp, plt.ylim()[0], f'BE\n${bp:,.0f}', ha='center', va='bottom', fontsize=8, color='blue')

# Annotate Max Profit Point
plt.axvline(max_pnl_spot, color='green', linestyle=':', linewidth=2, label=f'Max Profit: ${max_pnl_value:,.0f} @ ${max_pnl_spot:,.0f}')
plt.plot(max_pnl_spot, max_pnl_value, 'go', markersize=8) # Green circle marker

# Changed vertical alignment to 'bottom' and adjusted y-position to be at the bottom of the plot
plt.text(max_pnl_spot, plt.ylim()[0], # Set y-position to the bottom of the plot
         f'Max Profit\n${max_pnl_spot:,.0f}', # Only show spot price for brevity, as PnL value is in label
         ha='center', va='bottom', fontsize=9, color='green',
         bbox=dict(boxstyle="round,pad=0.3", fc="yellow", ec="green", lw=0.5, alpha=0.7))


# Label PnL at each strike interval of 25
for price in range(int(spot_prices.min()), int(spot_prices.max()) + 1, 25):
    idx = (np.abs(spot_prices - price)).argmin()
    pnl_val = total_pnl[idx]
    # Only annotate if not too close to other labels or if it's a significant point
    if abs(pnl_val) > 100000 or price % 50 == 0: # Example condition to reduce clutter
        plt.text(spot_prices[idx], pnl_val, f'${pnl_val:,.0f}', ha='center', va='bottom', fontsize=7, color='green')

formatter = FuncFormatter(lambda x, _: f"${x:,.0f}")
plt.gca().yaxis.set_major_formatter(formatter)

plt.legend()
plt.grid(True)
plt.tight_layout()
plt.show()
