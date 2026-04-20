import asyncio
import websockets
import json
import random
import os
import socket
import time
import logging

logging.getLogger("websockets").setLevel(logging.ERROR)

# -----------------------------
devices = {}
mobile_clients = set()
dashboard_clients = set()

device_last_seen = {}
device_disconnect_time = {}
device_status = {}

pending_acks = {}

PAIR_FILE = "paired.json"
pair_codes = {}

OFFLINE_THRESHOLD = 25        # heartbeat timeout
RECONNECT_GRACE = 8          # NEW: prevents flicker

# -----------------------------
def broadcast(msg):
    async def _send():
        payload = json.dumps(msg)
        targets = list(mobile_clients) + list(dashboard_clients)

        for c in targets:
            try:
                await c.send(payload)
            except:
                pass

    asyncio.create_task(_send())

# -----------------------------
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
async def send_to_device(device_id, payload):
    ws = devices.get(device_id)
    if not ws:
        return

    try:
        await ws.send(json.dumps(payload))
        print(f"[SEND → {device_id}] {payload['type']}")
    except:
        pass

# -----------------------------
async def handler(ws):
    device_id = None
    client_type = None

    try:
        async for msg in ws:
            data = json.loads(msg)
            msg_type = data.get("type")

            # ---------------- PC REGISTER ----------------
            if msg_type == "register":
                device_id = data["device_id"]
                client_type = "pc"

                devices[device_id] = ws
                device_last_seen[device_id] = time.time()

                # cancel pending offline if reconnect fast
                device_disconnect_time.pop(device_id, None)

                device_status[device_id] = "online"

                print(f"[PC ONLINE] {device_id}")

                broadcast({
                    "type": "pc_status",
                    "status": "online",
                    "device_id": device_id
                })

            # ---------------- MOBILE ----------------
            elif msg_type == "register_mobile":
                client_type = "mobile"
                mobile_clients.add(ws)
                print("[MOBILE CONNECTED]")

            # ---------------- DASHBOARD ----------------
            elif msg_type == "register_dashboard":
                client_type = "dashboard"
                dashboard_clients.add(ws)
                print("[DASHBOARD CONNECTED]")

            # ---------------- HEARTBEAT ----------------
            elif msg_type == "heartbeat":
                dev = data.get("device_id")
                if dev in devices:
                    device_last_seen[dev] = time.time()

                    if device_status.get(dev) != "online":
                        device_status[dev] = "online"

                        broadcast({
                            "type": "pc_status",
                            "status": "online",
                            "device_id": dev
                        })

            # ---------------- COMMANDS ----------------
            elif msg_type in ["shutdown_pc", "restart_pc", "lock_pc"]:
                await send_to_device(data.get("device_id"), {
                    "type": msg_type,
                    "data": {}
                })

            elif msg_type == "wake_pc":
                send_wol()

    except Exception as e:
        print("[WS ERROR]", e)

    finally:
        # ---------------- PC DISCONNECT ----------------
        if client_type == "pc" and device_id:

            # mark disconnect time instead of instantly offline
            device_disconnect_time[device_id] = time.time()

            print(f"[PC DISCONNECTED] {device_id}")

        # ---------------- MOBILE ----------------
        elif client_type == "mobile":
            mobile_clients.discard(ws)

        # ---------------- DASHBOARD ----------------
        elif client_type == "dashboard":
            dashboard_clients.discard(ws)

# -----------------------------
async def offline_checker():
    while True:
        now = time.time()

        for dev in list(devices.keys()):

            last_seen = device_last_seen.get(dev, 0)
            last_dc = device_disconnect_time.get(dev)

            # 1. heartbeat timeout
            if now - last_seen > OFFLINE_THRESHOLD:

                # 2. grace period check
                if last_dc and now - last_dc < RECONNECT_GRACE:
                    continue  # ignore flicker

                if device_status.get(dev) != "offline":
                    device_status[dev] = "offline"

                    print(f"[OFFLINE] {dev}")

                    broadcast({
                        "type": "pc_status",
                        "status": "offline",
                        "device_id": dev
                    })

        await asyncio.sleep(3)

# -----------------------------
async def main():
    print("[WS RUNNING] on 8000")

    async with websockets.serve(handler, "0.0.0.0", 8000):
        asyncio.create_task(offline_checker())
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())