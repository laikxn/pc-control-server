import asyncio
import websockets
import json
import random
import socket
import time
import logging

logging.getLogger("websockets").setLevel(logging.ERROR)

# -----------------------------
devices = {}
mobile_clients = set()
dashboard_clients = set()

device_last_seen = {}
device_status = {}

OFFLINE_THRESHOLD = 25
IDLE_THRESHOLD = 10

# -----------------------------
def now():
    return time.time()

def gen_code():
    return str(random.randint(100000, 999999))

# -----------------------------
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
    msg = json.dumps(payload)

    for group in [mobile_clients, dashboard_clients]:
        for ws in list(group):
            try:
                await ws.send(msg)
            except:
                group.discard(ws)

# -----------------------------
def calc_status(device_id):
    if device_id not in devices:
        return "offline"

    last = device_last_seen.get(device_id, 0)
    diff = now() - last

    if diff > OFFLINE_THRESHOLD:
        return "offline"
    if diff > IDLE_THRESHOLD:
        return "idle"
    return "online"

# -----------------------------
async def push_state(device_id):
    status = calc_status(device_id)

    if device_status.get(device_id) != status:
        device_status[device_id] = status

        print(f"[STATE] {device_id} → {status}")

        await broadcast({
            "type": "pc_status",
            "device_id": device_id,
            "status": status,
            "last_seen": device_last_seen.get(device_id, 0)
        })

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
                device_last_seen[device_id] = now()

                print(f"[PC CONNECTED] {device_id}")
                await push_state(device_id)

            # ---------------- HEARTBEAT ----------------
            elif msg_type == "heartbeat":
                dev = data.get("device_id")

                if dev:
                    device_last_seen[dev] = now()
                    await push_state(dev)

                    print(f"[HEARTBEAT] {dev}")

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

                # full snapshot
                for dev in devices:
                    await ws.send(json.dumps({
                        "type": "pc_status",
                        "device_id": dev,
                        "status": device_status.get(dev, "offline"),
                        "last_seen": device_last_seen.get(dev, 0)
                    }))

            # ---------------- COMMANDS ----------------
            elif msg_type in ["shutdown_pc", "restart_pc", "lock_pc"]:
                dev = data.get("device_id")

                if dev in devices:
                    await devices[dev].send(json.dumps({
                        "type": msg_type,
                        "data": {}
                    }))

            elif msg_type == "wake_pc":
                send_wol()

    except Exception as e:
        print("[WS ERROR]", e)

    finally:
        if client_type == "pc" and device_id:
            print(f"[PC DISCONNECTED] {device_id}")

            devices.pop(device_id, None)

            await push_state(device_id)

        if client_type == "mobile":
            mobile_clients.discard(ws)

        if client_type == "dashboard":
            dashboard_clients.discard(ws)

# -----------------------------
async def monitor():
    while True:
        for dev in list(device_last_seen.keys()):
            await push_state(dev)

        await asyncio.sleep(3)

# -----------------------------
async def main():
    print("[SERVER STARTED] ws://0.0.0.0:8000")

    async with websockets.serve(handler, "0.0.0.0", 8000):
        asyncio.create_task(monitor())
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())