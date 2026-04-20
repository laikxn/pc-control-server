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
devices = {}              # pc websockets
mobile_clients = set()
dashboard_clients = set()

device_last_seen = {}
device_status = {}        # ONLINE / IDLE / OFFLINE

pending_acks = {}

PAIR_FILE = "paired.json"
pair_codes = {}

# tuning
IDLE_THRESHOLD = 10       # seconds
OFFLINE_THRESHOLD = 25    # seconds

# -----------------------------
def gen_code():
    return str(random.randint(100000, 999999))

def send_wol():
    TARGET_MAC = "3C:6A:D2:41:58:F9"
    mac_bytes = bytes.fromhex(TARGET_MAC.replace(":", ""))
    packet = b"\xff" * 6 + mac_bytes * 16

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, ("255.255.255.255", 9))

    print("[WOL SENT]")

# -----------------------------
async def broadcast(payload):
    """Send to dashboards + mobile apps"""
    msg = json.dumps(payload)

    for group in [mobile_clients, dashboard_clients]:
        for ws in list(group):
            try:
                await ws.send(msg)
            except:
                group.discard(ws)

# -----------------------------
async def update_state(device_id):
    """Recalculate state from last_seen"""
    now = time.time()
    last = device_last_seen.get(device_id, 0)

    if device_id not in devices:
        new_status = "offline"
    else:
        diff = now - last

        if diff > OFFLINE_THRESHOLD:
            new_status = "offline"
        elif diff > IDLE_THRESHOLD:
            new_status = "idle"
        else:
            new_status = "online"

    old = device_status.get(device_id)

    if old != new_status:
        device_status[device_id] = new_status

        print(f"[STATE] {device_id} → {new_status}")

        await broadcast({
            "type": "pc_status",
            "device_id": device_id,
            "status": new_status,
            "last_seen": last
        })

# -----------------------------
async def send_to_device(device_id, payload):
    ws = devices.get(device_id)
    if not ws:
        return

    try:
        payload["command_id"] = str(random.randint(100000, 999999))

        await ws.send(json.dumps(payload))

        print(f"[SEND] {device_id} → {payload['type']}")

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

                print(f"[PC CONNECTED] {device_id}")

                await update_state(device_id)

            elif msg_type == "heartbeat":
                dev = data.get("device_id")

                if dev:
                    device_last_seen[dev] = time.time()
                    await update_state(dev)

            # ---------------- MOBILE ----------------
            elif msg_type == "register_mobile":
                client_type = "mobile"
                mobile_clients.add(ws)
                print("[MOBILE CONNECTED]")

                # send full snapshot
                for dev in device_status:
                    await ws.send(json.dumps({
                        "type": "pc_status",
                        "device_id": dev,
                        "status": device_status[dev],
                        "last_seen": device_last_seen.get(dev, 0)
                    }))

            # ---------------- DASHBOARD ----------------
            elif msg_type == "register_dashboard":
                client_type = "dashboard"
                dashboard_clients.add(ws)

                print("[DASHBOARD CONNECTED]")

                for dev in device_status:
                    await ws.send(json.dumps({
                        "type": "pc_status",
                        "device_id": dev,
                        "status": device_status[dev],
                        "last_seen": device_last_seen.get(dev, 0)
                    }))

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
            print(f"[PC DISCONNECTED] {device_id}")
            devices.pop(device_id, None)

            await update_state(device_id)

        if client_type == "mobile":
            mobile_clients.discard(ws)
            print("[MOBILE DISCONNECTED]")

        if client_type == "dashboard":
            dashboard_clients.discard(ws)
            print("[DASHBOARD DISCONNECTED]")

# -----------------------------
async def state_monitor():
    """continuously enforces state correctness"""
    while True:
        for dev in list(device_last_seen.keys()):
            await update_state(dev)

        await asyncio.sleep(3)

# -----------------------------
async def main():
    print("[SERVER STARTED] ws://0.0.0.0:8000")

    async with websockets.serve(handler, "0.0.0.0", 8000):
        asyncio.create_task(state_monitor())
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())