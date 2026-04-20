import asyncio
import websockets
import json
import uuid
import time
import os
import socket

SERVER_URL = "ws://192.168.1.230:8000"
DEVICE_ID_FILE = "device_id.txt"

TARGET_MAC = "3C:6A:D2:41:58:F9"

# -----------------------------
def get_device_id():
    if os.path.exists(DEVICE_ID_FILE):
        with open(DEVICE_ID_FILE, "r") as f:
            return f.read().strip()

    device_id = str(uuid.uuid4())
    with open(DEVICE_ID_FILE, "w") as f:
        f.write(device_id)

    return device_id

DEVICE_ID = get_device_id()

# -----------------------------
def shutdown_pc():
    print("[TEST] shutdown command received")

def restart_pc():
    os.system("shutdown /r /t 0")

def lock_pc():
    os.system("rundll32.exe user32.dll,LockWorkStation")

def wake_on_lan(mac):
    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    packet = b"\xff" * 6 + mac_bytes * 16

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, ("255.255.255.255", 9))

# -----------------------------
async def send_heartbeat(ws):
    print("[HEARTBEAT STARTED]")

    while True:
        try:
            await ws.send(json.dumps({
                "type": "heartbeat",
                "device_id": DEVICE_ID,
                "timestamp": time.time()
            }))

            print("[HEARTBEAT SENT]")

            await asyncio.sleep(10)

        except Exception as e:
            print("[HEARTBEAT FAILED]", e)
            break

# -----------------------------
async def handle_command(cmd, ws):
    t = cmd.get("type")
    cmd_id = cmd.get("command_id")

    print(f"[COMMAND] {t}")

    try:
        if t == "shutdown_pc":
            shutdown_pc()

        elif t == "restart_pc":
            restart_pc()

        elif t == "lock_pc":
            lock_pc()

        elif t == "wake_pc":
            wake_on_lan(TARGET_MAC)

        elif t == "reload_agent":
            os._exit(0)

        if cmd_id:
            await ws.send(json.dumps({
                "type": "ack",
                "command_id": cmd_id,
                "status": "executed"
            }))

    except Exception as e:
        print("[COMMAND ERROR]", e)

# -----------------------------
async def connect():
    while True:
        try:
            async with websockets.connect("ws://YOUR_SERVER:8000") as ws:
                print("[CONNECTED]")

                while True:
                    try:
                        msg = await ws.recv()
                        # handle msg here

                    except websockets.ConnectionClosed:
                        print("[DISCONNECTED] reconnecting...")
                        break

        except Exception as e:
            print("[CONNECTION ERROR]", e)

        await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(connect())
    except KeyboardInterrupt:
        print("\n[EXIT] Agent stopped cleanly")

# -----------------------------
if __name__ == "__main__":
    asyncio.run(connect())