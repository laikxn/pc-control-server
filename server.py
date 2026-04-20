import asyncio
import websockets
import json
import random
import os
import socket
import time
import logging

# -----------------------------
logging.getLogger("websockets").setLevel(logging.ERROR)

# -----------------------------
devices = {}
mobile_clients = set()
dashboard_clients = set()

paired_devices = set()
device_last_seen = {}
device_status = {}

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
async def broadcast_status(device_id, status):
    payload = json.dumps({
        "type": "pc_status",
        "status": status,
        "device_id": device_id
    })

    for client_set in [mobile_clients, dashboard_clients]:
        for c in list(client_set):
            try:
                await c.send(payload)
            except:
                client_set.discard(c)

# -----------------------------
async def broadcast_activity(device_id):
    payload = json.dumps({
        "type": "pc_activity",
        "device_id": device_id,
        "last_seen": device_last_seen.get(device_id, time.time())
    })

    for client_set in [mobile_clients, dashboard_clients]:
        for c in list(client_set):
            try:
                await c.send(payload)
            except:
                client_set.discard(c)

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
        print(f"[SEND → {device_id}] {payload['type']} ({command_id})")

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
                device_status[device_id] = "online"

                print(f"[PC ONLINE] {device_id}")
                await broadcast_status(device_id, "online")

            # ---------------- MOBILE ----------------
            elif msg_type == "register_mobile":
                client_type = "mobile"
                mobile_clients.add(ws)

                print("[MOBILE CONNECTED]")

                for dev in device_status:
                    await ws.send(json.dumps({
                        "type": "pc_status",
                        "status": device_status[dev],
                        "device_id": dev
                    }))

            # ---------------- DASHBOARD ----------------
            elif msg_type == "register_dashboard":
                client_type = "dashboard"
                dashboard_clients.add(ws)

                print("[DASHBOARD CONNECTED]")

                for dev in device_status:
                    await ws.send(json.dumps({
                        "type": "pc_status",
                        "status": device_status[dev],
                        "device_id": dev
                    }))

            # ---------------- HEARTBEAT ----------------
            elif msg_type == "heartbeat":
                dev = data.get("device_id")

                if dev in devices:
                    device_last_seen[dev] = time.time()

                    # keep alive update
                    await broadcast_activity(dev)

                    # recover from offline
                    if device_status.get(dev) != "online":
                        device_status[dev] = "online"
                        print(f"[RECOVERED] {dev}")
                        await broadcast_status(dev, "online")

            # ---------------- COMMANDS ----------------
            elif msg_type in ["shutdown_pc", "restart_pc", "lock_pc"]:
                print(f"[COMMAND RECEIVED] {msg_type}")

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
            print(f"[PC DISCONNECTED] {device_id}")

            device_status[device_id] = "offline"
            await broadcast_status(device_id, "offline")

        elif client_type == "mobile":
            mobile_clients.discard(ws)
            print("[MOBILE DISCONNECTED]")

        elif client_type == "dashboard":
            dashboard_clients.discard(ws)
            print("[DASHBOARD DISCONNECTED]")

# -----------------------------
async def cleanup_loop():
    while True:
        now = time.time()

        for dev in list(device_last_seen.keys()):
            last = device_last_seen.get(dev, 0)

            if now - last > OFFLINE_THRESHOLD:
                if device_status.get(dev) != "offline":
                    print(f"[OFFLINE] {dev}")

                    device_status[dev] = "offline"
                    await broadcast_status(dev, "offline")

        await asyncio.sleep(5)

# -----------------------------
async def main():
    load_pairs()

    print("[WS RUNNING] on 8000")

    async with websockets.serve(handler, "0.0.0.0", 8000):
        asyncio.create_task(cleanup_loop())
        await asyncio.Future()

# -----------------------------
if __name__ == "__main__":
    asyncio.run(main())