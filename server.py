import asyncio
import websockets
import json
import random
import os
import socket
import time

devices = {}
mobile_clients = set()
paired_devices = set()
device_last_seen = {}

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
async def send_to_device(device_id, payload):
    ws = devices.get(device_id)
    if ws:
        try:
            await ws.send(json.dumps(payload))
            print(f"[SEND → {device_id}] {payload['type']}")
        except Exception as e:
            print("[SEND ERROR]", e)

# -----------------------------
async def handler(ws):
    device_id = None
    mobile = False

    try:
        async for msg in ws:
            data = json.loads(msg)
            msg_type = data.get("type")

            # ---------------- PC REGISTER ----------------
            if msg_type == "register":
                device_id = data["device_id"]
                devices[device_id] = ws
                device_last_seen[device_id] = time.time()
                print(f"[PC ONLINE] {device_id}")

                # notify all mobiles PC is online
                for m in mobile_clients:
                    await m.send(json.dumps({
                        "type": "pc_status",
                        "status": "online",
                        "device_id": device_id
                    }))

            # ---------------- HEARTBEAT ----------------
            elif msg_type == "heartbeat":
                dev = data.get("device_id")
                if dev in devices:
                    device_last_seen[dev] = time.time()

            # ---------------- MOBILE REGISTER ----------------
            elif msg_type == "register_mobile":
                mobile = True
                mobile_clients.add(ws)

                print("[MOBILE CONNECTED]")

                # immediately tell mobile PC status
                for dev in devices:
                    await ws.send(json.dumps({
                        "type": "pc_status",
                        "status": "online",
                        "device_id": dev
                    }))

            # ---------------- PAIR REQUEST ----------------
            elif msg_type == "request_pair":
                code = gen_code()

                if devices:
                    target = list(devices.keys())[0]
                    pair_codes[code] = target

                    await ws.send(json.dumps({
                        "type": "pair_code",
                        "code": code
                    }))

            # ---------------- CONFIRM PAIR ----------------
            elif msg_type == "confirm_pair":
                code = data.get("code")

                if code in pair_codes:
                    dev = pair_codes[code]
                    paired_devices.add(dev)
                    save_pairs()

                    await ws.send(json.dumps({"type": "pair_success"}))
                    del pair_codes[code]

                    print(f"[PAIRED] {dev}")

            # ---------------- RELOAD AGENT ----------------
            elif msg_type == "reload_agent":
                await send_to_device(data.get("device_id"), {
                    "type": "reload_agent",
                    "data": {}
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
        # cleanup PC
        if device_id:
            devices.pop(device_id, None)
            device_last_seen.pop(device_id, None)
            print(f"[DISCONNECTED] {device_id}")

        # cleanup mobile
        if ws in mobile_clients:
            mobile_clients.remove(ws)

# -----------------------------
async def cleanup_loop():
    while True:
        now = time.time()
        dead = []

        for dev, last in device_last_seen.items():
            if now - last > 60:
                dead.append(dev)

        for d in dead:
            print(f"[TIMEOUT] Removing {d}")
            devices.pop(d, None)
            device_last_seen.pop(d, None)

        await asyncio.sleep(10)

# -----------------------------
async def main():
    load_pairs()

    port = 8000
    print(f"[WS RUNNING] on {port}")

    async with websockets.serve(handler, "0.0.0.0", port):
        asyncio.create_task(cleanup_loop())
        await asyncio.Future()

# -----------------------------
if __name__ == "__main__":
    asyncio.run(main())