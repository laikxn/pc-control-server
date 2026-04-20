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
devices = {}  # device_id -> ws
device_last_seen = {}
device_status = {}  # device_id -> "online"/"offline"

mobile_clients = set()
dashboard_clients = set()

pending_acks = {}

PAIR_FILE = "paired.json"
pair_codes = {}

OFFLINE_THRESHOLD = 20

# -----------------------------
def load_pairs():
    global paired_devices
    if os.path.exists(PAIR_FILE):
        with open(PAIR_FILE, "r") as f:
            return set(json.load(f))
    return set()

paired_devices = load_pairs()

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
async def broadcast(event):
    msg = json.dumps(event)

    targets = list(mobile_clients) + list(dashboard_clients)

    for c in targets:
        try:
            await c.send(msg)
        except:
            mobile_clients.discard(c)
            dashboard_clients.discard(c)

# -----------------------------
async def set_status(device_id, status):
    device_status[device_id] = status

    await broadcast({
        "type": "device_update",
        "device_id": device_id,
        "status": status,
        "last_seen": device_last_seen.get(device_id, time.time())
    })

# -----------------------------
async def send_to_device(device_id, payload):
    ws = devices.get(device_id)
    if not ws:
        return

    try:
        cmd_id = str(random.randint(100000, 999999))
        payload["command_id"] = cmd_id

        pending_acks[cmd_id] = {
            "device_id": device_id,
            "type": payload["type"],
            "time": time.time()
        }

        await ws.send(json.dumps(payload))
        print(f"[SEND → {device_id}] {payload['type']}")

    except Exception as e:
        print("[SEND ERROR]", e)

# -----------------------------
async def handler(ws):
    device_id = None
    client_type = None

    try:
        async for msg in ws:
            data = json.loads(msg)
            msg_type = data.get("type")

            # ---------------- PC ----------------
            if msg_type == "register":
                device_id = data["device_id"]
                client_type = "pc"

                devices[device_id] = ws
                device_last_seen[device_id] = time.time()

                await set_status(device_id, "online")

                print(f"[PC ONLINE] {device_id}")

                # full snapshot
                await ws.send(json.dumps({
                    "type": "device_snapshot",
                    "devices": device_status
                }))

            # ---------------- MOBILE ----------------
            elif msg_type == "register_mobile":
                client_type = "mobile"
                mobile_clients.add(ws)

                print("[MOBILE CONNECTED]")

                await ws.send(json.dumps({
                    "type": "device_snapshot",
                    "devices": device_status
                }))

            # ---------------- DASHBOARD ----------------
            elif msg_type == "register_dashboard":
                client_type = "dashboard"
                dashboard_clients.add(ws)

                print("[DASHBOARD CONNECTED]")

                await ws.send(json.dumps({
                    "type": "device_snapshot",
                    "devices": device_status
                }))

            # ---------------- HEARTBEAT ----------------
            elif msg_type == "heartbeat":
                dev = data.get("device_id")

                if dev in devices:
                    device_last_seen[dev] = time.time()

                    await broadcast({
                        "type": "device_heartbeat",
                        "device_id": dev,
                        "last_seen": device_last_seen[dev]
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
        if client_type == "pc" and device_id:
            devices.pop(device_id, None)
            device_last_seen.pop(device_id, None)

            await set_status(device_id, "offline")
            print(f"[PC DISCONNECTED] {device_id}")

        if client_type == "mobile":
            mobile_clients.discard(ws)
            print("[MOBILE DISCONNECTED]")

        if client_type == "dashboard":
            dashboard_clients.discard(ws)
            print("[DASHBOARD DISCONNECTED]")

# -----------------------------
async def cleanup_loop():
    while True:
        now = time.time()

        for dev in list(device_last_seen.keys()):
            if now - device_last_seen[dev] > OFFLINE_THRESHOLD:
                if device_status.get(dev) != "offline":
                    print(f"[OFFLINE] {dev}")
                    await set_status(dev, "offline")

        await asyncio.sleep(5)

# -----------------------------
async def main():
    print("[WS RUNNING] on 8000")

    async with websockets.serve(handler, "0.0.0.0", 8000):
        asyncio.create_task(cleanup_loop())
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())