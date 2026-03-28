#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# bridge.py — Copycat Network Bridge (run this on your Windows PC)
#
# Listens for HTTP requests from copycat.py running on Termux (or any remote
# device) and relays them to BlackjackCopier.mq5 via the local file bridge.
#
# How it fits together:
#   [Termux / Phone]  ──HTTP──▶  [bridge.py on PC]  ──files──▶  [MT5 EA]
#
# Setup:
#   1. Copy bridge.py into your trade-copier folder (same folder as copycat.py)
#   2. Add BRIDGE_TOKEN to your .env (same value on PC and phone)
#   3. Run: python bridge.py
#   4. Note the IP address printed on startup
#   5. Set BRIDGE_URL=http://<your-pc-ip>:7373 in your phone's .env
#
# The bridge only accepts requests that include the correct BRIDGE_TOKEN,
# so random devices on your network cannot send orders.
#
# For access outside your home network, use Tailscale (free):
#   https://tailscale.com — install on both PC and phone, use the Tailscale
#   IP instead of your local IP in BRIDGE_URL.
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import time
import json
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Load .env ─────────────────────────────────────────────────────────────────
def load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.exists(env_path):
        print("✗ .env file not found.")
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()

MT5_FILES_PATH = os.environ.get('MT5_FILES_PATH', '')
BRIDGE_TOKEN   = os.environ.get('BRIDGE_TOKEN', '')
BRIDGE_PORT    = int(os.environ.get('BRIDGE_PORT', '7373'))

# ── Colours ───────────────────────────────────────────────────────────────────
if sys.platform == 'win32':
    import ctypes
    ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)

GRN = '\033[92m'
RED = '\033[91m'
YLW = '\033[93m'
CYN = '\033[96m'
DIM = '\033[2m'
RST = '\033[0m'

def log(msg, colour=CYN):
    ts = time.strftime('%H:%M:%S')
    print(f"{DIM}[{ts}]{RST} {colour}{msg}{RST}")

# ── File bridge (same logic as copycat.py mt5_send) ───────────────────────────
_bridge_lock = threading.Lock()  # one signal at a time

def mt5_send(signal, timeout=20):
    if not MT5_FILES_PATH:
        return None, "MT5_FILES_PATH not set in .env"
    if not os.path.isdir(MT5_FILES_PATH):
        return None, f"MT5 files path not found: {MT5_FILES_PATH}"

    sig_path  = os.path.join(MT5_FILES_PATH, 'bj_signal.txt')
    resp_path = os.path.join(MT5_FILES_PATH, 'bj_response.txt')
    hb_path   = os.path.join(MT5_FILES_PATH, 'bj_heartbeat.txt')

    if not os.path.exists(hb_path):
        return None, "EA heartbeat not found — is BlackjackCopier running?"
    age = time.time() - os.path.getmtime(hb_path)
    if age > 8:
        return None, f"EA heartbeat stale ({age:.0f}s) — EA may have stopped"

    with _bridge_lock:
        # Clear stale response
        for _ in range(3):
            if os.path.exists(resp_path):
                try:
                    os.remove(resp_path)
                    break
                except Exception:
                    time.sleep(0.05)

        # Write signal
        with open(sig_path, 'w') as f:
            f.write(signal)

        # Poll for response
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.03)
            if not os.path.exists(resp_path):
                continue
            for _ in range(5):
                try:
                    with open(resp_path, 'r') as f:
                        resp = f.read().strip()
                    if resp:
                        try:
                            os.remove(resp_path)
                        except Exception:
                            pass
                        return resp, None
                except Exception:
                    pass
                time.sleep(0.03)

    return None, "Timeout — EA did not respond"

# ── HTTP request handler ───────────────────────────────────────────────────────
class BridgeHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default request logging, we do our own

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # ── Auth ──────────────────────────────────────────────────────────────
        token = self.headers.get('X-Bridge-Token', '')
        if BRIDGE_TOKEN and token != BRIDGE_TOKEN:
            log(f"Rejected request from {self.client_address[0]} — bad token", RED)
            self.send_json(401, {'error': 'Unauthorized'})
            return

        # ── Read body ─────────────────────────────────────────────────────────
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json(400, {'error': 'Invalid JSON'})
            return

        signal  = body.get('signal', '')
        timeout = int(body.get('timeout', 20))

        if not signal:
            self.send_json(400, {'error': 'Missing signal'})
            return

        # ── Relay to EA ───────────────────────────────────────────────────────
        log(f"← {self.client_address[0]}  {signal[:60]}{'...' if len(signal)>60 else ''}")
        resp, err = mt5_send(signal, timeout=timeout)

        if err:
            log(f"→ ERROR: {err}", RED)
            self.send_json(200, {'error': err})
        else:
            log(f"→ {resp[:60]}{'...' if len(resp)>60 else ''}", GRN)
            self.send_json(200, {'resp': resp})

    def do_GET(self):
        # Health check endpoint
        token = self.headers.get('X-Bridge-Token', '')
        if BRIDGE_TOKEN and token != BRIDGE_TOKEN:
            self.send_json(401, {'error': 'Unauthorized'})
            return

        hb_path = os.path.join(MT5_FILES_PATH, 'bj_heartbeat.txt')
        ea_alive = False
        if MT5_FILES_PATH and os.path.exists(hb_path):
            ea_alive = (time.time() - os.path.getmtime(hb_path)) < 8

        self.send_json(200, {
            'status':    'online',
            'ea_alive':  ea_alive,
            'mt5_path':  MT5_FILES_PATH,
        })

# ── Get local IP ──────────────────────────────────────────────────────────────
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not MT5_FILES_PATH:
        print(f"{RED}✗ MT5_FILES_PATH not set in .env{RST}")
        sys.exit(1)

    if not BRIDGE_TOKEN:
        print(f"{YLW}⚠ BRIDGE_TOKEN not set — requests will not be authenticated{RST}")

    local_ip = get_local_ip()
    server   = HTTPServer(('0.0.0.0', BRIDGE_PORT), BridgeHandler)

    print(f"""
{CYN}{'─'*54}
  COPYCAT BRIDGE — active
{'─'*54}{RST}
  Local URL   {GRN}http://{local_ip}:{BRIDGE_PORT}{RST}
  MT5 path    {DIM}{MT5_FILES_PATH}{RST}
  Auth        {'enabled' if BRIDGE_TOKEN else f'{YLW}disabled — set BRIDGE_TOKEN in .env{RST}'}

  Set in your phone's .env:
  {CYN}BRIDGE_URL=http://{local_ip}:{BRIDGE_PORT}{RST}
  {CYN}BRIDGE_TOKEN=<your token>{RST}

  {DIM}For remote access (outside home network): use Tailscale
  https://tailscale.com{RST}
{CYN}{'─'*54}{RST}
  Press Ctrl+C to stop
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n{DIM}Bridge stopped.{RST}")

if __name__ == '__main__':
    main()
