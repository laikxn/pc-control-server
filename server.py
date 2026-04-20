import asyncio
import websockets
import json
import random
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

devices = {}
paired_devices = set()

PAIR_FILE = "paired.json"
pair_codes = {}

# -----------------------------
def load_pairs():
    global paired_devices
    if os.path.exists(PAIR_FILE):
        with open(PAIR_FILE, "r") as f:
            paired_devices = set(json.load(f))

def save_pairs():
    with open(PAIR_FILE, "w") as f:
        json.dump(list(paired_devices), f)

def gen_code():
    return str(random.randint(100000, 999999))

# -----------------------------
TARGET_MAC = "3C:6A:D2:41:58:F9"

def send_wol():
    mac_bytes = bytes.fromhex(TARGET_MAC.replace(":", ""))
    packet = b"\xff" * 6 + mac_bytes * 16

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, ("255.255.255.255", 9))

    print("[WOL SENT]")

# -----------------------------
async def send_to_device(target, payload):
    if target in devices:
        await devices[target].send(json.dumps(payload))
        print(f"[SEND → {target}] {payload['type']}")

# -----------------------------
async def handler(ws):
    device_id = None

    try:
        async for msg in ws:
            data = json.loads(msg)
            msg_type = data.get("type")

            if msg_type == "register":
                device_id = data["device_id"]
                devices[device_id] = ws
                print(f"[PC ONLINE] {device_id}")

            elif msg_type == "register_mobile":
                print("[MOBILE CONNECTED]")

            elif msg_type == "request_pair":
                code = gen_code()

                if devices:
                    target = list(devices.keys())[0]
                    pair_codes[code] = target

                    await ws.send(json.dumps({
                        "type": "pair_code",
                        "code": code
                    }))

            elif msg_type == "confirm_pair":
                code = data.get("code")

                if code in pair_codes:
                    dev = pair_codes[code]
                    paired_devices.add(dev)
                    save_pairs()

                    await ws.send(json.dumps({"type": "pair_success"}))
                    del pair_codes[code]

                    print(f"[PAIRED] {dev}")

            elif msg_type == "shutdown_pc":
                await send_to_device(data.get("device_id"), {
                    "type": "shutdown_pc",
                    "data": {}
                })

            elif msg_type == "restart_pc":
                await send_to_device(data.get("device_id"), {
                    "type": "restart_pc",
                    "data": {}
                })

            elif msg_type == "lock_pc":
                await send_to_device(data.get("device_id"), {
                    "type": "lock_pc",
                    "data": {}
                })

            elif msg_type == "wake_pc":
                send_wol()

    except Exception as e:
        print("[WS ERROR]", e)

    finally:
        if device_id in devices:
            del devices[device_id]
            print(f"[DISCONNECTED] {device_id}")

# -----------------------------
# HTTP SERVER (Render health check fix)
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_http():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    print(f"[HTTP RUNNING] on {port}")
    server.serve_forever()

# -----------------------------
async def main_ws():
    load_pairs()

    port = 8000
    server = await websockets.serve(handler, "0.0.0.0", port)

    print(f"[WS RUNNING] on {port}")
    await server.wait_closed()

# -----------------------------
if __name__ == "__main__":
    threading.Thread(target=run_http, daemon=True).start()
    asyncio.run(main_ws())