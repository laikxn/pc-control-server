import asyncio
import websockets
import json
import uuid
import time
import os
import socket

SERVER_URL = "ws://192.168.1.230:8000"
DEVICE_ID_FILE = "device_id.txt"

# You can override per-device later if needed
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
    print("[ACTION] Shutdown triggered")
    os.system("shutdown /s /t 0")


def restart_pc():
    print("[ACTION] Restart triggered")
    os.system("shutdown /r /t 0")


def lock_pc():
    print("[ACTION] Lock triggered")
    os.system("rundll32.exe user32.dll,LockWorkStation")


def wake_on_lan(mac):
    try:
        print(f"[WOL] Sending magic packet to {mac}")

        mac_bytes = bytes.fromhex(mac.replace(":", ""))
        packet = b"\xff" * 6 + mac_bytes * 16

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, ("255.255.255.255", 9))

        print("[WOL] Packet sent successfully")

    except Exception as e:
        print("[WOL ERROR]", e)


# -----------------------------
async def send_heartbeat(ws):
    while True:
        try:
            await ws.send(json.dumps({
                "type": "heartbeat",
                "device_id": DEVICE_ID,
                "timestamp": time.time()
            }))

            print("[HEARTBEAT] sent")
            await asyncio.sleep(10)

        except Exception as e:
            print("[HEARTBEAT ERROR]", e)
            break


# -----------------------------
async def handle_command(cmd, ws):
    t = cmd.get("type")
    cmd_id = cmd.get("command_id")

    if not t:
        return

    print(f"[COMMAND RECEIVED] {t}")

    try:
        if t == "shutdown_pc":
            shutdown_pc()

        elif t == "restart_pc":
            restart_pc()

        elif t == "lock_pc":
            lock_pc()

        elif t == "wake_pc":
            # Future-proof: allow MAC override from server
            mac = cmd.get("mac", TARGET_MAC)
            wake_on_lan(mac)

        elif t == "reload_agent":
            print("[RELOAD] exiting agent")
            os._exit(0)

        # ACK back to server (optional but useful for logs)
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
            async with websockets.connect(SERVER_URL) as ws:
                print("[CONNECTED]")

                # register device
                await ws.send(json.dumps({
                    "type": "register",
                    "device_id": DEVICE_ID
                }))

                heartbeat_task = asyncio.create_task(send_heartbeat(ws))

                while True:
                    try:
                        msg = await ws.recv()
                        data = json.loads(msg)

                        await handle_command(data, ws)

                    except websockets.ConnectionClosed:
                        print("[DISCONNECTED] reconnecting...")
                        heartbeat_task.cancel()
                        break

                    except Exception as e:
                        print("[RECV ERROR]", e)

        except Exception as e:
            print("[CONNECTION ERROR]", e)

        await asyncio.sleep(3)


# -----------------------------
if __name__ == "__main__":
    try:
        asyncio.run(connect())
    except KeyboardInterrupt:
        print("\n[EXIT] Agent stopped cleanly")