import asyncio
import websockets
import json
import random
import os
import socket
import time
import logging

# -----------------------------
# reduce noisy websocket logs
logging.getLogger("websockets").setLevel(logging.ERROR)

# -----------------------------
devices = {}
mobile_clients = set()
dashboard_clients = set()

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
    client_type = None  # "pc", "mobile", "dashboard"

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

                # notify dashboards + mobiles
                for m in list(mobile_clients):
                    await m.send(json.dumps({
                        "type": "pc_status",
                        "status": "online",
                        "device_id": device_id
                    }))

            # ---------------- MOBILE REGISTER ----------------
            elif msg_type == "register_mobile":
                client_type = "mobile"
                mobile_clients.add(ws)

                print("[MOBILE CONNECTED]")

                for dev in devices:
                    await ws.send(json.dumps({
                        "type": "pc_status",
                        "status": "online",
                        "device_id": dev
                    }))

            # ---------------- DASHBOARD REGISTER ----------------
            elif msg_type == "register_dashboard":
                client_type = "dashboard"
                dashboard_clients.add(ws)

                print("[DASHBOARD CONNECTED]")

                # send current snapshot
                for dev in devices:
                    await ws.send(json.dumps({
                        "type": "pc_status",
                        "status": "online",
                        "device_id": dev
                    }))

            # ---------------- ACK ----------------
            elif msg_type == "ack":
                cmd_id = data.get("command_id")
                status = data.get("status")

                if cmd_id in pending_acks:
                    print(f"[ACK RECEIVED] {cmd_id} → {status}")
                    del pending_acks[cmd_id]

            # ---------------- HEARTBEAT ----------------
            elif msg_type == "heartbeat":
                dev = data.get("device_id")
                if dev in devices:
                    device_last_seen[dev] = time.time()

            # ---------------- PAIR ----------------
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
        # ---------------- PC CLEANUP ----------------
        if client_type == "pc" and device_id:
            devices.pop(device_id, None)
            device_last_seen.pop(device_id, None)

            print(f"[PC DISCONNECTED] {device_id}")

            # notify dashboards + mobiles
            for m in list(mobile_clients):
                try:
                    await m.send(json.dumps({
                        "type": "pc_status",
                        "status": "offline",
                        "device_id": device_id
                    }))
                except:
                    pass

        # ---------------- MOBILE CLEANUP ----------------
        if client_type == "mobile":
            mobile_clients.discard(ws)
            print("[MOBILE DISCONNECTED]")

        # ---------------- DASHBOARD CLEANUP ----------------
        if client_type == "dashboard":
            dashboard_clients.discard(ws)
            print("[DASHBOARD DISCONNECTED]")

# -----------------------------
async def cleanup_loop():
    while True:
        now = time.time()

        for dev in list(device_last_seen.keys()):
            last = device_last_seen.get(dev, 0)

            if now - last > OFFLINE_THRESHOLD:
                print(f"[OFFLINE] {dev}")

                device_last_seen.pop(dev, None)
                devices.pop(dev, None)

                for m in list(mobile_clients):
                    try:
                        await m.send(json.dumps({
                            "type": "pc_status",
                            "status": "offline",
                            "device_id": dev
                        }))
                    except:
                        pass

        await asyncio.sleep(5)

# -----------------------------
async def main():
    load_pairs()

    port = 8000
    print(f"[WS RUNNING] on {port}")

    async with websockets.serve(handler, "0.0.0.0", port):
        asyncio.create_task(cleanup_loop())
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())