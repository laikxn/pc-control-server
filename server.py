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

pending_acks = {}

IDLE_THRESHOLD = 10
OFFLINE_THRESHOLD = 25

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
async def debug(event, data):
    """central debug logger"""
    print(f"[DEBUG {event}] {json.dumps(data, default=str)}")

# -----------------------------
def calc_status(device_id):
    last = device_last_seen.get(device_id, 0)
    diff = now() - last

    if device_id not in devices:
        return "offline"

    if diff > OFFLINE_THRESHOLD:
        return "offline"
    elif diff > IDLE_THRESHOLD:
        return "idle"
    return "online"

# -----------------------------
async def push_state(device_id):
    status = calc_status(device_id)
    old = device_status.get(device_id)

    if old != status:
        device_status[device_id] = status

        await debug("STATE_CHANGE", {
            "device_id": device_id,
            "status": status,
            "last_seen_ago": round(now() - device_last_seen.get(device_id, 0), 2)
        })

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

            # ---------------- PC REGISTER ----------------
            if msg_type == "register":
                device_id = data["device_id"]
                client_type = "pc"

                devices[device_id] = ws
                device_last_seen[device_id] = now()

                await debug("PC_REGISTER", {
                    "device_id": device_id
                })

                await push_state(device_id)

            # ---------------- HEARTBEAT ----------------
            elif msg_type == "heartbeat":
                dev = data.get("device_id")

                await debug("HEARTBEAT_RECEIVED", {
                    "device_id": dev,
                    "valid": dev in devices,
                    "time": now()
                })

                if dev:
                    device_last_seen[dev] = now()
                    await push_state(dev)

            # ---------------- MOBILE ----------------
            elif msg_type == "register_mobile":
                client_type = "mobile"
                mobile_clients.add(ws)

                await debug("MOBILE_CONNECT", {})

            # ---------------- DASHBOARD ----------------
            elif msg_type == "register_dashboard":
                client_type = "dashboard"
                dashboard_clients.add(ws)

                await debug("DASHBOARD_CONNECT", {})

            # ---------------- COMMANDS ----------------
            elif msg_type in ["shutdown_pc", "restart_pc", "lock_pc"]:
                dev = data.get("device_id")

                await debug("COMMAND", {
                    "type": msg_type,
                    "device_id": dev
                })

                if dev in devices:
                    await devices[dev].send(json.dumps({
                        "type": msg_type,
                        "data": {}
                    }))

            elif msg_type == "wake_pc":
                send_wol()

    except Exception as e:
        await debug("WS_ERROR", {"error": str(e)})

    finally:
        if client_type == "pc" and device_id:
            await debug("PC_DISCONNECT", {"device_id": device_id})

            devices.pop(device_id, None)

            # DO NOT instantly kill state
            await asyncio.sleep(1)
            await push_state(device_id)

        if client_type == "mobile":
            mobile_clients.discard(ws)

        if client_type == "dashboard":
            dashboard_clients.discard(ws)

# -----------------------------
async def state_monitor():
    while True:
        for dev in list(device_last_seen.keys()):
            await debug("STATE_CHECK", {
                "device_id": dev,
                "age": round(now() - device_last_seen[dev], 2),
                "status": calc_status(dev)
            })

            await push_state(dev)

        await asyncio.sleep(3)

# -----------------------------
async def main():
    print("[SERVER STARTED] ws://0.0.0.0:8000")

    async with websockets.serve(handler, "0.0.0.0", 8000):
        asyncio.create_task(state_monitor())
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())