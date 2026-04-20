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
log_clients = set()

paired_devices = set()
device_last_seen = {}
pending_acks = {}

PAIR_FILE = "paired.json"
pair_codes = {}

OFFLINE_THRESHOLD = 20  # seconds

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
async def broadcast_log(device_id, message):
    payload = json.dumps({
        "type": "log_event",
        "device_id": device_id,
        "message": message,
        "time": time.time()
    })

    for c in list(log_clients):
        try:
            await c.send(payload)
        except:
            log_clients.discard(c)

# -----------------------------
async def broadcast_status(device_id, status):
    payload = json.dumps({
        "type": "pc_status",
        "status": status,
        "device_id": device_id
    })

    for c in list(mobile_clients) + list(dashboard_clients):
        try:
            await c.send(payload)
        except:
            pass

# -----------------------------
async def send_to_device(device_id, payload):
    ws = devices.get(device_id)
    if not ws:
        return

    try:
        command_id = str(random.randint(100000, 999999))
        payload["command_id"] = command_id

        pending_acks[command_id] = {
            "device_id": device_id,
            "type": payload["type"],
            "time": time.time()
        }

        await ws.send(json.dumps(payload))
        print(f"[SEND → {device_id}] {payload['type']}")

        await broadcast_log(device_id, f"Sent command: {payload['type']}")

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

            # ---------------- PC REGISTER ----------------
            if msg_type == "register":
                device_id = data["device_id"]
                client_type = "pc"

                devices[device_id] = ws
                device_last_seen[device_id] = time.time()

                print(f"[PC ONLINE] {device_id}")
                await broadcast_status(device_id, "online")
                await broadcast_log(device_id, "PC connected")

            # ---------------- MOBILE ----------------
            elif msg_type == "register_mobile":
                client_type = "mobile"
                mobile_clients.add(ws)
                print("[MOBILE CONNECTED]")

            # ---------------- DASHBOARD ----------------
            elif msg_type == "register_dashboard":
                client_type = "dashboard"
                dashboard_clients.add(ws)
                log_clients.add(ws)

                print("[DASHBOARD CONNECTED]")

            # ---------------- HEARTBEAT ----------------
            elif msg_type == "heartbeat":
                dev = data.get("device_id")

                if dev in devices:
                    device_last_seen[dev] = time.time()

            # ---------------- LIVE LOGS FROM PC ----------------
            elif msg_type == "log":
                dev = data.get("device_id", "unknown")
                message = data.get("message", "")

                await broadcast_log(dev, message)

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
        # cleanup
        if client_type == "pc" and device_id:
            devices.pop(device_id, None)
            device_last_seen.pop(device_id, None)

            await broadcast_status(device_id, "offline")
            await broadcast_log(device_id, "PC disconnected")

            print(f"[PC DISCONNECTED] {device_id}")

        if client_type == "mobile":
            mobile_clients.discard(ws)
            print("[MOBILE DISCONNECTED]")

        if client_type == "dashboard":
            dashboard_clients.discard(ws)
            log_clients.discard(ws)
            print("[DASHBOARD DISCONNECTED]")

# -----------------------------
async def cleanup_loop():
    while True:
        now = time.time()

        for dev in list(device_last_seen.keys()):
            if now - device_last_seen[dev] > OFFLINE_THRESHOLD:
                await broadcast_status(dev, "offline")

        await asyncio.sleep(5)

# -----------------------------
async def main():
    load_pairs()

    print("[WS RUNNING] on 8000")

    async with websockets.serve(handler, "0.0.0.0", 8000):
        asyncio.create_task(cleanup_loop())
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())