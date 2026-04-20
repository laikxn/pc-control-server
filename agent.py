import asyncio
import websockets
import json
import uuid
import time
import os
import subprocess
import socket

# -----------------------------
# CONFIG
# -----------------------------
SERVER_URL = "ws://localhost:8000"
DEVICE_ID_FILE = "device_id.txt"

# YOUR PC MAC ADDRESS (for WOL)
TARGET_MAC = "3C:6A:D2:41:58:F9"

# -----------------------------
# DEVICE ID
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
# ACTIONS
# -----------------------------
def launch_app(app_path):
    try:
        subprocess.Popen(app_path, shell=True)
        print(f"[OK] Launched: {app_path}")
    except Exception as e:
        print(f"[ERROR] Failed to launch {app_path}: {e}")


def shutdown_pc():
    print("[ACTION] Shutdown")
    os.system("shutdown /s /t 0")


def restart_pc():
    print("[ACTION] Restart")
    os.system("shutdown /r /t 0")


def lock_pc():
    print("[ACTION] Lock")
    os.system("rundll32.exe user32.dll,LockWorkStation")


def wake_on_lan(mac):
    print("[ACTION] Wake-on-LAN")
    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    magic_packet = b"\xff" * 6 + mac_bytes * 16

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic_packet, ("255.255.255.255", 9))


# -----------------------------
# COMMAND HANDLER
# -----------------------------
async def handle_command(command, ws):
    cmd_type = command.get("type")
    data = command.get("data", {})

    print(f"[COMMAND RECEIVED] {cmd_type}")

    if cmd_type == "launch_app":
        launch_app(data.get("path"))

    elif cmd_type == "shutdown_pc":
        shutdown_pc()

    elif cmd_type == "restart_pc":
        restart_pc()

    elif cmd_type == "lock_pc":
        lock_pc()

    elif cmd_type == "wake_pc":
        wake_on_lan(TARGET_MAC)

    else:
        print(f"[UNKNOWN COMMAND] {cmd_type}")


# -----------------------------
# HEARTBEAT
# -----------------------------
async def send_heartbeat(ws):
    while True:
        try:
            payload = {
                "type": "heartbeat",
                "device_id": DEVICE_ID,
                "timestamp": time.time()
            }
            await ws.send(json.dumps(payload))
            await asyncio.sleep(10)
        except:
            break


# -----------------------------
# CONNECTION LOOP
# -----------------------------
async def connect():
    print(f"[START] Device ID: {DEVICE_ID}")

    while True:
        try:
            async with websockets.connect(
                SERVER_URL,
                ping_interval=20,
                ping_timeout=20
            ) as ws:

                await ws.send(json.dumps({
                    "type": "register",
                    "device_id": DEVICE_ID
                }))

                print("[CONNECTED] to server")

                asyncio.create_task(send_heartbeat(ws))

                while True:
                    message = await ws.recv()
                    command = json.loads(message)

                    print(f"[RECEIVED] {command}")
                    await handle_command(command, ws)

        except Exception as e:
            print(f"[DISCONNECTED] retrying in 3s... ({e})")
            await asyncio.sleep(3)


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    asyncio.run(connect())