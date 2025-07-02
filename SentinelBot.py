import os
import json
import time
import hmac
import hashlib
import requests
from typing import Dict, Any

class PhemexClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.phemex.com"

    def _generate_signature(self, method: str, endpoint: str, expires: int, query_string: str = "", body: str = "") -> str:
        message = f"{method}{endpoint}{expires}{query_string}{body}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
        return signature

    def _send_request(self, method: str, endpoint: str, params: Dict[str, Any] = None, data: Dict[str, Any] = None):
        expires = int(time.time()) + 60
        query_string = f"?{requests.compat.urlencode(params)}" if params else ""
        body = json.dumps(data) if data else ""

        signature = self._generate_signature(method, endpoint, expires, query_string, body)

        headers = {
            "x-phemex-access-token": self.api_key,
            "x-phemex-request-expiry": str(expires),
            "x-phemex-request-signature": signature,
            "Content-Type": "application/json"
        }

        url = self.base_url + endpoint + (query_string if method == "GET" else "")
        response = requests.request(method, url, headers=headers, data=body)
        return response.json()

    def get_account_info(self):
        return self._send_request("GET", "/accounts/accountPositions")

    def place_limit_order(self, symbol: str, price: float, quantity: float, side: str):
        side_map = {"buy": 1, "sell": 2}
        data = {
            "symbol": symbol,
            "priceEp": int(price * 10000),  # Convert to priceEp format
            "orderQty": int(quantity * 1000),  # Convert to lot size format
            "side": side_map[side],
            "ordType": "Limit"
        }
        return self._send_request("POST", "/orders", data=data)

# Replace these with your actual credentials or load securely from environment
PHEMEX_API_KEY = os.getenv("PHEMEX_API_KEY", "your_api_key")
PHEMEX_API_SECRET = os.getenv("PHEMEX_API_SECRET", "your_api_secret")

client = PhemexClient(api_key=PHEMEX_API_KEY, api_secret=PHEMEX_API_SECRET)

# Example usage
# Commented out to prevent accidental order placement
# account_info = client.get_account_info()
# order_response = client.place_limit_order(symbol="ETHUSD", price=1800.0, quantity=0.01, side="buy")

"Ready to use PhemexClient for authenticated trading."
